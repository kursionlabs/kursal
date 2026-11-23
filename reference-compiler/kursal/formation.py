"""Type formation: Γ ⊢ T : kind. Nominal types are checked without unfolding.

This pass validates written contracts, not recursive layout or trait coherence.
Premises are checked for formation here and discharged by the assertion checker.
"""

from typing import Optional
from collections.abc import Callable, Iterable

from . import core as C
from .sexpr import KursError, Position
from .types import POINTER_TO_SUM, Kinds, Types, children, equal, free_brands, free_names, show, subst, walk


def formation_children(ty: C.Type) -> tuple[C.Type, ...]:
    """Children admissible for descent; a path cannot cross a binder."""
    if isinstance(ty, C.Exists):
        return ()
    if isinstance(ty, C.Foreach):
        return (ty.source,)
    return children(ty)


def strict_subterm(child: C.Type, parent: C.Type) -> bool:
    return any(equal(child, part) or strict_subterm(child, part)
               for part in formation_children(parent))


def nominal_descent(args: tuple[C.Type, ...], previous: tuple[C.Type, ...]) -> bool:
    """Componentwise subterms, with at least one strict component."""
    return (len(args) == len(previous) and
            all(equal(a, b) or strict_subterm(a, b) for a, b in zip(args, previous)) and
            any(strict_subterm(a, b) for a, b in zip(args, previous)))


def brand_occurs(ty: C.Type, label: str) -> bool:
    """A reference to one brand, respecting nested brand binders."""
    match ty:
        case C.Skolem(label=found):
            return found == label
        case C.Exists(binders=binders) if label in binders:
            return False
    return any(brand_occurs(t, label) for t in children(ty))


