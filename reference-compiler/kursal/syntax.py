"""The surface AST, and the entry points to the parser.

Parsing builds the written syntax. Resolution adds name references; signatures
translate declarations and cache constants separately. Inference annotates body
nodes in place for lowering. Comments beside annotation fields name their
producer and consumer; an annotation without a default is absent until filled.
Fields marked init=False are excluded from parsed-syntax equality and repr.
Lookup-only paths and subexpressions folded as constants need not have a ty.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Optional
from .sexpr import ESCAPES, Position


if TYPE_CHECKING:
    from .resolve import Decl
    from . import core as C
    from .infer import Callee, Capture, FnCall, TraitCall, VariantCall
    from .sigs import ConstValue


class ResolvedName:
    """Resolution -> signatures/inference/lowering: what a name or trait premise denotes.

    None before resolution, or where no reference is needed (e.g. a builtin
    struct-literal path). Local references carry names; inference supplies IDs.
    """
    ref: Optional[NameRef] = None


@dataclass
class Inferred:
    """Inference -> lowering: the node's core type, normalized by Infer.finish.

    Filled on expressions and patterns visited by inference, and on variant
    cases (their payload type). It is absent on wildcard and value cases.
    """
    ty: C.Type = field(init=False, repr=False, compare=False)


# ------------------------------------------------------------------ types --

@dataclass
class TName(ResolvedName):
    path: NamePath            # name segments
    linear: bool = False   # written `Name!`
    args: tuple[Type, ...] = ()
    determined: int = 0    # how many trailing args follow `->`


@dataclass
class TPtr:
    inner: Type


@dataclass
class TNever:
    pass


@dataclass
class TTuple:
    elems: tuple[Type, ...]


@dataclass
class TArray:
    elem: Type
    length: Expr         # an expression


@dataclass
class TFn:
    params: tuple[tuple[bool, Type], ...]          # ((lend, type), ...)
    ret: Type
    unsafe: bool = False


@dataclass
class TClosure:
    env: Optional[Type]
    params: tuple[tuple[bool, Type], ...]
    ret: Type


@dataclass
class TExists:
    brands: tuple[str, ...]
    body: Type


@dataclass
class TForeach:
    binder: str
    indirect: bool
    source: Type
    body: Type


@dataclass
class TForall:
    params: tuple[str, ...]
    body: Type


@dataclass
class TBrand:
    label: str


@dataclass
class TIgnored:
    pass


# ----------------------------------------------------------- expressions --

@dataclass
class Lit(Inferred):
    kind: Literal["int", "float", "str", "bool"]              # int float byte str bool
    value: int | str | bool
    suffix: Optional[str] = None
    pos: Optional[Position] = None


@dataclass
class FStr(Inferred):
    parts: tuple[str | Expr, ...]           # str or expression, in order
    pos: Optional[Position] = None


@dataclass
class Path(ResolvedName, Inferred):
    segments: tuple[tuple[str, bool, tuple[Type, ...]], ...]        # ((name, linear, type args), ...)
    pos: Optional[Position] = None
    # Inference -> lowering: binding ID for a local, or folded value for a constant.
    binder: int = field(init=False, repr=False, compare=False)
    folded: Optional[ConstValue] = field(default=None, init=False, repr=False, compare=False)
    # Inference-only flag: an unannotated binding may retain a function's brand quantifiers.
    generic: bool = field(default=False, init=False, repr=False, compare=False)
    # Inference -> lowering: function value or variant constructor (including direct variant calls).
    call: Optional[FnCall | VariantCall] = field(default=None, init=False, repr=False, compare=False)

    @property
    def names(self) -> NamePath:
        return tuple(s[0] for s in self.segments)


@dataclass
class Tuple(Inferred):
    elems: tuple[Expr, ...]
    pos: Optional[Position] = None


@dataclass
class Array(Inferred):
    elems: tuple[Expr, ...]
    fill: bool = False     # a trailing `..` repeats the last element
    pos: Optional[Position] = None


@dataclass
class Sizeof(Inferred):
    type: Type
    pos: Optional[Position] = None
    # Inference -> lowering: translation of the written type (ty is the usize result).
    target: C.Type = field(init=False, repr=False, compare=False)


@dataclass
class StructLit(Inferred):
    path: Path
    fields: tuple[tuple[str, Optional[Expr]], ...]          # ((name, expr or None for shorthand), ...)
    spread: Optional[str] = None
    pos: Optional[Position] = None
    # Inference -> lowering: shorthand fields' local IDs; spread_id only when spread is written.
    field_ids: dict[str, int] = field(init=False, repr=False, compare=False)
    spread_id: int = field(init=False, repr=False, compare=False)


@dataclass
class Block(Inferred):
    stmts: tuple[Statement, ...]
    tail: Optional[Expr] = None
    pos: Optional[Position] = None


@dataclass
class If(Inferred):
    cond: Expr
    then: Block
    other: Optional[Block | If] = None   # Block, If, or None
    pos: Optional[Position] = None


@dataclass
class Case(Inferred):
    """An arm alternative; a variant case's inferred `ty` is its payload type."""
    kind: Literal["variant", "wild", "value"]              # variant wild value
    path: Optional[Path] = None
    binders: Optional[tuple[str, ...]] = None  # None: no parentheses written
    low: Optional[Lit] = None     # value cases: Lit
    high: Optional[Lit] = None
    pos: Optional[Position] = None
    # Inference -> lowering, variant cases only: payload binding IDs; None for each `_`.
    binder_ids: list[Optional[int]] = field(init=False, repr=False, compare=False)


