"""First-order type unification, shared by inference and instance coherence."""

from collections.abc import Iterable, Sequence
from itertools import count
from typing import Literal, Optional
from . import core as C
from .sexpr import Position
from .types import children as type_children, extractable_names, foreach_binders, free_names, fresh, map_children, subst

type MetaKind = Literal["type", "int", "float", "brand"]
INTS = C.INTEGERS
FLOATS = C.FLOATS

_ids = count()


class Meta(C.Type):
    """An inference variable; `kind` is type, int, float or brand."""
    __slots__ = ("id", "ref", "kind", "pos")

    def __init__(self, kind: MetaKind = "type") -> None:
        self.id = next(_ids)
        self.ref: Optional[C.Type] = None
        self.kind: MetaKind = kind
        self.pos: Optional[Position] = None

    def __repr__(self) -> str:
        return f"?{self.id}" if self.ref is None else repr(self.ref)


def prune(ty: C.Type) -> C.Type:
    while isinstance(ty, Meta) and ty.ref is not None:
        ty = ty.ref
    return ty


def zonk(ty: C.Type) -> C.Type:
    """`ty` with every solved metavariable replaced by its solution."""
    return map_children(prune(ty), zonk)


def ground(ty: C.Type) -> bool:
    """`ty` mentions no unsolved metavariable."""
    ty = zonk(ty)
    return not isinstance(ty, Meta) and all(ground(c) for c in type_children(ty))


def instantiate(params: Iterable[C.Param]) -> dict[str, C.Type]:
    """A fresh metavariable for each type parameter (brand parameters get brand-kind ones)."""
    return {p.name: Meta("brand" if p.kind == C.BRAND else "type") for p in params}


def occurs(meta: Meta, ty: C.Type) -> bool:
    ty = prune(ty)
    return ty is meta or any(occurs(meta, c) for c in type_children(ty))


def fits_kind(kind: MetaKind, ty: C.Type) -> bool:
    match kind:
        case "int":
            return isinstance(ty, C.Prim) and ty.name in INTS
        case "float":
            return isinstance(ty, C.Prim) and ty.name in FLOATS
        case "brand":
            return isinstance(ty, C.Skolem)
    return not isinstance(ty, C.Skolem)


def metas(ty: C.Type) -> set[Meta]:
    """The unsolved variables reachable through a type, including premises."""
    ty = prune(ty)
    if isinstance(ty, Meta):
        return {ty}
    return {m for part in type_children(ty) for m in metas(part)}


def quantified(left: tuple[C.Param, ...], right: tuple[C.Param, ...],
               xs: tuple[C.Type, ...], ys: tuple[C.Type, ...], closed: bool, scoped: bool = False) -> bool:
    """The binders `left` of `xs` and `right` of `ys` are equated. Only a transform's body, `scoped`, has
    inference variables of its own, which an open unification may solve to name its binders; any other
    variable lives outside the binders, and a solution naming one fails."""
    if tuple(p.kind for p in left) != tuple(p.kind for p in right) or len(xs) != len(ys):
        return False
    if scoped and not closed:
        rename = {q.name: C.Skolem(p.name) if p.kind == C.BRAND else C.Named(p.name)
                  for p, q in zip(left, right)}
        return all(unify(zonk(x), subst(zonk(y), rename)) for x, y in zip(xs, ys))
    if not left:
        return all(unify(x, y, closed=closed) for x, y in zip(xs, ys))
    variables = {m for t in (*xs, *ys) for m in metas(t)}
    occupied = {n for t in (*xs, *ys) for n in free_names(zonk(t))}
    labels = []
    for _ in left:
        label = fresh("rigid")
        while label in occupied:
            label = fresh("rigid")
        labels.append(label)
    rigid = tuple(C.Skolem(n) if p.kind == C.BRAND else C.Named(n) for p, n in zip(left, labels))
    a = dict(zip((p.name for p in left), rigid))
    b = dict(zip((p.name for p in right), rigid))
    if (all(unify(subst(zonk(x), a), subst(zonk(y), b), closed=closed) for x, y in zip(xs, ys))
            and not any(free_names(zonk(m)) & set(labels) for m in variables)):
        return True
    for m in variables:              # a failure leaves no solution naming a rigid label
        m.ref = None
    return False