class Formation:
    def __init__(self, program: C.Program) -> None:
        self.program = program
        self.types = Types(program)
        self.pos: Optional[Position] = None
        self.discharge: Optional[Callable[[C.Premise, Kinds, tuple[C.Premise, ...], Optional[C.ModuleId]], bool]] = None
        self.premises: tuple[C.Premise, ...] = ()
        self.module: Optional[C.ModuleId] = None
        # The nominal applications whose premises are being formed, each with the number of binders
        # `opened` held when it began: a binder opened since is a different variable from any name its
        # arguments mention, however it is spelled.
        self.active: list[tuple[str, tuple[C.Type, ...], int]] = []
        self.opened: list[str] = []
        self.declaration: Optional[str] = None
        self.sizes = True   # off while the declarations are being formed (`declarations`)

    def fail(self, message: str) -> None:
        # A type declaration has no position of its own: its name says where.
        where = f" in type {self.declaration}" if self.declaration is not None else ""
        raise KursError(f"type formation: {message}{where}", self.pos)

    def extend(self, env: Kinds, params: tuple[C.Param, ...]) -> dict[str, C.Kind]:
        if len({p.name for p in params}) != len(params):
            self.fail("duplicate parameter")
        if any(p.name == "static" for p in params):
            self.fail("static is not a parameter name")
        # A parameter is a variable everywhere, but a name a declaration also has would read as that
        # declaration wherever a type is unfolded.
        for p in params:
            if p.name in self.program.types:
                self.fail(f"{p.name} is a declared type, not a parameter name")
        if any(p.kind not in {C.BRAND, C.COPY, C.ANY} for p in params):
            self.fail("unknown kind")
        return {**env, **{p.name: p.kind for p in params}}

    def fits(self, ty: C.Type, want: C.Kind, env: Kinds) -> None:
        got = self.kind(ty, env)
        if (got == C.BRAND) != (want == C.BRAND):
            self.fail(f"expected {want} argument")
        if want == C.COPY and not self.types.is_copy(ty, env):
            self.fail("expected Copy argument")

    def arguments(self, params: tuple[C.Param, ...], args: tuple[C.Type, ...], env: Kinds) -> None:
        if len(params) != len(args):
            self.fail("wrong number of type arguments")
        for param, arg in zip(params, args):
            self.fits(arg, param.kind, env)
            if param.kind != C.BRAND:
                self.flat(arg, "a type argument")

    def flat(self, ty: C.Type, what: str) -> None:
        """TY-FLAT: a type that expands to a foreach is a whole value's type, a function's parameter or
        result, or a declaration's body, never a component, a pointee, a package body, a foreach source
        or body, or a type argument (premises' included)."""
        if self.types.expands_to_foreach(ty):
            self.fail(f"{what} expands to a foreach type")

    def premise(self, premise: C.Premise, env: Kinds) -> None:
        params: tuple[C.Param, ...]
        if premise.trait in ("Exposed", "Sized"):
            params = (C.Param("T"),)
        else:
            trait = self.program.traits.get(premise.trait)
            if trait is None:
                self.fail(f"unknown trait {premise.trait}")
                return
            params = self.trait_params(trait)
        self.arguments(params, premise.args, env)

    def trait_params(self, trait: C.Trait) -> tuple[C.Param, ...]:
        kinds = trait.param_kinds or (C.ANY,) * len(trait.params)
        if len(kinds) != len(trait.params):
            self.fail("trait parameter kinds disagree")
        return tuple(C.Param(n, k) for n, k in zip(trait.params, kinds))

    def kind(self, ty: C.Type, env: Kinds) -> C.Kind:
        """Validate structure and distinguish brands from values; fits refines Copy."""
        match ty:
            case C.Skolem(label=label):
                if env.get(label) != C.BRAND:
                    self.fail(f"brand {label} is not in scope")
                return C.BRAND
            case C.Named(name=name, args=args):
                if name in env:
                    if args or env[name] == C.BRAND:
                        self.fail(f"invalid type parameter {name}")
                else:
                    decl = self.program.types.get(name)
                    if decl is None:
                        self.fail(f"unknown type {name}")
                        return C.ANY
                    self.arguments(decl.params, args, env)
                    if self.discharge is not None and decl.premises:
                        mentioned = set().union(*(free_names(a) for a in args))
                        if any(n == name and (mentioned & set(self.opened[depth:]) or not nominal_descent(args, previous))
                               for n, previous, depth in self.active):
                            self.fail(f"non-descending nominal premise formation for {name}")
                        self.active.append((name, args, len(self.opened)))
                        try:
                            sub = dict(zip((p.name for p in decl.params), args))
                            for p in decl.premises:
                                required = C.Premise(p.trait, tuple(subst(t, sub) for t in p.args))
                                self.premise(required, env)
                                if not self.discharge(required, env, self.premises, self.module):
                                    self.fail(f"unsatisfied nominal premise {required.trait} for {name}")
                        finally:
                            self.active.pop()
            case C.Brand(brand=brand):
                self.fits(brand, C.BRAND, env)
            case C.Exists(binders=binders, body=body):
                if not binders:
                    self.fail("existential requires at least one brand binder")
                inner = self.extend(env, tuple(C.Param(b, C.BRAND) for b in binders))
                if isinstance(body, C.Exists):
                    self.fail("nested existential binders must be flattened")
                if any(not brand_occurs(body, binder) for binder in binders):
                    self.fail("existential brand binder is unused")
                self.within(binders, lambda: self.fits(body, C.ANY, inner))
                self.flat(body, "a package body")
            case C.Foreach(binder=binder, source=source, body=body):
                self.fits(source, C.ANY, env)
                self.flat(source, "a foreach source")
                if self.types.points_to_sum(source, env):
                    self.fail(POINTER_TO_SUM)
                # The type has an expansion only if every component is a pointer for `*F` to match, and
                # it must have one at every instantiation: the source shows its pointers as written.
                if ty.indirect and not self.types.pattern_available(ty):
                    self.fail(f"foreach *{binder} needs a source written with pointer components")
                self.within((binder,), lambda: self.fits(body, C.ANY, self.extend(env, (C.Param(binder),))))
                self.flat(body, "a foreach body")
            case C.Fn(params=params, ret=ret, places=places):
                if not places <= set(range(len(params))):
                    self.fail("function place index out of range")
                for t in (*params, ret):
                    self.fits(t, C.ANY, env)
            case C.Prim(name=name):
                if name not in C.PRIMS:
                    self.fail(f"unknown primitive {name}")
            case C.Array(length=length) if length < 0:
                self.fail("negative array length")
            case C.Enum(variants=()):
                self.fail("an enum has at least one variant")
            case C.Struct(fields=fields) | C.Enum(variants=fields) if len({n for n, _ in fields}) != len(fields):
                self.fail("duplicate component name")
            case C.Ptr() | C.NonNull() | C.Tuple() | C.Array() | C.Struct() | C.Enum() | C.Never():
                for t in children(ty):
                    self.fits(t, C.ANY, env)
                    self.flat(t, "a pointee" if isinstance(ty, (C.Ptr, C.NonNull)) else "a component")
            case _:
                self.fail("unknown type node")
        if self.sizes:
            self.sized(ty)
        return C.ANY

    def sized(self, ty: C.Type) -> None:
        """TY-SIZE-LIMIT: a closed type's size is below 2^64, the target's address space. A type that
        mentions a parameter is checked at each instantiation: where it is written closed, and in the
        backend's specializations."""
        if self.types.closed(ty) and self.types.layout(ty) is None:
            self.fail(f"{show(ty)} is too large: its size is not below 2^64 bytes")

    def written(self, types: Iterable[C.Type], env: Kinds) -> None:
        """TY-EXISTS-FORM.scope: a package type binds no brand in scope where it is written, which
        `env` holds, nor one an enclosing package binds. A type the checker derives, as by unfolding a
        declaration, was checked where it is written."""
        for ty in types:
            match ty:
                case C.Exists(binders=binders, body=body):
                    if any(env.get(b) == C.BRAND for b in binders):
                        self.fail("an existential brand binder shadows a brand in scope")
                    self.written((body,), self.extend(env, tuple(C.Param(b, C.BRAND) for b in binders)))
                case _:
                    self.written(children(ty), env)

    def within(self, binders: tuple[str, ...], check: Callable[[], object]) -> None:
        """Run `check` with `binders` opened."""
        self.opened.extend(binders)
        try:
            check()
        finally:
            del self.opened[len(self.opened) - len(binders):]

    def function(self, fun: C.Fun, outer: tuple[C.Param, ...] = (),
                 premises: tuple[C.Premise, ...] = ()) -> None:
        self.pos = fun.pos
        self.module, self.premises = fun.module, premises + fun.premises
        env = self.extend({"static": C.BRAND}, outer + fun.params)
        for p in premises + fun.premises:
            self.premise(p, env)
            self.written(p.args, env)
        for t in (*fun.ptypes, fun.ret):
            self.fits(t, C.ANY, env)
        self.written((*fun.ptypes, fun.ret), env)
        generic_brands = {p.name for p in outer + fun.params if p.kind == C.BRAND}
        for _, t in fun.locals:
            if not free_brands(t) <= generic_brands:
                self.fail("local types may mention only function-generic free brands")
            self.fits(t, C.ANY, env)
            self.written((t,), env)
        for block in fun.blocks:
            self.pos = block.pos
            if any(env.get(b) == C.BRAND for b in block.brands):
                self.fail("a block brand parameter cannot rebind a function brand")
            scope = self.extend(env, tuple(C.Param(b, C.BRAND) for b in block.brands))
            for _, view_brands in block.opened:
                for brand in view_brands:
                    self.fits(C.Skolem(brand), C.BRAND, scope)
            for _, t in block.params:
                self.fits(t, C.ANY, scope)
            self.written((t for _, t in block.params), scope)
            for i in block.instrs:
                self.pos = i.pos
                if i.ty is not None:
                    self.fits(i.ty, C.ANY, scope)
                    self.written((i.ty,), scope)
                    if i.op == "alloca":
                        self.flat(i.ty, "a pointee")
                if i.op in {"call", "fn"} and i.args[0] in self.program.funs:
                    self.arguments(self.program.funs[i.args[0]].params, i.tys, scope)
                elif i.op in {"call", "fn"} and (i.args[0] in self.program.externs or i.args[0].startswith("%")):
                    self.arguments((), i.tys, scope)
                elif i.op == "foreach" and i.args[1] in self.program.funs:
                    count = 1 if isinstance(i.ty, C.Foreach) else 0
                    mapper = self.program.funs[i.args[1]]
                    if any(p.kind == C.BRAND for p in mapper.params[:count]):
                        self.fail("the foreach binder requires a value parameter")
                    self.arguments(mapper.params[count:], i.tys, scope)
                elif i.op == "trait_call":
                    self.premise(C.Premise(i.args[0], i.tys), scope)
                else:
                    for t in i.tys:
                        self.kind(t, scope)
                self.written(i.tys, scope)
                if i.op == "open":
                    scope = self.extend(scope, tuple(C.Param(b, C.BRAND) for b in i.binders))
            self.pos = block.term.pos
            match block.term:
                case C.Switch(ty=ty, arms=arms):
                    self.fits(ty, C.ANY, scope)
                    self.written((ty,), scope)
                    edges = [a.edge for a in arms]
                case C.Jnz(then=then, other=other):
                    edges = [then, other]
                case C.Jmp(edge=edge):
                    edges = [edge]
                case _:
                    edges = []
            for edge in edges:
                for t in edge.tys:
                    self.fits(t, C.BRAND, scope)

    def unmarked(self, decl: C.TypeDecl) -> None:
        """A type whose name has no `!` is not linear for every instantiation: it is Copy when its type
        arguments are."""
        if decl.linear:
            return
        args = tuple(C.Skolem(p.name) if p.kind == C.BRAND else C.Named(p.name) for p in decl.params)
        if not self.types.is_copy(C.Named(decl.name, args), {p.name: C.COPY for p in decl.params if p.kind != C.BRAND}):
            whatever = " whatever its type arguments" if any(p.kind != C.BRAND for p in decl.params) else ""
            self.fail(f"type {decl.name} holds a linear type{whatever}; declare it {decl.name}!")

    def declarations(self) -> None:
        env: dict[str, C.Kind] = {"static": C.BRAND}
        # A size unfolds other declarations, so the closed parts of each are sized once all are formed.
        self.sizes = False
        for decl in self.program.types.values():
            self.module, self.premises, self.declaration = decl.module, decl.premises, decl.name
            scope = self.extend(env, decl.params)
            for p in decl.premises:
                if p.trait == "Sized":
                    self.fail("Sized is not a premise of a type declaration")
                self.premise(p, scope)
            self.fits(decl.body, C.ANY, scope)
            self.written((decl.body, *(arg for p in decl.premises for arg in p.args)), scope)
        self.sizes = True
        for decl in self.program.types.values():
            self.declaration = decl.name
            for part in (decl.body, *(arg for p in decl.premises for arg in p.args)):
                for t in walk(part):
                    self.sized(t)
        self.declaration = None
        for decl in self.program.types.values():
            self.unmarked(decl)
        for trait in self.program.traits.values():
            self.module, self.premises = trait.module, ()
            scope = self.extend(env, self.trait_params(trait))
            for method in trait.methods:
                for t in (*method.ptypes, method.ret):
                    self.fits(t, C.ANY, scope)
                self.written((*method.ptypes, method.ret), scope)
        for insts in self.program.instances.values():
            for inst in insts:
                self.pos = inst.pos
                self.module, self.premises = inst.module, inst.premises
                scope = self.extend(env, inst.params)
                self.premise(C.Premise(inst.trait, inst.head), scope)
                for p in inst.premises:
                    self.premise(p, scope)
                self.written((*inst.head, *(arg for p in inst.premises for arg in p.args)), scope)
        self.module, self.premises = None, ()
        for ext in self.program.externs.values():
            self.pos = ext.pos
            for t in (*ext.ptypes, ext.ret):
                self.fits(t, C.ANY, env)
            self.written((*ext.ptypes, ext.ret), env)
        for data in self.program.datas.values():
            self.module = data.module
            self.pos = None
            self.fits(data.ty, C.ANY, env)
            self.written((data.ty,), env)
