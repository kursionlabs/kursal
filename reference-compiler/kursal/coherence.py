"""At most one instance head matches any fully specified trait application.

Compare full heads, including output parameters. Premises and Copy constraints
cannot establish disjointness. Each side gets fresh metavariables so coincident
parameter names never identify the two instances' scopes.
Every quantified parameter needs a binder-free occurrence in its full head.

The test over-approximates: every subterm that binds names (a package, a transform, a quantified function
type) is compared as a wildcard. Two heads that differ only under binders count as
overlapping, so no overlap can hide in how binders are compared.
"""

from collections.abc import Iterable, Sequence
from typing import Optional

from .core import BRAND, Exists, Fn, Foreach, Param, Premise, Type
from .sexpr import KursError, Position, located
from .types import extractable_names, map_children, show, subst
from .unify import Meta, instantiate, unify


def binders_as_wildcards(ty: Type) -> Type:
    if isinstance(ty, (Exists, Foreach)) or isinstance(ty, Fn) and ty.forall:
        return Meta()
    return map_children(ty, binders_as_wildcards)


def check_instance_range(head: Premise, params: tuple[Param, ...], pos: Optional[Position]) -> None:
    """Every quantified parameter is extractable without crossing a binder."""
    used = {name for ty in head.args for name in extractable_names(ty)}
    for param in params:
        if param.name not in used:
            name = f"${param.name}" if param.kind == BRAND else param.name
            raise KursError(f"instance of {head.trait}: parameter {name} needs a binder-free head occurrence", pos)


def check_coherence(headers: Iterable[tuple[Premise, tuple[Param, ...], Optional[Position]]],
                    files: Sequence[str] = ()) -> None:
    """`files` names the file of each header, when it has one."""
    seen: dict[str, list[tuple[Premise, tuple[Param, ...]]]] = {}
    for k, (head, params, pos) in enumerate(headers):
        try:
            coherent(head, params, pos, seen)
        except KursError as error:
            raise located(error, files[k], pos) if files else error from None
        seen.setdefault(head.trait, []).append((head, params))


def coherent(head: Premise, params: tuple[Param, ...], pos: Optional[Position],
             seen: dict[str, list[tuple[Premise, tuple[Param, ...]]]]) -> None:
    """The instance `head` over `params`: its range, and that it overlaps no instance `seen` before it."""
    check_instance_range(head, params, pos)
    for earlier, earlier_params in seen.get(head.trait, ()):
        # Freshen for every comparison: failed unification can leave some
        # variables solved, so its substitutions must not reach another pair.
        left, right = instantiate(params), instantiate(earlier_params)
        xs: tuple[Type, ...] = tuple(binders_as_wildcards(subst(t, left)) for t in head.args)
        ys = tuple(binders_as_wildcards(subst(t, right)) for t in earlier.args)
        if len(xs) == len(ys) and all(unify(x, y, closed=True) for x, y in zip(xs, ys)):
            current = ", ".join(show(t) for t in head.args)
            previous = ", ".join(show(t) for t in earlier.args)
            raise KursError(f"overlapping instances of {head.trait}: ({current}) and ({previous})", pos)
