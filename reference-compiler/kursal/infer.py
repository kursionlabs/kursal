"""Type inference for function bodies, by constraint solving.

Walking a body assigns types, using fresh metavariables for unknowns. Unification solves equalities
immediately. Field selection, method lookup and trait selection can need information learned later:
`wait` stores a readiness predicate, an action and a diagnostic position. `later` waits for a type's head.

Adding a waiter or unifying types calls `solve`, which scans waiting actions until none is ready.
Each ready action is removed before it runs. Actions may unify types or add waiters; `solving` prevents
nested solver loops, so the outer loop picks up the resulting work.

Once ready work is exhausted, `run_defaults` gives still-unknown `pack` types their payload-derived
types and resumes solving. Defaults run at function finalization, or earlier when `shape` needs a known
type head. Integer literals have no default: their types must be determined by context or a suffix.
`finish` drains ready work and defaults, rejects remaining waiters and undetermined numeric literals,
then replaces solved metavariables in recorded AST types and call annotations for lowering.

Results are recorded on the AST for lowering:
  expression.ty      its core type
  Call.call          what is called (see `Callee`)
  Closure/Foreach.captures   Capture(name, binder_id) records, in first-use order
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence, Set
from typing import Literal, Never, Optional
from dataclasses import dataclass, field
from itertools import count
from . import core as C
from . import syntax as S
from .resolve import Decl, ResolveError, Resolver, listed
from .sigs import InstanceSig, Scope, Sig, Signatures
from .sexpr import KursError, Position
from .types import (Surface, Types, brand_differences, brand_name, equal, free_brands, free_names, fresh, made_from,
                    mapped_component, subst)
from .termination import check_instance_termination
from .coherence import check_coherence
from .unify import Meta, ground, instantiate, match_head, prune, unify, zonk

INTS = set(C.INTEGERS)
FLOATS = C.FLOATS
# Pointer operations, as (parameter types, result): "T" is the pointee, "O" a memory ordering.
POINTER_OPS = {
    "load": ((), "T"), "load_volatile": ((), "T"),
    "store": (("T",), "()"), "store_volatile": (("T",), "()"),
    "atomic_load": (("O",), "T"), "atomic_store": (("T", "O"), "()"),
    "compare_exchange": (("T", "T", "O", "O"), "(T, bool)"),
    **{name: (("T", "O"), "T") for name in ("atomic_swap", "fetch_add", "fetch_sub", "fetch_and", "fetch_or", "fetch_xor")},
}
POINTER_METHODS = set(POINTER_OPS)
BUILTIN_ARITY = {"fresh": 0, "static_brand": 0, "sizeof": 0, "alloca": 0, "enum_variant": 1,
                 "fence": 1, "load_volatile": 1, "store_volatile": 2, "trap": 0}


ARITH = {"+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>"}
COMPARE = {"==", "!=", "<", "<=", ">", ">="}
type Operand = tuple[C.Type, bool]  # (operand_ty, lends_place)
type MethodLookup = tuple[Literal["pointer"]] | tuple[Literal["method"], Sig] | tuple[Literal["trait"], C.Trait, list[C.Type]]

CONSTANTS: dict[str, C.Type] = {"bool": C.BOOL, "()": C.UNIT, "!": C.Never()}


class InferError(KursError):
    pass


def fail(message: str, pos: Optional[Position] = None) -> Never:
    raise InferError(message, pos)


# ------------------------------------------------------------------ types --

_ids = count()  # source binding identities


# ------------------------------------------------------- what gets recorded --

@dataclass
class Receiver:
    value: S.Expr
    lend: bool


@dataclass
class FnCall:
    sig: Sig
    sub: dict[str, C.Type]
    receiver: Optional[Receiver] = None

    @property
    def name(self) -> str:
        decl = self.sig.decl
        return decl.node.name if decl is not None and decl.kind == "extern" else self.sig.name

    @property
    def targs(self) -> tuple[C.Type, ...]:
        """In signature order; Ignored retains a function value's brand binder."""
        return tuple(self.sub.get(p.name, C.Ignored()) for p in self.sig.params)


@dataclass
class TraitCall:
    trait: str
    method: str
    targs: tuple[C.Type, ...]
    receiver: Optional[Receiver] = None


@dataclass
class ValueCall:
    sub: dict[str, C.Type]

    @property
    def targs(self) -> tuple[C.Type, ...]:
        return tuple(self.sub.values())


@dataclass
class ClosureCall:
    pass


@dataclass
class VariantCall:
    variant: str


@dataclass
class TupleCall:
    pass


@dataclass
class BuiltinCall:
    name: str


@dataclass
class TypedBuiltinCall:
    """A builtin of one type argument, `alloca[T]()` or `sizeof[T]()`."""
    name: str
    target: C.Type


@dataclass
class PointerCall:
    name: str
    receiver: S.Expr


@dataclass
class FStringWrite:
    string: S.FStr


type Callee = (
    FnCall | TraitCall | ValueCall | ClosureCall | VariantCall | TupleCall
    | BuiltinCall | TypedBuiltinCall | PointerCall | FStringWrite
)


@dataclass
class Bundle:
    """The fields a `..rest` pattern left over: known once the type matched against is."""
    types: dict[str, C.Type] = field(default_factory=dict)
    filled: bool = False


@dataclass
class Local:
    type: C.Type
    mutable: bool = False   # a var, or an `&` parameter
    rest: Optional[Bundle] = None     # a `..rest` bundle
    depth: int = 0          # how many closures deep it was bound
    id: int = field(default_factory=lambda: next(_ids))


@dataclass
class Level:
    """A lexical scope: the names bound in it, and the brands a `let exists` opened in it."""
    names: dict[str, Local] = field(default_factory=dict)
    brands: dict[str, str] = field(default_factory=dict)  # source spelling -> unique witness
    opened: set[str] = field(default_factory=set)  # includes witnesses whose spelling was shadowed


@dataclass(frozen=True)
class Capture:
    """One captured binding, in first-use order."""
    name: str
    binder_id: int


@dataclass
class Frame:
    """A closure or foreach body being walked: the outer locals it captures, in first-use order."""
    locals: list[tuple[str, Local]] = field(default_factory=list)   # (name, Local)


# -------------------------------------------------------------- inference --

