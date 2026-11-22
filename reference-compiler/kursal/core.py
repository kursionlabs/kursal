"""The typed KURS core. Its text serialization is read and written by kurs.py.

Types are frozen and compared structurally; language-level equality
(nominal declarations, alpha-renamed binders) lives in `types`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional
from .sexpr import Form, Position

type Kind = Literal["brand", "copy", "any"]

# The fixed-width integer types, with their widths in bits.
WIDTHS = {"i8": 8, "i16": 16, "i32": 32, "i64": 64, "u8": 8, "u16": 16, "u32": 32, "u64": 64}
# `usize` and `isize` are pointer-sized: their width is the target's, which only the C compiler knows.
POINTER_SIZED = frozenset({"isize", "usize"})
INTEGERS = frozenset(WIDTHS) | POINTER_SIZED
# The range of an integer literal, in bits: its type's width, and 64 bits for `usize` and `isize`.
LITERAL_BITS = {**WIDTHS, "isize": 64, "usize": 64}
FLOATS = {"f32", "f64"}
PRIMS = set(INTEGERS) | FLOATS | {"bool"}

# Kinds: a brand, a Copy value type, or any value type.
BRAND: Kind = "brand"
COPY: Kind = "copy"
ANY: Kind = "any"


class Type:
    pass


@dataclass(frozen=True)
class Prim(Type):
    name: str


@dataclass(frozen=True)
class Never(Type):
    pass


@dataclass(frozen=True)
class Named(Type):
    name: str
    args: tuple[Type, ...] = ()


@dataclass(frozen=True)
class Struct(Type):
    fields: tuple[tuple[str, Type], ...]  # ((name, Type), ...)


@dataclass(frozen=True)
class Enum(Type):
    variants: tuple[tuple[str, Type], ...]  # ((name, Type), ...), in tag order


@dataclass(frozen=True)
class Tuple(Type):
    elems: tuple[Type, ...]


@dataclass(frozen=True)
class Array(Type):
    elem: Type
    length: int


@dataclass(frozen=True)
class Ptr(Type):
    inner: Type


@dataclass(frozen=True)
class NonNull(Type):
    """`non_null T`: a pointer like `*T` that is never null. Its all-zero value is no value of the
    type, the niche an enum's layout may use (`Types.niche`)."""
    inner: Type


@dataclass(frozen=True)
class Skolem(Type):
    """A brand variable or identity; kind Brand, never a value type."""
    label: str


@dataclass(frozen=True)
class Brand(Type):
    """`brand $k`, the Copy witness type of brand `$k`."""
    brand: Type  # Skolem in checked IR; inference may use a brand metavariable.


@dataclass(frozen=True)
class Exists(Type):
    binders: tuple[str, ...]  # brand labels
    body: Type


@dataclass(frozen=True)
class Param:
    name: str
    kind: Kind = ANY


@dataclass(frozen=True)
class Premise:
    trait: str
    args: tuple[Type, ...]


@dataclass(frozen=True)
class Foreach(Type):
    binder: str
    indirect: bool  # the `(ptr F)` pattern
    source: Type
    body: Type


@dataclass(frozen=True)
class Fn(Type):
    params: tuple[Type, ...]
    ret: Type
    forall: tuple[Param, ...] = ()     # retained brand Params; a function value carries no premises
    unsafe: bool = False
    places: frozenset[int] = frozenset()  # indices of place parameters


@dataclass(frozen=True)
class Ignored(Type):
    """`_`: a retained brand slot of `fn`'s type arguments."""


UNIT = Tuple(())
BOOL = Prim("bool")
U32 = Prim("u32")
USIZE = Prim("usize")


@dataclass(frozen=True)
class ModuleId:
    package: int
    path: tuple[str, ...] = ()

    def child(self, name: str) -> ModuleId:
        return ModuleId(self.package, (*self.path, name))

    def within(self, scope: ModuleId) -> bool:
        return self.package == scope.package and self.path[:len(scope.path)] == scope.path


type Exposure = tuple[Literal["owner", "within"], ModuleId]


@dataclass
class TypeDecl:
    name: str              # a linear type's name ends with `!`
    params: tuple[Param, ...]
    premises: tuple[Premise, ...]
    body: Type
    module: Optional[ModuleId] = None
    exposure: Optional[Exposure] = None  # None: transparent; ("owner", m) or ("within", m)

    @property
    def linear(self) -> bool:
        return self.name.endswith("!")


