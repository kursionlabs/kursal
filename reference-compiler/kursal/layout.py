"""Finite type expansion, productive C aliases, and finite by-value layouts.

First, recursive nominal applications pass parameters unchanged or choose fixed
types. Then symbolic unfolding checks aliases and stored values. Pointer and
function representations break storage cycles; aggregate names break C alias
cycles. A foreach whose source has known components unfolds to its expansion,
like a name, and its source is walked; one whose source a type parameter decides
is opaque, and the walk covers its source and, for the pointer pattern, the
origin of its rows, the pointee of the pointer source. Last, a C representation
key, which expands every foreach, must reach a name before the same foreach
again. No concrete specialization budget is needed.
"""

from collections.abc import Callable, Mapping, Set
from typing import NoReturn, Optional

from . import core as C
from .sexpr import KursError, Position, located
from .termination import Key, components
from .types import children, equal, free_brands, free_names, subst

type Rows = tuple[Optional[C.Type], list[tuple[str, C.Type]]]

# A `*F` pattern binds its binder to the pointee of each component. Where a type parameter decides the
# source's shape only an explicit pointer source has pointer components at every instantiation.
STUCK_POINTER_PATTERN = "needs a source written as a pointer *P, as a type parameter decides its shape"


class Walk:
    """The by-value walk of a type, with the names of `bound` (type parameters and binders) as leaves.
    `fail` reports a type that fails it, naming the declaration it is in, or None for a type written
    elsewhere."""

    def __init__(self, program: C.Program, fail: Callable[[Optional[str], str], NoReturn]) -> None:
        self.program = program
        self.fail = fail

    def unfold(self, ty: C.Named, bound: Set[str]) -> Optional[C.Type]:
        if ty.name in bound or ty.name not in self.program.types:
            return None  # formation reports unknown names and arities separately
        decl = self.program.types[ty.name]
        return subst(decl.body, {p.name: a for p, a in zip(decl.params, ty.args)})

    @staticmethod
    def key(ty: C.Type) -> C.Type:
        # Brand identity cannot affect size or C representation. Equality also
        # ignores alpha-renaming of binders introduced by subst.
        return subst(ty, {b: C.Skolem("_") for b in free_brands(ty)})

    def shape(self, owner: Optional[str], ty: C.Type, active: tuple[C.Type, ...], bound: Set[str],
              pointer: bool = True) -> None:
        """Demand just the outer shape needed by foreach rows (one pointer layer, which only the pointer
        pattern looks through)."""
        if isinstance(ty, C.Named):
            found = self.unfold(ty, bound)
            if found is not None:
                current = self.key(ty)
                if any(equal(current, prior) for prior in active):
                    self.fail(owner, f"recursive foreach source shape through {ty.name}")
                self.shape(owner, found, (*active, current), bound, pointer)
        elif isinstance(ty, C.Foreach):
            self.shape(owner, ty.source, active, bound, ty.indirect)
        elif isinstance(ty, C.Ptr) and pointer:
            self.shape(owner, ty.inner, active, bound, False)

    # The components of a foreach source, as `Types.rows` and `Types.expand` find them, following only
    # what `shape` has demanded of it.
    def outer(self, ty: C.Type, bound: Set[str]) -> Optional[C.Type]:
        """The outer shape of `ty`: a structural type, or the parameter of `bound` that decides it; None
        for an unknown name or a pattern that does not fit its components (formation reports those)."""
        if isinstance(ty, C.Named) and ty.name in bound:
            return ty
        if isinstance(ty, C.Named):
            found = self.unfold(ty, bound)
            return None if found is None else self.outer(found, bound)
        if isinstance(ty, C.Foreach):
            rows = self.rows(ty.source, ty.indirect, bound)
            return rows if not isinstance(rows, tuple) else self.expand(ty, rows)
        return ty

    def rows(self, source: C.Type, indirect: bool, bound: Set[str]) -> Rows | C.Named | None:
        """The skeleton and rows of `source` under the pattern `indirect` (`*F`) or not, or the parameter
        that decides them (the foreach is stuck). Only `*F` looks through a pointer source; under the
        value pattern it is a leaf."""
        s = self.outer(source, bound)
        wrap: Callable[[C.Type], C.Type] = lambda c: c
        if isinstance(s, C.Ptr) and indirect:
            s, wrap = self.outer(s.inner, bound), C.Ptr
        if s is None or isinstance(s, C.Named):
            return s
        match s:
            case C.Struct(fields=fields) | C.Enum(variants=fields):
                return s, [(n, wrap(f)) for n, f in fields]
            case C.Tuple(elems=elems):
                return s, [(str(i), wrap(e)) for i, e in enumerate(elems)]
            case C.Array(elem=elem):
                return s, [("0", wrap(elem))]
        return None, [("0", source)]

    @staticmethod
    def expand(ty: C.Foreach, rows: Rows) -> Optional[C.Type]:
        skeleton, items = rows
        mapped = []
        for label, item in items:
            if ty.indirect:
                if not isinstance(item, C.Ptr):
                    return None
                item = item.inner
            mapped.append((label, subst(ty.body, {ty.binder: item})))
        match skeleton:
            case C.Struct():
                return C.Struct(tuple(mapped))
            case C.Enum():
                return C.Enum(tuple(mapped))
            case C.Array(length=length):
                return C.Array(mapped[0][1], length)
        return C.Tuple(tuple(m for _, m in mapped))

    def origin(self, source: C.Type, bound: Set[str]) -> Optional[C.Type]:
        """The pointee of a pointer source, whose components the rows of the pointer pattern point to."""
        while isinstance(source, C.Named):
            found = self.unfold(source, bound)
            if found is None:
                return None
            source = found
        return source.inner if isinstance(source, C.Ptr) else None

    def walk(self, owner: Optional[str], ty: C.Type, active: tuple[C.Type, ...], bound: Set[str],
             aliases: bool) -> None:
        if isinstance(ty, C.Named):
            found = self.unfold(ty, bound)
            if found is not None:
                current = self.key(ty)
                if any(equal(current, prior) for prior in active):
                    reason = "recursive alias needs an aggregate boundary" if aliases else "infinite by-value layout"
                    self.fail(owner, f"type {ty.name}: {reason}")
                self.walk(owner, found, (*active, current), bound, aliases)
        elif isinstance(ty, C.Foreach):
            self.shape(owner, ty.source, (), bound, ty.indirect)
            if aliases:
                return  # every foreach result has an aggregate representation
            self.walk(owner, ty.source, active, bound, aliases)
            rows = self.rows(ty.source, ty.indirect, bound)
            if isinstance(rows, C.Named) and ty.indirect:
                # Stuck: what the binder is bound to at an instantiation is a component of the pointee, the
                # origin of the rows, which only an explicit pointer source gives at every instantiation.
                if not isinstance(ty.source, C.Ptr):
                    where = f" in type {owner}" if owner else ""
                    self.fail(owner, f"foreach *{ty.binder} {STUCK_POINTER_PATTERN}{where}")
                origin = self.origin(ty.source, bound)
                if origin is not None:
                    self.walk(owner, origin, active, bound, aliases)
            expanded = self.expand(ty, rows) if isinstance(rows, tuple) else None
            if expanded is None:
                # Opaque here; its rows are those of the source, and of the origin walked above, at each
                # instantiation.
                self.walk(owner, ty.body, active, {*bound, ty.binder}, aliases)
                return
            # A component may hold this foreach again by value, unfolding no name on the way: it is
            # active like a name.
            current = self.key(ty)
            if any(equal(current, prior) for prior in active):
                self.fail(owner, f"type {owner}: infinite by-value layout" if owner else "infinite by-value layout")
            self.walk(owner, expanded, (*active, current), bound, aliases)
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
                self.walk(owner, child, active, bound, aliases)

    def represent(self, owner: str, ty: C.Type, active: tuple[C.Type, ...], bound: Set[str]) -> None:
        """The C representation key (`cgen.erase`) keeps names and expands every foreach, behind
        pointers and functions too: a foreach whose expansion reaches it again before a name has none
        (`struct T { f: fn(foreach *F of *T => *F) -> u32 }`)."""
        if isinstance(ty, C.Foreach):
            self.shape(owner, ty.source, (), bound, ty.indirect)
            rows = self.rows(ty.source, ty.indirect, bound)
            expanded = self.expand(ty, rows) if isinstance(rows, tuple) else None
            if expanded is None:
                self.represent(owner, ty.body, active, {*bound, ty.binder})
                return
            current = self.key(ty)
            if any(equal(current, prior) for prior in active):
                self.fail(owner, f"type {owner}: recursive foreach needs a named type between")
            self.represent(owner, expanded, (*active, current), bound)
        else:
            for child in children(ty):
                self.represent(owner, child, active, bound)