class Infer:
    def __init__(self, resolver: Resolver, sigs: Signatures) -> None:
        files = [str(resolver.file(module)) for _, _, module in sigs.instances]
        check_instance_termination([(C.Premise(inst.trait, inst.head), inst.premises, inst.pos)
                                    for inst, _, _ in sigs.instances], files, surface=True)
        check_coherence([(C.Premise(inst.trait, inst.head), inst.params, inst.pos)
                         for inst, _, _ in sigs.instances], files, surface=True)
        self.r, self.sigs = resolver, sigs
        self.types = Types(C.Program(types=sigs.types, traits=sigs.traits))
        self.decl_of: dict[str, Decl] = {}
        for scope in resolver.scopes.values():
            for decl in scope.decls.values():
                if decl.kind in ("struct", "enum", "tuple", "trait"):
                    self.decl_of[sigs.name(decl)] = decl
        self.inputs = {name: len(decl.node.forall.params) if decl.node.forall else 1
                       for name, decl in self.decl_of.items() if decl.kind == "trait"}
        self.instances: dict[str, list[InstanceSig]] = {}
        self.visible: dict[C.ModuleId, set[Decl]] = {}
        self.origins: dict[str, Position] = {}    # an opened brand's label -> where its `let exists` is
        for inst, item, module in sigs.instances:
            self.instances.setdefault(inst.trait, []).append(inst)

    def run(self) -> None:
        for decl, sig in self.sigs.sigs.items():
            if isinstance(decl.node, S.Fn) and decl.node.body is not None:
                with self.r.located(decl.module, decl.node.pos):
                    self.function(sig, decl.node, decl.module)
        for inst, item, module in self.sigs.instances:
            for sig, node in zip(inst.methods, item.methods):
                with self.r.located(module, node.pos):
                    self.function(sig, node, module, instance=inst)

    def function(self, sig: Sig, node: S.Fn, module: C.ModuleId, instance: Optional[InstanceSig] = None) -> None:
        self.module, self.sig = module, sig
        self.kinds: dict[str, C.Kind] = {p.name: p.kind for p in sig.params}
        self.assumptions = list(sig.premises) + ([C.Premise(instance.trait, instance.head)] if instance else [])
        self.waiting: list[tuple[Callable[[], bool], Callable[[], None], Optional[Position]]] = []
        self.defaults: list[tuple[C.Type, Callable[[], None]]] = []
        self.recorded: list[S.Expr | S.Pattern | S.Case] = []
        self.literals: list[Meta] = []
        self.brand_checks: list[tuple[C.Type, set[str], Optional[Position]]] = []
        # The binder of each `foreach`, with its item's name and where it is; the binders whose bodies are
        # open; and each local's type, with the binders open where it was bound.
        self.foreaches: dict[str, tuple[str, Optional[Position]]] = {}
        self.open_binders: tuple[str, ...] = ()
        self.binder_checks: list[tuple[C.Type, tuple[str, ...]]] = []
        # Each instantiation of a generic declaration: its parameters, their metavariables, the name it is
        # used by, and where.
        self.instantiations: list[tuple[tuple[C.Param, ...], dict[str, C.Type], str, Optional[Position]]] = []
        # Each type no source writes that the body must determine: the type, what it is, and where.
        self.unwritten: list[tuple[C.Type, str, Optional[Position]]] = []
        self.solving = False
        self.returns = [sig.ret]
        level, node.param_ids = Level(), []
        self.scopes, self.frames = [level], [Frame()]
        for k, (name, ty) in enumerate(zip(sig.pnames, sig.ptypes)):
            level.names[name] = Local(ty, mutable=k in sig.places)
            node.param_ids.append(level.names[name].id)
        assert node.body is not None
        self.check(node.body, sig.ret)
        self.finish()

    def finish(self) -> None:
        """Finish pending inference, report unresolved requirements, and normalize annotations for lowering."""
        self.solve()
        self.run_defaults()
        if self.waiting:
            _, _, pos = self.waiting[0]
            fail("a type here is not known yet; annotate it", pos)
        for m in self.literals:
            if isinstance(prune(m), Meta):
                fail(f"a {'floating' if m.kind == 'float' else 'integer'} literal's type is not determined; write a suffix", m.pos)
        for ty, allowed, pos in self.brand_checks:
            escaped = free_brands(zonk(ty)) - allowed
            if escaped:
                fail(f"brand {self.shown(C.Skolem(min(escaped)))} escapes its lexical scope", pos)
        for ty, open_binders in self.binder_checks:
            escaped = {b for b in free_names(zonk(ty)) if b in self.foreaches and b not in open_binders}
            if escaped:
                item, pos = self.foreaches[min(escaped)]
                fail(f"the type of foreach item {item} escapes its lexical scope", pos)
        for ty, what, pos in self.unwritten:
            if not ground(ty):
                fail(f"cannot infer the {what}", pos)
        for params, sub, owner, pos in self.instantiations:
            for p in params:
                if not ground(sub[p.name]):
                    argument = brand_name(p.name) if p.kind == C.BRAND else Surface((), {}).name(p.name)
                    fail(f"cannot infer the type argument {argument} of {owner}", pos)
        for e in self.recorded:
            e.ty = zonk(e.ty)
            if isinstance(e, S.For):
                e.option = zonk(e.option)
                e.next_call.targs = tuple(zonk(t) for t in e.next_call.targs)
            if not isinstance(e, (S.Path, S.Call)) or e.call is None:
                continue
            call = e.call
            match call:
                case FnCall() | ValueCall():
                    call.sub = {k: zonk(v) for k, v in call.sub.items()}
                case TraitCall():
                    call.targs = tuple(zonk(t) for t in call.targs)
                case TypedBuiltinCall():
                    call.target = zonk(call.target)

    # ---- the solver
    def wait(self, ready: Callable[[], bool], action: Callable[[], None], pos: Optional[Position] = None) -> None:
        """Run `action()` once `ready()` holds."""
        self.waiting.append((ready, action, pos))
        self.solve()

    def later(self, ty: C.Type, action: Callable[[], None], pos: Optional[Position] = None) -> None:
        """Run `action()` once the head of the type `ty` is known."""
        self.wait(lambda: not isinstance(zonk(ty), Meta), action, pos)

    def solve(self) -> None:
        """Drain ready actions; a nested call leaves its work to the active solver loop."""
        if self.solving:
            return
        self.solving = True
        try:
            progress = True
            while progress:
                progress = False
                for item in list(self.waiting):
                    ready, action, _ = item
                    if ready():
                        self.waiting.remove(item)
                        action()
                        progress = True
        finally:
            self.solving = False

    def run_defaults(self) -> None:
        """What is still unknown when nothing else can run takes its default."""
        while self.defaults:
            t, action = self.defaults.pop(0)
            if isinstance(zonk(t), Meta):
                action()
                self.solve()

    def unify(self, a: C.Type, b: C.Type, pos: Optional[Position] = None) -> None:
        if not unify(a, b):
            fail(self.mismatch(zonk(a), zonk(b)), pos)
        self.solve()

    def mismatch(self, a: C.Type, b: C.Type) -> str:
        """Why `a` and `b` failed to unify. Unification is symmetric and its callers pass the two sides in
        no fixed order, so the message names neither as the expected one. Types that differ only in brands
        are a brand mismatch, which says where a `let exists` opened each brand that differs."""
        surface = Surface((a, b), self.origins)
        both = f"{surface.show(a)} and {surface.show(b)}"
        brands = brand_differences(a, b)
        if not brands:
            return f"type mismatch: {both}"
        opened = [label for label in brands if label in self.origins and label not in surface.qualified]
        where = ", ".join(f"{surface.brand(label)}{' was opened' if k == 0 else ''} at "
                          f"{self.origins[label][0]}:{self.origins[label][1]}" for k, label in enumerate(opened))
        return f"brand mismatch: {both}" + (f"; {where}" if where else "")

    def shown(self, ty: C.Type) -> str:
        """`ty` as a message writes it."""
        return Surface((ty,), self.origins).show(ty)

    def coerce(self, actual: C.Type, expected: Optional[C.Type], pos: Optional[Position]) -> None:
        if expected is not None and not isinstance(prune(actual), C.Never):
            self.unify(actual, expected, pos)

    def join(self, a: C.Type, b: C.Type, pos: Optional[Position]) -> C.Type:
        if isinstance(prune(a), C.Never):
            return b
        if not isinstance(prune(b), C.Never):
            self.unify(a, b, pos)
        return a

    def shape(self, ty: C.Type, pos: Optional[Position] = None) -> C.Type:
        """The structure of `ty`, needed now: a type still unknown takes its defaults, else it is an error."""
        if isinstance(zonk(ty), Meta):
            self.solve()
            self.run_defaults()
        ty = zonk(ty)
        if isinstance(ty, Meta):
            fail("the type here is not known yet; annotate it", pos)
        if isinstance(ty, C.Named) and ty.name not in self.types.program.types:
            return ty
        return self.types.shape(ty) if isinstance(ty, (C.Named, C.Foreach)) else ty

    # ---- premises
    def require_visible_shape(self, ty: C.Type, pos: Optional[Position]) -> None:
        """Check nominal authority at shape-using source operations, not type uses."""
        ty = zonk(ty)
        if isinstance(ty, C.Named) and ty.name in self.types.program.types:
            decl = self.types.program.types[ty.name]
            if not self.types.exposed(decl, self.module):
                fail(f"the shape of {ty.name} is not exposed here", pos)

    def require_copy(self, ty: C.Type, pos: Optional[Position]) -> None:
        def known() -> None:
            if not self.types.is_copy(zonk(ty), self.kinds):
                fail("type argument must satisfy Copy", pos)
        self.wait(lambda: ground(ty), known, pos)

    def require_parameter_kinds(self, params: Sequence[C.Param], sub: Mapping[str, C.Type], pos: Optional[Position]) -> None:
        for param in params:
            if param.kind == C.COPY:
                self.require_copy(sub[param.name], pos)

    def require(self, trait: str, args: Sequence[C.Type], pos: Optional[Position]) -> None:
        """Discharge a premise once its input arguments are known."""
        n = self.inputs.get(trait, len(args))
        self.wait(lambda: all(ground(a) for a in args[:n]), lambda: self.discharge(trait, args, n, pos), pos)

    def discharge(self, trait: str, args: Sequence[C.Type], input_count: int, pos: Optional[Position]) -> None:
        """An assumption or a unique matching instance solves the output arguments."""
        if trait == "Exposed":
            concrete = tuple(zonk(arg) for arg in args)
            if len(concrete) != 1 or not self.types.exposure_available(
                    concrete[0], self.module, self.kinds, self.assumptions):
                fail("Exposed requires an exact premise or an authorized traversable shape", pos)
            return
        if trait == "Sized":
            concrete = tuple(zonk(arg) for arg in args)
            if len(concrete) != 1 or not self.types.sized_available(concrete[0], self.assumptions):
                fail("Sized requires an exact premise or a type without parameters whose size is not zero", pos)
            return
        inputs = [zonk(a) for a in args[:input_count]]
        for p in self.assumptions:
            if p.trait == trait and len(p.args) == len(args) and all(equal(zonk(a), b) for a, b in zip(p.args[:input_count], inputs)):
                for a, b in zip(p.args[input_count:], args[input_count:]):
                    self.unify(a, b, pos)
                return
        found: list[tuple[InstanceSig, dict[str, C.Type]]] = []
        for inst in self.instances.get(trait, ()):
            sub = match_head(inst.params, inst.head[:input_count], inputs)
            if sub is not None and self.copy_ok(inst, sub):
                found.append((inst, sub))
        if len(found) != 1:
            written = [zonk(a) for a in args]
            surface = Surface(written, self.origins, (trait,))
            shown = ", ".join(map(surface.show, written))
            fail(f"{'no' if not found else 'several'} instance of {surface.name(trait)}[{shown}]", pos)
        inst, sub = found[0]
        for param in inst.params:
            sub.setdefault(param.name, Meta("brand" if param.kind == C.BRAND else "type"))
        for h, a in zip(inst.head, args):
            self.unify(subst(h, sub), a, pos)
        for premise in inst.premises:
            self.require(premise.trait, [subst(a, sub) for a in premise.args], pos)

    def instantiate(self, params: Sequence[C.Param], owner: str, pos: Optional[Position]) -> dict[str, C.Type]:
        """A fresh metavariable for each of `params`, the parameters of `owner` used at `pos`; each must be
        solved by the end of the body."""
        sub = instantiate(params)
        self.instantiations.append((tuple(params), sub, owner, pos))
        return sub

    def copy_ok(self, inst: InstanceSig, sub: Mapping[str, C.Type]) -> bool:
        return all(p.name not in sub or self.types.is_copy(sub[p.name], self.kinds)
                   for p in inst.params if p.kind == C.COPY)

    # ---- scopes
    def record(self, expr: S.Expr, ty: C.Type) -> C.Type:
        expr.ty = ty
        self.check_brand_scope(ty, expr.pos)
        self.recorded.append(expr)
        return ty

    def library(self, name: str) -> str:
        return self.sigs.name(self.sigs.library(self.module, name))

    def lookup(self, name: str) -> Optional[Local]:
        for level in reversed(self.scopes):
            local = level.names.get(name)
            if local is not None:
                for frame in self.frames[local.depth + 1:]:      # every closure it is used inside captures it
                    if all(l is not local for _, l in frame.locals):
                        frame.locals.append((name, local))
                return local
        return None

    def bind(self, name: str, ty: C.Type, mutable: bool = False, rest: Optional[Bundle] = None) -> Local:
        self.check_brand_scope(ty, None)
        self.binder_checks.append((ty, self.open_binders))
        local = self.scopes[-1].names[name] = Local(ty, mutable, rest, len(self.frames) - 1)
        return local

    def brand_sub(self) -> dict[str, C.Skolem]:
        return {name: C.Skolem(label) for level in self.scopes for name, label in level.brands.items()}

    def check_brand_scope(self, ty: C.Type, pos: Optional[Position]) -> None:
        # Check after solving: an outer metavariable may acquire a local brand later.
        allowed = {p.name for p in self.sig.params if p.kind == C.BRAND} | {"static"}
        allowed.update(label for level in self.scopes for label in level.opened)
        self.brand_checks.append((ty, allowed, pos))

    def type(self, type_expr: S.Type) -> C.Type:
        scope = Scope({p.name: p.kind for p in self.sig.params if p.kind != C.BRAND},
                      {p.name for p in self.sig.params if p.kind == C.BRAND} | set(self.brand_sub()))
        # a foreach binder as written names the binder of its own `foreach`
        binders = {made_from(b): C.Named(b) for b in self.open_binders if made_from(b)}
        return subst(self.sigs.type(type_expr, scope), {**binders, **self.brand_sub()})

    def argument(self, type_expr: S.Type, pos: Optional[Position]) -> C.Type:
        """A type argument written at `pos`, which is never a foreach type (TY-FLAT)."""
        ty = self.type(type_expr)
        if isinstance(ty, C.Foreach):
            fail("a type argument is a foreach type, which is only the type of a whole value, parameter or result", pos)
        return ty

    # ---- statements
    def block(self, block: S.Block) -> C.Type:
        self.scopes.append(Level())
        try:
            diverges = False
            for s in block.stmts:
                diverges |= self.stmt(s)
            if block.tail is not None:
                ty = self.expr(block.tail)
            elif diverges:
                ty = C.Never()
            else:
                ty = C.UNIT
        finally:
            self.scopes.pop()
        return self.record(block, ty)

    def stmt(self, stmt: S.Statement) -> bool:
        """Type one statement; True when it cannot complete normally."""
        match stmt:
            case S.Let(brands=brands) if brands:
                return self.let_exists(stmt)
            case S.Let(pattern=pattern, type=ty, value=value, pos=pos):
                want = self.type(ty) if ty is not None else None
                got = self.check(self.generic(value, want), want)
                self.pattern(pattern, want or got, pos)
                return isinstance(prune(got), C.Never)
            case S.Var(name=name, type=ty, value=value):
                want = self.type(ty) if ty is not None else None
                got = self.check(self.generic(value, want), want)
                stmt.binder = self.bind(name, want or got, mutable=True).id
            case S.Const(name=name, type=ty, value=value):
                want = self.type(ty)
                stmt.folded = self.sigs.typed(name, value, want, self.module)
                stmt.binder = self.bind(name, want).id
            case S.Assign(target=target, value=value):
                self.check(value, self.place_type(target, assign=True))
            case S.ExprStmt(value=value):
                return isinstance(prune(self.expr(value)), C.Never)
            case _:
                fail(f"no rule for statement {type(stmt).__name__}", stmt.pos)
        return False

    def generic(self, value: S.Expr, want: Optional[C.Type]) -> S.Expr:
        """A bare function name bound with no annotation keeps its quantified brands."""
        if want is None and isinstance(value, S.Path):
            value.generic = True
        return value

    def let_exists(self, stmt: S.Let) -> bool:
        package = self.shape(self.expr(stmt.value), stmt.pos)
        if not isinstance(package, C.Exists) or len(package.binders) != len(stmt.brands):
            fail(f"let exists opens {len(stmt.brands)} brands of {self.shown(zonk(package))}", stmt.pos)
        names = tuple(b[1:] for b in stmt.brands)
        if len(set(names)) != len(names):
            fail("an existential opening repeats a brand name", stmt.pos)
        labels = stmt.opened_brands = tuple(f"{name}#open{next(_ids)}" for name in names)
        if stmt.pos is not None:
            self.origins.update((label, stmt.pos) for label in labels)
        self.scopes[-1].brands.update(zip(names, labels))
        self.scopes[-1].opened.update(labels)
        payload = subst(package.body, {b: C.Skolem(l) for b, l in zip(package.binders, labels)})
        if stmt.type is not None:
            self.unify(payload, self.type(stmt.type), stmt.pos)
        self.pattern(stmt.pattern, payload, stmt.pos)
        return False

    def pattern(self, pattern: S.Pattern, ty: C.Type, pos: Optional[Position], mutable: bool = False) -> None:
        pattern.ty = ty
        self.recorded.append(pattern)
        match pattern:
            case S.PBind(name=name):
                pattern.binder = self.bind(name, ty, mutable).id
            case S.PTuple(elems=elems) | S.PArray(elems=elems):
                types = [Meta() for _ in elems]          # the names are bound now, their types related when known

                def known() -> None:
                    self.require_visible_shape(ty, pos)
                    match pattern, self.shape(ty, pos):
                        case S.PTuple(), C.Tuple(elems=actual) if len(actual) == len(elems):
                            pass
                        case S.PArray(), C.Array(elem=elem, length=length) if length == len(elems):
                            actual = (elem,) * length
                        case _:
                            fail(f"pattern does not match {self.shown(zonk(ty))}", pos)
                    for t, a in zip(types, actual):
                        self.unify(t, a, pos)
                self.later(ty, known, pos)
                for q, t in zip(elems, types):
                    self.pattern(q, t, pos, mutable)
            case S.PStruct(fields=pfields, rest=rest):
                field_types, bundle = {name: Meta() for name, _ in pfields}, Bundle()

                def known() -> None:
                    self.require_visible_shape(ty, pos)
                    s = self.shape(ty, pos)
                    if not isinstance(s, C.Struct):
                        fail(f"a struct pattern needs a struct, not {self.shown(zonk(ty))}", pos)
                    fields = dict(s.fields)
                    for name, t in field_types.items():
                        if name not in fields:
                            fail(f"no field {name}", pos)
                        self.unify(t, fields[name], pos)
                    bundle.types.update({n: t for n, t in s.fields if n not in field_types})
                    bundle.filled = True
                self.later(ty, known, pos)
                for name, q in pfields:
                    self.pattern(q, field_types[name], pos, mutable)
                if rest:
                    pattern.rest_id = self.bind(rest, C.UNIT, rest=bundle).id

    # ---- expressions
    def check(self, expr: S.Expr, expected: Optional[C.Type]) -> C.Type:
        ty = self.expr(expr)
        self.coerce(ty, expected, expr.pos)
        return ty

    def expr(self, expr: S.Expr) -> C.Type:
        return self.record(expr, self.infer_expr(expr))

    def infer_expr(self, expr: S.Expr) -> C.Type:
        match expr:
            case S.While():
                return self.apply(((expr.cond, "bool"), (expr.body, "()")), "()", expr.pos)
            case S.If(other=None):
                return self.apply(((expr.cond, "bool"), (expr.then, "()")), "()", expr.pos)
            case S.If(other=other):
                assert other is not None
                return self.apply(((expr.cond, "bool"), (expr.then, "t"), (other, "t")), "t", expr.pos)
            case S.Binop(op=op):
                if op in {"&&", "||"}:
                    return self.apply(((expr.left, "bool"), (expr.right, "bool")), "bool", expr.pos)
                if op in COMPARE | ARITH:
                    result = self.apply(((expr.left, "t"), (expr.right, "t")), "bool" if op in COMPARE else "t", expr.pos)
                    operand = expr.right.ty if isinstance(prune(expr.left.ty), C.Never) else expr.left.ty
                    self.require_primitive(op, operand, expr.pos)
                    return result
            case S.Unary(op=op) if op in {"unsafe", "-", "!", "*"}:
                result = self.apply(((expr.operand, "*t" if op == "*" else "t"),), "t", expr.pos)
                if op in {"-", "!"}:
                    self.require_primitive(op, expr.operand.ty, expr.pos, unary=True)
                return result
            case S.UnsafeStore():
                return self.apply(((expr.pointer, "*t"), (expr.value, "t")), "()", expr.pos)
            case S.Lit(): return self.expr_lit(expr)
            case S.FStr(): return self.expr_fstring(expr, C.Named(self.library("Buffer!")))
            case S.Path(): return self.expr_path(expr)
            case S.Tuple(): return self.expr_tuple(expr)
            case S.Array(): return self.expr_array(expr)
            case S.StructLit(): return self.expr_struct_lit(expr)
            case S.Block(): return self.expr_block(expr)
            case S.For(): return self.expr_for(expr)
            case S.Return(): return self.expr_return(expr)
            case S.Match(): return self.expr_match(expr)
            case S.Closure(): return self.expr_closure(expr)
            case S.Pack(): return self.expr_pack(expr)
            case S.Foreach(): return self.expr_foreach(expr)
            case S.Cast(): return self.expr_cast(expr)
            case S.Unary(): return self.expr_unary(expr)
            case S.Field(): return self.expr_field(expr)
            case S.Index(): return self.expr_field(expr)
            case S.Try(): return self.expr_try(expr)
            case S.Call(): return self.expr_call(expr)
        fail(f"no inference rule for {type(expr).__name__}", expr.pos)

    def require_primitive(self, op: str, operand: C.Type, pos: Optional[Position], *, unary: bool = False) -> None:
        """Primitive applicability waits for inference, without unfolding nominal types."""
        if isinstance(prune(operand), C.Never):
            return  # No operand value reaches this operation.

        def known() -> None:
            ty = zonk(operand)
            name = ty.name if isinstance(ty, C.Prim) else None
            integer, numeric = name in INTS, name in INTS | FLOATS
            if unary:
                valid = numeric if op == "-" else integer or ty == C.BOOL
            elif op in {"==", "!="}:
                valid = isinstance(ty, (C.Prim, C.Ptr, C.NonNull))
            elif op in {"%", "&", "|", "^", "<<", ">>"}:
                valid = integer
            else:
                valid = numeric
            if not valid:
                fail(f"operator {op} is not defined for {self.shown(ty)}", pos)

        self.later(operand, known, pos)

    def apply(self, premises: Sequence[tuple[S.Expr, str]], result: str, pos: Optional[Position]) -> C.Type:
        """Each premise pairs an expression with a type pattern; variables are shared by the rule."""
        variables: dict[str, C.Type] = {}

        def pattern(pattern_text: str) -> C.Type:
            if pattern_text.startswith("*"):
                return C.Ptr(pattern(pattern_text[1:]))
            return CONSTANTS.get(pattern_text) or variables.setdefault(pattern_text, Meta())
        for child, p in premises:
            got = self.expr(child)
            if not isinstance(prune(got), C.Never):       # a child that diverges fits anywhere
                self.unify(got, pattern(p), pos)
        return pattern(result) if result in CONSTANTS or result in variables else C.Never()

    def expr_lit(self, expr: S.Lit) -> C.Type:
        match expr:
            case S.Lit(kind="int", suffix=None):
                m = Meta("int")
                m.pos = expr.pos
                self.literals.append(m)
                return m
            case S.Lit(kind="int" | "float", suffix=suffix) if suffix:
                return C.Prim(suffix)
            case S.Lit(kind="float"):
                m = Meta("float")
                m.pos = expr.pos
                self.literals.append(m)
                return m
            case S.Lit(kind="bool"):
                return C.BOOL
        return C.Named(self.library("Slice"), (C.Prim("u8"), C.Skolem("static")))

    def expr_fstring(self, expr: S.FStr, writer: C.Type) -> C.Type:
        """Each `{x}` of the f-string needs `Format[X, W]`, for `W` the `writer` it is written into."""
        fmt = self.library("Format")
        for part in expr.parts:
            if not isinstance(part, str):
                value = self.expr(part.operand if isinstance(part, S.Unary) and part.op == "&" else part)
                self.require(fmt, [value, writer], part.pos)
        return C.Named(self.library("Buffer!"))

    def expr_path(self, expr: S.Path) -> C.Type:
        ref = expr.ref
        match ref:
            case ("local", name):
                if any(args for _, args in expr.segments):
                    fail("explicit type arguments on local values are not supported; specialize the named function before binding", expr.pos)
                local = self.lookup(name)
                if local is None:
                    fail(f"unknown local {name}", expr.pos)
                if local.rest is not None:
                    fail(f"{name} is a ..rest bundle, not a value", expr.pos)
                expr.binder = local.id
                return local.type
            case ("decl", Decl(kind="const") as decl):
                if any(args for _, args in expr.segments):
                    fail("constant paths take no type arguments", expr.pos)
                expr.folded = self.sigs.const(decl)
                assert expr.folded[2] is not None
                return expr.folded[2]
            case ("decl", Decl(kind="fn" | "method" | "extern") as decl):
                return self.function_value(expr, self.sigs.sigs[decl])
            case ("variant", enum_decl, name):
                return self.variant(expr, enum_decl, name, None)
        fail(f"{'::'.join(expr.names)} is not a value", expr.pos)

    def function_value(self, expr: S.Path, sig: Sig) -> C.Type:
        """A named function keeps its declared parameter modes and result type.

        Bare names bound by let retain unconstrained brand quantifiers; other
        parameters are instantiated at the occurrence.
        """
        retain = expr.generic and not any(args for _, args in expr.segments)
        kept = tuple(p for p in sig.params if p.kind == C.BRAND and retain and
                     not any(p.name in free_names(zonk(a)) for q in sig.premises for a in q.args))
        sub = self.instantiate([p for p in sig.params if p not in kept], expr.names[-1], expr.pos)
        self.explicit_args(expr, sig, sub)
        expr.call = FnCall(sig, sub)
        self.require_signature(sig, sub, expr.pos)
        # A kept brand gets a name no source spells: a type argument solved later never captures it.
        labels = {p.name: fresh(p.name) for p in kept}
        whole = {**sub, **{name: C.Skolem(label) for name, label in labels.items()}}
        ptypes = tuple(subst(t, whole) for t in sig.ptypes)
        ret = subst(sig.ret, whole)
        return C.Fn(ptypes, ret, tuple(C.Param(labels[p.name], C.BRAND) for p in kept), sig.unsafe, sig.places)

    def explicit_args(self, path: S.Path, sig: Sig, sub: dict[str, C.Type]) -> None:
        """Type arguments written on a callee path: on its last segment, the function's own; on the type
        segment of `Type[A]::f`, its impl's."""
        segments, decl = path.segments, sig.decl
        own = segments[-1][1]
        for index, (_, args) in enumerate(segments[:-1]):
            if args and not (index == len(segments) - 2 and decl is not None and decl.kind == "method"):
                fail("function type arguments cannot appear on a namespace", path.pos)
        impl_params = len(decl.impl.forall.params + decl.impl.forall.determined) \
            if decl is not None and decl.kind == "method" and decl.impl is not None and decl.impl.forall else 0
        if own:
            explicit = [p for p in sig.params if not p.name.startswith("Env#")]
            mine, impl = explicit[impl_params:], explicit[:impl_params]
            params = next((ps for ps in (mine, impl + mine, impl) if len(ps) == len(own)), None)
            if params is None:
                fail(f"{segments[-1][0]} takes {len(mine)} type arguments", path.pos)
            for p, a in zip(params, own):
                if not isinstance(a, S.TIgnored) and a != S.TBrand("'_") and p.name in sub:
                    self.unify(sub[p.name], self.argument(a, path.pos), path.pos)
        if len(segments) >= 2 and segments[-2][1] and decl is not None and decl.kind == "method":
            assert decl.impl is not None
            target = self.sigs.type(decl.impl.type, self.sigs.forall(decl.impl.forall, Scope())[2])
            if not isinstance(target, C.Named):
                fail("type arguments on a method path need a named impl target", path.pos)
            owner = self.r.lookup(self.module, path.names[:-1], path.pos)
            if not isinstance(owner, Decl) or self.sigs.name(owner) != target.name:
                fail("method type arguments belong on the impl type, not a namespace", path.pos)
            if len(segments[-2][1]) != len(target.args):
                fail(f"the impl type qualifier takes {len(target.args)} type arguments", path.pos)
            written = C.Named(target.name, tuple(self.argument(a, path.pos) for a in segments[-2][1]))
            self.unify(subst(target, sub), written, path.pos)

    def path_arguments(self, path: S.Path, declaration: Decl, *, member: bool = False) -> tuple[S.Type, ...]:
        """Arguments belong on the declaration name, before a variant/method if present."""
        owner = len(path.segments) - (2 if member else 1)
        for index, (_, args) in enumerate(path.segments):
            if args and index != owner:
                fail(f"type arguments belong on {declaration.name}, not a namespace or member", path.pos)
        written = path.segments[owner][1] if owner >= 0 else ()
        if member and written and self.r.lookup(self.module, path.names[:-1], path.pos) is not declaration:
            fail(f"type arguments belong on {declaration.name}, not a namespace", path.pos)
        return written

    def construct(self, expr: S.Expr, decl: Decl, written: Sequence[S.Type]) -> tuple[C.TypeDecl, dict[str, C.Type], C.Named]:
        """The type a struct, tuple or enum is built at: its parameters instantiated, and any written ones applied."""
        td = self.sigs.types[self.sigs.name(decl)]
        self.require_visible_shape(C.Named(td.name), expr.pos)
        if len(written) > len(td.params):
            fail(f"{td.name} accepts at most {len(td.params)} type arguments, got {len(written)}", expr.pos)
        sub = self.instantiate(td.params, Surface((), {}, (td.name,)).name(td.name), expr.pos)
        for p, a in zip(td.params, written):
            self.unify(sub[p.name], self.argument(a, expr.pos), expr.pos)
        self.require_parameter_kinds(td.params, sub, expr.pos)
        return td, sub, C.Named(td.name, tuple(sub[p.name] for p in td.params))

    def variant(self, expr: S.Path, enum_decl: Decl, name: str, payload_args: Optional[tuple[S.Expr, ...]]) -> C.Type:
        enum, sub, result = self.construct(expr, enum_decl, self.path_arguments(expr, enum_decl, member=True))
        assert isinstance(enum.body, C.Enum)
        payload = subst(dict(enum.body.variants)[name], sub)
        expr.call = VariantCall(name)
        if payload_args is None:
            if payload != C.UNIT:
                fail(f"variant {name} carries a payload", expr.pos)
        elif len(payload_args) == 1:
            self.check(payload_args[0], payload)
        elif isinstance(payload, C.Tuple) and len(payload.elems) == len(payload_args):
            for a, t in zip(payload_args, payload.elems):
                self.check(a, t)
        else:
            fail(f"variant {name} takes one payload", expr.pos)
        return result

    def expr_tuple(self, expr: S.Tuple) -> C.Type:
        return C.Tuple(tuple(self.expr(x) for x in expr.elems))

    def expr_array(self, expr: S.Array) -> C.Type:
        elem = Meta()
        if not expr.elems:
            self.unwritten.append((elem, "element type of the empty array", expr.pos))
        for x in expr.elems:
            self.check(x, elem)
        if not expr.fill:
            return C.Array(elem, len(expr.elems))
        result = Meta()           # `[x, ..]` gets its length from the type it is used at

        def fill() -> None:
            want = self.shape(result, expr.pos)
            if not isinstance(want, C.Array):
                fail("an array literal with `..` needs its length from the expected type", expr.pos)
            self.unify(elem, want.elem, expr.pos)
        self.later(result, fill, expr.pos)
        return result

    def expr_struct_lit(self, expr: S.StructLit) -> C.Type:
        ref = expr.path.ref
        assert ref is not None and ref[0] == "decl"
        td, sub, result = self.construct(expr, ref[1], self.path_arguments(expr.path, ref[1]))
        assert isinstance(td.body, C.Struct)
        fields = {n: subst(t, sub) for n, t in td.body.fields}
        given: set[str] = set()
        expr.field_ids = {}
        for name, value in expr.fields:
            if name not in fields or name in given:
                fail(f"field {name} is unknown or given twice", expr.pos)
            given.add(name)
            if value is not None:
                self.check(value, fields[name])
                continue
            local = self.lookup(name)
            if local is None:
                fail(f"unknown local {name}", expr.pos)
            expr.field_ids[name] = local.id
            self.unify(local.type, fields[name], expr.pos)
        if expr.spread:
            rest = self.lookup(expr.spread)
            if rest is None or rest.rest is None:
                fail(f"{expr.spread} is not a ..rest bundle", expr.pos)
            expr.spread_id = rest.id
            bundle = rest.rest

            def spread() -> None:
                for name in [n for n in fields if n not in given]:
                    if name not in bundle.types:
                        fail(f"..{expr.spread} has no field {name}", expr.pos)
                    self.unify(bundle.types[name], fields[name], expr.pos)
            self.wait(lambda: bundle.filled, spread, expr.pos)
        elif given != set(fields):
            missing = sorted(set(fields) - given)
            fail(f"missing field{'s' if len(missing) > 1 else ''} {listed(missing)}", expr.pos)
        return result

    def expr_block(self, expr: S.Block) -> C.Type:
        return self.block(expr)

    def expr_for(self, expr: S.For) -> C.Type:
        iterator = expr.iterator
        if not (isinstance(iterator, S.Unary) and iterator.op == "&"):
            fail("a for loop lends its iterator: `for x in &it`", expr.pos)
        it, item = self.place_type(iterator.operand), Meta()
        expr.next_call = TraitCall(self.library("Iterator"), "next", (it, item))
        self.require(expr.next_call.trait, expr.next_call.targs, expr.pos)
        expr.option = C.Named(self.library("Option"), (item,))
        self.require_visible_shape(expr.option, expr.pos)
        self.scopes.append(Level())
        expr.binder = self.bind(expr.name, item).id
        self.check(expr.body, C.UNIT)
        self.scopes.pop()
        return C.UNIT

    def expr_return(self, expr: S.Return) -> C.Type:
        if expr.value is not None:
            self.check(expr.value, self.returns[-1])
        else:
            self.coerce(C.UNIT, self.returns[-1], expr.pos)
        return C.Never()

    def expr_match(self, expr: S.Match) -> C.Type:
        scrutinee = expr.scrutinee
        expr.lent = isinstance(scrutinee, S.Unary) and scrutinee.op == "&" and self.is_place(scrutinee.operand)
        if expr.lent:
            assert isinstance(scrutinee, S.Unary)
            ty = self.place_type(scrutinee.operand)
        else:
            ty = self.expr(scrutinee)
        self.later(ty, lambda: self.require_visible_shape(ty, expr.pos), expr.pos)
        result = None
        for arm in expr.arms:
            self.scopes.append(Level())
            for case in arm.cases:
                self.case(case, ty, expr.lent, len(arm.cases) == 1, expr.pos)
            arm_ty = self.expr(arm.body)
            self.scopes.pop()
            result = arm_ty if result is None else self.join(result, arm_ty, arm.pos)
        return result if result is not None else C.Never()

    def case(self, case: S.Case, ty: C.Type, lent: bool, alone: bool, pos: Optional[Position]) -> None:
        if case.kind == "wild":
            return
        if case.kind == "value":
            def integer() -> None:
                s = self.shape(ty, pos)
                if not (isinstance(s, C.Prim) and s.name in INTS):
                    fail("value patterns need an integer scrutinee", pos)
            self.later(ty, integer, pos)
            return
        assert case.path is not None
        ref = case.path.ref
        if ref is None:                         # a bare label: a variant of the scrutinee's sum
            name = case.path.names[0]
            shape = self.shape(ty, pos)
            variants = dict(shape.variants) if isinstance(shape, C.Enum) else {}
            if name not in variants:
                fail(f"{self.shown(ty)} has no variant {name}", case.pos or pos)
            payload = variants[name]
        else:
            assert ref[0] == "variant"
            _, enum_decl, name = ref
            enum, sub, scrutinee = self.construct(case.path, enum_decl, self.path_arguments(case.path, enum_decl, member=True))
            self.unify(scrutinee, ty, pos)
            assert isinstance(enum.body, C.Enum)
            payload = subst(dict(enum.body.variants)[name], sub)
        case.ty, case.label = payload, name
        self.recorded.append(case)
        case.binder_ids, binders = [], case.binders
        if binders is None:
            return
        if not alone:
            fail("a grouped arm binds no payload", pos)
        if len(binders) == 1:
            types = [payload]
        elif isinstance(payload, C.Tuple) and len(payload.elems) == len(binders):
            types = list(payload.elems)
        else:
            fail(f"variant {name} does not carry {len(binders)} values", pos)
        case.binder_ids = [None if b == "_" else self.bind(b, t, mutable=lent).id for b, t in zip(binders, types)]

    def expr_closure(self, expr: S.Closure) -> C.Type:
        params = [self.type(t) if t is not None else Meta() for _, _, t in expr.params]
        self.unwritten += [(ty, f"type of closure parameter {name}", expr.pos) for (_, name, t), ty in zip(expr.params, params) if t is None]
        ret = self.type(expr.ret) if expr.ret is not None else Meta()
        if expr.ret is None:
            self.unwritten.append((ret, "return type of the closure", expr.pos))
        places = frozenset(k + 1 for k, (lend, _, _) in enumerate(expr.params) if lend)
        frame = Frame()
        self.frames.append(frame)
        self.scopes.append(Level())
        expr.param_ids = [self.bind(name, ty, mutable=lend).id for (lend, name, _), ty in zip(expr.params, params)]
        self.returns.append(ret)
        self.check(expr.body, ret)
        self.returns.pop()
        self.scopes.pop()
        self.frames.pop()
        expr.captures = tuple(Capture(name, local.id) for name, local in frame.locals)
        env = C.Tuple(tuple(l.type for _, l in frame.locals))
        return C.Struct((("env", env), ("code", C.Fn((env, *params), ret, places=places))))

    def expr_pack(self, expr: S.Pack) -> C.Type:
        names = tuple(b[1:] for b in expr.brands)
        scope = {p.name for p in self.sig.params if p.kind == C.BRAND} | {"static"} | set(self.brand_sub())
        for name in names:
            if name not in scope:
                fail(f"pack brand {name} is not in scope", expr.pos)
        witnesses = tuple(self.brand_sub().get(n, C.Skolem(n)) for n in names)
        expr.witnesses = witnesses
        labels = tuple(w.label for w in witnesses)
        value, result = self.expr(expr.value), Meta()

        def known() -> None:                      # the type it is used at says which brands are hidden
            want = self.shape(result, expr.pos)
            if not isinstance(want, C.Exists) or len(want.binders) != len(labels):
                fail("pack does not match the type it is used at", expr.pos)
            self.unify(value, subst(want.body, {b: C.Skolem(l) for b, l in zip(want.binders, labels)}), expr.pos)

        def default() -> None:                    # otherwise it is the payload's type with the brands abstracted
            # Binders no source spells: a brand of the payload, even one solved later, is never captured.
            binders = tuple(f"p{k}#" for k in range(len(labels)))
            self.unify(result, C.Exists(binders, subst(zonk(value), {l: C.Skolem(b) for l, b in zip(labels, binders)})))
        self.later(result, known, expr.pos)
        self.defaults.append((result, default))
        return result

    def expr_foreach(self, expr: S.Foreach) -> C.Type:
        operand = zonk(self.expr(expr.source))
        if isinstance(operand, Meta):
            operand = self.shape(operand, expr.pos)
        # A value of a foreach type is mapped (the map form): the result has its source, binder and
        # pattern, and each item is a component of the value. Anything else is the source itself.
        source = operand.source if isinstance(operand, C.Foreach) else operand
        # Through a pointer to a sum the traversal of the pointer reads the variant, an unsafe read that
        # lowering authorizes; a mapped value holds its variant, and reads nothing. Anything else it
        # traverses needs only the shape.
        if not self.types.sum_behind_pointer(source, self.module, self.kinds):
            self.require("Exposed", (source,), expr.pos)
        # Each `foreach` binds a binder of its own, named after the one written, as `F#F12` for `F`.
        written = expr.binder or ""
        if written and (any(p.name == written for p in self.sig.params) or
                        any(made_from(b) == written for b in self.open_binders)):
            fail(f"foreach binder {written} shadows another type named {written}", expr.pos)
        binder = f"{written}#F{next(_ids)}"
        self.foreaches[binder] = (expr.name, expr.pos)
        self.kinds[binder] = C.ANY
        outer = self.open_binders
        item: C.Type
        if isinstance(operand, C.Foreach):
            indirect, item = operand.indirect, mapped_component(operand, C.Named(binder))
        else:
            indirect = isinstance(self.shape(source, expr.pos), C.Ptr)
            item = C.Ptr(C.Named(binder)) if indirect else C.Named(binder)
        self.open_binders = (*outer, binder)
        frame = Frame()
        self.frames.append(frame)
        self.scopes.append(Level())
        expr.name_id = self.bind(expr.name, item).id
        body = zonk(self.expr(expr.body))
        self.scopes.pop()
        self.frames.pop()
        self.open_binders = outer
        expr.captures = tuple(Capture(name, local.id) for name, local in frame.locals)
        return C.Foreach(binder, indirect, source, body)

    def expr_cast(self, expr: S.Cast) -> C.Type:
        self.expr(expr.value)
        return self.type(expr.type)

    def expr_unary(self, expr: S.Unary) -> C.Type:                  # the other operators have rules; `&` lends the receiver of a call
        first = self.first_call(expr.operand)
        if first is None:
            fail("& lends a place only as a call argument, a receiver, or a match scrutinee", expr.pos)
        first.lend_marked = True
        return self.expr(expr.operand)

    def pointee(self, pointer: C.Type, pos: Optional[Position]) -> C.Type:
        """The type behind a pointer."""
        target = Meta()
        self.unify(pointer, C.Ptr(target), pos)
        return target

    def first_call(self, expr: S.Expr) -> Optional[S.Call]:
        """The innermost method call of a postfix chain: the one `&` lends the receiver of."""
        while True:
            match expr:
                case S.Call(callee=S.Field(value=receiver)) if self.is_place(receiver):
                    return expr
                case S.Call(callee=S.Field(value=receiver)):
                    expr = receiver
                case S.Field(value=v) | S.Index(value=v) | S.Try(value=v):
                    expr = v
                case _:
                    return None

    def expr_field(self, expr: S.Field | S.Index) -> C.Type:
        return self.projection(expr)

    def expr_try(self, expr: S.Try) -> C.Type:
        """`x?` unwraps success or returns failure from the same Option/Result family."""
        operand, returned, result = self.expr(expr.value), self.returns[-1], Meta()
        self.wait(lambda: not isinstance(prune(operand), Meta) and not isinstance(prune(returned), Meta),
                  lambda: self.unify(result, self.resolve_try(expr, operand, returned)), expr.pos)
        return result

    def resolve_try(self, expr: S.Try, operand: C.Type, returned: C.Type) -> C.Type:
        operand, returned = zonk(operand), zonk(returned)
        family = self.family(operand)
        if family is None or self.family(returned) != family or not isinstance(operand, C.Named) or not isinstance(returned, C.Named) or operand.name != returned.name:
            fail("? needs the operand and the function result to be the same direct Option/Result type", expr.pos)
        variants, other = self.variants_of(operand), self.variants_of(returned)
        self.require_visible_shape(operand, expr.pos)
        self.require_visible_shape(returned, expr.pos)
        ok, error = ("Ok", "Err") if family == "result" else ("Some", "None")
        self.unify(variants[error], other[error], expr.pos)
        expr.kind = family
        return variants[ok]

    def family(self, ty: C.Type) -> Optional[Literal["option", "result"]]:
        if not isinstance(ty, C.Named) or ty.name not in self.types.program.types:
            return None
        body = self.types.program.types[ty.name].body
        names = {n for n, _ in body.variants} if isinstance(body, C.Enum) else set()
        if names == {"Ok", "Err"}:
            return "result"
        if names == {"Some", "None"}:
            return "option"
        return None

    def variants_of(self, ty: C.Named) -> dict[str, C.Type]:
        td = self.types.program.types[ty.name]
        assert isinstance(td.body, C.Enum)
        return {n: subst(f, {p.name: a for p, a in zip(td.params, ty.args)}) for n, f in td.body.variants}

    # ---- places
    def place_type(self, expr: S.Expr, assign: bool = False) -> C.Type:
        """The type of a place expression: a local, or fields and tuple elements of one."""
        match expr:
            case S.Path(ref=("local", name)):
                local = self.lookup(name)
                if local is None:
                    fail(f"unknown local {name}", expr.pos)
                if assign and not local.mutable:
                    fail(f"{name} is not a var", expr.pos)
                expr.binder = local.id
                return self.record(expr, local.type)
            case S.Field() | S.Index():
                return self.projection(expr, assign)
        if assign:
            fail("not an assignable place", expr.pos)
        return self.expr(expr)

    def projection(self, expr: S.Field | S.Index, assign: bool = False) -> C.Type:
        """The type of `owner.name` or `owner.N`; an owner that is a place is typed as one, so that an
        assignment is checked down to its root local."""
        owner = self.place_type(expr.value, assign) if self.is_place(expr.value) else self.expr(expr.value)
        result = Meta()

        def component() -> None:
            self.require_visible_shape(owner, expr.pos)
            s = self.shape(owner, expr.pos)
            if isinstance(expr, S.Field) and isinstance(s, C.Struct) and expr.name in dict(s.fields):
                self.unify(result, dict(s.fields)[expr.name], expr.pos)
            elif isinstance(expr, S.Index) and isinstance(s, C.Tuple) and expr.index < len(s.elems):
                self.unify(result, s.elems[expr.index], expr.pos)
            else:
                fail(f"{self.shown(zonk(owner))} has no component {expr.name if isinstance(expr, S.Field) else expr.index}", expr.pos)
        self.later(owner, component, expr.pos)
        return self.record(expr, result)

    def is_place(self, expr: S.Expr) -> bool:
        return isinstance(expr, S.Path) and expr.ref is not None and expr.ref[0] == "local" or \
            isinstance(expr, (S.Field, S.Index)) and self.is_place(expr.value)

    # ---- calls
    def expr_call(self, expr: S.Call) -> C.Type:
        callee, lent_receiver = expr.callee, expr.lend_marked
        if isinstance(callee, S.Field):
            return self.method_call(expr, lent_receiver)
        if lent_receiver:
            fail("& before a call lends its receiver: `&x.method()`", expr.pos)
        if isinstance(callee, S.Path):
            match callee.ref:
                case ("variant", enum_decl, name):
                    ty = self.variant(callee, enum_decl, name, expr.args)
                    expr.call = callee.call
                    return self.record(callee, ty)
                case ("decl", Decl(kind="fn" | "method" | "extern") as decl):
                    sig = self.sigs.sigs[decl]
                    sub = self.instantiate(sig.params, callee.names[-1], expr.pos)
                    self.explicit_args(callee, sig, sub)
                    expr.call = FnCall(sig, sub)
                    return self.call_sig(expr, expr.call, self.operands(expr.args))
                case ("trait_method", trait_decl, method):
                    return self.trait_call(expr, callee, trait_decl, method)
                case ("decl", Decl(kind="tuple") as decl):
                    td, sub, result = self.construct(expr, decl, self.path_arguments(callee, decl))
                    assert isinstance(td.body, C.Tuple)
                    if len(expr.args) != len(td.body.elems):
                        fail(f"{td.name} has {len(td.body.elems)} elements", expr.pos)
                    for a, t in zip(expr.args, td.body.elems):
                        self.check(a, subst(t, sub))
                    expr.call = TupleCall()
                    return self.record(callee, result)
                case ("builtin", name):
                    return self.builtin(expr, name)
                case ("assoc", param, name):
                    return self.assoc_call(expr, param, name)
        return self.call_value(expr)

    def call_value(self, expr: S.Call) -> C.Type:
        """A call of a value: a closure `{env, code}`, or a function pointer."""
        callee, got, result = self.expr(expr.callee), self.operands(expr.args), Meta()

        def known() -> None:
            ptypes: list[C.Type]
            places: Set[int]
            s = self.shape(callee, expr.pos)
            if isinstance(s, C.Struct) and dict(s.fields).get("code") is not None:
                self.require_visible_shape(callee, expr.pos)
                if tuple(name for name, _ in s.fields) != ("env", "code"):
                    fail("a closure must have exactly env and code fields, in that order", expr.pos)
                env, code_type = s.fields[0][1], s.fields[1][1]
                code = self.shape(code_type, expr.pos)
                if not isinstance(code, C.Fn):
                    fail("a closure code field must have a function type", expr.pos)
                if code.forall:
                    fail("a closure code field must have a monomorphic function type", expr.pos)
                if not code.params or 0 in code.places:
                    fail("a closure code function must take its environment by value first", expr.pos)
                self.unify(env, code.params[0], expr.pos)
                expr.call = ClosureCall()
                ptypes, places, ret = list(code.params[1:]), {k - 1 for k in code.places}, code.ret
            elif isinstance(s, C.Fn):
                sub = self.instantiate(s.forall, expr.callee.names[-1] if isinstance(expr.callee, S.Path) else "the callee", expr.pos)
                expr.call = ValueCall(sub)
                ptypes, places, ret = [subst(t, sub) for t in s.params], s.places, subst(s.ret, sub)
            else:
                fail(f"{self.shown(zonk(callee))} is not callable", expr.pos)
            self.bind_operands(expr, got, ptypes, places)
            self.unify(result, ret, expr.pos)
        self.later(callee, known, expr.pos)
        return result

    def operands(self, args: tuple[S.Expr, ...]) -> list[Operand]:
        """Type each argument now: (type, whether it lends a place). Holding it to its parameter waits for the callee."""
        return [self.operand(a) for a in args]

    def operand(self, arg: S.Expr) -> Operand:
        match arg:
            case S.Unary(op="unsafe", operand=S.Unary(op="&", operand=target) as lend) if not isinstance(target, S.Call):
                operand_ty, lends_place = self.operand(lend)
                return self.record(arg, operand_ty), lends_place
            case S.Unary(op="&", operand=S.Unary(op="*", operand=pointer)):       # `&*p`: a raw pointer's target
                return self.record(arg, self.pointee(self.expr(pointer), arg.pos)), True
            case S.Unary(op="&", operand=target) if self.is_place(target):
                return self.record(arg, self.place_type(target)), True
        return self.expr(arg), False

    def bind_operands(self, expr: S.Call, got: Sequence[Optional[Operand]], ptypes: Sequence[C.Type], places: Set[int]) -> None:
        """Hold the operands to the parameters: the count, the `&` modes, the types. A method's receiver is `None`."""
        if len(got) != len(ptypes):
            fail(f"expected {len(ptypes)} arguments, got {len(got)}", expr.pos)
        for k, (operand, want) in enumerate(zip(got, ptypes)):
            if operand is not None:
                operand_ty, lends_place = operand
                if (k in places) != lends_place:
                    fail("an & parameter takes `&place`, and only an & parameter does", expr.pos)
                self.unify(operand_ty, want, expr.pos)

    def require_signature(self, sig: Sig, sub: Mapping[str, C.Type], pos: Optional[Position]) -> None:
        self.require_parameter_kinds(sig.params, sub, pos)
        for premise in sig.premises:
            self.require(premise.trait, [subst(a, sub) for a in premise.args], pos)

    def call_sig(self, expr: S.Call, call: FnCall, got: Sequence[Optional[Operand]]) -> C.Type:
        sig, sub = call.sig, call.sub
        self.require_signature(sig, sub, expr.pos)
        self.bind_operands(expr, got, [subst(t, sub) for t in sig.ptypes], sig.places)
        return subst(sig.ret, sub)

    def trait_call(self, expr: S.Call, callee: S.Path, trait_decl: Decl, method: str) -> C.Type:
        trait = self.sigs.traits[self.sigs.name(trait_decl)]
        written = self.path_arguments(callee, trait_decl, member=True)
        if len(written) > len(trait.params):
            fail(f"{trait.name} accepts at most {len(trait.params)} type arguments, got {len(written)}", expr.pos)
        targs = [Meta("brand" if kind == C.BRAND else "type") for kind in trait.param_kinds]
        for t, a in zip(targs, written):
            if not isinstance(a, S.TIgnored):
                self.unify(t, self.argument(a, expr.pos), expr.pos)
        return self.trait_method(expr, trait, method, targs, self.operands(expr.args), receiver=None)

    def trait_method(self, expr: S.Call, trait: C.Trait, method: str, targs: Sequence[C.Type],
                     got: Sequence[Optional[Operand]], receiver: Optional[Receiver]) -> C.Type:
        sig = next(m for m in trait.methods if m.name == method)
        sub = dict(zip(trait.params, targs))
        for kind, arg in zip(trait.param_kinds, targs):
            if kind == C.COPY:
                self.require_copy(arg, expr.pos)
        expr.call = TraitCall(trait.name, method, tuple(targs), receiver)
        self.require(trait.name, targs, expr.pos)
        self.bind_operands(expr, got, [subst(t, sub) for t in sig.ptypes], sig.places)
        return subst(sig.ret, sub)

    def assoc_call(self, expr: S.Call, param: str, name: str) -> C.Type:
        """`T::name(args)` for a type parameter `T`: the one bound of `T` with that method."""
        assert isinstance(expr.callee, S.Path)
        if any(args for _, args in expr.callee.segments):
            fail("an associated parameter call takes its type arguments from its trait premise", expr.pos)
        found = [p for p in self.assumptions if p.trait in self.sigs.traits
                 and any(m.name == name for m in self.sigs.traits[p.trait].methods)
                 and p.args and p.args[0] == C.Named(param)]
        if len(found) != 1:
            fail(f"{param}::{name} needs exactly one bound of {param} with that method", expr.pos)
        return self.trait_method(expr, self.sigs.traits[found[0].trait], name, list(found[0].args), self.operands(expr.args),
                                 receiver=None)

    def builtin(self, expr: S.Call, name: str) -> C.Type:
        if name not in BUILTIN_ARITY:
            fail(f"unknown builtin {name}", expr.pos)
        if len(expr.args) != BUILTIN_ARITY[name]:
            if name == "fence":
                fail("fence takes one ordering argument", expr.pos)
            fail(f"{name} takes {BUILTIN_ARITY[name]} value arguments", expr.pos)
        assert isinstance(expr.callee, S.Path)
        targs = expr.callee.segments[-1][1]
        if any(args for _, args in expr.callee.segments[:-1]):
            fail(f"type arguments belong on {name}, not a namespace", expr.pos)
        expected_types = 1 if name in ("alloca", "sizeof") else 0
        if len(targs) != expected_types:
            fail(f"{name} takes {expected_types} type arguments", expr.pos)
        expr.call = BuiltinCall(name)
        match name:
            case "fresh":
                return C.Exists(("x",), C.Brand(C.Skolem("x")))
            case "static_brand":
                return C.Brand(C.Skolem("static"))
            case "trap":
                return C.Never()
            case "enum_variant":
                arg = expr.args[0]
                lent = isinstance(arg, S.Unary) and arg.op == "&"
                if isinstance(arg, S.Unary) and arg.op == "&":
                    if not self.is_place(arg.operand):
                        fail("enum_variant(&place) requires a place", arg.pos)
                    operand = self.record(arg, self.place_type(arg.operand))
                else:
                    operand = self.expr(arg)

                def known_enum() -> None:
                    self.require_visible_shape(operand, arg.pos)
                    if not isinstance(self.shape(operand, arg.pos), C.Enum):
                        fail("enum_variant requires an enum operand", arg.pos)

                if not isinstance(prune(operand), C.Never):
                    self.later(operand, known_enum, arg.pos)
                return C.U32 if lent else C.Tuple((operand, C.U32))
            case "sizeof":
                expr.call = TypedBuiltinCall(name, self.argument(targs[0], expr.pos))
                return C.USIZE
            case "alloca":
                target = self.argument(targs[0], expr.pos)
                expr.call = TypedBuiltinCall(name, target)
                return C.Ptr(target)
            case "fence":
                self.check(expr.args[0], C.Named(self.library("Ordering")))
                return C.UNIT
            case "load_volatile" | "store_volatile":
                pointer, rest = self.expr(expr.args[0]), [self.expr(a) for a in expr.args[1:]]
                return self.pointer_op(expr, name, pointer, rest)
        fail(f"unknown builtin {name}", expr.pos)

    def method_call(self, expr: S.Call, lent_receiver: bool) -> C.Type:
        assert isinstance(expr.callee, S.Field)
        receiver, name = expr.callee.value, expr.callee.name
        if isinstance(receiver, S.FStr) and name == "write":
            writer = Meta()
            self.record(receiver, self.expr_fstring(receiver, writer))
            expr.call = FStringWrite(receiver)
            if len(expr.args) != 1:
                fail("write takes the writer", expr.pos)
            out = self.expr(expr.args[0])
            self.unify(writer, out, expr.pos)
            self.require(self.library("Writable"), [out], expr.pos)
            return out
        if lent_receiver and not self.is_place(receiver):
            fail("& lends a local or a field of one", expr.pos)
        receiver_ty = self.place_type(receiver) if lent_receiver else self.expr(receiver)
        actual = Receiver(receiver, lent_receiver)
        got, result = [None] + self.operands(expr.args), Meta()
        self.later(receiver_ty, lambda: self.unify(result, self.resolve_method(expr, receiver_ty, name, actual, got), expr.pos), expr.pos)
        return result

    def resolve_method(self, expr: S.Call, receiver_ty: C.Type, name: str, receiver: Receiver, got: Sequence[Optional[Operand]]) -> C.Type:
        """The method `name` of the receiver's type: its signature instantiated, and its result type."""
        match self.find_method(receiver_ty, name, expr.pos):
            case ("pointer",):
                expr.call = PointerCall(name, receiver.value)
                operand_types = []
                for operand in got[1:]:
                    if operand is not None:
                        operand_ty, _ = operand
                        operand_types.append(operand_ty)
                return self.pointer_op(expr, name, receiver_ty, operand_types)
            case ("trait", trait, targs):
                method = next(m for m in trait.methods if m.name == name)
                if (0 in method.places) != receiver.lend:
                    fail(f"{name} takes its receiver {'by &' if 0 in method.places else 'by value'}", expr.pos)
                return self.trait_method(expr, trait, name, targs, got, receiver=receiver)
            case ("method", sig):
                if not sig.ptypes:
                    fail(f"{name} takes no receiver", expr.pos)
                if (0 in sig.places) != receiver.lend:
                    fail(f"{name} takes its receiver {'by &' if 0 in sig.places else 'by value'}", expr.pos)
                sub = self.instantiate(sig.params, name, expr.pos)
                self.unify(subst(sig.ptypes[0], sub), receiver_ty, expr.pos)
                expr.call = FnCall(sig, sub, receiver)
                return self.call_sig(expr, expr.call, got)

    def find_method(self, receiver_ty: C.Type, name: str, pos: Optional[Position]) -> MethodLookup:
        """Method lookup by name: the receiver type's own method, else the one trait (of a single input) or
        blanket impl method of that name visible in the calling module. A name that several of them share
        is written `Trait::name(x)`."""
        t = zonk(receiver_ty)
        if isinstance(t, C.Ptr) and name in POINTER_METHODS:
            return ("pointer",)
        owner: Optional[Decl | str] = None
        if isinstance(t, C.Named):
            owner = self.decl_of.get(t.name)
        elif isinstance(t, C.Prim):
            owner = t.name
        elif isinstance(t, C.NonNull):
            owner = "NonNull"
        own = [d for d in self.r.attached.get((owner, name), []) if d.kind == "method"] if owner else []
        candidates: list[MethodLookup]
        if own:
            candidates = [("method", self.sigs.sigs[d]) for d in own]
        else:
            visible = self.visible_traits(self.module)
            candidates = [("trait", tr, [t] + [Meta("brand" if kind == C.BRAND else "type") for kind in tr.param_kinds[1:]]) for n, tr in self.sigs.traits.items()
                          if self.inputs.get(n) == 1 and self.decl_of[n] in visible and any(m.name == name for m in tr.methods)]
            candidates.extend(("method", self.sigs.sigs[d]) for d in self.r.attached.get((None, name), [])
                              if d.kind == "method" and self.blanket_visible(d, visible))
        if len(candidates) != 1:
            fail(f"{self.shown(t)} has {'no' if not candidates else 'several'} method {name}", pos)
        return candidates[0]

    def visible_traits(self, module: C.ModuleId) -> set[Decl]:
        """The traits a name in `module`'s scope resolves to: declared, imported by name or glob, or
        re-exported. A glob's names are those its source module declares, imports or exports, and
        every trait's own name."""
        if module in self.visible:
            return self.visible[module]
        scope = self.r.scopes[module]
        names = [*scope.decls, *scope.imports, *(d.name for d in self.decl_of.values() if d.kind == "trait")]
        for path in scope.globs:
            try:
                source = self.r.lookup(module, path)
            except ResolveError:
                continue
            if isinstance(source, C.ModuleId) and source in self.r.scopes:
                other = self.r.scopes[source]
                names += [*other.decls, *other.imports, *other.exports]
        found: set[Decl] = set()
        for name in dict.fromkeys(names, True):
            try:
                target = self.r.lookup(module, (name,))
            except ResolveError:
                continue
            if isinstance(target, Decl) and target.kind == "trait":
                found.add(target)
        self.visible[module] = found
        return found

    def blanket_visible(self, method: Decl, visible: set[Decl]) -> bool:
        """A blanket impl's method is found from a package that is the impl's or depends on it, directly
        or through other packages, when every trait bounding the impl's type parameter is visible there
        (and it has one)."""
        if not self.depends_on(self.module.package, method.module.package):
            return False
        impl = method.impl
        assert impl is not None and impl.forall is not None and isinstance(impl.type, S.TName)
        param = impl.type.path[0]
        bounds = [args.ref[1] for _, args in impl.forall.premises
                  if args.args and isinstance(args.args[0], S.TName) and args.args[0].path == (param,)
                  and args.ref is not None and args.ref[0] == "decl"]
        return bool(bounds) and all(b in visible for b in bounds)

    def depends_on(self, package: int, target: int) -> bool:
        """`package` is `target` or depends on it, directly or through other packages."""
        seen, pending = set(), [package]
        while pending:
            p = pending.pop()
            if p == target:
                return True
            if p not in seen:
                seen.add(p)
                pending.extend(self.r.packages[p].deps.values())
        return False

    def pointer_op(self, expr: S.Call, name: str, pointer: C.Type, operands: Sequence[C.Type]) -> C.Type:
        """A pointer operation: its operands held to their types, once the pointee is known."""
        params, result = POINTER_OPS[name]
        if len(operands) != len(params):
            fail(f"{name} takes {len(params)} arguments", expr.pos)
        out = Meta()

        def known() -> None:
            p = self.shape(pointer, expr.pos)
            if not isinstance(p, C.Ptr):
                fail(f"{name} needs a pointer", expr.pos)
            types = {"T": p.inner, "O": C.Named(self.library("Ordering")), "()": C.UNIT,
                     "(T, bool)": C.Tuple((p.inner, C.BOOL))}
            for operand, want in zip(operands, params):
                self.unify(operand, types[want], expr.pos)
            self.unify(out, types[result], expr.pos)
        self.later(pointer, known, expr.pos)
        return out


def infer(resolver: Resolver, sigs: Signatures) -> None:
    Infer(resolver, sigs).run()
