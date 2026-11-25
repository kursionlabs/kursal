"""Type operations shared by the checker and the backend: substitution,
equality, multiplicity, exposure and logical shape."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Set
from itertools import count
from typing import Optional

from .core import (COPY, WIDTHS, Array, Brand, Enum, Exists, Fn, Foreach, Named, Never, NonNull, Prim,
                   Ptr, Kind, ModuleId, Premise, Program, Skolem, Struct, Tuple, Type, TypeDecl)
from .sexpr import KursError, Position, counted

type Substitution = Mapping[str, Type]
type Kinds = Mapping[str, Kind]
type AlphaEnv = tuple[dict[str, object], dict[str, object]]
type ForeachRow = tuple[str, Type]
type Skeleton = Struct | Enum | Tuple | Array

# The target's pointer width in bytes, which the layout assumes: of a pointer, a function value, `usize` and
# `isize`. Every formed type's size is below 2^64, the address space of that width.
WORD = 8
SIZE_LIMIT = 1 << (8 * WORD)
PRIM_SIZES = {**{name: bits // 8 for name, bits in WIDTHS.items()}, "usize": WORD, "isize": WORD,
              "f32": 4, "f64": 8, "bool": 1}

# A traversal through a pointer reaches a product, an array or a leaf: a pointer to a sum is never
# `Exposed`, as the components there are those of the variant held. Nor is a foreach type: a map takes
# the components of a value of one, over a source that is none.
POINTER_TO_SUM = "a foreach source cannot be a pointer to a sum"
FOREACH_OF_FOREACH = "a foreach source cannot be a foreach type"

_fresh = count()


def fresh(label: str) -> str:
    return f"{label.split('#')[0]}#{next(_fresh)}"


def _round_up(offset: int, align: int) -> int:
    return (offset + align - 1) // align * align


def _fitted(size: int, align: int) -> Optional[tuple[int, int]]:
    return (size, align) if size < SIZE_LIMIT else None


def _union(parts: list[tuple[int, int]]) -> tuple[int, int]:
    """A C union of parts that are not zero-size: as big as the biggest, rounded up to the strictest
    alignment; (0, 1), erased, for none."""
    if not parts:
        return 0, 1
    align = max(a for _, a in parts)
    return _round_up(max(s for s, _ in parts), align), align


def map_children(ty: Type, transform: Callable[[Type], Type]) -> Type:
    """Rebuild one type layer, applying `transform` to its type children (including premises).

    Binders, labels, lengths and calling conventions are preserved. The caller
    controls recursion and scope; this helper does not substitute bound names.
    """
    match ty:
        case Named(name=name, args=args) if args:
            return Named(name, tuple(transform(a) for a in args))
        case Brand(brand=brand):
            return Brand(transform(brand))
        case Ptr(inner=inner):
            return Ptr(transform(inner))
        case NonNull(inner=inner):
            return NonNull(transform(inner))
        case Array(elem=elem, length=length):
            return Array(transform(elem), length)
        case Tuple(elems=elems):
            return Tuple(tuple(transform(e) for e in elems))
        case Struct(fields=fields):
            return Struct(tuple((n, transform(t)) for n, t in fields))
        case Enum(variants=variants):
            return Enum(tuple((n, transform(t)) for n, t in variants))
        case Exists(binders=binders, body=body):
            return Exists(binders, transform(body))
        case Foreach(binder=binder, indirect=indirect, source=source, body=body):
            return Foreach(binder, indirect, transform(source), transform(body))
        case Fn(params=params, ret=ret):
            return Fn(tuple(transform(p) for p in params), transform(ret), ty.unsafe, ty.places)
    return ty


def subst(ty: Type, sub: Substitution) -> Type:
    """Simultaneous, capture-avoiding substitution.

    `sub` maps type-variable names (written `Named(name)`) and brand labels
    (written `Skolem(label)`) to types; a brand maps to a `Skolem`."""
    if not sub:
        return ty
    match ty:
        case Named(name=name, args=()) if name in sub:
            return sub[name]
        case Skolem(label=label):
            return sub.get(label, ty)
        case Exists(binders=binders, body=body):
            binders, inner = _rebind(binders, sub)
            return Exists(binders, subst(body, inner))
        case Foreach(binder=binder, indirect=indirect, source=source, body=body):
            source = subst(source, sub)
            (binder,), inner = _rebind((binder,), sub, brand=False)
            return Foreach(binder, indirect, source, subst(body, inner))
    return map_children(ty, lambda child: subst(child, sub))


def apart(ty: Type, scope: Set[str]) -> Type:
    """`ty` with each package binder that would shadow a brand of `scope` or of an enclosing package,
    which a written type's binder may not, renamed `b#n`, a spelling no written brand has: a type the
    compiler writes may come from a declaration whose binders are spelled as a brand where it is
    written."""
    match ty:
        case Exists(binders=binders, body=body):
            taken = set(scope) | set(binders)
            spelled = []
            for b in binders:
                n = 0
                while b in scope and f"{b.split('#')[0]}#{n}" in taken:
                    n += 1
                spelled.append(f"{b.split('#')[0]}#{n}" if b in scope else b)
                taken.add(spelled[-1])
            renamed: Substitution = {b: Skolem(new) for b, new in zip(binders, spelled) if b != new}
            return Exists(tuple(spelled), apart(subst(body, renamed), taken))
    return map_children(ty, lambda child: apart(child, scope))


def foreach_item(ty: Foreach) -> Type:
    """The mapper's input where the operand is the source: the binder, or a pointer to it."""
    return Ptr(Named(ty.binder)) if ty.indirect else Named(ty.binder)