def check_written_type(program: C.Program, ty: C.Type, bound: Set[str]) -> None:
    """The by-value walk of a type written outside a declaration, with the names of `bound` leaves: a use
    is walked as a declaration is, where it is written."""
    def fail(owner: Optional[str], message: str) -> NoReturn:
        raise KursError(message)

    Walk(program, fail).walk(None, ty, (), bound, False)


def check_type_expansion(program: C.Program, places: Optional[Mapping[str, tuple[str, Optional[Position]]]] = None) -> None:
    """`places` names the file and position of a declaration, by its name, when it has them."""
    def fail(owner: Optional[str], message: str) -> NoReturn:
        error = KursError(message)
        raise located(error, *places[owner]) if places and owner is not None and owner in places else error

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
                tainted = {binder} if free_names(source) & (params | derived) else set()
                collect(owner, body, bound | {binder}, params - {binder}, (derived - {binder}) | tainted)
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

    # Every declaration is walked, one whose body is a foreach or holds one like any other.
    walker = Walk(program, fail)
    for decl in program.types.values():
        bound = {p.name for p in decl.params}
        root = walker.key(C.Named(decl.name, tuple(C.Skolem(p.name) if p.kind == C.BRAND else C.Named(p.name)
                                                   for p in decl.params)))
        walker.walk(decl.name, decl.body, (root,), bound, False)
        walker.walk(decl.name, decl.body, (root,), bound, True)
        walker.represent(decl.name, decl.body, (), bound)