def unify(a: C.Type, b: C.Type, *, closed: bool = False) -> bool:
    """Solve equality; closed instance solutions cannot depend on nested binders."""
    def compare(x: C.Type, y: C.Type) -> bool:
        return unify(x, y, closed=closed)

    a, b = prune(a), prune(b)
    if a is b:
        return True
    if isinstance(a, Meta) or isinstance(b, Meta):
        if not isinstance(a, Meta):
            a, b = b, a
        assert isinstance(a, Meta)
        if isinstance(b, Meta):      # bind the less specific variable to the more specific one
            if a.kind != "type" and b.kind != "type" and a.kind != b.kind:
                return False
            if a.kind != "type":
                a, b = b, a
            a.ref = b
            return True
        if not fits_kind(a.kind, b) or occurs(a, b):
            return False
        a.ref = b
        return True
    if type(a) is not type(b):
        return False
    match a, b:
        case C.Named(name=n, args=xs), C.Named(name=m, args=ys):
            return n == m and len(xs) == len(ys) and all(compare(x, y) for x, y in zip(xs, ys))
        case (C.Prim() | C.Skolem() | C.Never() | C.Ignored()), _:
            return a == b
        case C.Array(length=n), C.Array(length=m) if n != m:
            return False
        case C.Struct(fields=fa), C.Struct(fields=fb):
            return len(fa) == len(fb) and all(n == m and compare(x, y) for (n, x), (m, y) in zip(fa, fb))
        case C.Enum(variants=fa), C.Enum(variants=fb):
            return len(fa) == len(fb) and all(n == m and compare(x, y) for (n, x), (m, y) in zip(fa, fb))
        case C.Exists(binders=xs, body=p), C.Exists(binders=ys, body=q):
            return quantified(tuple(C.Param(x, C.BRAND) for x in xs),
                              tuple(C.Param(y, C.BRAND) for y in ys), (p,), (q,), closed)
        case C.Foreach(), C.Foreach():
            return (a.indirect == b.indirect and compare(a.source, b.source) and
                    quantified(tuple(C.Param(n) for n in foreach_binders(a)),
                               tuple(C.Param(n) for n in foreach_binders(b)), (a.body,), (b.body,), closed,
                               scoped=True))
        case C.Fn(), C.Fn():
            if len(a.params) != len(b.params) or a.places != b.places or a.unsafe != b.unsafe:
                return False
            return quantified(a.forall, b.forall, (*a.params, a.ret), (*b.params, b.ret), closed)
    ca, cb = type_children(a), type_children(b)
    return len(ca) == len(cb) and all(compare(x, y) for x, y in zip(ca, cb))


def match_head(params: tuple[C.Param, ...], head: Sequence[C.Type], query: Sequence[C.Type],
               *, require_extractable: bool = False) -> Optional[dict[str, C.Type]]:
    """Match rigid query types by solving only fresh instance parameters.

    Surface inference passes the input prefix; its output-only variables remain
    unsolved. Only binder-free occurrences contribute extracted parameters.
    KURS passes the full head and requires every parameter to be extractable.
    Failure discards all tentative bindings.
    """
    reachable = {name for ty in head for name in extractable_names(ty)}
    if (len(head) != len(query) or not all(ground(t) for t in query)
            or (require_extractable and any(p.name not in reachable for p in params))):
        return None
    sub = instantiate(params)
    if not all(unify(subst(h, sub), t, closed=True) for h, t in zip(head, query)):
        return None
    return {name: zonk(t) for name, t in sub.items() if name in reachable and ground(t)}
