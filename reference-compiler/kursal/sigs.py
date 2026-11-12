"""Signatures: every resolved declaration in core terms.

Types become core types, with each nominal declaration named by its
identity. Functions, methods, externs and trait methods get core
signatures, with place modes. Consts are folded to values.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Never, Optional
from . import core as C
from . import syntax as S
from .resolve import Decl, Resolver
from .sexpr import KursError, Position
from .layout import check_type_expansion
from .literals import valid_literal
from .types import Types

LIBRARY = {  # name: (home library, path there, path through std)
    "Option": ("core", ("option", "Option"), ("option", "Option")),
    "Result": ("core", ("option", "Result"), ("option", "Result")),
    "Slice": ("core", ("slice", "Slice"), ("slice", "Slice")),
    "Buffer": ("alloc", ("buffer", "Buffer"), ("bytes", "buffer", "Buffer")),
    "Iterator": ("core", ("iter", "Iterator"), ("iter", "Iterator")),
    "Format": ("core", ("fmt", "Format"), ("text", "fmt", "Format")),
    "Writable": ("core", ("fmt", "Writable"), ("text", "fmt", "Writable")),
    "Slice::from_raw_parts": ("core", ("slice", "Slice", "from_raw_parts"), ("slice", "Slice", "from_raw_parts")),
    "Buffer::new": ("alloc", ("buffer", "Buffer", "new"), ("bytes", "buffer", "Buffer", "new")),
    "Ordering": ("core", ("atomic", "Ordering"), ("atomic", "Ordering")),
}

WIDTHS = C.INTEGERS


class SigError(KursError):
    pass


def fail(message: str, pos: Optional[Position] = None) -> Never:
    raise SigError(message, pos)


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
type ConstValue = IntConst | BoolConst | FloatConst | StringConst


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
        self.copy_checks: list[tuple[C.Type, dict[str, C.Kind]]] = []
        self.types_ready = False
        for scope in resolver.scopes.values():
            for decl in scope.decls.values():
                if decl.kind in ("struct", "enum", "tuple"):
                    self.types[self.name(decl)] = self.type_decl(decl)
        check_type_expansion(C.Program(types=self.types))
        self.types_ready = True
        for ty, kinds in self.copy_checks:
            self.require_copy(ty, kinds)
        self.copy_checks.clear()
        for scope in resolver.scopes.values():
            for decl in scope.decls.values():
                if decl.kind == "trait":
                    self.traits[self.name(decl)] = self.trait(decl)
                elif decl.kind in ("fn", "method", "extern"):
                    self.sigs[decl] = self.signature(decl)
                elif decl.kind == "const":
                    self.const(decl)
            for item in scope.items:
                if isinstance(item, S.InstanceDecl):
                    self.instances.append(self.instance(item, scope.module))

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
        premises = tuple(self.premise(trait, args, inner) for trait, args in forall.premises if trait != ("Copy",))
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

    def premise(self, trait: S.NamePath, args: S.TypeArgs, scope: Scope) -> C.Premise:
        if args.ref == ("builtin", "Exposed"):
            return C.Premise("builtin::Exposed", tuple(self.type(a, scope) for a in args.args))
        ref = args.ref
        assert ref is not None and ref[0] == "decl"
        actual = tuple(self.type(a, scope) for a in args.args)
        self.check_arguments(ref[1], actual, scope)
        return C.Premise(self.name(ref[1]), actual)

    def check_arguments(self, decl: Decl, args: Sequence[C.Type], scope: Scope) -> None:
        forall = decl.node.forall
        params = forall.params + forall.determined if forall else (("Self",) if decl.kind == "trait" else ())
        if len(args) != len(params):
            fail(f"{decl.name} takes {len(params)} type arguments, got {len(args)}")
        copied = self.copy_names(forall)
        for param, arg in zip(params, args):
            if param.startswith("'") != isinstance(arg, C.Skolem):
                fail(f"argument for {param} of {decl.name} has the wrong kind (brand versus type)")
            if param in copied:
                self.require_copy(arg, scope.kinds)

    def require_copy(self, ty: C.Type, kinds: dict[str, C.Kind]) -> None:
        if not self.types_ready:
            self.copy_checks.append((ty, dict(kinds)))
        elif not Types(C.Program(types=self.types)).is_copy(ty, kinds):
            fail("type argument must satisfy Copy")

    # ---- types
    def type(self, type_expr: S.Type, scope: Scope) -> C.Type:
        match type_expr:
            case S.TName(args=targs):
                args = tuple(self.type(a, scope) for a in targs)
                match type_expr.ref:
                    case ("param", name):
                        if args:
                            fail(f"type parameter {name} takes no type arguments")
                        return C.Named(name)
                    case ("prim", name):
                        if args:
                            fail(f"primitive {name} takes no type arguments")
                        return C.Prim(name)
                    case ("builtin", "Brand"):
                        if len(args) != 1 or not isinstance(args[0], C.Skolem):
                            fail("Brand requires exactly one brand argument")
                        return C.Brand(args[0])
                    case ("builtin", "Never"):
                        if args:
                            fail("Never takes no type arguments")
                        return C.Never()
                    case ("decl", decl):
                        self.check_arguments(decl, args, scope)
                        return C.Named(self.name(decl), args)
                    case _:
                        fail(f"not a type name: {type_expr.path}")
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
                return C.Exists(labels, self.type(body, scope.extend(C.Param(b, C.BRAND) for b in labels)))
            case S.TForeach(binder=binder, indirect=indirect, source=source, body=body):
                return C.Foreach(binder, indirect, self.type(source, scope),
                                 self.type(body, scope.extend([C.Param(binder, C.ANY)])))
            case S.TForall(params=tparams, body=body):
                quantified = tuple(C.Param(p[1:], C.BRAND) for p in tparams)
                inner_type = self.type(body, scope.extend(quantified))
                if not isinstance(inner_type, C.Fn) or any(not p.startswith("'") for p in tparams):
                    fail("forall in a type quantifies brands of a function type")
                return C.Fn(inner_type.params, inner_type.ret, quantified, inner_type.unsafe, inner_type.places)
        fail(f"no core type for {type(type_expr).__name__}")

    def array_length(self, expr: S.Expr) -> int:
        value = self.fold(expr, C.USIZE)
        if value[0] != "int":
            fail("an array length is an integer constant")
        return value[1]

    # ---- declarations
    def type_decl(self, decl: Decl) -> C.TypeDecl:
        node = decl.node
        assert isinstance(node, (S.Struct, S.TupleDecl, S.EnumDecl))
        params, premises, scope = self.forall(node.forall, Scope())
        body: C.Type
        match node:
            case S.Struct(fields=fields):
                body = C.Struct(tuple((n, self.type(t, scope)) for n, t in fields))
            case S.TupleDecl(elems=elems):
                body = C.Tuple(tuple(self.type(t, scope) for t in elems))
            case S.EnumDecl():
                body = C.Enum(tuple((n, self.payload(p, scope)) for n, p in node.variants))
        exposure: Optional[C.Exposure] = ("owner", decl.module)
        if node.exposed is not None:
            exposure = None if node.exposed.scope is None else ("within", self.scope_module(decl, node.exposed.scope))
        return C.TypeDecl(self.name(decl), node.linear, params, premises, body, decl.module, exposure)

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
        methods = []
        for m in node.methods:
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
        self.check_arguments(trait, arguments, scope)
        methods = tuple(self.signature(m, params, premises, scope, name=m.name) for m in item.methods)
        for m in methods:
            m.module = module
        inst = InstanceSig(self.name(trait), params, premises, arguments,
                          methods, module, item.pos)
        return inst, item, module

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
            want = self.type(node.type, Scope())
            value = self.fold(node.value, want, decl.module)
            if value[2] != want:
                fail(f"const {decl.name} is not a {want}")
            self.consts[decl] = value
            return value
        finally:
            self.folding.discard(decl)

    def fold(self, expr: S.Expr, want: Optional[C.Type] = None, module: Optional[C.ModuleId] = None, *, evaluate: bool = True) -> ConstValue:
        """Type a constant expression and optionally evaluate its value.

        Unevaluated short-circuit operands still validate operators and literals;
        only computed values (and their division-by-zero traps) are skipped.
        """
        if isinstance(expr, S.Lit):
            if expr.kind == "int":
                assert isinstance(expr.value, int)
                ty = C.Prim(expr.suffix) if expr.suffix else want
                if ty is None or not (isinstance(ty, C.Prim) and ty.name in WIDTHS):
                    fail("an integer constant needs a type", expr.pos)
                return self.wrapped(expr.value, ty, expr.pos, exact=True)
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
                return ("str", expr.value, want)
        if isinstance(expr, S.Path):
            ref = expr.ref
            if ref and ref[0] == "decl" and ref[1].kind == "const":
                if any(args for _, _, args in expr.segments):
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
                if not isinstance(ty, C.Prim) or ty.name not in WIDTHS:
                    fail("an integer constant needs a type", expr.pos)
                return self.wrapped(-literal.value, ty, expr.pos, exact=True)
            v = self.fold(expr.operand, want, module, evaluate=evaluate)
            if expr.op == "-" and v[0] == "float":
                return ("float", v[1][1:] if v[1].startswith("-") else "-" + v[1], v[2])
            if v[0] == "bool" and expr.op == "!":
                return ("bool", not v[1], C.BOOL)
            if v[0] != "int":
                fail("operator needs an integer", expr.pos)
            return self.wrapped(-v[1] if expr.op == "-" else ~v[1], v[2], expr.pos)
        if isinstance(expr, S.Cast):
            v = self.fold(expr.value, None, module, evaluate=evaluate)
            to = self.type(expr.type, Scope())
            if v[0] != "int" or not (isinstance(to, C.Prim) and to.name in WIDTHS):
                fail("a constant cast converts between integer types", expr.pos)
            return self.wrapped(v[1], to, expr.pos)
        if isinstance(expr, S.Binop):
            return self.binop(expr, want, module, evaluate=evaluate)
        fail("not a constant expression", expr.pos)

    def binop(self, expr: S.Binop, want: Optional[C.Type], module: Optional[C.ModuleId], *, evaluate: bool = True) -> ConstValue:
        if expr.op in ("&&", "||"):
            a = self.fold(expr.left, C.BOOL, module, evaluate=evaluate)
            if a[0] != "bool":
                fail("logical constant operands must be booleans", expr.pos)
            needed = a[1] if expr.op == "&&" else not a[1]
            b = self.fold(expr.right, C.BOOL, module, evaluate=evaluate and needed)
            if b[0] != "bool":
                fail("logical constant operands must be booleans", expr.pos)
            return ("bool", a[1] and b[1] if expr.op == "&&" else a[1] or b[1], C.BOOL)
        comparison = expr.op in ("==", "!=", "<", "<=", ">", ">=")
        hint = None if comparison else want
        try:
            a = self.fold(expr.left, hint, module, evaluate=evaluate)
            b = self.fold(expr.right, a[2], module, evaluate=evaluate)
        except SigError:
            b = self.fold(expr.right, hint, module, evaluate=evaluate)
            a = self.fold(expr.left, b[2], module, evaluate=evaluate)
        if a[0] == "bool" and expr.op in ("==", "!="):
            if b[0] != "bool":
                fail("constant comparison operands must have the same type", expr.pos)
            return ("bool", (a[1] == b[1]) == (expr.op == "=="), C.BOOL)
        if a[0] != "int" or b[0] != "int" or a[2] != b[2]:
            fail("constant operands must be integers of one type", expr.pos)
        x, y, ty = a[1], b[1], a[2]
        if not evaluate:
            return ("bool", False, C.BOOL) if comparison else self.wrapped(0, ty, expr.pos)
        if comparison:
            return ("bool", {"==": x == y, "!=": x != y, "<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[expr.op],
                    C.BOOL)
        bits = WIDTHS[ty.name]
        if expr.op in ("/", "%"):
            if y == 0:
                fail("constant division by zero", expr.pos)
            q = abs(x) // abs(y) * (1 if (x < 0) == (y < 0) else -1)
            value = q if expr.op == "/" else x - q * y
        elif expr.op in ("<<", ">>"):
            shift = y % bits
            value = x << shift if expr.op == "<<" else x >> shift
        else:
            value = {"+": x + y, "-": x - y, "*": x * y, "&": x & y, "|": x | y, "^": x ^ y}[expr.op]
        return self.wrapped(value, ty, expr.pos)

    def wrapped(self, value: int, ty: C.Prim, pos: Optional[Position], exact: bool = False) -> IntConst:
        """`value` in `ty`'s width; a literal must already fit (a negated one may reach MIN)."""
        bits = WIDTHS[ty.name]
        signed = ty.name.startswith("i")
        if exact and not valid_literal(ty, str(value)):
            fail(f"{value} does not fit {ty.name}", pos)
        value &= (1 << bits) - 1
        if signed and value >= 1 << bits - 1:
            value -= 1 << bits
        return ("int", value, ty)


def signatures(resolver: Resolver) -> Signatures:
    return Signatures(resolver)