@dataclass
class Arm:
    cases: tuple[Case, ...]
    body: Expr
    pos: Optional[Position] = None


@dataclass
class Match(Inferred):
    scrutinee: Expr      # a Lend scrutinee is `match &x`
    arms: tuple[Arm, ...]
    pos: Optional[Position] = None
    # Inference -> lowering: scrutinee lends a place; payload bindings become place aliases.
    lent: bool = field(init=False, repr=False, compare=False)


@dataclass
class Closure(Inferred):
    params: tuple[tuple[bool, str, Optional[Type]], ...]          # ((lend, name, type or None), ...)
    ret: Optional[Type]
    body: Expr
    pos: Optional[Position] = None
    # Inference -> lifted-function lowering: parameter IDs and captures in first-use order.
    param_ids: list[int] = field(init=False, repr=False, compare=False)
    captures: tuple[Capture, ...] = field(init=False, repr=False, compare=False)


@dataclass
class Return(Inferred):
    value: Optional[Expr] = None
    pos: Optional[Position] = None


@dataclass
class Pack(Inferred):
    brands: tuple[str, ...]
    value: Expr
    pos: Optional[Position] = None
    witnesses: tuple[C.Type, ...] = field(init=False, repr=False, compare=False)


@dataclass
class Foreach(Inferred):
    name: str
    binder: Optional[str]
    source: Expr
    body: Block
    pos: Optional[Position] = None
    # Inference -> mapper lowering: item binding ID and captures in first-use order.
    name_id: int = field(init=False, repr=False, compare=False)
    captures: tuple[Capture, ...] = field(init=False, repr=False, compare=False)


@dataclass
class Binop(Inferred):
    op: str                # + - * / % & | ^ << >> == != < <= > >= && ||
    left: Expr
    right: Expr
    pos: Optional[Position] = None


@dataclass
class Unary(Inferred):
    op: str                # - ! * & unsafe
    operand: Expr
    pos: Optional[Position] = None


@dataclass
class Cast(Inferred):
    value: Expr
    type: Type
    pos: Optional[Position] = None


@dataclass
class UnsafeStore(Inferred):
    pointer: Expr
    value: Expr
    pos: Optional[Position] = None


@dataclass
class Field(Inferred):
    value: Expr
    name: str
    pos: Optional[Position] = None


@dataclass
class Index(Inferred):
    value: Expr
    index: int
    pos: Optional[Position] = None