def mapped_operand(operand: Type) -> Optional[Foreach]:
    """The operand of a traversal, when it is mapped (the map form): a value of a foreach type, whose
    components are the mapper's inputs. None where the operand is the source, which is never a foreach."""
    return operand if isinstance(operand, Foreach) else None


def mapped_component(operand: Foreach, binding: Type) -> Type:
    """The type of a mapped operand's component whose row binds the binder to `binding`."""
    return subst(operand.body, {operand.binder: binding})


def _rebind(
    binders: tuple[str, ...], sub: Substitution, brand: bool = True
) -> tuple[tuple[str, ...], dict[str, Type]]:
    """Drop `binders` from `sub`, renaming any a replacement would capture."""
    inner = {k: v for k, v in sub.items() if k not in binders}
    captured = set().union(*(free_names(v) for v in inner.values()))
    out = []
    for b in binders:
        if b in captured:
            new = fresh(b)
            inner[b] = Skolem(new) if brand else Named(new)
            out.append(new)
        else:
            out.append(b)
    return tuple(out), inner


def free_names(ty: Type, bound: Set[str] = frozenset()) -> set[str]:
    """Free type-variable names and brand labels of `ty` (over-approximating
    nominal names as variables, which only makes renaming more eager)."""
    match ty:
        case Named(name=name, args=args):
            own = {name} if not args and name not in bound else set()
            return own.union(*(free_names(a, bound) for a in args))
        case Skolem(label=label):
            return set() if label in bound else {label}
        case Exists(binders=binders, body=body):
            return free_names(body, bound | set(binders))
        case Foreach(binder=binder, source=source, body=body):
            return free_names(source, bound) | free_names(body, bound | {binder})
    return set().union(*(free_names(c, bound) for c in children(ty)))


def free_brands(ty: Type) -> set[str]:
    def skolems(part: Type) -> set[str]:
        found = {part.label} if isinstance(part, Skolem) else set()
        return found.union(*(skolems(child) for child in children(part)))

    return free_names(ty) & skolems(ty)


def children(ty: Type) -> tuple[Type, ...]:
    match ty:
        case Ptr(inner=inner) | NonNull(inner=inner):
            return (inner,)
        case Brand(brand=brand):
            return (brand,)
        case Array(elem=elem):
            return (elem,)
        case Tuple(elems=elems):
            return elems
        case Struct(fields=fields):
            return tuple(f for _, f in fields)
        case Enum(variants=variants):
            return tuple(f for _, f in variants)
        case Named(args=args):
            return args
        case Exists(body=body):
            return (body,)
        case Foreach(source=source, body=body):
            return (source, body)
        case Fn(params=params, ret=ret):
            return (*params, ret)
    return ()


def formation_children(ty: Type) -> tuple[Type, ...]:
    """Children admissible for descent; a path cannot cross a binder."""
    if isinstance(ty, Exists):
        return ()
    if isinstance(ty, Foreach):
        return (ty.source,)
    return children(ty)


def strict_subterm(child: Type, parent: Type) -> bool:
    return any(equal(child, part) or strict_subterm(child, part)
               for part in formation_children(parent))


def extractable_names(ty: Type) -> set[str]:
    """Free names reachable by matching without crossing a type binder."""
    match ty:
        case Exists():
            return set()
        case Foreach(source=source):
            return extractable_names(source)
        case Fn(params=params, ret=ret):
            return set().union(*(extractable_names(part) for part in (*params, ret)))
        case Named(name=name, args=()):
            return {name}
        case Skolem(label=label):
            return {label}
    return set().union(*(extractable_names(part) for part in children(ty)))


def walk(ty: Type) -> Iterator[Type]:
    yield ty
    for c in children(ty):
        yield from walk(c)


