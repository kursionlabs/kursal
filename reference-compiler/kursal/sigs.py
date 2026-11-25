"""Signatures: every resolved declaration in core terms.

Types become core types, with each nominal declaration named by its
identity. Functions, methods, externs and trait methods get core
signatures, with place modes. Consts are folded to values, except arithmetic
on `usize` and `isize`, which the C compiler folds.

Errors are reported together, in source order, a step at a time: the type aliases, the type declarations,
the Copy checks of their type arguments, which declarations and aliases are linear, the other declarations,
and whether each instance conforms to its trait. A step runs only when every step before it succeeded, and
a declaration reports at most its first error. An error in a constant stops at once: it would recur
wherever the constant is used.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal, Never, Optional
from . import core as C
from . import syntax as S
from .resolve import Decl, Resolver
from .sexpr import KursError, Position, counted, located
from .layout import check_type_expansion, check_written_type
from .literals import valid_literal
from .types import Surface, Types, equal, free_names, subst

LIBRARY = {  # name: (home library, path there, path through std)
    "Option": ("core", ("option", "Option"), ("option", "Option")),
    "Result": ("core", ("option", "Result"), ("option", "Result")),
    "Str": ("core", ("str", "Str"), ("str", "Str")),
    "Buffer!": ("alloc", ("buffer", "Buffer!"), ("buffer", "Buffer!")),
    "Iterator": ("core", ("iter", "Iterator"), ("iter", "Iterator")),
    "Format": ("core", ("fmt", "Format"), ("fmt", "Format")),
    "Writer": ("core", ("fmt", "Writer"), ("fmt", "Writer")),
    "Str::from_raw_parts_branded": ("core", ("str", "Str", "from_raw_parts_branded"),
                                    ("str", "Str", "from_raw_parts_branded")),
    "Buffer!::new": ("alloc", ("buffer", "Buffer!", "new"), ("buffer", "Buffer!", "new")),
    "ReadingWriter!": ("core", ("fmt", "ReadingWriter!"), ("fmt", "ReadingWriter!")),
    "ReadingWriter!::new": ("core", ("fmt", "ReadingWriter!", "new"), ("fmt", "ReadingWriter!", "new")),
    "ReadingWriter!::finish": ("core", ("fmt", "ReadingWriter!", "finish"), ("fmt", "ReadingWriter!", "finish")),
    "Ordering": ("core", ("atomic", "Ordering"), ("atomic", "Ordering")),
}

# The KURS instruction of each binary operator.
OPERATORS = {"+": "add", "-": "sub", "*": "mul", "/": "div", "%": "mod", "&": "bit_and", "|": "bit_or",
             "^": "bit_xor", "<<": "shl", ">>": "shr", "==": "eq", "!=": "ne", "<": "lt", "<=": "le",
             ">": "gt", ">=": "ge"}


class SigError(KursError):
    pass


class ConstError(SigError):
    """An error met folding a constant."""


def name_pos(decl: Decl) -> Optional[Position]:
    """Where a type declaration writes its name."""
    assert isinstance(decl.node, (S.Struct, S.EnumDecl, S.TupleDecl))
    return decl.node.name_pos


def fail(message: str, pos: Optional[Position] = None) -> Never:
    raise SigError(message, pos)


def aliases_named(ty: S.Type) -> list[Decl]:
    """The type aliases that `ty` names, in the order they are written."""
    out: list[Decl] = []

    def walk(t: S.Type) -> None:
        match t:
            case S.TName(args=args):
                if t.ref is not None and t.ref[0] == "decl" and t.ref[1].kind == "alias":
                    out.append(t.ref[1])
                for a in args:
                    walk(a)
            case S.TPtr(inner=inner):
                walk(inner)
            case S.TTuple(elems=elems):
                for e in elems:
                    walk(e)
            case S.TArray(elem=elem):
                walk(elem)
            case S.TFn(params=params, ret=ret) | S.TClosure(params=params, ret=ret):
                for _, p in params:
                    walk(p)
                walk(ret)
                if isinstance(t, S.TClosure) and t.env is not None:
                    walk(t.env)
            case S.TExists(body=body):
                walk(body)
            case S.TForeach(source=source, body=body):
                walk(source)
                walk(body)

    walk(ty)
    return out


@dataclass
class Sig:
    """A callable's core signature."""
    name: str              # its core name
    decl: Optional[Decl]           # the Decl, or None for an instance method
    params: tuple[C.Param, ...]          # forall Params, implicit closure environments included
    premises: tuple[C.Premise, ...]
    pnames: tuple[str, ...]          # parameter names
    ptypes: tuple[C.Type, ...]
    places: frozenset[int]      # indices of `&` parameters
    ret: C.Type
    unsafe: bool = False
    module: Optional[C.ModuleId] = None


@dataclass
class InstanceSig:
    """An instance header before its method bodies have been lowered to core functions."""
    trait: str
    params: tuple[C.Param, ...]
    premises: tuple[C.Premise, ...]
    head: tuple[C.Type, ...]
    methods: tuple[Sig, ...]
    module: C.ModuleId
    pos: Optional[Position] = None


