"""Finite type expansion, productive C aliases, and finite by-value layouts.

First, recursive nominal applications pass parameters unchanged or choose fixed
types. Then symbolic unfolding checks aliases and stored values. Pointer and
function representations break storage cycles; aggregate names break C alias
cycles. A foreach whose source has known components unfolds to its expansion,
like a name. Last, a C representation key, which expands every foreach, must
reach a name before the same foreach again. No concrete specialization budget
is needed.
"""

from collections.abc import Callable, Mapping
from typing import NoReturn, Optional

from . import core as C
from .sexpr import KursError, Position, located
from .termination import Key, components
from .types import children, equal, foreach_binders, free_brands, free_names, subst


def check_type_expansion(program: C.Program, places: Optional[Mapping[str, tuple[str, Optional[Position]]]] = None) -> None:
    """`places` names the file and position of a declaration, by its name, when it has them."""
    def fail(owner: str, message: str) -> NoReturn:
        error = KursError(message)
        raise located(error, *places[owner]) if places and owner in places else error

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
                fail(owner, f"recursive type expansion {owner} -> {ty.name}: "
                            "arguments must be unchanged parameters or independent of them; "
                            "foreach components of parameter-dependent sources may grow")

    def unfold(ty: C.Named, bound: set[str]) -> Optional[C.Type]:
        if ty.name in bound or ty.name not in program.types:
            return None  # formation reports unknown names and arities separately
        decl = program.types[ty.name]
        body = subst(decl.body, {p.name: a for p, a in zip(decl.params, ty.args)})
        return body

    def key(ty: C.Type) -> C.Type:
        # Brand identity cannot affect size or C representation. Equality also
        # ignores alpha-renaming of binders introduced by subst.
        return subst(ty, {b: C.Skolem("_") for b in free_brands(ty)})

    def shape(owner: str, ty: C.Type, active: tuple[C.Type, ...], bound: set[str], pointer: bool = True) -> None:
        """Demand just the outer shape needed by foreach rows (one pointer layer)."""
        if isinstance(ty, C.Named):
            found = unfold(ty, bound)
            if found is not None:
                current = key(ty)
                if any(equal(current, prior) for prior in active):
                    fail(owner, f"recursive foreach source shape through {ty.name}")
                shape(owner, found, (*active, current), bound, pointer)
        elif isinstance(ty, C.Foreach):
            shape(owner, ty.source, active, bound)
        elif isinstance(ty, C.Ptr) and pointer:
            shape(owner, ty.inner, active, bound, False)

    # The components of a foreach source, as `Types.rows` and `Types.expand` find them, following only
    # what `shape` has demanded of it; None when a parameter or an unknown name decides them, or the
    # pattern does not fit them (formation reports that).
    def outer(ty: C.Type, bound: set[str]) -> Optional[C.Type]:
        if not isinstance(ty, (C.Named, C.Foreach)):
            return ty
        found = unfold(ty, bound) if isinstance(ty, C.Named) else expand(ty, bound)
        return None if found is None else outer(found, bound)

    def rows(source: C.Type, bound: set[str]) -> Optional[tuple[Optional[C.Type], list[tuple[str, C.Type, dict[str, C.Type]]]]]:
        if isinstance(source, C.Foreach):
            inner = rows(source.source, bound)
            if inner is None:
                return None
            mapped = []
            for label, item, extra in inner[1]:
                sub = bind(source, item, extra)
                if sub is None:
                    return None
                mapped.append((label, subst(source.body, sub), sub))
            return inner[0], mapped
        s = outer(source, bound)
        if s is None:
            return None
        wrap: Callable[[C.Type], C.Type] = lambda c: c
        if isinstance(s, C.Ptr):
            s, wrap = outer(s.inner, bound), C.Ptr
            if s is None or isinstance(s, C.Enum):
                return None
        match s:
            case C.Struct(fields=fields):
                return s, [(n, wrap(f), {}) for n, f in fields]
            case C.Tuple(elems=elems):
                return s, [(str(i), wrap(e), {}) for i, e in enumerate(elems)]
            case C.Array(elem=elem):
                return s, [("0", wrap(elem), {})]
            case C.Enum(variants=variants):
                return s, [(n, f, {}) for n, f in variants]
        return None, [("0", source, {})]

    def bind(ty: C.Foreach, item: C.Type, extra: dict[str, C.Type]) -> Optional[dict[str, C.Type]]:
        retained = {name: extra[name] for name in (ty.retained or ()) if name in extra}
        if not ty.indirect:
            return {**retained, ty.binder: item}
        return {**retained, ty.binder: item.inner} if isinstance(item, C.Ptr) else None

    def expand(ty: C.Foreach, bound: set[str]) -> Optional[C.Type]:
        found = rows(ty.source, bound)
        if found is None:
            return None
        skeleton, items = found
        mapped = []
        for label, item, extra in items:
            sub = bind(ty, item, extra)
            if sub is None:
                return None
            mapped.append((label, subst(ty.body, sub)))
        match skeleton:
            case C.Struct():
                return C.Struct(tuple(mapped))
            case C.Enum():
                return C.Enum(tuple(mapped))
            case C.Array(length=length):
                return C.Array(mapped[0][1], length)
        return C.Tuple(tuple(m for _, m in mapped))

    def walk(owner: str, ty: C.Type, active: tuple[C.Type, ...], bound: set[str], aliases: bool) -> None:
        if isinstance(ty, C.Named):
            found = unfold(ty, bound)
            if found is not None:
                current = key(ty)
                if any(equal(current, prior) for prior in active):
                    reason = "recursive alias needs an aggregate boundary" if aliases else "infinite by-value layout"
                    fail(owner, f"type {ty.name}: {reason}")
                walk(owner, found, (*active, current), bound, aliases)
        elif isinstance(ty, C.Foreach):
            shape(owner, ty.source, (), bound)
            if aliases:
                return  # every foreach result has an aggregate representation
            walk(owner, ty.source, active, bound, aliases)
            expanded = expand(ty, bound)
            if expanded is None:
                # Opaque here; every use walks the expansion at its type arguments.
                walk(owner, ty.body, active, bound | set(foreach_binders(ty)), aliases)
                return
            # A component may hold this foreach again by value, unfolding no name on the way
            # (`struct T { f: foreach *F of *T => F }`): it is active like a name.
            current = key(ty)
            if any(equal(current, prior) for prior in active):
                fail(owner, f"type {owner}: infinite by-value layout")
            walk(owner, expanded, (*active, current), bound, aliases)
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
                walk(owner, child, active, bound, aliases)

    def represent(owner: str, ty: C.Type, active: tuple[C.Type, ...], bound: set[str]) -> None:
        """The C representation key (`cgen.erase`) keeps names and expands every foreach, behind
        pointers and functions too: a foreach whose expansion reaches it again before a name has none
        (`struct T { f: foreach *F of *T => *F }`)."""
        if isinstance(ty, C.Foreach):
            shape(owner, ty.source, (), bound)
            expanded = expand(ty, bound)
            if expanded is None:
                represent(owner, ty.body, active, bound | set(foreach_binders(ty)))
                return
            current = key(ty)
            if any(equal(current, prior) for prior in active):
                fail(owner, f"type {owner}: recursive foreach needs a named type between")
            represent(owner, expanded, (*active, current), bound)
        else:
            for child in children(ty):
                represent(owner, child, active, bound)

    for decl in program.types.values():
        bound = {p.name for p in decl.params}
        root = key(C.Named(decl.name, tuple(C.Skolem(p.name) if p.kind == C.BRAND else C.Named(p.name)
                                           for p in decl.params)))
        walk(decl.name, decl.body, (root,), bound, False)
        walk(decl.name, decl.body, (root,), bound, True)
        represent(decl.name, decl.body, (), bound)