def equal(a: Type, b: Type, env: Optional[AlphaEnv] = None) -> bool:
    """Nominal/structural equality modulo renaming of bound names."""
    env = env or ({}, {})
    left, right = env
    if type(a) is not type(b):
        return False
    match a, b:
        case Skolem(label=x), Skolem(label=y):
            return left.get(x, x) == right.get(y, y)
        case Named(name=x, args=()), Named(name=y, args=()) if x in left or y in right:
            return left.get(x, x) == right.get(y, y)
        case Named(name=x, args=xs), Named(name=y, args=ys):
            return x == y and len(xs) == len(ys) and all(equal(p, q, env) for p, q in zip(xs, ys))
        case Exists(binders=xs, body=p), Exists(binders=ys, body=q):
            return len(xs) == len(ys) and equal(p, q, _bind(env, xs, ys))
        case Foreach(), Foreach():
            return (a.indirect == b.indirect and equal(a.source, b.source, env)
                    and equal(a.body, b.body, _bind(env, (a.binder,), (b.binder,))))
        case Fn(), Fn():
            if a.unsafe != b.unsafe or a.places != b.places or len(a.params) != len(b.params):
                return False
            return equal(a.ret, b.ret, env) and all(equal(x, y, env) for x, y in zip(a.params, b.params))
        case Struct(fields=fa), Struct(fields=fb):
            return len(fa) == len(fb) and all(n == m and equal(x, y, env) for (n, x), (m, y) in zip(fa, fb))
        case Enum(variants=fa), Enum(variants=fb):
            return len(fa) == len(fb) and all(n == m and equal(x, y, env) for (n, x), (m, y) in zip(fa, fb))
        case Array(), Array():
            return a.length == b.length and equal(a.elem, b.elem, env)
        case Tuple(elems=xs), Tuple(elems=ys):
            return len(xs) == len(ys) and all(equal(x, y, env) for x, y in zip(xs, ys))
        case (Ptr() | NonNull() | Brand()), _:
            return equal(children(a)[0], children(b)[0], env)
    return a == b


def _bind(env: AlphaEnv, left: Iterable[str], right: Iterable[str]) -> AlphaEnv:
    # Each side has its own scope: a name on the left may also occur on the right.
    a, b = map(dict, env)
    for x, y in zip(left, right):
        mark = object()
        a[x], b[y] = mark, mark
    return a, b


def fits(a: Type, b: Type) -> bool:
    """A value of type `a` may go where `b` is expected."""
    return isinstance(a, Never) or equal(a, b)


