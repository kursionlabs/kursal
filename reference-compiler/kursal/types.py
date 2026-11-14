"""Type operations shared by the checker and the backend: substitution,
equality, multiplicity, exposure and logical shape."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Set
from itertools import count
from typing import Optional

from .core import (COPY, Array, Brand, Enum, Exists, Fn, Foreach, Ignored, Named, Never, NonNull, Param, Prim, Ptr,
                   Kind, ModuleId, Premise, Program, Skolem, Struct, Tuple, Type, TypeDecl)
from .sexpr import KursError

type Substitution = Mapping[str, Type]
type Kinds = Mapping[str, Kind]
type AlphaEnv = tuple[dict[str, object], dict[str, object]]
type ForeachRow = tuple[str, Type, dict[str, Type]]
type Skeleton = Struct | Enum | Tuple | Array

_fresh = count()


def fresh(label: str) -> str:
    return f"{label.split('#')[0]}#{next(_fresh)}"


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
            return Foreach(binder, indirect, transform(source), transform(body), ty.retained)
        case Fn(params=params, ret=ret, forall=forall):
            return Fn(tuple(transform(p) for p in params), transform(ret), forall, ty.unsafe, ty.places)
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
        case Foreach():
            return subst_foreach(ty, sub)[0]
        case Fn(params=params, ret=ret, forall=forall):
            names, inner = _rebind(tuple(p.name for p in forall), sub)
            return Fn(tuple(subst(p, inner) for p in params), subst(ret, inner),
                      tuple(Param(n, p.kind) for n, p in zip(names, forall)), ty.unsafe, ty.places)
    return map_children(ty, lambda child: subst(child, sub))


def foreach_binders(ty: Foreach) -> tuple[str, ...]:
    """The binder prefix fixed when the transform was formed."""
    return (ty.binder, *(ty.retained or ()))


def foreach_item(ty: Foreach) -> Type:
    """Abstract mapper input, using only the retained binders of this transform."""
    if ty.retained and isinstance(ty.source, Foreach):
        return ty.source.body
    return Ptr(Named(ty.binder)) if ty.indirect else Named(ty.binder)


def subst_foreach(ty: Foreach, sub: Substitution) -> tuple[Foreach, dict[str, Type]]:
    """Transport retained binder renamings into each enclosing transform body."""
    source: Type
    if isinstance(ty.source, Foreach):
        source, retained = subst_foreach(ty.source, sub)
    else:
        source, retained = subst(ty.source, sub), dict(sub)
    (binder,), inner = _rebind((ty.binder,), retained, brand=False)
    names = tuple(inner.get(b, Named(b)).name for b in (ty.retained or ()))
    return Foreach(binder, ty.indirect, source, subst(ty.body, inner), names), inner


def _rebind(
    binders: tuple[str, ...], sub: Substitution, brand: bool = True
) -> tuple[tuple[str, ...], dict[str, Type]]:
    """Drop `binders` from `sub`, renaming any a replacement would capture."""
    inner = {k: v for k, v in sub.items() if k not in binders}
    captured = set().union(*(free_names(v) for v in inner.values())) if inner else set()
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
            return free_names(source, bound) | free_names(body, bound | set(foreach_binders(ty)))
        case Fn(params=params, ret=ret, forall=forall):
            inner = bound | {p.name for p in forall}
            return set().union(free_names(ret, inner), *(free_names(p, inner) for p in params))
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


def extractable_names(ty: Type) -> set[str]:
    """Free names reachable by matching without crossing a type binder."""
    match ty:
        case Exists():
            return set()
        case Foreach(source=source):
            return extractable_names(source)
        case Fn(forall=forall) if forall:
            return set()
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
                    and len(foreach_binders(a)) == len(foreach_binders(b))
                    and equal(a.body, b.body, _bind(env, foreach_binders(a), foreach_binders(b))))
        case Fn(), Fn():
            if (a.unsafe != b.unsafe or a.places != b.places or len(a.params) != len(b.params)
                    or len(a.forall) != len(b.forall)
                    or [p.kind for p in a.forall] != [p.kind for p in b.forall]):
                return False
            inner = _bind(env, [p.name for p in a.forall], [p.name for p in b.forall])
            return equal(a.ret, b.ret, inner) and all(equal(x, y, inner) for x, y in zip(a.params, b.params))
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

    def decl(self, name: str) -> TypeDecl:
        decl = self.program.types.get(name)
        if decl is None:
            raise KursError(f"unknown type {name}")
        return decl

    def zero_size(self, ty: Type) -> bool:
        """`ty` has no bytes of its own: unit-like products, empty arrays, brand witnesses, Never. A type
        parameter is answered when it is instantiated."""
        match ty:
            case Brand() | Never():
                return True
            case Named(args=()) if ty.name not in self.program.types:
                return False
        match self.shape(ty):
            case Tuple(elems=elems):
                return all(self.zero_size(t) for t in elems)
            case Struct(fields=fields):
                return all(self.zero_size(t) for _, t in fields)
            case Array(elem=elem, length=length):
                return length == 0 or self.zero_size(elem)
            case Exists(body=body):
                return self.zero_size(body)
        return False

    def niche(self, ty: Type) -> Optional[tuple[str, ...]]:
        """Where a value of `ty` has bytes that hold no value of it: the path (field names, tuple indices,
        `0` for an array's first element) to its first `NonNull` in layout order, whose all-zero value is
        none of its values. A struct, tuple or non-empty array has the niche of its first component that
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
            case Array(elem=elem, length=length) if length > 0:
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
            raise KursError(f"{ty.name} takes {len(decl.params)} type arguments")
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

    def visible_shape(self, ty: Type, module: Optional[ModuleId], kinds: Kinds) -> Optional[Type]:
        """Reveal only the outer shape, without treating abstract types as leaves."""
        while True:
            if isinstance(ty, Named):
                if ty.name in kinds:
                    return None
                decl = self.decl(ty.name)
                if decl.exposure is not None and (module is None or not self.exposed(decl, module)):
                    return None
                ty = self.unfold(ty, module)
            elif isinstance(ty, Foreach):
                if not self.concrete_exposure(ty.source, module, kinds):
                    return None
                ty = self.expand(ty, module)
            else:
                return ty

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
            case Prim() | Ptr() | NonNull() | Brand() | Fn() | Never() | Ignored():
                return True
            case Named(name=name, args=()) if name in kinds:
                return kinds[name] == COPY
            case Named(name=name):
                if self.decl(name).linear:
                    return False
                key = (ty, tuple(sorted(kinds.items())))   # whether `T` is Copy changes the answer for `Node[T]`
                if key not in self._copy:
                    # An active nominal Copy query cannot justify itself.
                    # A valid recursive type reaches a pointer, function, or
                    # zero-length array before demanding the same query.
                    self._copy[key] = False
                    try:
                        self._copy[key] = self.is_copy(self.unfold(ty), kinds)
                    except Exception:
                        del self._copy[key]
                        raise
                return self._copy[key]
            case Array(elem=elem, length=length):
                return length == 0 or self.is_copy(elem, kinds)
            case Exists(body=body):
                return self.is_copy(body, kinds)
            case Foreach(binder=binder, body=body):
                return not (set(foreach_binders(ty)) & free_names(body)) and self.is_copy(body, kinds)
        return all(self.is_copy(c, kinds) for c in children(ty))

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

    def rows(self, source: Type, module: Optional[ModuleId] = None, *,
             schema: bool = False) -> tuple[Optional[Skeleton], list[ForeachRow]]:
        """One-level traversal of a foreach source: its outer skeleton (a
        structural type, or None for a leaf) and one (label, item type,
        retained bindings) row per component, in order. With `schema`, an array
        contributes one element type even when its runtime length is zero."""
        if isinstance(source, Foreach):
            skeleton, inner = self.rows(source.source, module, schema=schema)
            mapped = []
            for label, item, extra in inner:
                bound = self.bind(source, item, extra)
                mapped.append((label, subst(source.body, bound), bound))
            return skeleton, mapped
        s = self.shape(source, module)
        wrap: Callable[[Type], Type] = lambda c: c
        if isinstance(s, Ptr):
            s, wrap = self.shape(s.inner, module), Ptr
            if isinstance(s, Enum):
                raise KursError("a pointer source cannot be a sum")
        match s:
            case Struct(fields=fields):
                return s, [(n, wrap(f), {}) for n, f in fields]
            case Tuple(elems=elems):
                return s, [(str(i), wrap(e), {}) for i, e in enumerate(elems)]
            case Array(elem=elem, length=length):
                return s, [(str(i), wrap(elem), {}) for i in range(1 if schema else length)]
            case Enum(variants=variants):
                return s, [(n, f, {}) for n, f in variants]
        return None, [("0", source, {})]

    def bind(self, ty: Foreach, item: Type, extra: Substitution) -> dict[str, Type]:
        """The substitution under which `ty`'s body maps one row."""
        retained = {name: extra[name] for name in (ty.retained or ()) if name in extra}
        if ty.indirect:
            if not isinstance(item, Ptr):
                raise KursError("a pointer pattern needs pointer components")
            return {**retained, ty.binder: item.inner}
        return {**retained, ty.binder: item}

    def pattern_available(self, ty: Foreach, module: Optional[ModuleId], kinds: Kinds) -> bool:
        """Prove *F matches without assuming the shape of abstract components.

        Source exposure is a separate obligation. An explicit pointer gives
        pointer components even when its authorized pointee shape is abstract.
        """
        if not ty.indirect:
            return True
        if isinstance(ty.source, Foreach):
            return isinstance(ty.source.body, Ptr)
        if isinstance(ty.source, Ptr):
            return True
        if self.visible_shape(ty.source, module, kinds) is None:
            return False
        _, rows = self.rows(ty.source, module, schema=True)
        return all(isinstance(item, Ptr) for _, item, _ in rows)

    def expand(self, ty: Foreach, module: Optional[ModuleId] = None) -> Type:
        """The structural type a foreach transform denotes."""
        skeleton, rows = self.rows(ty.source, module, schema=True)
        mapped = [(label, subst(ty.body, self.bind(ty, item, extra))) for label, item, extra in rows]
        match skeleton:
            case Struct():
                return Struct(tuple(mapped))
            case Enum():
                return Enum(tuple(mapped))
            case Array(length=length):
                return Array(mapped[0][1], length)
        return Tuple(tuple(m for _, m in mapped))


def show(ty: Type) -> str:
    match ty:
        case Prim(name=name):
            return name
        case Never():
            return "!"
        case Named(name=name, args=()):
            return name
        case Named(name=name, args=args):
            return f"({name} {' '.join(map(show, args))})"
        case Skolem(label=label):
            return "$" + label
        case Brand(brand=brand):
            return f"(brand {show(brand)})"
        case Ptr(inner=inner):
            return f"(ptr {show(inner)})"
        case NonNull(inner=inner):
            return f"(non_null {show(inner)})"
        case Tuple(elems=elems):
            return f"(tuple {' '.join(map(show, elems))})".replace(" )", ")")
        case Array(elem=elem, length=length):
            return f"(array {show(elem)} {length})"
        case Struct(fields=fields):
            return "(struct " + " ".join(f"({n} {show(f)})" for n, f in fields) + ")"
        case Enum(variants=variants):
            return "(enum " + " ".join(f"({n} {show(f)})" for n, f in variants) + ")"
        case Exists(binders=binders, body=body):
            return f"(exists ({' '.join('$' + b for b in binders)}) {show(body)})"
        case Foreach(binder=binder, indirect=indirect, source=source, body=body):
            pattern = f"(ptr {binder})" if indirect else binder
            inferred = (source.binder, *(source.retained or ())) if isinstance(source, Foreach) else ()
            suffix = "" if ty.retained == inferred else " (retain " + " ".join(ty.retained or ()) + ")"
            return f"(foreach ({pattern} {show(source)}) {show(body)}{suffix})"
        case Fn(params=params, ret=ret, unsafe=unsafe):
            return f"(fn {'(unsafe) ' if unsafe else ''}({' '.join(map(show, params))}) {show(ret)})"
    return "_"