type IntConst = tuple[Literal["int"], int, C.Prim]
type BoolConst = tuple[Literal["bool"], bool, C.Prim]
type FloatConst = tuple[Literal["float"], str, Optional[C.Type]]
type StringConst = tuple[Literal["str"], str, Optional[C.Type]]
# An operation the C compiler folds: one on `usize` or `isize`, at the target's width; one on floats, or
# a conversion with a float on either side, in IEEE arithmetic; or one on the value of another such
# operation. Its operator is a binary operator's instruction (`OPERATORS`), `int_cast`, `float_cast`,
# `neg`, `not`, `and` or `or`; its type is the result's.
type OpConst = tuple[Literal["op"], tuple[str, tuple[ConstValue, ...]], C.Prim]
type ConstValue = IntConst | BoolConst | FloatConst | StringConst | OpConst


@dataclass
class Scope:
    """The type names in scope while translating one declaration."""
    kinds: dict[str, C.Kind] = field(default_factory=dict)  # type parameter -> COPY/ANY
    brands: set[str] = field(default_factory=set)
    implicit: Optional[list[C.Param]] = None                       # hidden closure environments, when allowed

    def extend(self, params: Iterable[C.Param]) -> Scope:
        out = Scope(dict(self.kinds), set(self.brands), self.implicit)
        for p in params:
            if p.kind == C.BRAND:
                out.brands.add(p.name)
            else:
                out.kinds[p.name] = p.kind
        return out