class Types:
    """Declaration-aware queries over one program."""

    def __init__(self, program: Program) -> None:
        self.program = program
        self._copy: dict[tuple[Type, tuple[tuple[str, Kind], ...]], bool] = {}
        self._expanding: list[Foreach] = []  # the foreach types whose Copy query is active
        self._layout: dict[Type, Optional[tuple[int, int]]] = {}

    def decl(self, name: str) -> TypeDecl:
        decl = self.program.types.get(name)
        if decl is None:
            raise KursError(f"unknown type {name}")
        return decl

    def zero_size(self, ty: Type, parameters: bool = False) -> bool:
        """`ty` has no bytes of its own: unit-like products, arrays of them, brand witnesses, Never. A type
        parameter is answered when it is instantiated, or with `parameters` is taken to be zero-size, as
        it may be, and so is a foreach type that mentions one: `ty` then has bytes only through a part
        that has them whatever the parameters are."""
        match ty:
            case Brand() | Never():
                return True
            case Named(args=()) if ty.name not in self.program.types:
                return parameters
            case Foreach() if parameters and not self.closed(ty):
                return True
        match self.shape(ty):
            case Tuple(elems=elems):
                return all(self.zero_size(t, parameters) for t in elems)
            case Struct(fields=fields):
                return all(self.zero_size(t, parameters) for _, t in fields)
            case Array(elem=elem):
                return self.zero_size(elem, parameters)
            case Exists(body=body):
                return self.zero_size(body, parameters)
        return False

    def size_sources(self, ty: Type) -> list[Type]:
        """The foreach sources `sizeof` of `ty` needs `Exposed` of, as a traversal does: the source of the
        foreach `ty` is, declarations unfolded whether exposed or not, whatever that source is; and each
        pointer source that mentions a type parameter of the foreach types in `ty`, declarations that
        mention one unfolded, once, under either pattern."""
        head = ty
        while isinstance(head, Named) and head.name in self.program.types:
            head = self.unfold(head)
        sources: list[Type] = [head.source] if isinstance(head, Foreach) else []
        seen: list[Type] = []

        def visit(t: Type) -> None:
            for part in walk(t):
                if isinstance(part, Foreach) and isinstance(part.source, Ptr) and not self.closed(part.source):
                    sources.append(part.source)
                elif isinstance(part, Named) and part.name in self.program.types and not self.closed(part) \
                        and not any(equal(part, s) for s in seen):
                    seen.append(part)
                    visit(self.unfold(part))
        visit(ty)
        return sources

    def closed(self, ty: Type) -> bool:
        """`ty` mentions no type parameter or binder: every name it has is a declaration's. Brands do not
        change a layout."""
        return all(name in self.program.types for name in free_names(ty) - free_brands(ty))

    def layout(self, ty: Type) -> Optional[tuple[int, int]]:
        """The size and alignment of a closed type's value as the backend lays it out at the target's
        pointer width: a primitive its width, a pointer or a function value a word, a zero-size part
        erased, a product a C struct of the rest, and an enum a `uint32_t` tag and a union of its payloads,
        or its payload in the niche layout. None when the size is 2^64 or more: formation rejects such a
        type."""
        if ty not in self._layout:
            self._layout[ty] = self._lay_out(ty)
        return self._layout[ty]

    def _lay_out(self, ty: Type) -> Optional[tuple[int, int]]:
        match ty:
            case Prim(name=name):
                return PRIM_SIZES[name], PRIM_SIZES[name]
            case Ptr() | NonNull() | Fn():
                return WORD, WORD
            case Named() | Foreach():
                return self.layout(self.shape(ty))
            case Exists(body=body):
                return self.layout(body)
            case Array(elem=elem, length=length):
                part = self.layout(elem)
                return None if part is None else _fitted(part[0] * length, part[1])
            case Tuple(elems=elems):
                return self._product([self.layout(t) for t in elems])
            case Struct(fields=fields):
                return self._product([self.layout(t) for _, t in fields])
            case Enum(variants=variants):
                held = self.niche_variant(variants)
                if held is not None:
                    return self.layout(variants[held][1])
                payloads = [self.layout(t) for _, t in variants]
                if any(p is None for p in payloads):
                    return None
                material = [p for p in payloads if p is not None and p[0] > 0]
                union = _union(material)
                return self._product([(4, 4), union])
        return 0, 1   # a brand witness or Never

    @staticmethod
    def _product(parts: list[Optional[tuple[int, int]]]) -> Optional[tuple[int, int]]:
        """A C struct of the parts that are not zero-size, in order."""
        offset, align = 0, 1
        for part in parts:
            if part is None:
                return None
            size, part_align = part
            if size > 0:
                offset = _round_up(offset, part_align) + size
                align = max(align, part_align)
        return _fitted(_round_up(offset, align), align)

    def niche(self, ty: Type) -> Optional[tuple[str, ...]]:
        """Where a value of `ty` has bytes that hold no value of it: the path (field names, tuple indices,
        `0` for an array's first element) to its first `NonNull` in layout order, whose all-zero value is
        none of its values. A struct, tuple or array has the niche of its first component that
        has one; a pointer, a primitive, a zero-size type and an enum have none. A type parameter is
        answered when it is instantiated."""
        match ty:
            case NonNull():
                return ()
            case Named(args=()) if ty.name not in self.program.types:
                return None
        match self.shape(ty):
            case NonNull():
                return ()
            case Struct(fields=fields):
                parts = list(fields)
            case Tuple(elems=elems):
                parts = [(str(k), e) for k, e in enumerate(elems)]
            case Array(elem=elem):
                parts = [("0", elem)]
            case Exists(body=body):
                return self.niche(body)
            case _:
                return None
        for label, part in parts:
            path = self.niche(part)
            if path is not None:
                return (label, *path)
        return None

    def niche_variant(self, variants: tuple[tuple[str, Type], ...]) -> Optional[int]:
        """The variant an enum of these variants is represented as, when it has the niche layout: one
        variant's payload has a niche, and the one other variant is zero-size, written as the niche's
        invalid value. A `NonNull` offers one invalid value, so one dataless variant. Otherwise the enum
        is a tag and a union of its payloads."""
        if len(variants) != 2:
            return None
        full = [k for k, (_, t) in enumerate(variants) if not self.zero_size(t)]
        if len(full) != 1 or self.niche(variants[full[0]][1]) is None:
            return None
        return full[0]

    def chosen_layout(self, ty: Type) -> bool:
        """The compiler chooses how `ty`'s value is laid out: an enum or a `NonNull` is in its value
        representation (named types unfolded; not through a pointer or a function; a type parameter is a
        leaf)."""
        match ty:
            case Enum() | NonNull():
                return True
            case Ptr() | Fn() | Brand():
                return False
            case Named(args=()) if ty.name not in self.program.types:
                return False
            case Named() | Foreach():
                return self.chosen_layout(self.shape(ty))
        return any(self.chosen_layout(c) for c in children(ty))

    def unfold(self, ty: Named, module: Optional[ModuleId] = None) -> Type:
        """One nominal step: the declaration's body at its arguments.

        With `module` given, the declaration must expose its shape there."""
        decl = self.decl(ty.name)
        if len(ty.args) != len(decl.params):
            raise KursError(f"{ty.name} takes {counted(len(decl.params), 'type argument')}")
        if not self.exposed(decl, module):
            raise KursError(f"the shape of {ty.name} is not exposed here")
        return subst(decl.body, {p.name: a for p, a in zip(decl.params, ty.args)})

    def exposed(self, decl: TypeDecl, module: Optional[ModuleId]) -> bool:
        if decl.exposure is None or module is None:
            return True
        how, scope = decl.exposure
        return module == scope if how == "owner" else module.within(scope)

    def exposure_available(self, source: Type, module: Optional[ModuleId], kinds: Kinds,
                           premises: Iterable[Premise]) -> bool:
        """Exact assumed authority, or a concrete traversable outer shape.

        Authority is not recursive field exposure. A derived foreach source
        cannot borrow an assumption about its underlying source; it needs its
        own exact assumption or a concretely visible shape.
        """
        if any(p.trait == "Exposed" and len(p.args) == 1 and equal(p.args[0], source)
               for p in premises):
            return True
        return self.concrete_exposure(source, module, kinds)

    def sized_available(self, ty: Type, premises: Iterable[Premise]) -> bool:
        """`Sized[ty]`: `ty` has a finite by-value layout of non-zero size, at every instantiation.
        An exact assumption gives it; otherwise its shape decides it, with each type parameter standing for
        the zero-size `()`: a part of it held by value, a primitive, a pointer, a sum or a function value,
        has bytes whatever the parameters are. So `Sized[T]` and `Sized[U]` do not give `Sized[(T, U)]`,
        and the same holds on every target. A raw load, store or `alloca` at `ty` needs it. Every
        declaration's by-value layout is finite (`check_type_expansion`)."""
        if any(p.trait == "Sized" and len(p.args) == 1 and equal(p.args[0], ty) for p in premises):
            return True
        return not self.zero_size(ty, parameters=True)

    def concrete_exposure(self, source: Type, module: Optional[ModuleId], kinds: Kinds) -> bool:
        shape = self.visible_shape(source, module, kinds)
        if shape is None:
            return False
        if isinstance(shape, Ptr):
            # An indirect traversal requires an explicit outer pointer.
            if not isinstance(source, Ptr):
                return False
            inner = self.visible_shape(shape.inner, module, kinds)
            return inner is not None and not isinstance(inner, Enum)
        return True

    def points_to_sum(self, source: Type, kinds: Kinds) -> bool:
        """A foreach source that is a pointer to a sum, named types unfolded whether exposed or not: no
        traversal has one (`POINTER_TO_SUM`). A parameter decides nothing here; an instantiation at a sum
        is not `Exposed`."""
        shape = self.fixed_shape(source, kinds)
        return isinstance(shape, Ptr) and isinstance(self.fixed_shape(shape.inner, kinds), Enum)

    def visible_shape(self, ty: Type, module: Optional[ModuleId], kinds: Kinds) -> Optional[Type]:
        """Reveal only the outer shape, without treating abstract types as leaves. A foreach type has
        none: no traversal has one as its source (`FOREACH_OF_FOREACH`)."""
        while isinstance(ty, Named):
            if ty.name in kinds:
                return None
            decl = self.decl(ty.name)
            if decl.exposure is not None and (module is None or not self.exposed(decl, module)):
                return None
            ty = self.unfold(ty, module)
        return None if isinstance(ty, Foreach) else ty

    def shape(self, ty: Type, module: Optional[ModuleId] = None) -> Type:
        """The outer structural type of `ty` (Named unfolded, foreach expanded)."""
        while True:
            match ty:
                case Named():
                    ty = self.unfold(ty, module)
                case Foreach():
                    ty = self.expand(ty, module)
                case _:
                    return ty

    def is_copy(self, ty: Type, kinds: Optional[Kinds] = None) -> bool:
        kinds = kinds or {}
        match ty:
            case Prim() | Ptr() | NonNull() | Brand() | Fn() | Never():
                return True
            case Named(name=name, args=()) if name in kinds:
                return kinds[name] == COPY
            case Named(name=name):
                if self.decl(name).linear:
                    return False
                key = (ty, tuple(sorted(kinds.items())))   # whether `T` is Copy changes the answer for `Node[T]`
                if key not in self._copy:
                    # An active nominal Copy query cannot justify itself.
                    # A valid recursive type reaches a pointer or function
                    # before demanding the same query.
                    self._copy[key] = False
                    try:
                        self._copy[key] = self.is_copy(self.unfold(ty), kinds)
                    except Exception:
                        del self._copy[key]
                        raise
                return self._copy[key]
            case Array(elem=elem):
                return self.is_copy(elem, kinds)
            case Exists(body=body):
                return self.is_copy(body, kinds)
            case Foreach(body=body):
                # A source whose rows are fixed expands; otherwise every row is the body only when the
                # body does not mention a binder.
                if self.rows_fixed(ty.source, ty.indirect, kinds):
                    # Like a nominal query, an active one cannot justify itself: a foreach whose
                    # expansion holds it again by value has no finite layout, which
                    # `check_type_expansion` rejects.
                    if any(equal(ty, prior) for prior in self._expanding):
                        return False
                    self._expanding.append(ty)
                    try:
                        return self.is_copy(self.expand(ty), kinds)
                    finally:
                        self._expanding.pop()
                return ty.binder not in free_names(body) and self.is_copy(body, kinds)
        return all(self.is_copy(c, kinds) for c in children(ty))

    def rows_fixed(self, source: Type, indirect: bool, kinds: Kinds) -> bool:
        """Whether a foreach source's rows under the pattern `indirect` are the same whatever the type
        parameters in `kinds`, and any name no declaration gives, stand for: its outer shape, and with
        `*F` a pointer's pointee's, is reached without meeting one. A parameter's shape is unknown, and so
        is whether its rows are Copy: a parameter of kind Any may have linear components, and under `*F`
        the rows are components of the pointee, which need not be Copy even when the pointer is. Under the
        value pattern a pointer source is a leaf, whatever its pointee."""
        shape = self.fixed_shape(source, kinds)
        if isinstance(shape, Ptr) and indirect:
            shape = self.fixed_shape(shape.inner, kinds)
        return shape is not None

    def fixed_shape(self, ty: Type, kinds: Kinds) -> Optional[Type]:
        """The outer structural type of `ty`, or None when a parameter or undeclared name decides it."""
        while True:
            match ty:
                case Named(name=name, args=args):
                    if (not args and name in kinds) or name not in self.program.types:
                        return None
                    ty = self.unfold(ty)
                case Foreach(source=source, indirect=indirect):
                    if not self.rows_fixed(source, indirect, kinds):
                        return None
                    ty = self.expand(ty)
                case _:
                    return ty

    def fields(self, ty: Type, module: Optional[ModuleId] = None) -> tuple[Type, ...]:
        """The component types of a product (struct, tuple or array)."""
        match self.shape(ty, module):
            case Struct(fields=fields):
                return tuple(f for _, f in fields)
            case Tuple(elems=elems):
                return elems
            case Array(elem=elem, length=length):
                return (elem,) * length
        raise KursError(f"{show(ty)} is not a product")

    def variants(self, ty: Type, module: Optional[ModuleId] = None) -> tuple[tuple[str, Type], ...]:
        s = self.shape(ty, module)
        if not isinstance(s, Enum):
            raise KursError(f"{show(ty)} is not a sum")
        return s.variants

    def rows(self, source: Type, indirect: bool,
             module: Optional[ModuleId] = None) -> tuple[Optional[Skeleton], list[ForeachRow]]:
        """One-level traversal of a foreach source under the pattern `indirect` (`*F`) or not: its outer
        skeleton (a structural type, or None for a leaf) and one (label, item type) row per component, in
        order. An array has one row, its element's, which every element of the skeleton shares.

        Under the value pattern a pointer source, written or reached through a name, is a leaf: its one
        row is the source itself, and the pointee is not looked at. Under `*F` each component behind it is
        a pointer to it: of a sum, a pointer to each payload. No traversal has a pointer to a sum as its
        source, which is never `Exposed`."""
        s = self.shape(source, module)
        wrap: Callable[[Type], Type] = lambda c: c
        if isinstance(s, Ptr):
            if not indirect:
                return None, [("0", source)]
            s, wrap = self.shape(s.inner, module), Ptr
        match s:
            case Struct(fields=fields):
                return s, [(n, wrap(f)) for n, f in fields]
            case Tuple(elems=elems):
                return s, [(str(i), wrap(e)) for i, e in enumerate(elems)]
            case Array(elem=elem):
                return s, [("0", wrap(elem))]
            case Enum(variants=variants):
                return s, [(n, wrap(f)) for n, f in variants]
        return None, [("0", source)]

    def binding(self, ty: Foreach, item: Type) -> Type:
        """What `ty`'s binder stands for at the row whose item is `item`: the item, or the pointee of a
        pointer item for the `*F` pattern."""
        if not ty.indirect:
            return item
        if not isinstance(item, Ptr):
            raise KursError("a pointer pattern needs pointer components")
        return item.inner

    def pattern_available(self, ty: Foreach) -> bool:
        """Prove *F matches every component from the source as written: an explicit pointer, or a tuple,
        struct, enum or array whose components are written as pointers. A name is not looked through,
        even where its shape is visible.

        Source exposure is a separate obligation.
        """
        if not ty.indirect:
            return True
        match ty.source:
            case Ptr():
                return True
            case Tuple(elems=elems):
                return all(isinstance(t, Ptr) for t in elems)
            case Struct(fields=members) | Enum(variants=members):
                return all(isinstance(t, Ptr) for _, t in members)
            case Array(elem=elem):
                return isinstance(elem, Ptr)
        return False

    def expand(self, ty: Foreach, module: Optional[ModuleId] = None) -> Type:
        """The structural type a foreach transform denotes."""
        skeleton, rows = self.rows(ty.source, ty.indirect, module)
        mapped = [(label, subst(ty.body, {ty.binder: self.binding(ty, item)})) for label, item in rows]
        match skeleton:
            case Struct():
                return Struct(tuple(mapped))
            case Enum():
                return Enum(tuple(mapped))
            case Array(length=length):
                return Array(mapped[0][1], length)
        return Tuple(tuple(m for _, m in mapped))