@dataclass
class Call(Inferred):
    callee: Expr
    args: tuple[Expr, ...]
    pos: Optional[Position] = None
    # Inference -> lowering: resolved call target; filled before Infer.finish succeeds.
    call: Optional[Callee] = field(default=None, init=False, repr=False, compare=False)
    # Inference-only flag: `&` on a postfix chain marks the call lending its receiver.
    lend_marked: bool = field(default=False, init=False, repr=False, compare=False)


@dataclass
class Try(Inferred):
    value: Expr
    pos: Optional[Position] = None
    # Inference -> lowering: validated enum family selects success/failure variants.
    kind: Literal["option", "result"] = field(init=False, repr=False, compare=False)


# ------------------------------------------------------- statements, patterns --

@dataclass
class Let:
    pattern: Pattern
    type: Optional[Type]
    value: Expr
    brands: tuple[str, ...] = ()     # `let exists 'a. ...`
    pos: Optional[Position] = None
    opened_brands: tuple[str, ...] = field(init=False, repr=False, compare=False)


@dataclass
class Var:
    name: str
    type: Optional[Type]
    value: Expr
    pos: Optional[Position] = None
    # Inference -> lowering: mutable local's binding ID.
    binder: int = field(init=False, repr=False, compare=False)


@dataclass
class Assign:
    target: Expr
    value: Expr
    pos: Optional[Position] = None


@dataclass
class ExprStmt:
    value: Expr
    pos: Optional[Position] = None


@dataclass
class While(Inferred):
    cond: Expr
    body: Block
    pos: Optional[Position] = None


@dataclass
class For(Inferred):
    name: str
    iterator: Expr
    body: Block
    pos: Optional[Position] = None
    # Inference -> lowering: item binding, next() result type, and explicit Iterator call.
    binder: int = field(init=False, repr=False, compare=False)
    option: C.Type = field(init=False, repr=False, compare=False)
    next_call: TraitCall = field(init=False, repr=False, compare=False)


@dataclass
class PBind(Inferred):
    name: str
    # Inference -> lowering: the binding introduced by this pattern.
    binder: int = field(init=False, repr=False, compare=False)


@dataclass
class PWild(Inferred):
    pass


@dataclass
class PTuple(Inferred):
    elems: tuple[Pattern, ...]


@dataclass
class PArray(Inferred):
    elems: tuple[Pattern, ...]


@dataclass
class PStruct(Inferred):
    fields: tuple[tuple[str, Pattern], ...]          # ((field name, pattern), ...)
    rest: Optional[str] = None
    # Inference -> lowering: rest-bundle binding ID, only when rest is written.
    rest_id: int = field(init=False, repr=False, compare=False)


# ------------------------------------------------------------------ items --

class Declaration:
    """The grammar permits an optional forall header before any declaration."""
    forall: Optional[Forall] = None


@dataclass
class Forall:
    params: tuple[str, ...]          # names; brands keep their quote
    determined: tuple[str, ...] = () # params after `->`
    premises: tuple[tuple[NamePath, TypeArgs], ...] = ()   # ((trait path, type args), ...)


@dataclass
class Param:
    name: str
    type: Optional[Type] = None
    lend: bool = False     # written `&name: T` or `&self`


@dataclass
class Use(Declaration):
    tree: UseTree            # (path, star, children, alias)
    export: bool = False
    pos: Optional[Position] = None


@dataclass
class Module(Declaration):
    path: NamePath
    pos: Optional[Position] = None


@dataclass
class Struct(Declaration):
    name: str
    linear: bool
    fields: tuple[tuple[str, Type], ...]          # ((name, type), ...)
    exposed: Optional[Exposed] = None # None (restricted) or Exposed
    forall: Optional[Forall] = None
    pos: Optional[Position] = None


@dataclass
class TupleDecl(Declaration):
    name: str
    linear: bool
    elems: tuple[Type, ...]
    exposed: Optional[Exposed] = None
    forall: Optional[Forall] = None
    pos: Optional[Position] = None


@dataclass
class EnumDecl(Declaration):
    name: str
    linear: bool
    variants: tuple[tuple[str, tuple[Type, ...]], ...]        # ((name, payload types), ...)
    exposed: Optional[Exposed] = None
    forall: Optional[Forall] = None
    pos: Optional[Position] = None