class Signatures:
    def __init__(self, resolver: Resolver) -> None:
        self.r = resolver
        self.packages = resolver.packages
        clash = len({p.name for p in self.packages}) != len(self.packages)
        self.package_names = {p.id: f"{p.name}#{p.id}" if clash else p.name for p in self.packages}
        self.types: dict[str, C.TypeDecl] = {}
        self.sigs: dict[Decl, Sig] = {}
        self.traits: dict[str, C.Trait] = {}
        self.instances: list[tuple[InstanceSig, S.InstanceDecl, C.ModuleId]] = []        # (Instance with Sig methods, InstanceDecl, C.ModuleId)
        self.consts: dict[Decl, ConstValue] = {}
        self.folding: set[Decl] = set()
        self.implicit = 0
        # Copy checks of type arguments in type declarations, made once every type is known: the type, the kinds
        # in scope, the declaration and where the argument is written.
        self.copy_checks: list[tuple[C.Type, dict[str, C.Kind], Decl, Optional[Position]]] = []
        # The same for the formation of foreach types (`foreach_formation`).
        self.foreach_checks: list[tuple[C.Foreach, dict[str, C.Kind], Decl, Optional[Position]]] = []
        self.declaring: Optional[Decl] = None
        self.types_ready = False
        # Each type alias's parameters and the core type it stands for, put in its place wherever it is written.
        self.aliases: dict[Decl, tuple[tuple[C.Param, ...], C.Type]] = {}
        failed: set[Decl] = set()
        for decl in self.aliases_in_order():
            node = decl.node
            assert isinstance(node, S.TypeAlias)
            if any(named in failed for named in aliases_named(node.body)):
                failed.add(decl)
                continue
            with self.reporting(decl.module, node.pos):
                failed.add(decl)
                self.alias(decl)
                failed.discard(decl)
        resolver.stop(SigError)
        for scope in resolver.scopes.values():
            for decl in scope.decls.values():
                if decl.kind in ("struct", "enum", "tuple"):
                    with self.reporting(decl.module, decl.node.pos):
                        self.declaring = decl
                        self.types[self.name(decl)] = self.type_decl(decl)
        resolver.stop(SigError)
        check_type_expansion(C.Program(types=self.types),
                             {self.name(decl): (str(resolver.file(decl.module)), name_pos(decl))
                              for scope in resolver.scopes.values() for decl in scope.decls.values()
                              if decl.kind in ("struct", "enum", "tuple")})
        self.types_ready = True
        for ty, kinds, declaring, pos in self.copy_checks:
            with self.reporting(declaring.module, declaring.node.pos):
                self.require_copy(ty, kinds, pos)
        for formed, kinds, declaring, pos in self.foreach_checks:
            with self.reporting(declaring.module, declaring.node.pos):
                self.foreach_formation(formed, kinds, pos)
        resolver.stop(SigError)
        self.copy_checks.clear()
        self.foreach_checks.clear()
        types = Types(C.Program(types=self.types))
        for scope in resolver.scopes.values():
            for decl in scope.decls.values():
                if decl.kind in ("struct", "enum", "tuple") and not decl.name.endswith("!"):
                    with self.reporting(decl.module, decl.node.pos):
                        self.unmarked(decl, types)
        for decl in self.aliases:
            with self.reporting(decl.module, decl.node.pos):
                self.alias_linearity(decl, types)
        resolver.stop(SigError)
        for scope in resolver.scopes.values():
            for decl in scope.decls.values():
                with self.reporting(decl.module, decl.node.pos):
                    if decl.kind == "trait":
                        self.traits[self.name(decl)] = self.trait(decl)
                    elif decl.kind in ("fn", "method", "extern"):
                        self.sigs[decl] = self.signature(decl)
                    elif decl.kind == "const":
                        self.const(decl)
            for item in scope.items:
                if isinstance(item, S.InstanceDecl):
                    with self.reporting(scope.module, item.pos):
                        self.instances.append(self.instance(item, scope.module))
        resolver.stop(SigError)
        for inst, item, module in self.instances:
            with self.reporting(module, item.pos):
                self.conforms(inst, item)
        resolver.stop(SigError)

    @contextmanager
    def reporting(self, module: C.ModuleId, pos: Optional[Position]) -> Iterator[None]:
        """A signature error raised inside is reported, in `module`'s file and, when it has no position, at
        `pos`, and the rest of the block is skipped; one in a constant stops with the errors reported. Other
        errors stop, named as in that file and at `pos`."""
        try:
            yield
        except SigError as error:
            self.r.report(error, module, pos)
            if isinstance(error, ConstError):
                self.r.stop(SigError)
        except KursError as error:
            raise located(error, self.r.file(module), pos) from None

    # ---- naming
    def name(self, decl: Decl) -> str:
        return "::".join((self.package_names[decl.module.package], *decl.module.path, decl.name))

    def library(self, module: C.ModuleId, name: str) -> Decl:
        """Resolve a compiler-known library item in the requesting module's package."""
        home, path, std_path = LIBRARY[name]
        decl = self.r.library(module, home, path, std_path)
        assert isinstance(decl, Decl)
        return decl

    # ---- headers
    def forall(self, forall: Optional[S.Forall], scope: Scope) -> tuple[tuple[C.Param, ...], tuple[C.Premise, ...], Scope]:
        """Core Params and premises of a `forall` header, and the scope it opens."""
        if forall is None:
            return (), (), scope
        copy = self.copy_names(forall)
        params = tuple(C.Param(p[1:], C.BRAND) if p.startswith("'") else C.Param(p, C.COPY if p in copy else C.ANY)
                       for p in forall.params + forall.determined)
        inner = scope.extend(params)
        for name in copy:
            if name not in inner.kinds:
                fail(f"Copy requires an in-scope type parameter, not {name}")
            inner.kinds[name] = C.COPY
        premises = tuple(self.premise(args, inner) for trait, args in forall.premises if trait != ("Copy",))
        return params, premises, inner

    def copy_names(self, forall: Optional[S.Forall]) -> set[str]:
        names: set[str] = set()
        for trait, args in forall.premises if forall else ():
            if trait == ("Copy",):
                if len(args.args) != 1 or not isinstance(args.args[0], S.TName):
                    fail("Copy requires one type parameter")
                arg = args.args[0]
                if arg.args or len(arg.path) != 1 or arg.ref is None or arg.ref[0] != "param":
                    fail("Copy requires one bare, in-scope type parameter")
                names.add(arg.ref[1])
        return names

    def premise(self, args: S.TypeArgs, scope: Scope) -> C.Premise:
        for builtin in ("Exposed", "Sized"):
            if args.ref == ("builtin", builtin):
                if len(args.args) != 1:
                    fail(f"{builtin} takes 1 type argument, got {len(args.args)}", args.pos)
                return C.Premise(builtin, tuple(self.type(a, scope) for a in args.args))
        ref = args.ref
        assert ref is not None and ref[0] == "decl"
        actual = tuple(self.type(a, scope) for a in args.args)
        self.check_arguments(ref[1], actual, scope, args.pos)
        return C.Premise(self.name(ref[1]), actual)

    def check_arguments(self, decl: Decl, args: Sequence[C.Type], scope: Scope, pos: Optional[Position]) -> None:
        """The type arguments `args` of `decl`, written at `pos`."""
        forall = decl.node.forall
        params = forall.params + forall.determined if forall else (("Self",) if decl.kind == "trait" else ())
        if len(args) != len(params):
            fail(f"{decl.name} takes {counted(len(params), 'type argument')}, got {len(args)}", pos)
        copied = self.copy_names(forall)
        for param, arg in zip(params, args):
            if param.startswith("'") != isinstance(arg, C.Skolem):
                fail(f"argument for {param} of {decl.name} has the wrong kind (brand versus type)", pos)
            if param in copied:
                self.require_copy(arg, scope.kinds, pos)

    def require_copy(self, ty: C.Type, kinds: dict[str, C.Kind], pos: Optional[Position]) -> None:
        if not self.types_ready:
            assert self.declaring is not None
            self.copy_checks.append((ty, dict(kinds), self.declaring, pos))
        elif not Types(C.Program(types=self.types)).is_copy(ty, kinds):
            fail("type argument must satisfy Copy", pos)

    # ---- types
    def type(self, type_expr: S.Type, scope: Scope) -> C.Type:
        match type_expr:
            case S.TName(args=targs, pos=pos):
                args = tuple(self.type(a, scope) for a in targs)
                match type_expr.ref:
                    case ("param", name):
                        if args:
                            fail(f"type parameter {name} takes no type arguments", pos)
                        return C.Named(name)
                    case ("prim", name):
                        if args:
                            fail(f"primitive {name} takes no type arguments", pos)
                        return C.Prim(name)
                    case ("builtin", "Brand"):
                        if len(args) != 1 or not isinstance(args[0], C.Skolem):
                            fail("Brand requires exactly one brand argument", pos)
                        return C.Brand(args[0])
                    case ("builtin", "NonNull"):
                        if len(args) != 1 or isinstance(args[0], C.Skolem):
                            fail("NonNull requires exactly one type argument", pos)
                        return C.NonNull(args[0])
                    case ("builtin", "Never"):
                        if args:
                            fail("Never takes no type arguments", pos)
                        return C.Never()
                    case ("decl", decl) if decl.kind == "alias":
                        self.check_arguments(decl, args, scope, pos)
                        params, body = self.alias(decl)
                        return subst(body, {p.name: a for p, a in zip(params, args)})
                    case ("decl", decl):
                        self.check_arguments(decl, args, scope, pos)
                        return C.Named(self.name(decl), args)
                    case _:
                        fail(f"not a type name: {'::'.join(type_expr.path)}", pos)
            case S.TBrand(label=label):
                label = label[1:]
                if label not in scope.brands and label != "static":
                    fail(f"brand '{label} is not in scope")
                return C.Skolem(label)
            case S.TPtr(inner=inner):
                return C.Ptr(self.type(inner, scope))
            case S.TNever():
                return C.Never()
            case S.TTuple(elems=elems):
                return C.Tuple(tuple(self.type(e, scope) for e in elems))
            case S.TArray(elem=elem, length=length):
                return C.Array(self.type(elem, scope), self.array_length(length))
            case S.TFn(params=params, ret=ret, unsafe=unsafe):
                return C.Fn(tuple(self.type(p, scope) for _, p in params), self.type(ret, scope),
                            unsafe=unsafe, places=frozenset(i for i, (lend, _) in enumerate(params) if lend))
            case S.TClosure(env=env, params=params, ret=ret):
                if env is not None:
                    environment = self.type(env, scope)
                elif scope.implicit is not None:
                    self.implicit += 1
                    name = f"Env#{self.implicit}"
                    scope.implicit.append(C.Param(name, C.ANY))
                    environment = C.Named(name)
                else:
                    fail("a closure type needs [Env] outside a parameter list")
                code = C.Fn((environment, *(self.type(p, scope) for _, p in params)), self.type(ret, scope),
                            places=frozenset(i + 1 for i, (lend, _) in enumerate(params) if lend))
                return C.Struct((("env", environment), ("code", code)))
            case S.TExists(brands=brands, body=body):
                labels = tuple(b[1:] for b in brands)
                for i, label in enumerate(labels):
                    if label in labels[:i]:
                        fail("an exists type repeats a binder", type_expr.brand_pos[i])
                    if label in scope.brands or label == "static":
                        fail(f"exists binder '{label} shadows a brand in scope", type_expr.brand_pos[i])
                return C.Exists(labels, self.type(body, scope.extend(C.Param(b, C.BRAND) for b in labels)))
            case S.TForeach(binder=binder, indirect=indirect, source=source, body=body, pos=pos):
                formed = C.Foreach(binder, indirect, self.type(source, scope),
                                   self.type(body, scope.extend((C.Param(binder, C.ANY),))))
                self.foreach_formation(formed, scope.kinds, pos)
                return formed
        assert isinstance(type_expr, S.TIgnored)
        fail("_ stands for a type argument only in an expression, where inference fills it in", type_expr.pos)

    def foreach_formation(self, ty: C.Foreach, kinds: dict[str, C.Kind], pos: Optional[Position]) -> None:
        """The foreach type `ty`, written at `pos`, has an expansion at every instantiation, as type
        formation requires: a `*F` pattern's source whose shape is decided is written with pointer
        components, and the walk of `ty` asks for an explicit pointer where a type parameter decides it.
        Inside a type declaration it waits until every type is known."""
        if not self.types_ready:
            assert self.declaring is not None
            self.foreach_checks.append((ty, dict(kinds), self.declaring, pos))
            return
        program = C.Program(types=self.types)
        if ty.indirect and Types(program).rows_fixed(ty.source, True, kinds) and not Types(program).pattern_available(ty):
            fail(f"foreach *{ty.binder} needs a source written with pointer components", pos)
        try:
            check_written_type(program, ty, set(kinds))
        except KursError as error:
            fail(error.message, pos)

    def array_length(self, expr: S.Expr) -> int:
        value = self.fold(expr, C.USIZE)
        # A type's array length is the compiler's to know, and arithmetic on `usize` and `isize` is the C
        # compiler's to fold.
        if value[0] == "op":
            fail("an array length cannot use arithmetic on usize or isize")
        if value[0] != "int":
            fail("an array length is an integer constant")
        if value[1] < 0:
            fail("negative array length")
        if value[1] == 0:
            fail("an array has at least one element", expr.pos)
        return value[1]

    # ---- type aliases
    def aliases_in_order(self) -> list[Decl]:
        """Every type alias, each after the aliases its body names. An alias that refers to itself, directly
        or through others, is reported, and left out with every alias that names it."""
        order: list[Decl] = []
        done: dict[Decl, bool] = {}
        stack: list[Decl] = []

        def visit(decl: Decl) -> bool:
            if decl in done:
                return done[decl]
            node = decl.node
            assert isinstance(node, S.TypeAlias)
            if decl in stack:
                chain = stack[stack.index(decl) + 1:]
                through = f" through {', '.join(d.name for d in chain)}" if chain else ""
                self.r.report(SigError(f"type alias {decl.name} refers to itself{through}", node.pos), decl.module)
                return False
            stack.append(decl)
            fine = all([visit(named) for named in aliases_named(node.body)])
            stack.pop()
            done[decl] = fine
            if fine:
                order.append(decl)
            return fine

        for scope in self.r.scopes.values():
            for decl in scope.decls.values():
                if decl.kind == "alias":
                    visit(decl)
        return order

    def alias(self, decl: Decl) -> tuple[tuple[C.Param, ...], C.Type]:
        """The parameters of the type alias `decl` and the core type it stands for. Its forall lists only
        parameters, and its body uses each of them."""
        if decl in self.aliases:
            return self.aliases[decl]
        node = decl.node
        assert isinstance(node, S.TypeAlias)
        if node.exposed is not None:
            fail("a type alias is never exposed: it has no representation of its own", node.pos)
        if node.forall is not None and (node.forall.premises or node.forall.determined):
            fail(f"type alias {decl.name} takes no premises or determined parameters", node.pos)
        declaring, self.declaring = self.declaring, decl
        try:
            params, _, scope = self.forall(node.forall, Scope())
            body = self.type(node.body, scope)
        finally:
            self.declaring = declaring
        used = free_names(body)
        for p in params:
            if p.name not in used:
                spelled = f"'{p.name}" if p.kind == C.BRAND else p.name
                fail(f"type alias {decl.name} does not use its parameter {spelled}", node.pos)
        self.aliases[decl] = (params, body)
        return params, body

    def alias_linearity(self, decl: Decl, types: Types) -> None:
        """A type alias's name ends with `!` exactly when the type it stands for is linear whatever its type
        arguments are."""
        params, body = self.aliases[decl]
        node = decl.node
        assert isinstance(node, S.TypeAlias)
        linear = not types.is_copy(body, {p.name: C.COPY for p in params if p.kind != C.BRAND})
        if linear and not decl.name.endswith("!"):
            fail(f"type alias {decl.name} stands for a linear type; name it {decl.name}!", node.name_pos)
        if not linear and decl.name.endswith("!"):
            fail(f"type alias {decl.name} stands for a type that is not linear; name it {decl.name[:-1]}",
                 node.name_pos)

    # ---- declarations
    def type_decl(self, decl: Decl) -> C.TypeDecl:
        node = decl.node
        assert isinstance(node, (S.Struct, S.TupleDecl, S.EnumDecl))
        params, premises, scope = self.forall(node.forall, Scope())
        if any(p.trait == "Sized" for p in premises):
            fail("Sized is a premise of functions and instances, not of a type declaration", node.pos)
        body: C.Type
        match node:
            case S.Struct(fields=fields):
                body = C.Struct(tuple((n, self.type(t, scope)) for n, t in fields))
            case S.TupleDecl(elems=elems):
                body = C.Tuple(tuple(self.type(t, scope) for t in elems))
            case S.EnumDecl():
                if not node.variants:
                    fail(f"enum {node.name} has no variant; an enum has at least one", node.pos)
                body = C.Enum(tuple((n, self.payload(p, scope)) for n, p in node.variants))
        exposure: Optional[C.Exposure] = ("owner", decl.module)
        if node.exposed is not None:
            exposure = None if node.exposed.scope is None else ("within", self.scope_module(decl, node.exposed.scope))
        return C.TypeDecl(self.name(decl), params, premises, body, decl.module, exposure)

    def unmarked(self, decl: Decl, types: Types) -> None:
        """A declaration whose name has no `!` is not linear for every instantiation: it is Copy when its
        type arguments are."""
        td = self.types[self.name(decl)]
        args = tuple(C.Skolem(p.name) if p.kind == C.BRAND else C.Named(p.name) for p in td.params)
        kinds = {p.name: C.COPY for p in td.params if p.kind != C.BRAND}
        if types.is_copy(C.Named(td.name, args), kinds):
            return
        parts: Sequence[tuple[str, C.Type]]
        match td.body:
            case C.Struct(fields=fields):
                parts = tuple((f"field {name}", ty) for name, ty in fields)
            case C.Enum(variants=variants):
                parts = tuple((f"variant {name}", ty) for name, ty in variants)
            case C.Tuple(elems=elems):
                parts = tuple((f"element {k}", ty) for k, ty in enumerate(elems))
            case _:
                parts = ()
        # the first field, variant or element that is linear when every type argument is Copy
        part = next((f" in its {part}" for part, ty in parts if not types.is_copy(ty, kinds)), "")
        whatever = " whatever its type arguments" if kinds else ""
        fail(f"{decl.kind} {decl.name} holds a linear type{part}{whatever}; declare it {decl.name}!", name_pos(decl))

    def payload(self, types: Sequence[S.Type], scope: Scope) -> C.Type:
        if not types:
            return C.UNIT
        if len(types) == 1:
            return self.type(types[0], scope)
        return C.Tuple(tuple(self.type(t, scope) for t in types))

    def scope_module(self, decl: Decl, path: S.NamePath) -> C.ModuleId:
        target = self.r.lookup(decl.module, path)
        if not isinstance(target, C.ModuleId) or not decl.module.within(target):
            fail(f"an exposure scope is an enclosing module of {decl.name}")
        return target

    def signature(self, decl: Decl | S.Fn | S.Extern, outer: tuple[C.Param, ...] = (),
                  outer_premises: tuple[C.Premise, ...] = (), outer_scope: Optional[Scope] = None,
                  name: Optional[str] = None) -> Sig:
        node = decl.node if isinstance(decl, Decl) else decl
        assert isinstance(node, (S.Fn, S.Extern))
        if isinstance(node, S.Extern) and node.abi != "C":
            fail('the only extern ABI is "C"', node.abi_pos)
        scope = outer_scope or Scope()
        if isinstance(decl, Decl) and decl.kind == "method":
            assert decl.impl is not None
            impl_params, impl_premises, scope = self.forall(decl.impl.forall, scope)
            outer, outer_premises = outer + impl_params, outer_premises + impl_premises
        params, premises, scope = self.forall(node.forall, scope)
        copied = self.copy_names(node.forall)
        outer = tuple(C.Param(p.name, C.COPY) if p.name in copied and p.kind == C.ANY else p for p in outer)
        scope.kinds.update({p.name: p.kind for p in outer if p.kind != C.BRAND})
        scope.implicit = []
        pnames, ptypes, places = [], [], set()
        for k, p in enumerate(node.params):
            if p.type is None:
                if p.name != "self" or not isinstance(decl, Decl) or decl.kind != "method":
                    fail(f"parameter {p.name} needs a type", node.pos)
                assert decl.impl is not None
                ptypes.append(self.type(decl.impl.type, scope))
            else:
                ptypes.append(self.type(p.type, scope))
            if scope.implicit and isinstance(node, S.Extern):
                fail(f"a closure parameter of extern {node.name} needs [Env]", p.pos)
            pnames.append(p.name)
            if p.lend:
                places.add(k)
        implicit = tuple(scope.implicit)
        scope.implicit = None
        ret = C.UNIT if node.ret is None else self.type(node.ret, scope)
        if name is None:
            assert isinstance(decl, Decl)
            name = self.name(decl)
        return Sig(name, decl if isinstance(decl, Decl) else None,
                   outer + params + implicit, outer_premises + premises, tuple(pnames), tuple(ptypes),
                   frozenset(places), ret, node.unsafe if isinstance(node, S.Fn) else True,
                   decl.module if isinstance(decl, Decl) else None)

    def trait(self, decl: Decl) -> C.Trait:
        node = decl.node
        assert isinstance(node, S.TraitDecl)
        params, premises, scope = self.forall(node.forall, Scope())
        if premises:
            fail("trait declarations cannot have their own trait premises", node.pos)
        if not node.forall:
            params, scope = (C.Param("Self", C.ANY),), scope.extend([C.Param("Self", C.ANY)])
        methods: list[C.Method] = []
        for m in node.methods:
            if any(other.name == m.name for other in methods):
                fail(f"method {m.name} is declared twice", m.pos)
            sig = self.signature(m, outer_scope=scope, name=m.name)
            if sig.params:
                fail(f"trait method {m.name} cannot be generic itself")
            methods.append(C.Method(m.name, sig.ret, sig.ptypes, sig.unsafe, sig.places))
        return C.Trait(self.name(decl), tuple(p.name for p in params), tuple(methods), decl.module,
                       tuple(p.kind for p in params))

    def instance(self, item: S.InstanceDecl, module: C.ModuleId) -> tuple[InstanceSig, S.InstanceDecl, C.ModuleId]:
        params, premises, scope = self.forall(item.forall, Scope())
        head = item.type
        assert isinstance(head, S.TName)
        ref = head.ref
        assert ref is not None and ref[0] == "decl"
        trait = ref[1]
        arguments = tuple(self.type(a, scope) for a in head.args)
        self.check_arguments(trait, arguments, scope, head.pos)
        methods = tuple(self.signature(m, params, premises, scope, name=m.name) for m in item.methods)
        for m in methods:
            m.module = module
        inst = InstanceSig(self.name(trait), params, premises, arguments,
                          methods, module, item.pos)
        return inst, item, module

    def conforms(self, inst: InstanceSig, item: S.InstanceDecl) -> None:
        """An instance defines its trait's methods, once each, each with the trait's signature at the
        instance's arguments: parameter types, which parameters are places, result, and no `unsafe` the
        trait's method lacks."""
        trait = self.traits[inst.trait]
        head: dict[str, C.Type] = dict(zip(trait.params, inst.head))
        declared = {m.name for m in trait.methods}
        written = Surface((), {}, (inst.trait,)).name(inst.trait)
        defined: dict[str, tuple[Sig, S.Fn]] = {}
        for sig, node in zip(inst.methods, item.methods):
            if node.name in defined:
                fail(f"method {node.name} is defined twice", node.pos)
            if node.name not in declared:
                fail(f"{node.name} is not a method of trait {written}", node.pos)
            defined[node.name] = (sig, node)
        for method in trait.methods:
            if method.name not in defined:
                fail(f"the instance lacks trait method {method.name}")
            sig, node = defined[method.name]
            if sig.params[len(inst.params):] or sig.premises[len(inst.premises):]:
                fail(f"instance method {method.name} cannot be generic itself", node.pos)
            want = C.Fn(tuple(subst(t, head) for t in method.ptypes), subst(method.ret, head), places=method.places)
            got = C.Fn(sig.ptypes, sig.ret, places=sig.places)
            if not equal(got, want) or sig.unsafe and not method.unsafe:
                fail(f"method {method.name} does not match its declaration in trait {written}", node.pos)

    # ---- consts
    def const(self, decl: Decl) -> ConstValue:
        if decl in self.consts:
            return self.consts[decl]
        if decl in self.folding:
            fail(f"const {decl.name} depends on itself")
        self.folding.add(decl)
        try:
            node = decl.node
            assert isinstance(node, S.Const)
            value = self.typed(decl.name, node.value, self.type(node.type, Scope()), decl.module)
            self.consts[decl] = value
            return value
        except SigError as error:
            raise error if isinstance(error, ConstError) else ConstError(error.message, error.pos) from None
        finally:
            self.folding.discard(decl)

    def typed(self, name: str, expr: S.Expr, want: C.Type, module: C.ModuleId) -> ConstValue:
        """The value of `const name: want = expr`, which must have type `want`."""
        value = self.fold(expr, want, module)
        got = value[2]
        if got != want:
            assert got is not None
            surface = Surface((got, want), {})
            fail(f"const {name} has type {surface.show(got)}, but its declared type is {surface.show(want)}", expr.pos)
        return value

    def string_type(self, module: C.ModuleId) -> C.Type:
        """`Str['static]`, the type of a string literal."""
        return C.Named(self.name(self.library(module, "Str")), (C.Skolem("static"),))

    def fold(self, expr: S.Expr, want: Optional[C.Type] = None, module: Optional[C.ModuleId] = None, *, evaluate: bool = True) -> ConstValue:
        """Type a constant expression and optionally evaluate its value.

        Unevaluated short-circuit operands still validate operators and literals;
        only computed values (and their division-by-zero traps) are skipped. An operation on `usize`
        or `isize`, or on the value of one, is not evaluated: it is an `OpConst`, which the C compiler
        folds at the target's width. Nor is arithmetic on floats, or a conversion to or from one: the
        C compiler folds it too.
        """
        if isinstance(expr, S.Lit):
            if expr.kind == "int":
                assert isinstance(expr.value, int)
                ty = C.Prim(expr.suffix) if expr.suffix else want
                if ty is None or not (isinstance(ty, C.Prim) and ty.name in C.INTEGERS):
                    fail("an integer constant needs a type", expr.pos)
                return self.literal(expr.value, ty, expr.pos)
            if expr.kind == "bool":
                assert isinstance(expr.value, bool)
                return ("bool", expr.value, C.BOOL)
            if expr.kind == "float":
                assert isinstance(expr.value, str)
                ty = C.Prim(expr.suffix) if expr.suffix else want
                if not isinstance(ty, C.Prim) or ty.name not in C.FLOATS:
                    fail("a floating constant needs a floating type", expr.pos)
                if not valid_literal(ty, expr.value):
                    fail(f"literal {expr.value} does not fit {ty.name}", expr.pos)
                return ("float", expr.value, ty)
            if expr.kind == "str":
                assert isinstance(expr.value, str)
                return ("str", expr.value, self.string_type(module) if module is not None else None)
        if isinstance(expr, S.Path):
            ref = expr.ref
            if ref and ref[0] == "decl" and ref[1].kind == "const":
                if any(args for _, args in expr.segments):
                    fail("constant paths take no type arguments", expr.pos)
                return self.const(ref[1])
            fail("a constant expression names only consts", expr.pos)
        if isinstance(expr, S.Tuple) and len(expr.elems) == 1:
            return self.fold(expr.elems[0], want, module, evaluate=evaluate)
        if isinstance(expr, S.Unary) and expr.op in ("-", "!"):
            if expr.op == "-" and isinstance(expr.operand, S.Lit) and expr.operand.kind == "int":
                literal = expr.operand
                assert isinstance(literal.value, int)
                ty = C.Prim(literal.suffix) if literal.suffix else want
                if not isinstance(ty, C.Prim) or ty.name not in C.INTEGERS:
                    fail("an integer constant needs a type", expr.pos)
                return self.literal(-literal.value, ty, expr.pos)
            v = self.fold(expr.operand, want, module, evaluate=evaluate)
            if expr.op == "-" and v[0] == "float":
                return ("float", v[1][1:] if v[1].startswith("-") else "-" + v[1], v[2])
            if expr.op == "-" and v[0] == "op" and floating(v) is not None:
                return ("op", ("neg", (v,)), v[2])
            if v[0] == "bool" and expr.op == "!":
                return ("bool", not v[1], C.BOOL)
            if v[0] == "op" and v[2] == C.BOOL and expr.op == "!":
                return ("op", ("not", (v,)), C.BOOL)
            ty = integer(v)
            if ty is None:
                fail("operator needs an integer", expr.pos)
            if v[0] != "int" or ty.name in C.POINTER_SIZED:
                return ("op", ("neg" if expr.op == "-" else "not", (v,)), ty)
            return self.wrapped(-v[1] if expr.op == "-" else ~v[1], ty)
        if isinstance(expr, S.Cast):
            v = self.fold(expr.value, None, module, evaluate=evaluate)
            to = self.type(expr.type, Scope())
            source = integer(v) or floating(v)
            if source is None or not (isinstance(to, C.Prim) and to.name in C.INTEGERS | C.FLOATS):
                fail("a constant cast converts between number types", expr.pos)
            if source.name in C.FLOATS or to.name in C.FLOATS:
                return ("op", ("float_cast", (v,)), to)
            if v[0] != "int" or source.name in C.POINTER_SIZED or to.name in C.POINTER_SIZED:
                return ("op", ("int_cast", (v,)), to)
            return self.wrapped(v[1], to)
        if isinstance(expr, S.Binop):
            return self.binop(expr, want, module, evaluate=evaluate)
        fail("not a constant expression", expr.pos)

    def binop(self, expr: S.Binop, want: Optional[C.Type], module: Optional[C.ModuleId], *, evaluate: bool = True) -> ConstValue:
        if expr.op in ("&&", "||"):
            a = self.fold(expr.left, C.BOOL, module, evaluate=evaluate)
            if not boolean(a):
                fail("logical constant operands must be booleans", expr.pos)
            # An operand the C compiler folds may be either value: the other is needed.
            needed = a[0] == "op" or a[1] == (expr.op == "&&")
            b = self.fold(expr.right, C.BOOL, module, evaluate=evaluate and needed)
            if not boolean(b):
                fail("logical constant operands must be booleans", expr.pos)
            if a[0] == "op":
                return ("op", ("and" if expr.op == "&&" else "or", (a, b)), C.BOOL)
            return b if needed else a
        comparison = expr.op in ("==", "!=", "<", "<=", ">", ">=")
        hint = None if comparison else want
        try:
            a = self.fold(expr.left, hint, module, evaluate=evaluate)
            b = self.fold(expr.right, a[2], module, evaluate=evaluate)
        except SigError:
            b = self.fold(expr.right, hint, module, evaluate=evaluate)
            a = self.fold(expr.left, b[2], module, evaluate=evaluate)
        if boolean(a) and expr.op in ("==", "!="):
            if not boolean(b):
                fail("constant comparison operands must have the same type", expr.pos)
            if a[0] == "op" or b[0] == "op":
                return ("op", (OPERATORS[expr.op], (a, b)), C.BOOL)
            return ("bool", (a[1] == b[1]) == (expr.op == "=="), C.BOOL)
        number = floating(a)
        if number is not None and floating(b) == number and (comparison or expr.op in ("+", "-", "*", "/")):
            # The C compiler folds arithmetic on floats.
            return ("op", (OPERATORS[expr.op], (a, b)), C.BOOL if comparison else number)
        ty = integer(a)
        if ty is None or integer(b) != ty:
            fail("constant operands must be integers of one type", expr.pos)
        # A literal 0 is 0 at every width.
        if evaluate and expr.op in ("/", "%") and b[0] == "int" and b[1] == 0:
            fail("constant division by zero", expr.pos)
        if a[0] != "int" or b[0] != "int" or ty.name in C.POINTER_SIZED:
            return ("op", (OPERATORS[expr.op], (a, b)), C.BOOL if comparison else ty)
        x, y = a[1], b[1]
        if not evaluate:
            return ("bool", False, C.BOOL) if comparison else self.wrapped(0, ty)
        if comparison:
            return ("bool", {"==": x == y, "!=": x != y, "<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[expr.op],
                    C.BOOL)
        bits = C.WIDTHS[ty.name]
        if expr.op in ("/", "%"):
            q = abs(x) // abs(y) * (1 if (x < 0) == (y < 0) else -1)
            value = q if expr.op == "/" else x - q * y
        elif expr.op in ("<<", ">>"):
            shift = y % bits
            value = x << shift if expr.op == "<<" else x >> shift
        else:
            value = {"+": x + y, "-": x - y, "*": x * y, "&": x & y, "|": x | y, "^": x ^ y}[expr.op]
        return self.wrapped(value, ty)

    def literal(self, value: int, ty: C.Prim, pos: Optional[Position]) -> IntConst:
        """A literal's value, which must fit `ty` (a negated one may reach MIN)."""
        if not valid_literal(ty, str(value)):
            fail(f"{value} does not fit {ty.name}", pos)
        return ("int", value, ty)

    def wrapped(self, value: int, ty: C.Prim) -> IntConst:
        """`value` in the width of `ty`, a fixed-width integer type."""
        bits = C.WIDTHS[ty.name]
        value &= (1 << bits) - 1
        if ty.name.startswith("i") and value >= 1 << bits - 1:
            value -= 1 << bits
        return ("int", value, ty)


def boolean(value: ConstValue) -> bool:
    """Whether `value` is a `bool` constant."""
    return value[0] in ("bool", "op") and value[2] == C.BOOL


def floating(value: ConstValue) -> Optional[C.Prim]:
    """The type of a float constant, or None for any other constant."""
    ty = value[2]
    return ty if value[0] in ("float", "op") and isinstance(ty, C.Prim) and ty.name in C.FLOATS else None


def integer(value: ConstValue) -> Optional[C.Prim]:
    """The type of an integer constant, or None for any other constant."""
    ty = value[2]
    return ty if value[0] in ("int", "op") and isinstance(ty, C.Prim) and ty.name in C.INTEGERS else None


def signatures(resolver: Resolver) -> Signatures:
    return Signatures(resolver)