def show(ty: Type) -> str:
    """`ty` as KURS writes it, a brand as `$label`: for messages about KURS, and for dumps."""
    return _render(ty)


class Surface:
    """Types as source writes them: `Vec![T]`, `Brand['k]`, `(A, B)`, `*T`, `[T; 4]`, `exists 'k. T`,
    `fn(A, &_: B) -> R`. A brand reads as written, `'k`; a name the compiler made reads as the name it was
    made from (`'b` for the brand `b#open9` that `let exists 'b.` opened, `Env` for a closure's environment
    `Env#1`), or `_` when it was made from none.

    One `Surface` writes every type of one message, and the `nominal` names it writes outside them, such as
    a trait's. A nominal name reads as its last path segment unless another name in the message reads the
    same, when it reads as its full path. Where two different brands would read the same, each that a
    `let exists` opened, at a position in `origins`, says where: `'k (opened at 15:9)`, and any other the
    compiler made reads as its stem numbered apart from every other reading, as `'b1` for the brand `b#4`
    that a function value keeps beside the brand `'b`."""

    def __init__(self, types: Iterable[Type], origins: Mapping[str, Position], nominal: Iterable[str] = ()) -> None:
        labels: set[str] = set()
        names = set(nominal)
        for ty in types:
            _brand_labels(ty, labels)
            _type_names(ty, names)
        spellings: dict[str, int] = {}
        for label in labels:
            spellings[made_from(label)] = spellings.get(made_from(label), 0) + 1
        self.origins = origins
        self.qualified = {label for label in labels if spellings[made_from(label)] > 1 and label in origins}
        # Any other brand the compiler made that reads as another does reads as its stem numbered anew.
        taken = set(spellings)
        self.renamed: dict[str, str] = {}
        for label in sorted(label for label in labels
                            if spellings[made_from(label)] > 1 and label not in origins and "#" in label):
            n = 1
            while f"{made_from(label)}{n}" in taken:
                n += 1
            self.renamed[label] = f"{made_from(label)}{n}"
            taken.add(self.renamed[label])
        readings: dict[str, int] = {}
        for name in names:
            readings[_short(name)] = readings.get(_short(name), 0) + 1
        self.paths = {name for name in names if readings[_short(name)] > 1}

    def name(self, name: str) -> str:
        """A nominal type's name or a type variable, as this message writes it."""
        if "::" not in name:
            return _variable(name)
        return name if name in self.paths else _short(name)

    def brand(self, label: str) -> str:
        name = brand_name(label)
        if label in self.qualified:
            line, col = self.origins[label]
            return f"{name} (opened at {line}:{col})"
        if label in self.renamed:
            return "'" + self.renamed[label]
        return name

    def show(self, ty: Type) -> str:
        return _source(ty, self)