@dataclass
class Fn(Declaration):
    name: str
    params: tuple[Param, ...]
    ret: Optional[Type]
    body: Optional[Block] = None     # None: a signature
    unsafe: bool = False
    forall: Optional[Forall] = None
    pos: Optional[Position] = None
    # Inference -> lowering, functions with bodies only: IDs in parameter order.
    param_ids: list[int] = field(init=False, repr=False, compare=False)


@dataclass
class Extern(Declaration):
    abi: str
    name: str
    params: tuple[Param, ...]
    ret: Optional[Type]
    pos: Optional[Position] = None


@dataclass
class Const(Declaration):
    name: str
    type: Type
    value: Expr
    forall: Optional[Forall] = None
    pos: Optional[Position] = None
    # Local constants only: inference -> lowering, binding ID and folded value.
    # Declaration constants use Signatures.consts; these fields remain absent there.
    binder: int = field(init=False, repr=False, compare=False)
    folded: ConstValue = field(init=False, repr=False, compare=False)


@dataclass
class Impl(Declaration):
    type: Type
    items: tuple[Fn | Const, ...]
    forall: Optional[Forall] = None
    pos: Optional[Position] = None


@dataclass
class TraitDecl(Declaration):
    name: str
    methods: tuple[Fn, ...]
    forall: Optional[Forall] = None
    pos: Optional[Position] = None


@dataclass
class InstanceDecl(Declaration):
    type: Type
    methods: tuple[Fn, ...]
    unsafe: bool = False
    forall: Optional[Forall] = None
    pos: Optional[Position] = None


# The parser's categories are explicit unions, with no generic tagged nodes.
type NamePath = tuple[str, ...]
type Type = (
    TName | TPtr | TNever | TTuple | TArray | TFn | TClosure
    | TExists | TForeach | TForall | TBrand | TIgnored
)
type Expr = (
    Lit | FStr | Path | Tuple | Array | Sizeof | StructLit | Block | If | Match
    | Closure | Return | Pack | Foreach | Binop | Unary | Cast | UnsafeStore
    | Field | Index | Call | Try | While | For
)
type Pattern = PBind | PWild | PTuple | PArray | PStruct
type Statement = Let | Var | Assign | ExprStmt | Const
type Item = (
    Use | Module | Struct | TupleDecl | EnumDecl | Fn | Extern
    | Const | Impl | TraitDecl | InstanceDecl
)
type UseTree = tuple[NamePath, bool, tuple[UseTree, ...], Optional[str]]


# ----------------------------------------------------------------- tokens --

def unescape(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and text[i + 1] == "x":
            out.append(chr(int(text[i + 2:i + 4], 16)))
            i += 4
        elif text[i] == "\\":
            out.append(ESCAPES[text[i + 1]])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


INT_SUFFIXES = ("isize", "usize", "i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64")


def split_suffix(text: str, suffixes: Iterable[str]) -> tuple[str, Optional[str]]:
    for s in suffixes:
        if text.endswith(s):
            return text[:-len(s)], s
    return text, None


@dataclass
class Exposed:
    scope: Optional[NamePath] = None  # None: exposed everywhere


@dataclass
class TypeArgs(ResolvedName):
    args: tuple[Type, ...]
    determined: int


# ----------------------------------------------------------------- parsing --

def parse_expr(text: str) -> Expr:
    """One expression."""
    from .parser import parse_expr as parse_one
    return parse_one(text)


def parse(text: str, filename: str = "<input>") -> list[Item]:
    """The items of one source file."""
    from .parser import parse as parse_items
    return parse_items(text, filename)

# References keep their discriminants, so consumers can narrow their payloads.
type DeclRef = tuple[Literal["decl"], Decl]
type VariantRef = tuple[Literal["variant"], Decl, str]
type TraitMethodRef = tuple[Literal["trait_method"], Decl, str]
type NameRef = (
    DeclRef | VariantRef | TraitMethodRef
    | tuple[Literal["assoc"], str, str]
    | tuple[Literal["param", "prim", "builtin", "local"], str]
)
