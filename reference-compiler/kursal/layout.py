"""Finite type expansion, productive C aliases, and finite by-value layouts.

First, recursive nominal applications pass parameters unchanged or choose fixed
types. Then symbolic unfolding checks aliases and stored values. Pointer and
function representations break storage cycles; aggregate names break C alias
cycles. No concrete specialization budget is needed.
"""

from collections.abc import Mapping
from typing import Optional

from . import core as C
from .sexpr import KursError, Position, located
from .termination import Key, components
from .types import children, equal, foreach_binders, free_brands, free_names, subst


def check_type_expansion(program: C.Program, places: Optional[Mapping[str, tuple[str, Optional[Position]]]] = None) -> None:
    """`places` names the file and position of a declaration, by its name, when it has them."""
    graph: dict[Key, set[Key]] = {(name,): set() for name in program.types}
    sites: list[tuple[str, C.Named, set[str], set[str]]] = []

    def collect(owner: str, ty: C.Type, bound: set[str], params: set[str], derived: set[str]) -> None:
        match ty:
            case C.Named(name=name, args=args):
                if name not in bound and name in program.types:
                    graph[(owner,)].add((name,))
                    sites.append((owner, ty, params, derived))
                for arg in args:
                    collect(owner, arg, bound, params, derived)
            case C.Foreach(binder=binder, source=source, body=body):
                collect(owner, source, bound, params, derived)
                binders = set(foreach_binders(ty))
                tainted = binders if free_names(source) & (params | derived) else set()
                collect(owner, body, bound | binders, params - binders, (derived - binders) | tainted)
            case _:
                for child in children(ty):
                    collect(owner, child, bound, params, derived)

    for decl in program.types.values():
        params = {p.name for p in decl.params if p.kind != C.BRAND}
        collect(decl.name, decl.body, params, params, set())
    scc = components(graph)
    for owner, ty, params, derived in sites:
        if scc[(owner,)] != scc[(ty.name,)]:
            continue
        for arg in ty.args:
            names = free_names(arg)
            bare = isinstance(arg, C.Named) and not arg.args and arg.name in params
            if names & derived or names & params and not bare:
                error = KursError(f"recursive type expansion {owner} -> {ty.name}: "
                                  "arguments must be unchanged parameters or independent of them; "
                                  "foreach components of parameter-dependent sources may grow")
                raise located(error, *places[owner]) if places and owner in places else error

    def unfold(ty: C.Named, bound: set[str]) -> Optional[C.Type]:
        if ty.name in bound or ty.name not in program.types:
            return None  # formation reports unknown names and arities separately
        decl = program.types[ty.name]
        body = subst(decl.body, {p.name: a for p, a in zip(decl.params, ty.args)})
        return body

    def key(ty: C.Named) -> C.Named:
        # Brand identity cannot affect size or C representation. Equality also
        # ignores alpha-renaming of binders in arguments introduced by subst.
        return C.Named(ty.name, tuple(subst(a, {b: C.Skolem("_") for b in free_brands(a)}) for a in ty.args))

    def shape(ty: C.Type, active: tuple[C.Named, ...], bound: set[str], pointer: bool = True) -> None:
        """Demand just the outer shape needed by foreach rows (one pointer layer)."""
        if isinstance(ty, C.Named):
            found = unfold(ty, bound)
            if found is not None:
                current = key(ty)
                if any(equal(current, prior) for prior in active):
                    raise KursError(f"recursive foreach source shape through {ty.name}")
                shape(found, (*active, current), bound, pointer)
        elif isinstance(ty, C.Foreach):
            shape(ty.source, active, bound)
        elif isinstance(ty, C.Ptr) and pointer:
            shape(ty.inner, active, bound, False)

    def walk(ty: C.Type, active: tuple[C.Named, ...], bound: set[str], aliases: bool) -> None:
        if isinstance(ty, C.Named):
            found = unfold(ty, bound)
            if found is not None:
                current = key(ty)
                if any(equal(current, prior) for prior in active):
                    reason = "recursive alias needs an aggregate boundary" if aliases else "infinite by-value layout"
                    raise KursError(f"type {ty.name}: {reason}")
                walk(found, (*active, current), bound, aliases)
        elif isinstance(ty, C.Foreach):
            shape(ty.source, (), bound)
            if aliases:
                return  # every foreach result has an aggregate representation
            walk(ty.source, active, bound, aliases)
            walk(ty.body, active, bound | set(foreach_binders(ty)), aliases)
        elif isinstance(ty, C.Brand):
            return
        elif isinstance(ty, (C.Ptr, C.NonNull, C.Fn)) and not aliases:
            return
        elif isinstance(ty, (C.Tuple, C.Struct, C.Enum, C.Array)) and aliases:
            return
        elif isinstance(ty, C.Array) and ty.length == 0:
            return
        else:
            for child in children(ty):
                walk(child, active, bound, aliases)

    for decl in program.types.values():
        bound = {p.name for p in decl.params}
        root = key(C.Named(decl.name, tuple(C.Skolem(p.name) if p.kind == C.BRAND else C.Named(p.name)
                                           for p in decl.params)))
        walk(decl.body, (root,), bound, False)
        walk(decl.body, (root,), bound, True)