def brand_name(label: str) -> str:
    """The brand `label` as written in source, as `Surface` writes it where no other brand reads the same."""
    return "'" + made_from(label)


def made_from(name: str) -> str:
    """The name a compiler-made name `name#...` was made from; a source name is its own."""
    return name.split("#", 1)[0]


def _variable(name: str) -> str:
    """A type variable as a message writes it; a nominal type's path is its own."""
    if "::" in name or "#" not in name:
        return name
    return made_from(name) or "_"


def _short(name: str) -> str:
    """A nominal type's last path segment; a type variable as a message writes it."""
    return name.rsplit("::", 1)[-1] if "::" in name else _variable(name)


def _type_names(ty: Type, out: set[str]) -> None:
    match ty:
        case Named(name=name):
            out.add(name)
        case Foreach(binder=binder):
            out.add(binder)
    for child in children(ty):
        _type_names(child, out)


def _brand_labels(ty: Type, out: set[str]) -> None:
    if isinstance(ty, Skolem):
        out.add(ty.label)
    elif isinstance(ty, Exists):
        out.update(ty.binders)
    for child in children(ty):
        _brand_labels(child, out)


def _source(ty: Type, surface: Surface) -> str:
    def go(part: Type) -> str:
        return _source(part, surface)

    def all_of(parts: Iterable[Type]) -> str:
        return ", ".join(map(go, parts))

    match ty:
        case Prim(name=name):
            return name
        case Never():
            return "!"
        case Named(name=name, args=()):
            return surface.name(name)
        case Named(name=name, args=args):
            return f"{surface.name(name)}[{all_of(args)}]"
        case Skolem(label=label):
            return surface.brand(label)
        case Brand(brand=inner):
            return f"Brand[{go(inner)}]"
        case Ptr(inner=inner):
            return f"*{go(inner)}"
        case NonNull(inner=inner):
            return f"NonNull[{go(inner)}]"
        case Tuple(elems=(elem,)):
            return f"({go(elem)},)"
        case Tuple(elems=elems):
            return f"({all_of(elems)})"
        case Array(elem=elem, length=length):
            return f"[{go(elem)}; {length}]"
        case Struct(fields=(("env", env), ("code", Fn(params=(_, *params), ret=ret) as code))):
            # A closure: its environment, then a function of the environment and the arguments.
            shown = ", ".join(f"&_: {go(p)}" if k + 1 in code.places else go(p) for k, p in enumerate(params))
            environment = go(env)
            return f"closure{'' if environment == '_' else f'[{environment}]'}({shown}) -> {go(ret)}"
        case Struct(fields=fields):
            return "struct { " + ", ".join(f"{n}: {go(f)}" for n, f in fields) + " }"
        case Enum(variants=variants):
            return "enum { " + ", ".join(f"{n}({go(f)})" for n, f in variants) + " }"
        case Exists(binders=binders, body=body):
            return f"exists {', '.join(map(surface.brand, binders))}. {go(body)}"
        case Foreach(binder=binder, indirect=indirect, source=source, body=body):
            return f"foreach {'*' if indirect else ''}{surface.name(binder)} of {go(source)} => {go(body)}"
        case Fn(params=params, ret=ret, unsafe=unsafe):
            shown = ", ".join(f"&_: {go(p)}" if k in ty.places else go(p) for k, p in enumerate(params))
            return f"{'unsafe ' if unsafe else ''}fn({shown}) -> {go(ret)}"
    return "_"