# The atomic operations, each by its kind: one per atomic method of `*T` (`ptr_atomic_` and the kind,
# whatever the method's own name), and the fence, which takes no pointer. Each carries its ordering.
ATOMIC_OPS = {"ptr_atomic_" + kind: kind for kind in ("load", "store", "swap", "compare_exchange", "fetch_add",
                                                       "fetch_sub", "fetch_and", "fetch_or", "fetch_xor")}
ATOMIC_OPS["fence"] = "fence"
# The operations that need an unsafe context: the instruction's `(unsafe ...)` marker, or an unsafe function.
UNSAFE_OPS = frozenset({"cast", "alloca", "static_brand", "ptr_load", "ptr_store", "ptr_load_volatile",
                        "ptr_store_volatile", *ATOMIC_OPS})


@dataclass
class Instr:
    lhs: tuple[str, ...]
    op: str
    args: tuple[str, ...] = ()
    ty: Optional[Type] = None        # the type operand
    tys: tuple[Type, ...] = ()        # type arguments
    lit: Optional[str] = None
    unsafe: bool = False   # the instruction's discharge marker
    binders: tuple[str, ...] = ()    # `open`'s brand binders
    pos: Optional[Position] = None


@dataclass
class Ret:
    value: str
    pos: Optional[Position] = None


@dataclass
class Edge:
    target: str
    args: tuple[str, ...] = ()
    tys: tuple[Type, ...] = ()


@dataclass
class Jmp:
    edge: Edge
    pos: Optional[Position] = None


@dataclass
class Jnz:
    cond: str
    then: Edge
    other: Edge
    pos: Optional[Position] = None


@dataclass
class Arm:
    variant: str
    edge: Edge


@dataclass
class Switch:
    scrutinee: str
    ty: Type
    arms: tuple[Arm, ...]
    pos: Optional[Position] = None


@dataclass
class Unreachable:
    pos: Optional[Position] = None


type Terminator = Ret | Jmp | Jnz | Switch | Unreachable


@dataclass
class Block:
    label: str
    params: tuple[tuple[str, Type], ...]          # ((name, Type), ...)
    instrs: tuple[Instr, ...]
    term: Terminator
    brands: tuple[str, ...] = ()     # brand labels
    empty: tuple[str, ...] = ()      # place paths declared empty
    known: tuple[tuple[str, str], ...] = ()      # ((path, variant), ...)
    opened: tuple[tuple[str, tuple[str, ...]], ...] = ()  # ((package path, view brands), ...)
    pos: Optional[Position] = None


@dataclass
class Fun:
    name: str
    params: tuple[Param, ...]          # forall Params
    premises: tuple[Premise, ...]
    ret: Type
    ptypes: tuple[Type, ...]          # a place parameter's type is the root type; see `places`
    blocks: tuple[Block, ...]
    locals: tuple[tuple[str, Type], ...] = ()     # (("&name", Type), ...)
    unsafe: bool = False
    module: Optional[ModuleId] = None
    pos: Optional[Position] = None

    @property
    def places(self) -> frozenset[int]:
        """Indices of the place parameters, whose entry names start with `&`."""
        return frozenset(i for i, (name, _) in enumerate(self.blocks[0].params) if name.startswith("&"))


@dataclass
class Extern:
    name: str
    ret: Optional[Type] = None
    ptypes: Optional[tuple[Type, ...]] = None   # None for an extern value
    pos: Optional[Position] = None


@dataclass
class Method:
    name: str
    ret: Type
    ptypes: tuple[Type, ...]
    unsafe: bool = False
    places: frozenset[int] = frozenset()


@dataclass
class Trait:
    name: str
    params: tuple[str, ...]          # names
    methods: tuple[Method, ...]
    module: Optional[ModuleId] = None
    param_kinds: tuple[Kind, ...] = ()  # omitted kinds are Any, as in plain KURS headers


@dataclass
class Instance:
    trait: str
    params: tuple[Param, ...]
    premises: tuple[Premise, ...]
    head: tuple[Type, ...]            # types, one per trait parameter
    methods: tuple[Fun, ...]         # Funs with bare method names
    module: Optional[ModuleId] = None
    pos: Optional[Position] = None


@dataclass
class Data:
    name: str
    ty: Type
    init: Form           # the raw form
    module: Optional[ModuleId] = None


@dataclass
class Program:
    types: dict[str, TypeDecl] = field(default_factory=dict)
    funs: dict[str, Fun] = field(default_factory=dict)
    externs: dict[str, Extern] = field(default_factory=dict)
    datas: dict[str, Data] = field(default_factory=dict)
    traits: dict[str, Trait] = field(default_factory=dict)
    instances: dict[str, list[Instance]] = field(default_factory=dict)  # trait -> [Instance]