def _render(ty: Type) -> str:
    """`ty` as KURS writes it."""
    go = _render

    def brand(label: str) -> str:
        return "$" + label

    match ty:
        case Prim(name=name):
            return name
        case Never():
            return "!"
        case Named(name=name, args=()):
            return name
        case Named(name=name, args=args):
            return f"({name} {' '.join(map(go, args))})"
        case Skolem(label=label):
            return brand(label)
        case Brand(brand=inner):
            return f"(brand {go(inner)})"
        case Ptr(inner=inner):
            return f"(ptr {go(inner)})"
        case NonNull(inner=inner):
            return f"(non_null {go(inner)})"
        case Tuple(elems=elems):
            return f"(tuple {' '.join(map(go, elems))})".replace(" )", ")")
        case Array(elem=elem, length=length):
            return f"(array {go(elem)} {length})"
        case Struct(fields=fields):
            return "(struct " + " ".join(f"({n} {go(f)})" for n, f in fields) + ")"
        case Enum(variants=variants):
            return "(enum " + " ".join(f"({n} {go(f)})" for n, f in variants) + ")"
        case Exists(binders=binders, body=body):
            return f"(exists ({' '.join(map(brand, binders))}) {go(body)})"
        case Foreach(binder=binder, indirect=indirect, source=source, body=body):
            pattern = f"(ptr {binder})" if indirect else binder
            return f"(foreach ({pattern} {go(source)}) {go(body)})"
        case Fn(params=params, ret=ret, unsafe=unsafe):
            return f"(fn {'(unsafe) ' if unsafe else ''}({' '.join(map(go, params))}) {go(ret)})"
    return "_"


def brand_differences(a: Type, b: Type) -> Optional[list[str]]:
    """When `a` and `b` differ only in brands, as `equal` compares them: the brand labels where they differ,
    in the order met, each pair's left one first. None when they differ in more than brands."""
    found: list[str] = []

    def same_all(xs: Iterable[Type], ys: Iterable[Type], env: AlphaEnv) -> bool:
        return all(same(x, y, env) for x, y in zip(xs, ys))

    def same(x: Type, y: Type, env: AlphaEnv) -> bool:
        left, right = env
        if type(x) is not type(y):
            return False
        match x, y:
            case Skolem(label=p), Skolem(label=q):
                if left.get(p, p) != right.get(q, q):
                    found.extend(label for label in (p, q) if label not in found)
                return True
            case Named(name=p, args=()), Named(name=q, args=()) if p in left or q in right:
                return left.get(p, p) == right.get(q, q)
            case Named(name=p, args=xs), Named(name=q, args=ys):
                return p == q and len(xs) == len(ys) and same_all(xs, ys, env)
            case Exists(binders=xs, body=p), Exists(binders=ys, body=q):
                return len(xs) == len(ys) and same(p, q, _bind(env, xs, ys))
            case Foreach(), Foreach():
                return (x.indirect == y.indirect and same(x.source, y.source, env)
                        and same(x.body, y.body, _bind(env, (x.binder,), (y.binder,))))
            case Fn(), Fn():
                if x.unsafe != y.unsafe or x.places != y.places or len(x.params) != len(y.params):
                    return False
                return same_all(x.params, y.params, env) and same(x.ret, y.ret, env)
            case (Struct(fields=fx), Struct(fields=fy)) | (Enum(variants=fx), Enum(variants=fy)):
                return (len(fx) == len(fy) and all(n == m for (n, _), (m, _) in zip(fx, fy))
                        and same_all((t for _, t in fx), (t for _, t in fy), env))
            case Array(), Array():
                return x.length == y.length and same(x.elem, y.elem, env)
            case Tuple(elems=xs), Tuple(elems=ys):
                return len(xs) == len(ys) and same_all(xs, ys, env)
            case (Ptr() | NonNull() | Brand()), _:
                return same(children(x)[0], children(y)[0], env)
        return x == y

    return found if same(a, b, ({}, {})) else None
