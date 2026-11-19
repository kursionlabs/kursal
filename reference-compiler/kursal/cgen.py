"""Checked KURS to C: one C local per SSA value the C needs, places as fixed C objects, edges as
assignments to the target's parameters.

Generic functions are specialized on demand by logical type arguments, up to their brands.
Representation erasure is separate from code-specialization identity.

A zero-size type (`Types.zero_size`: unit, brand witnesses, Never, products of zero-size components,
arrays of length 0 or of zero-size elements) has no C representation: its C type is `void` (`erased`),
and no struct member, parameter, local, argument or assignment has it. A function returning one
returns `void`; constructing, destructuring, copying, loading or storing one writes nothing; a pointer
to one is a `void*`. The one C object a zero-size type gets is a `data` of it, whose address the
program observes: a `char`.

An enum is a tag and a union of its payloads, or, in the niche layout (`Types.niche_variant`), its one
payload with a niche, the other variant a null there (`niche_layout`, `tag`, `payload`, `set_variant`).

What a function's C leaves out (dead values, copies, forwarded places, return joins) and where it
reads values where they lie (residences, lend adapters in place) is decided by plan.py, after local
places that only hold what was moved out of another place are given its storage (`homes`). A value
parameter of more than four words is taken by pointer (`by_ref`, `by_address`, `thunk`). Beyond that
the backend makes calls in tail position jumps (`tail_call`), and qualifies place parameters `const`
and `restrict` where the whole program allows it (`qualifiers`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
import re
from typing import Optional

from .core import (ATOMIC_OPS, FLOATS, INTEGERS, POINTER_SIZED, UNIT, WIDTHS, Array, Block, Brand, Enum, Exists, Fn,
                   Foreach, Ignored, Jmp, Jnz, Named, Never, NonNull, Param, Prim, Ptr, Ret, Skolem, Struct, Switch,
                   Tuple, Unreachable, Type, Program, Fun, Data, Instr, Edge, Terminator)
from .check import Checker, overlap, prefix
from .sexpr import Form, KursError, List, Str
from .types import foreach_binders, ForeachRow, Types, equal, map_children, subst, subst_foreach
from .termination import check_mono_termination
from .mangle import encode, mangle, path_segments, type_key, unbranded
from .plan import PURE, Adapter, Plan, instr_reads, root, term_edges, term_reads

PRELUDE = """#include <stddef.h>
#include <stdint.h>
#define KUR_TRAP() __builtin_trap()
/* A call the C compiler must make a tail call, where it can be told to: Clang's `musttail`. */
#if defined(__clang__) && defined(__has_attribute)
#if __has_attribute(musttail)
#define KUR_MUSTTAIL __attribute__((musttail))
#endif
#endif
#ifndef KUR_MUSTTAIL
#define KUR_MUSTTAIL
#endif
/* A component's address by integer arithmetic: projection never dereferences, so it is defined for any
   pointer value, null and dangling included. The `__typeof__` operands are not evaluated. */
#define KUR_FIELD(p, m) ((__typeof__(&(p)->m))((uintptr_t)(p) + offsetof(__typeof__(*(p)), m)))
static inline int8_t kur_dec_i8(uint64_t b) { b &= 0xFF; return b < 0x80 ? (int8_t)b : (int8_t)((int64_t)b - 0x100); }
static inline int16_t kur_dec_i16(uint64_t b) { b &= 0xFFFF; return b < 0x8000 ? (int16_t)b : (int16_t)((int64_t)b - 0x10000); }
static inline int32_t kur_dec_i32(uint64_t b) { b &= 0xFFFFFFFF; return b < 0x80000000 ? (int32_t)b : (int32_t)((int64_t)b - 0x100000000); }
static inline int64_t kur_dec_i64(uint64_t b) { return b <= (uint64_t)INT64_MAX ? (int64_t)b : -INT64_C(1) - (int64_t)(UINT64_MAX - b); }
/* usize and isize are pointer-sized: uintptr_t and intptr_t, of KUR_PTR_BITS bits. */
#define KUR_PTR_BITS (8 * sizeof(uintptr_t))
static inline intptr_t kur_dec_isize(uintptr_t b) { return b <= (uintptr_t)INTPTR_MAX ? (intptr_t)b : -(intptr_t)1 - (intptr_t)(UINTPTR_MAX - b); }
"""

C_PRIMS = {"bool": "_Bool", "f32": "float", "f64": "double", "usize": "uintptr_t", "isize": "intptr_t",
           **{n: f"{'u' if n[0] == 'u' else ''}int{w}_t" for n, w in WIDTHS.items()}}
# The signed integer types, each with the suffix of its `kur_dec_i` decoder.
SIGNED = {n: n[1:] for n in INTEGERS if n[0] == "i"}
C_ORDER = {o: f"__ATOMIC_{o.upper()}" for o in ("relaxed", "acquire", "release", "acq_rel", "seq_cst")}
C_OPS = {"add": "+", "sub": "-", "mul": "*", "div": "/", "mod": "%", "bit_and": "&", "bit_or": "|",
         "bit_xor": "^", "eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}


C_KEYWORDS = set("auto break case char const continue default do double else enum extern float for goto if "
                 "inline int long register restrict return short signed sizeof static struct switch typedef "
                 "union unsigned void volatile while _Alignas _Alignof _Atomic _Bool _Complex _Generic "
                 "_Imaginary _Noreturn _Static_assert _Thread_local".split())
# The C function, in `lib/libc/fs.c`, that C's `main` calls to report the error an entry returns.
REPORT_ERROR = "kur_libc_process_report_error"
RUNTIME_NAMES = {"KUR_TRAP", "KUR_MUSTTAIL", "KUR_FIELD", "KUR_PTR_BITS", *(f"kur_dec_i{n}" for n in SIGNED.values()),
                 "SIZE_MAX", "PTRDIFF_MIN", "PTRDIFF_MAX", "SIG_ATOMIC_MIN", "SIG_ATOMIC_MAX",
                 "WCHAR_MIN", "WCHAR_MAX", "WINT_MIN", "WINT_MAX", *C_ORDER.values()}
for _stem in ("8", "16", "32", "64", "_LEAST8", "_LEAST16", "_LEAST32", "_LEAST64",
              "_FAST8", "_FAST16", "_FAST32", "_FAST64", "PTR", "MAX"):
    for _unsigned in ("", "U"):
        RUNTIME_NAMES.add(f"{_unsigned.lower()}int{_stem.lower()}_t")
        RUNTIME_NAMES.update(f"{_unsigned}INT{_stem}_{suffix}" for suffix in ("MIN", "MAX", "C", "WIDTH"))


def allocate(base: str, taken: set[str]) -> str:
    """Allocate a C identifier; the readable spelling is only a hint."""
    if base[0].isdigit():
        base = "k_" + base
    name, index = base, 0
    while name in taken:
        index += 1
        name = f"{base}_{index}"
    taken.add(name)
    return name


def readable(name: str) -> str:
    """Decode the linker's hex-encoded identity segments, then make a C identifier."""
    parts = []
    for seg in name.split("::"):
        if re.fullmatch(r"[md](?:[0-9a-f]{2})+", seg):
            try:
                seg = bytes.fromhex(seg[1:]).decode()
            except ValueError:
                pass
        parts.append(seg)
    return re.sub(r"[^A-Za-z0-9_]", "_", "_".join(parts)).strip("_") or "x"


def homed(fun: Fun, homes: Mapping[str, str]) -> Fun:
    """`fun` with each path under a local place of `homes` rewritten under the path it shares."""
    def path(arg: str) -> str:
        if arg.startswith("&") and not arg.startswith("&*") and root(arg) in homes:
            return homes[root(arg)] + arg[len(root(arg)):]
        return arg
    blocks = []
    for block in fun.blocks:
        term = block.term
        if isinstance(term, Switch):
            term = replace(term, scrutinee=path(term.scrutinee))
        blocks.append(replace(block, instrs=tuple(replace(i, args=tuple(path(a) for a in i.args)) for i in block.instrs),
                              term=term))
    return replace(fun, blocks=tuple(blocks), locals=tuple((n, t) for n, t in fun.locals if n not in homes))


def erase(ty: Type, types: Types) -> Type:
    """The representation key: brands erased, packages unwrapped, transforms expanded."""
    if isinstance(ty, Skolem):
        return Skolem("_")
    if isinstance(ty, Brand):
        return UNIT
    if isinstance(ty, Exists):
        return erase(ty.body, types)
    if isinstance(ty, Foreach):
        return erase(types.expand(ty), types)
    if isinstance(ty, Fn):
        return Fn(tuple(erase(p, types) for p in ty.params), erase(ty.ret, types), places=ty.places)
    return map_children(ty, lambda child: erase(child, types))


class Backend:
    def __init__(self, program: Program, checker: Checker) -> None:
        self.program, self.checker = program, checker
        self.types = Types(program)
        self.names: dict[Type, str] = {}        # representation key -> C type name
        self.represented: dict[Type, Type] = {}   # an enum in the niche layout -> its payload's key
        self.pending: dict[Type, Tuple | Struct | Array | Enum] = {}      # named but not yet defined aggregates
        self.forward: list[str] = []
        self.defs: list[str] = []
        self.symbols: dict[int, list[tuple[tuple[Type, ...], str]]] = {}  # function -> its specializations' unbranded arguments and C symbols
        self.queue: list[tuple[Fun, Mapping[str, Type], str]] = []
        # Each function in order: its symbol, when its head waits for `qualifiers`, else its head; its body.
        self.emitted: list[tuple[Optional[str], str, str]] = []
        # symbol -> its C result, whether it never returns, and its parameters: (C type, C name, whether a
        # place); a parameter of a zero-size type has the C type `void` and no name, and is not written
        self.heads: dict[str, tuple[str, bool, list[tuple[str, str, bool]]]] = {}
        # Every place lent to a function by symbol: (callee, its parameter, where the place is: "param" under
        # the caller's place parameter, "local" in its local place, "raw" behind a pointer; the caller, and
        # its parameter for "param").
        self.lends: list[tuple[str, int, str, str, int]] = []
        self.written: set[tuple[str, int]] = set()      # place parameters the function may write
        self.address_taken: set[str] = set()
        self.looping: set[str] = set()     # functions whose tail call to themselves moves a place parameter
        self.adapters: dict[str, Optional[tuple[Adapter, Fun]]] = {}  # lend adapters by name (`adapter`)
        # symbol -> for each of its parameters, whether it takes an aggregate by pointer (`by_ref`)
        self.by_ref: dict[str, tuple[bool, ...]] = {}
        # symbol of a function whose address is taken -> its by-value entry (`thunk`)
        self.thunks: dict[str, str] = {}
        # symbol -> the elements of its tuple result it writes through out pointers (`request`)
        self.outs: dict[str, tuple[int, ...]] = {}
        # symbol -> the C type and name of each out pointer's pointee, in element order
        self.out_params: dict[str, list[tuple[str, str]]] = {}
        self.results: dict[str, Type] = {}     # symbol with out pointers -> its whole result type
        self.datas: dict[str, tuple[str, Data]] = {}
        # One conservative pool for ordinary identifiers, including function
        # locals: later global declarations must not be shadowed by earlier locals.
        self.taken = C_KEYWORDS | RUNTIME_NAMES | {"main", REPORT_ERROR} | {e.name for e in program.externs.values()}
        self.component_names: dict[tuple[str, str], str] = {}
        self.component_taken: set[str] = set()
        self.instance_params: dict[int, tuple[Param, ...]] = {}
        self.function_ids = {id(f): (path_segments(f.name), encode(("function", f.name)))
                             for f in program.funs.values()}
        for instances in program.instances.values():
            for inst in instances:
                scope = tuple(p.name for p in inst.params)
                head = encode(tuple(type_key(t, scope) for t in inst.head))
                for fun in inst.methods:
                    self.instance_params[id(fun)] = inst.params + fun.params
                    self.function_ids[id(fun)] = (
                        (*path_segments(inst.trait), "impl", *path_segments(fun.name)),
                        encode(("instance", inst.trait, head, fun.name)))

    # ------------------------------------------------- representations --

    def fresh_name(self, hint: str) -> str:
        return allocate(f"K_{readable(hint)[-48:]}", self.taken)

    def ctype(self, ty: Type, defined: bool = True) -> str:
        """The C type of `ty`; through a pointer, an aggregate needs only its name."""
        ty = erase(ty, self.types)
        if isinstance(ty, Prim):
            return C_PRIMS[ty.name]
        if isinstance(ty, Never):
            return "void"
        if isinstance(ty, (Ptr, NonNull)):
            inner = erase(ty.inner, self.types)
            return self.ctype(inner, False) + "*"
        if ty not in self.names:
            self.register(ty)
        key = self.represented.get(ty, ty)
        if defined and key in self.pending:
            self.define(key)
        return self.names[ty]

    def register(self, ty: Type) -> None:
        if self.types.zero_size(ty):
            self.names[ty] = "void"
            return
        s = self.types.shape(ty) if isinstance(ty, Named) else ty
        match s:
            case Fn(params=params, ret=ret, places=places):
                name = self.names[ty] = self.fresh_name("fn")
                # Function declarations may name forward-declared aggregates.
                # Define their fields after this typedef, which they may use.
                result = self.ctype(ret, False)
                cparams = [(self.ctype(p, False), k in places) for k, p in enumerate(params)]
                plist = ", ".join(ct + ("*" if place else "") for ct, place in cparams if ct != "void") or "void"
                self.defs.append(f"typedef {result} (*{name})({plist});")
            case Enum(variants=variants) if (niche := self.types.niche_variant(variants)) is not None:
                # The niche layout: the payload's C type, the other variant a null at the niche.
                payload = erase(variants[niche][1], self.types)
                self.names[ty] = self.ctype(payload, False)
                self.represented[ty] = payload
            case Tuple() | Struct() | Array() | Enum():
                name = self.names[ty] = self.fresh_name(ty.name if isinstance(ty, Named) else type(s).__name__.lower())
                self.forward.append(f"typedef struct {name} {name};")
                self.pending[ty] = s
            case _:
                self.names[ty] = self.ctype(s)

    def define(self, ty: Type) -> None:
        s = self.pending.pop(ty)
        match s:
            case Array(elem=elem, length=length):
                body = f"{self.ctype(elem)} items[{length}];"
            case Enum(variants=variants):
                members = " ".join(f"{self.ctype(f)} {self.variant(n)};" for n, f in variants if not self.erased(f))
                body = f"uint32_t tag; union {{ {members} }} payload;" if members else "uint32_t tag;"
            case _:
                body = " ".join(f"{self.ctype(f)} {m};" for m, f in self.members(s) if not self.erased(f))
        self.defs.append(f"struct {self.names[ty]} {{ {body} }};")

    def members(self, shape: Type) -> list[tuple[str, Type]]:
        """(C member, component type) of a struct or tuple shape, in order."""
        if isinstance(shape, Tuple):
            return [(f"e{i}", e) for i, e in enumerate(shape.elems)]
        assert isinstance(shape, Struct)
        return [(self.component(n, "f"), f) for n, f in shape.fields]

    def member_of(self, shape: Type, label: str) -> tuple[str, Type]:
        """(C member, component type) of the component `label` (a field name or a tuple index) of a
        struct or tuple shape."""
        assert isinstance(shape, (Struct, Tuple))
        names = [n for n, _ in shape.fields] if isinstance(shape, Struct) else [str(i) for i in range(len(shape.elems))]
        return dict(zip(names, self.members(shape)))[label]

    def component(self, name: str, prefix: str) -> str:
        key = (prefix, name)
        if key not in self.component_names:
            self.component_names[key] = allocate(f"{prefix}_{readable(name)}", self.component_taken)
        return self.component_names[key]

    def variant(self, name: str) -> str:
        return self.component(name, "v")

    def shape(self, ty: Type) -> Type:
        return self.types.shape(erase(ty, self.types))

    def element(self, ty: Type, k: int) -> Type:
        """Element `k` of a tuple type."""
        shape = self.shape(ty)
        if not isinstance(shape, Tuple):
            raise KursError(f"backend: {ty} is not a tuple")
        return shape.elems[k]

    # ------------------------------------------------------- enum layouts --

    def niche_layout(self, ty: Type) -> Optional[tuple[int, str]]:
        """For an enum in the niche layout (`Types.niche_variant`): the index of the variant it is
        represented as, and the C access from a value of it to the niche's pointer (`""` when the
        payload is that pointer)."""
        variants = self.types.variants(erase(ty, self.types))
        k = self.types.niche_variant(variants)
        if k is None:
            return None
        payload = variants[k][1]
        path = self.types.niche(payload)
        assert path is not None
        access = ""
        for label in path:
            s = self.shape(payload)
            if isinstance(s, Array):
                access, payload = access + ".items[0]", s.elem
            else:
                member, payload = self.member_of(s, label)
                access += "." + member
        return k, access

    def tag(self, source: str, ty: Type) -> str:
        """The index of the variant a value of the enum `ty` holds, as C."""
        layout = self.niche_layout(ty)
        if layout is None:
            return f"{source}.tag"
        k, access = layout
        return f"({source}{access} == 0 ? {1 - k} : {k})"

    def payload(self, source: str, ty: Type, variant: str) -> str:
        """The C object of one variant's payload within a value or place of the enum `ty`."""
        return source if self.niche_layout(ty) is not None else f"{source}.payload.{self.variant(variant)}"

    def set_variant(self, dst: str, ty: Type, variant: str, value: Optional[str]) -> list[str]:
        """C that makes `dst` hold `variant` of the enum `ty`, with the payload `value` (None for a payload
        of a zero-size type). In the niche layout, the payload variant is its payload; the other is
        the null at the niche, and the rest of the object is not written."""
        names = [n for n, _ in self.types.variants(erase(ty, self.types))]
        index = names.index(variant)
        layout = self.niche_layout(ty)
        if layout is not None:
            k, access = layout
            return [f"{dst} = {value};" if index == k else f"{dst}{access} = 0;"]
        payload = [f"{dst}.payload.{self.variant(variant)} = {value};"] if value is not None else []
        return [*payload, f"{dst}.tag = {index};"]

    def arguments_type(self, ty: Type) -> bool:
        """Whether an entry function may take `ty`, which C's `main` fills from `argv` and `argc`
        (`std::process::Args`): a struct of exactly a `*(*u8)` and a `usize`, in that order."""
        shape = self.shape(ty)
        return isinstance(shape, Struct) and len(shape.fields) == 2 and \
            equal(shape.fields[0][1], Ptr(Ptr(Prim("u8")))) and equal(shape.fields[1][1], Prim("usize"))

    def entry_result(self, ty: Type) -> Optional[str]:
        """How C's `main` makes an entry function's result `ty` its exit status, or None if it cannot:
        "status" returns it (`i32`), "zero" returns 0 (`()`), and "report" (`Result[(), std::io::Error]`)
        returns 0 for `Ok`, and 1 for `Err` once `REPORT_ERROR` has reported its errno on stderr. As
        `Args` is, the result is known by its shape alone: an enum of `Ok` of `()` and `Err` of a struct
        of exactly one `i32`."""
        if equal(ty, Prim("i32")):
            return "status"
        if equal(ty, UNIT):
            return "zero"
        shape = self.shape(ty)
        if not isinstance(shape, Enum) or [name for name, _ in shape.variants] != ["Ok", "Err"]:
            return None
        error = self.shape(shape.variants[1][1])
        if equal(shape.variants[0][1], UNIT) and isinstance(error, Struct) and len(error.fields) == 1 and \
                equal(error.fields[0][1], Prim("i32")):
            return "report"
        return None

    def erased(self, ty: Type) -> bool:
        """`ty` has no C representation: it is zero-size (Never included)."""
        return self.ctype(ty) == "void"

    def erased_arg(self, arg: str) -> bool:
        """A call argument passes nothing: a value, a lent place or a raw target of a zero-size type."""
        if arg.startswith("&*"):
            pointer = self.type_of(arg[2:])
            assert isinstance(pointer, Ptr)
            return self.erased(pointer.inner)
        if arg.startswith("&"):
            return self.erased(self.place(arg)[1])
        return self.erased(self.type_of(arg))

    # ---------------------------------------------------- specialization --

    def request(self, fun: Fun, sub: Mapping[str, Type]) -> str:
        params = self.instance_params.get(id(fun), fun.params)
        if set(sub) != {p.name for p in params}:
            raise KursError(f"specialization arguments disagree for {fun.name}")
        # One specialization for type arguments that differ only in brands, made for the first
        # request's. Otherwise logical alpha-equivalence, not representation equality.
        # Capture-avoiding substitution may freshen bound names on recursive requests.
        args = tuple(unbranded(sub[p.name]) for p in params)
        for previous, symbol in self.symbols.get(id(fun), ()):
            if all(equal(a, b) for a, b in zip(args, previous)):
                return symbol
        path, function_id = self.function_ids[id(fun)]
        signature = Fn(fun.ptypes, fun.ret, params, fun.unsafe, fun.places)
        symbol = self.function_name(path, encode((function_id, type_key(signature),
                                                  *(type_key(t) for t in args))))
        self.symbols.setdefault(id(fun), []).append((args, symbol))
        self.queue.append((fun, dict(sub), symbol))
        self.by_ref[symbol] = tuple(not name.startswith("&") and self.by_pointer(subst(ty, sub))
                                    for name, ty in fun.blocks[0].params)
        result = self.shape(subst(fun.ret, sub))
        self.outs[symbol] = tuple(k for k, e in enumerate(result.elems) if self.by_pointer(e)) \
            if isinstance(result, Tuple) else ()
        return symbol

    def reduced(self, ret: Type, outs: Sequence[int]) -> Type:
        """The C result of a function writing the elements `outs` of its tuple result `ret` through out
        pointers: a tuple of the other elements."""
        result = self.shape(ret)
        assert isinstance(result, Tuple)
        return Tuple(tuple(e for k, e in enumerate(result.elems) if k not in outs))

    def by_pointer(self, ty: Type) -> bool:
        """A value of `ty` is passed by pointer: its C representation is more than four words. Up to four
        words the C compiler copies it in as many moves, while a pointer costs the callee a load per read
        and the inliner a higher estimate: `Interp::cons` of `examples/lisp`, taking two four-word values,
        is not inlined into the interpreter's loop when it takes them by pointer."""
        return self.words(ty) > 4

    def words(self, ty: Type) -> int:
        """The size of `ty`'s C representation in 8-byte words, as estimated from its structure: a
        scalar is a word, an enum its tag and its largest payload (in the niche layout, its payload)."""
        t = erase(ty, self.types)
        if self.types.zero_size(t):
            return 0
        s = self.types.shape(t)
        if isinstance(s, Struct):
            return sum(self.words(f) for _, f in s.fields)
        if isinstance(s, Tuple):
            return sum(self.words(e) for e in s.elems)
        if isinstance(s, Array):
            return s.length * self.words(s.elem)
        if isinstance(s, Enum):
            k = self.types.niche_variant(s.variants)
            if k is not None:
                return self.words(s.variants[k][1])
            return 1 + max((self.words(payload) for _, payload in s.variants), default=0)
        return 1

    def function_name(self, path: tuple[str, ...], identity: str) -> str:
        salt = 0
        symbol = mangle(path, identity)
        while symbol in self.taken:
            salt += 1
            symbol = mangle(path, identity, salt)
        self.taken.add(symbol)
        return symbol

    def function_address(self, instr: Instr) -> str:
        name = instr.args[0]
        if name in self.program.externs:
            return name
        fun = self.program.funs[name]
        # Stable distinct rigid identities: not a wildcard and not static.
        sub = {p.name: Skolem(encode(("#retained", fun.name, str(k)))) if isinstance(t, Ignored)
               else self.substitute_type(t) for k, (p, t) in enumerate(zip(fun.params, instr.tys))}
        symbol = self.request(fun, sub)
        self.address_taken.add(symbol)
        if not any(self.by_ref[symbol]) and not self.outs[symbol]:
            return symbol
        # A function value calls with the plain convention: through an entry taking every value as is.
        if symbol not in self.thunks:
            self.thunks[symbol] = allocate(f"{symbol}_value", self.taken)
        return self.thunks[symbol]

    def resolve_trait(self, trait_name: str, method: str, tys: tuple[Type, ...]) -> tuple[Fun, dict[str, Type]]:
        # Checking already discharged authority at the use site. Specialization
        # selects its coherent head; the callee's module need not own that shape.
        found = self.checker.matching_instance(trait_name, tys)
        if found is None:
            raise KursError(f"no instance of {trait_name} for these types")
        inst, sub = found
        fun = next(m for m in inst.methods if m.name == method)
        return fun, sub

    # ------------------------------------------------------------ program --

    def emit_program(self, entry: Optional[str] = None) -> str:
        check_mono_termination(self.program)
        externs = []
        for ext in self.program.externs.values():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", ext.name) or ext.name in C_KEYWORDS:
                raise KursError(f"backend: foreign symbol {ext.name} is not a C identifier", ext.pos)
            if ext.name in RUNTIME_NAMES or ext.name.startswith(("__builtin_", "__atomic_")) or ext.name in ("main", REPORT_ERROR) and entry is not None:
                raise KursError(f"backend: foreign symbol {ext.name} conflicts with the C runtime prelude or entry wrapper", ext.pos)
            assert ext.ret is not None
            if ext.ptypes is None:
                externs.append(f"extern {self.ctype(ext.ret)} {ext.name};")
                continue
            ret = self.ctype(ext.ret)
            noreturn = "_Noreturn " if isinstance(ext.ret, Never) else ""
            params = ", ".join(self.ctype(p) for p in ext.ptypes) or "void"
            externs.append(f"{noreturn}{ret} {ext.name}({params});")
        main: Optional[str] = None
        if entry is not None:
            fun = self.program.funs.get(entry)
            if fun is None or fun.params or fun.premises or fun.unsafe or fun.places or self.entry_result(fun.ret) is None or \
                    not (fun.ptypes == () or len(fun.ptypes) == 1 and self.arguments_type(fun.ptypes[0])):
                raise KursError(f"backend: the entry {entry} must be `fn()`, `fn() -> i32` or "
                                "`fn() -> Result[(), std::io::Error]`, each with or without a parameter "
                                "`args: std::process::Args`, and safe, not generic and without premises")
            main = self.request(fun, {})
        else:
            # The roots are the functions with nothing left to choose or discharge. One with premises gets code
            # only when a checked call, which discharged them, requests it.
            for fun in self.program.funs.values():
                if not fun.params and not fun.premises:
                    self.request(fun, {})
        while self.queue:
            self.emit_function(*self.queue.pop(0))
        for symbol, thunk in self.thunks.items():
            self.emitted.append((None, *self.thunk(symbol, thunk)))
        datas = [self.emit_data(d) for d in self.datas.values()]
        while self.pending:
            self.define(next(iter(self.pending)))
        const, restrict = self.qualifiers()
        protos, bodies = [], []
        for owner, head, body in self.emitted:
            if owner is not None:
                head = self.head(owner, const, restrict)
            protos.append(head + ";")
            bodies.append(head + body)
        out = [PRELUDE, *self.forward, *self.defs, *externs, *datas, *protos, *bodies]
        if entry is not None:
            assert main is not None
            out.append(self.entry_wrapper(self.program.funs[entry], main))
        return "\n".join(out) + "\n"

    def entry_wrapper(self, fun: Fun, symbol: str) -> str:
        """C's `main`: it calls the entry `fun`, whose C is `symbol`, with `Args` from `argv` and `argc` if
        `fun` takes them, and makes its result the exit status (`entry_result`)."""
        if fun.ptypes:
            shape = self.shape(fun.ptypes[0])
            assert isinstance(shape, Struct)
            (_, argv), (_, count) = shape.fields
            head = "int main(int argc, char **argv)"
            call = f"{symbol}(({self.ctype(fun.ptypes[0])}){{ ({self.ctype(argv)})argv, ({self.ctype(count)})argc }})"
        else:
            head, call = "int main(void)", f"{symbol}()"
        kind = self.entry_result(fun.ret)
        if kind == "status":
            return f"{head} {{ return (int){call}; }}"
        if kind == "zero":
            return f"{head} {{ {call}; return 0; }}"
        result = self.shape(fun.ret)
        assert isinstance(result, Enum)
        error = self.shape(result.variants[1][1])
        assert isinstance(error, Struct)
        member, _ = self.member_of(error, error.fields[0][0])
        status = self.fresh_name("status")
        return (f"void {REPORT_ERROR}(int32_t);\n{head} {{ {self.ctype(fun.ret)} {status} = {call}; "
                f"if ({self.tag(status, fun.ret)} == 0) return 0; "
                f"{REPORT_ERROR}({self.payload(status, fun.ret, 'Err')}.{member}); return 1; }}")

    # -------------------------------------------------------------- data --

    def data_symbol(self, name: str) -> str:
        if name not in self.datas:
            self.datas[name] = (allocate(f"d{len(self.datas)}_{readable(name)[-40:]}", self.taken), self.program.datas[name])
        return self.datas[name][0]

    def emit_data(self, entry: tuple[str, Data]) -> str:
        """A data's C object; one of a zero-size type is a `char`, for the address the program sees."""
        symbol, data = entry
        if self.erased(data.ty):
            return f"static char {symbol};"
        return f"static {self.ctype(data.ty)} {symbol} = {self.initializer(data.ty, data.init)};"

    def initializer(self, ty: Type, form: Form) -> str:
        s = self.shape(ty)
        if isinstance(s, Prim):
            return self.literal(s, str(form))
        assert isinstance(form, List)
        if isinstance(s, Array):
            values: list[str] = []
            for part in form:
                if s.elem == Prim("u8") and isinstance(part, Str):
                    values.extend(str(b) for b in part.encode())
                else:
                    values.append(self.initializer(s.elem, part))
            return "{ { " + ", ".join(values) + " } }"
        return "{ " + ", ".join(self.initializer(f, part) for (_, f), part in zip(self.members(s), form)
                                if not self.erased(f)) + " }"

    def literal(self, ty: Prim, lit: str) -> str:
        if ty.name == "bool":
            return "1" if lit in ("1", "true", "True") else "0"
        if ty.name in FLOATS:
            text = lit if any(c in lit for c in ".eE") else lit + ".0"
            return text + ("f" if ty.name == "f32" else "")
        if ty.name in SIGNED:
            return "(-INT64_C(9223372036854775807) - 1)" if int(lit) == -(1 << 63) else f"INT64_C({int(lit)})"
        return f"UINT64_C({int(lit)})"

    # ---------------------------------------------------------- functions --

    def emit_function(self, fun: Fun, sub: Mapping[str, Type], symbol: str) -> None:
        self.fun, self.sub, self.symbol = fun, sub, symbol
        components: dict[tuple[str, int], tuple[str, ...]] = {}
        shapes: dict[tuple[str, int], Type] = {}
        adapters: dict[str, Adapter] = {}
        for block in fun.blocks:
            for i, instr in enumerate(block.instrs):
                if instr.op in ("destruct", "construct"):
                    ty = self.type_operand(instr)
                    labels = self.labels_of(ty)
                    if labels is not None:
                        components[block.label, i] = labels
                        shapes[block.label, i] = erase(ty, self.types)
                elif instr.op == "call" and instr.args[0] in self.program.funs:
                    adapter = self.adapter(instr.args[0])
                    if adapter is not None:
                        adapters[instr.args[0]] = adapter[0]
        params = frozenset(name for name, _ in fun.blocks[0].params if name.startswith("&"))
        self.plan = Plan(fun, components, adapters, params, shapes)
        typed = self.checker.typed[id(fun)]
        substitute_type = self.substitute_type
        self.place_c: dict[str, str] = {}
        self.roots = {**{n: substitute_type(t) for n, t in fun.blocks[0].params if n.startswith("&")},
                      **{n: substitute_type(t) for n, t in fun.locals}}
        homes = self.homes(fun, typed)
        if homes:
            fun = homed(fun, homes)
            self.fun = fun
            self.plan = Plan(fun, components, adapters, params, shapes)
        self.locals: dict[tuple[str, str], str] = {}
        self.aliases: dict[tuple[str, str], str] = {}
        self.forwards: dict[str, str] = {}
        self.lines: list[str] = []
        self.scratch = 0
        label_taken: set[str] = set()
        self.labels = {b.label: allocate(f"L_{readable(b.label)}", label_taken) for b in fun.blocks}
        decls: list[str] = []
        self.temps: list[str] = []
        entry = fun.blocks[0]
        self.params = [name for name, _ in entry.params]
        self.place_c = {}
        cparams = []
        # The parameters taken by pointer: read through it, or copied into the parameter's variable
        # where the function writes that (a call of itself in tail position, or a lend in place).
        written = self.written_params(fun)
        self.deref: set[str] = set()
        prologue: list[str] = []
        for k, (name, ty) in enumerate(entry.params):
            ctype = self.ctype(substitute_type(ty))
            if ctype == "void":
                cparams.append((ctype, "", name.startswith("&")))
                continue
            cname = self.cname(entry.label, name)
            if name.startswith("&"):
                self.place_c[name] = f"(*{cname})"
            elif self.by_ref[symbol][k] and name in written:
                pointer = allocate(f"{cname}_ref", self.taken)
                decls.append(f"{ctype} {cname};")
                prologue.append(f"{cname} = *{pointer};")
                cname = pointer
            elif self.by_ref[symbol][k]:
                self.deref.add(name)
            cparams.append((ctype, cname, name.startswith("&")))
        ret_type = substitute_type(fun.ret)
        self.ret_type = ret_type
        outs = self.outs[symbol]
        if outs:
            result = self.shape(ret_type)
            assert isinstance(result, Tuple)
            self.out_params[symbol] = [(self.ctype(result.elems[k]), allocate(f"out_{k}", self.taken)) for k in outs]
            self.results[symbol] = ret_type
            self.heads[symbol] = (self.ctype(self.reduced(ret_type, outs)), False, cparams)
        else:
            self.heads[symbol] = (self.ctype(ret_type), isinstance(ret_type, Never), cparams)
        # (block, value, element) of each element of a returned value already in its out pointer's pointee
        self.at_out: set[tuple[str, str, int]] = set()
        for name, ty in fun.locals:
            if name in self.plan.forwarded or self.erased(substitute_type(ty)):
                continue
            cname = allocate("p_" + readable(name), self.taken)
            self.place_c[name] = cname
            decls.append(f"{self.ctype(substitute_type(ty))} {cname};")
        self.roots = {**{n: substitute_type(t) for n, t in entry.params if n.startswith("&")},
                      **{n: substitute_type(t) for n, t in fun.locals}}
        self.typed = typed
        values = []
        for (label, name), ty in typed.items():
            if label == entry.label and any(name == n for n, _ in entry.params) or self.erased(substitute_type(ty)):
                continue
            values.append((label, name, ty, self.cname(label, name)))
        self.blocks = {b.label: b for b in fun.blocks}
        self.returning = False
        self.out_paths: list[str] = []
        self.lines.extend(prologue)
        for block in self.plan.emitted:
            self.label = block.label
            self.line(f"{self.labels[block.label]}: ;")
            call = self.plan.tail_call(block)
            for self.index, instr in enumerate(block.instrs):
                if instr is not call:
                    self.instr(instr)
            if call is None or not self.tail_call(call):
                if call is not None:
                    self.returning = True
                    self.instr(call)
                    self.returning = False
                self.term(block.term)
        decls.extend(f"{self.ctype(substitute_type(ty))} {cname};" for label, name, ty, cname in values
                     if (label, name) in self.plan.variables)
        self.emitted.append((symbol, "", " {\n" + "".join(f"  {d}\n" for d in decls + self.temps) +
                             "".join(f"  {line}\n" for line in self.lines) + "}\n"))

    def homes(self, fun: Fun, typed: Mapping[tuple[str, str], Type]) -> dict[str, str]:
        """The local places that share the storage of a place path (`Plan` decides the rest on the
        function with their paths rewritten to it): a local L whose every whole store stores a value
        lying at one place path P, of L's type, that is never full where P is full. Each block's header
        declares which places are empty at its entry; a load or a place destruct of a value that is not
        `Copy` empties its path, and a store fills its path, and the paths around it, conservatively."""
        plan = self.plan
        kinds = {p.name: p.kind for p in self.instance_params.get(id(fun), fun.params)}
        seen: dict[str, Optional[str]] = {}
        for block in fun.blocks:
            for instr in block.instrs:
                a = instr.args
                # A forwarded place has no storage to share.
                if instr.op == "store" and a[0] not in plan.forwarded and a[0] in seen.keys() | {n for n, _ in fun.locals}:
                    path = plan.resident.get((block.label, a[1]))
                    known = seen.get(a[0], "")
                    if path is None or not path.startswith("&") or known not in ("", path):
                        seen[a[0]] = None
                    else:
                        seen[a[0]] = path
        found = {local: path for local, path in seen.items() if path is not None and local not in plan.forwarded
                 and root(path) not in plan.forwarded and root(path) != local}
        found = {local: path for local, path in found.items() if root(path) not in found and
                 not any(overlap(path, other) for o, other in found.items() if o != local) and
                 not self.erased(self.roots[local]) and
                 erase(self.roots[local], self.types) == erase(self.place(path)[1], self.types)}
        if not found:
            return found
        # A local whose contents C reads as a value stays a variable the C compiler can keep in registers:
        # each whole load of it is moved back to the path alone, directly or as the member at the path of
        # a construction stored in place, and nothing reads part of it but a lend.
        for block in fun.blocks:
            reads: dict[str, list[int]] = {}
            for i, instr in enumerate(block.instrs):
                for name in instr_reads(instr):
                    reads.setdefault(name, []).append(i)
            for name in term_reads(block.term) + [name for edge, _ in term_edges(block.term) for name in edge.args]:
                reads.setdefault(name, []).append(len(block.instrs))
            for i, instr in enumerate(block.instrs):
                a = instr.args
                for k, arg in enumerate(a):
                    local = root(arg) if arg.startswith("&") and not arg.startswith("&*") else ""
                    if local not in found or instr.op in ("call", "trait_call", "store") and arg != local or \
                            instr.op == "store" and k == 0:
                        continue
                    back = instr.op == "load" and arg == local and len(reads.get(instr.lhs[0], ())) == 1
                    if back:
                        value, j = instr.lhs[0], reads[instr.lhs[0]][0]
                        # A value waiting in a forwarded place is read where it is loaded again.
                        while j < len(block.instrs) and block.instrs[j].op == "store" and \
                                block.instrs[j].args[0] in plan.forwarded:
                            again = next(m for m in range(j + 1, len(block.instrs)) if block.instrs[m].op == "load"
                                         and block.instrs[m].args == (block.instrs[j].args[0],))
                            value = block.instrs[again].lhs[0]
                            j = reads[value][0] if len(reads.get(value, ())) == 1 else len(block.instrs) + 1
                        user = block.instrs[j] if j < len(block.instrs) else None
                        if user is None or not (user.op == "store" and user.args == (found[local], value) or
                                                user.op == "construct" and (block.label, j) in plan.fused and
                                                any(arg2 == value and f"{block.instrs[j + 1].args[0]}.{part}" == found[local]
                                                    for arg2, part in zip(user.args, self.labels_of(self.type_operand(user)) or ()))):
                            back = False
                    if not back and instr.op not in ("call", "trait_call"):
                        found.pop(local, None)
            if isinstance(block.term, Switch) and block.term.scrutinee.startswith("&"):
                found.pop(root(block.term.scrutinee), None)
        blocks = {block.label: block for block in fun.blocks}
        reach: dict[str, set[str]] = {}
        for block in fun.blocks:
            seen_blocks: set[str] = set()
            todo = [edge.target for edge, _ in term_edges(block.term)]
            while todo:
                label = todo.pop()
                if label not in seen_blocks:
                    seen_blocks.add(label)
                    todo.extend(edge.target for edge, _ in term_edges(blocks[label].term))
            reach[block.label] = seen_blocks

        def names(local: str, block: Block, start: int) -> bool:
            """An instruction of `block` from `start` on, or its terminator, names a path under `local`."""
            places = [arg for instr in block.instrs[start:] for arg in instr.args]
            if isinstance(block.term, Switch):
                places.append(block.term.scrutinee)
            return any(arg.startswith("&") and not arg.startswith("&*") and root(arg) == local for arg in places)

        out: dict[str, str] = {}
        for local, path in found.items():
            def apart(empty: set[str], block: Block, at: int) -> bool:
                """`local` is empty or named no more from instruction `at` of `block` on, or `path` is empty."""
                return any(prefix(e, local) for e in empty) or any(prefix(e, path) for e in empty) or \
                    not names(local, block, at) and not any(names(local, blocks[b], 0) for b in reach[block.label])
            fits = True
            for block in fun.blocks:
                # Every local place is empty where the function starts.
                empty = set(block.empty) | ({n for n, _ in fun.locals} if block is fun.blocks[0] else set())
                fits = fits and apart(empty, block, 0)
                for i, instr in enumerate(block.instrs):
                    if not fits:
                        break
                    a = instr.args
                    if instr.op == "load" and a[0].startswith("&") and not a[0].startswith("&*"):
                        if not self.types.is_copy(typed[block.label, instr.lhs[0]], kinds):
                            empty.add(a[0])
                    elif instr.op == "destruct" and a[0].startswith("&"):
                        assert instr.ty is not None
                        if not self.types.is_copy(instr.ty, kinds):
                            empty.add(a[0])
                    elif instr.op == "store":
                        empty = {e for e in empty if not overlap(e, a[0])}
                    fits = apart(empty, block, i + 1)
            if fits:
                out[local] = path
        return out

    def labels_of(self, ty: Type) -> Optional[tuple[str, ...]]:
        """The labels of a struct's or tuple's components in a place path; none for other types."""
        s = self.shape(ty)
        if isinstance(s, Struct):
            return tuple(n for n, _ in s.fields)
        if isinstance(s, Tuple):
            return tuple(str(k) for k in range(len(s.elems)))
        return None

    def adapter(self, name: str) -> Optional[tuple[Adapter, Fun]]:
        """The lend adapter `name` is, by its shape alone: one block that stores some of its value
        parameters into its local places, one apiece, makes one call lending each place, and returns the
        places reloaded and the call's result as a tuple, or never returns."""
        if name not in self.adapters:
            self.adapters[name] = None
            fun = self.program.funs[name]
            block = fun.blocks[0]
            names = [n for n, _ in block.params]
            k = len(fun.locals)
            instrs = block.instrs
            if len(fun.blocks) != 1 or k == 0 or len(instrs) < k + 1:
                return None
            slots = [n for n, _ in fun.locals]
            stores = instrs[:k]
            call = instrs[k]
            if not all(s.op == "store" and s.args[0] == slot and s.args[1] in names and s.args[1].startswith("%")
                       for s, slot in zip(stores, slots)) or call.op not in ("call", "trait_call") or len(call.lhs) != 1:
                return None
            if not all(sum(arg == slot for arg in call.args) == 1 for slot in slots):
                return None
            borrowed = tuple(names.index(s.args[1]) for s in stores)
            rest, term = instrs[k + 1:], block.term
            if isinstance(term, Unreachable) and not rest:
                self.adapters[name] = (Adapter(borrowed, False), fun)
            elif isinstance(term, Ret) and len(rest) == k + 1 and \
                    all(r.op == "load" and r.args == (slot,) for r, slot in zip(rest, slots)) and \
                    rest[k].op == "construct" and rest[k].args == (*(r.lhs[0] for r in rest[:k]), call.lhs[0]) and \
                    term.value == rest[k].lhs[0]:
                self.adapters[name] = (Adapter(borrowed, True), fun)
        return self.adapters[name]

    def written_params(self, fun: Fun) -> set[str]:
        """The value parameters the function being written assigns: every one where it may call itself
        in tail position (the call assigns the parameters and runs the entry block again), and those
        holding a path a lend in place lends."""
        plan = self.plan
        for block in plan.emitted:
            call = plan.tail_call(block)
            if call is not None and (call.op == "trait_call" or call.args[0] == fun.name):
                return {name for name, _ in fun.blocks[0].params}
        return {root(path) for paths in plan.in_place.values() for path in paths if path.startswith("%")}

    def thunk(self, symbol: str, thunk: str) -> tuple[str, str]:
        """The head and body of `thunk`, the by-value entry of `symbol` that function values call: it
        takes each aggregate `symbol` takes by pointer as a value, and passes its address."""
        ret, noreturn, cparams = self.heads[symbol]
        params, args = [], []
        for k, (ctype, cname, place) in enumerate(cparams):
            if ctype == "void":
                continue
            params.append(f"{ctype}{'*' if place else ''} {cname}")
            args.append(f"&{cname}" if self.by_ref[symbol][k] else cname)
        outs = self.out_params.get(symbol, [])
        if not outs:
            call = f"{symbol}({', '.join(args)});"
            body = f" {{\n  {call}\n}}\n" if ret == "void" else f" {{\n  return {call}\n}}\n"
            return f"{'_Noreturn ' if noreturn else ''}static {ret} {thunk}({', '.join(params) or 'void'})", body
        # It takes the result's elements written through out pointers into locals, and returns the whole.
        whole, ks = self.results[symbol], self.outs[symbol]
        members = self.members(self.shape(whole))
        lines = [f"{ctype} {cname};" for ctype, cname in outs]
        call = f"{symbol}({', '.join(args + ['&' + cname for _, cname in outs])})"
        rest = iter(self.members(self.shape(self.reduced(whole, ks))))
        parts = []
        for k, (member, t) in enumerate(members):
            source = f"{outs[ks.index(k)][1]}" if k in ks else f"reduced.{next(rest)[0]}"
            if not self.erased(t):
                parts.append(f".{member} = {source}")
        lines.append(f"{call};" if ret == "void" else f"{ret} reduced = {call};")
        lines.append(f"return ({self.ctype(whole)}){{ {', '.join(parts)} }};")
        body = " {\n" + "".join(f"  {line}\n" for line in lines) + "}\n"
        return f"static {self.ctype(whole)} {thunk}({', '.join(params) or 'void'})", body

    def head(self, symbol: str, const: set[tuple[str, int]], restrict: set[tuple[str, int]]) -> str:
        """A function's head, with its place parameters qualified as `qualifiers` allows."""
        ret, noreturn, cparams = self.heads[symbol]
        params = []
        for k, (ctype, cname, place) in enumerate(cparams):
            if ctype == "void":
                continue
            if place:
                ctype += (" const" if (symbol, k) in const else "") + "*" + (" restrict" if (symbol, k) in restrict else "")
            elif self.by_ref[symbol][k]:
                ctype += " const*"
            params.append(f"{ctype} {cname}")
        params.extend(f"{ctype}* {cname}" for ctype, cname in self.out_params.get(symbol, ()))
        return f"{'_Noreturn ' if noreturn else ''}static {ret} {symbol}({', '.join(params) or 'void'})"

    def substitute_type(self, ty: Type) -> Type:
        return subst(ty, self.sub)

    def cname(self, label: str, name: str) -> str:
        key = (label, name)
        if key not in self.locals:
            base = f"{readable(label)}_{readable(name)}"
            self.locals[key] = allocate(base, self.taken)
        return self.locals[key]

    def value_name(self, name: str) -> str:
        """The C variable holding a value of the current block: its own, the one it reads, or the place it
        lies in (`Plan`)."""
        key = (self.label, name)
        if key in self.plan.resident:
            return self.place(self.plan.resident[key])[0]
        if self.label == self.fun.blocks[0].label and name in self.deref:
            return f"(*{self.cname(self.label, name)})"
        if key in self.plan.shared:
            return self.cname(*self.plan.shared[key])
        if key in self.plan.arrived:
            block, scrutinee, variant = self.plan.arrived[key]
            here, self.label = self.label, block
            source, ty = self.value_name(scrutinee), self.type_of(scrutinee)
            self.label = here
            return self.payload(source, ty, variant)
        return self.aliases[key] if key in self.aliases else self.cname(self.label, name)

    def value_base(self, name: str) -> str:
        """The C variable an assignment from `name` reads: for a value lying in a place under a place
        parameter, the parameter's pointer, which a self tail call may assign."""
        key = (self.label, name)
        if key in self.plan.resident:
            path = self.plan.resident[key]
            if path.startswith("%"):
                return self.cname(self.label, root(path))
            return self.cname(self.fun.blocks[0].label, root(path)) if root(path) in self.params else self.place_c[root(path)]
        if key in self.plan.arrived:
            block, scrutinee, _ = self.plan.arrived[key]
            here, self.label = self.label, block
            base = self.value_base(scrutinee)
            self.label = here
            return base
        return self.value_name(name)

    def is_live(self, name: str) -> bool:
        return self.plan.is_live(self.label, name)

    def type_of(self, name: str) -> Type:
        return self.substitute_type(self.typed[self.label, name])

    def line(self, text: str) -> None:
        self.lines.append(text)

    # -------------------------------------------------------------- places --

    def place(self, path: str) -> tuple[str, Type]:
        """The C lvalue and type of a place path."""
        parts = path.split(".")
        if parts[0].lstrip("&").startswith("%"):
            # A path in the C variable of a value of the current block (`Plan.residences`).
            value = parts[0].lstrip("&")
            expr, ty = self.value_name(value), self.type_of(value)
        else:
            expr, ty = self.place_c.get(parts[0], ""), self.roots[parts[0]]
        for seg in parts[1:]:
            ty = self.view(ty)
            s = self.shape(ty)
            if isinstance(s, Enum):
                expr, ty = self.payload(expr, ty, seg), dict(s.variants)[seg]
            else:
                member, ty = self.member_of(s, seg)
                expr = f"{expr}.{member}"
        return expr, ty

    def view(self, ty: Type) -> Type:
        """Opened views share the package's representation."""
        return ty.body if isinstance(ty, Exists) else ty

    def operand(self, arg: str) -> str:
        """A call argument: a value, a lent place's address, or a raw target."""
        if arg.startswith("&*"):
            return self.value_name(arg[2:])
        if arg.startswith("&"):
            return f"&{self.place(arg)[0]}"
        return self.value_name(arg)

    # -------------------------------------------------------- instructions --

    def instr(self, instr: Instr) -> None:
        op, a = instr.op, instr.args
        # A pure instruction is written when the C reads one of its results: one that is live and not of a
        # zero-size type (so never `static_brand`, `fresh`, nor `pack` and `open` of a place).
        if op in PURE and not any(self.is_live(name) and not self.erased(self.type_of(name)) for name in instr.lhs):
            return
        here, plan = (self.label, self.index), self.plan
        if here in plan.in_place:
            self.lend_in_place(instr, plan.in_place[here])
            return
        if here in plan.fused or op in ("load", "open", "pack", "construct") and (self.label, instr.lhs[0]) in plan.resident:
            return
        if op == "destruct" and (self.label, self.index - 1) in plan.in_place:
            self.handed_back(instr, plan.in_place[self.label, self.index - 1])
            return
        if here in plan.unwritten:
            return
        if op == "store" and (self.label, self.index - 1) in plan.fused:
            self.store_in_place(a[0], self.blocks[self.label].instrs[self.index - 1])
            return
        dst = self.value_name(instr.lhs[0]) if len(instr.lhs) == 1 and not self.erased(self.type_of(instr.lhs[0])) else None
        substitute_type = self.substitute_type
        if op == "const":
            assert isinstance(instr.ty, Prim) and instr.lit is not None
            self.line(f"{dst} = {self.literal(instr.ty, instr.lit)};")
        elif op == "copy":
            self.aliases[self.label, instr.lhs[0]] = self.value_name(a[0])
        elif op == "load" and a[0] in self.plan.forwarded:
            self.aliases[self.label, instr.lhs[0]] = self.forwards[a[0]]
        elif op == "store" and a[0] in self.plan.forwarded:
            if not self.erased(self.type_of(a[1])):
                self.forwards[a[0]] = self.value_name(a[1])
        elif op == "construct":
            assert dst is not None
            self.construct(dst, self.type_operand(instr), a, self.away(instr.lhs[0]))
        elif op == "destruct":
            self.destruct(instr)
        elif op == "construct_variant":
            assert instr.lit is not None and dst is not None
            ty = self.type_operand(instr)
            payload = dict(self.types.variants(erase(ty, self.types)))[instr.lit]
            value = None if self.erased(payload) else self.value_name(a[0])
            for statement in self.set_variant(dst, ty, instr.lit, value):
                self.line(statement)
        elif op == "variant_tag":
            source = self.place(a[0])[0] if a[0].startswith("&") else self.value_name(a[0])
            self.line(f"{dst} = {self.tag(source, self.type_operand(instr))};")
        elif op == "to_ptr":
            self.line(f"{dst} = {self.value_name(a[0])};")
        elif op == "int_cast":
            self.line(f"{dst} = {self.convert(self.value_name(a[0]), self.type_operand(instr))};")
        elif op == "cast":
            self.has_memory_form(self.type_of(a[0]), instr)
            self.has_memory_form(self.type_operand(instr), instr)
            assert dst is not None
            self.cast(dst, self.value_name(a[0]), self.type_of(a[0]), self.type_operand(instr))
        elif op in C_OPS or op in ("shl", "shr"):
            assert dst is not None
            self.primitive(dst, op, a)
        elif op == "call":
            self.call(instr)
        elif op == "trait_call":
            self.trait_call(instr)
        elif op == "fn":
            self.line(f"{dst} = {self.function_address(instr)};")
        elif op == "alloca":
            self.has_memory_form(self.type_operand(instr), instr)
            ct = self.ctype(self.type_operand(instr))
            self.line(f"{dst} = ({ct}*)__builtin_alloca(sizeof({ct}));")
        elif op == "sizeof":
            ct = self.ctype(self.type_operand(instr))
            self.line(f"{dst} = UINT64_C(0);" if ct == "void" else f"{dst} = (uintptr_t)sizeof({ct});")
        elif op == "data":
            self.line(f"{dst} = &{self.data_symbol(a[0])};")
        elif op == "trap":
            self.line("KUR_TRAP();")
        elif op in ("pack", "open"):
            self.line(f"{dst} = {self.value_name(a[0])};")
        elif op == "ptr_load":
            pointer = self.type_of(a[0])
            assert isinstance(pointer, Ptr)
            self.has_memory_form(pointer.inner, instr)
            self.line(f"{dst} = *{self.value_name(a[0])};")
        elif op == "ptr_load_volatile":
            pointer = self.type_of(a[0])
            assert isinstance(pointer, Ptr)
            self.has_memory_form(pointer.inner, instr)
            self.line(f"{dst} = *({self.ctype(pointer.inner)} volatile*){self.value_name(a[0])};")
        elif op in ("ptr_store", "ptr_store_volatile"):
            pointer = self.type_of(a[0])
            assert isinstance(pointer, Ptr)
            self.has_memory_form(pointer.inner, instr)
            qual = f"({self.ctype(pointer.inner)} volatile*)" if op.endswith("volatile") else ""
            self.line(f"*{qual}{self.value_name(a[0])} = {self.value_name(a[1])};")
        elif op in ATOMIC_OPS:
            self.atomic(instr, dst)
        elif op == "load":
            self.line(f"{dst} = {self.place(a[0])[0]};")
        elif op == "store":
            path, ty = self.place(a[0])
            if not self.erased(ty):
                if root(a[0]) in self.params:
                    self.written.add((self.symbol, self.params.index(root(a[0]))))
                self.line(f"{path} = {self.value_name(a[1])};")
        elif op == "foreach":
            self.foreach(instr, dst)
        else:
            raise KursError(f"no C recipe for {op}")

    def has_memory_form(self, ty: Type, instr: Instr) -> None:
        """The checker's zero-size rule, for types a specialization chose."""
        if self.types.zero_size(ty):
            raise KursError("a zero-size type has no memory form", instr.pos)

    def type_operand(self, instr: Instr) -> Type:
        assert instr.ty is not None
        return self.substitute_type(instr.ty)

    def construct(self, dst: str, ty: Type, args: Sequence[str], away: Optional[dict[int, str]] = None) -> None:
        """Assigns each component to its member of `dst`, but the components `away` sends elsewhere: to a
        place path where a homed component ends up, or through an out pointer (`*out`), after the others."""
        s = self.shape(ty)
        if isinstance(s, Array):
            for k in range(s.length):
                self.line(f"{dst}.items[{k}] = {self.value_name(args[min(k, len(args) - 1)])};")
            return
        away = away or {}
        for k, ((member, t), arg) in enumerate(zip(self.members(s), args)):
            if not self.erased(t) and k not in away:
                self.line(f"{dst}.{member} = {self.value_name(arg)};")
        for k, target in away.items():
            if target.startswith("*"):
                self.line(f"{target} = {self.value_name(args[k])};")
            else:
                self.write_home(target, self.value_name(args[k]), "")

    def away(self, name: str) -> dict[int, str]:
        """Where the components of the value `name` that a construction or a call makes go instead of its
        variable: the homed ones of a value shared into a homed parameter (`Plan.home_params`) to their
        place paths; when the function writes elements of its result through out pointers, and the value
        is returned and read nothing else, those elements to `*out` (then marked as there)."""
        key = (self.label, name)
        if key in self.plan.made_home:
            return dict(self.plan.homed[self.plan.shared[key]])
        outs = self.out_params.get(self.symbol)
        if not outs or not self.returned_alone(name):
            return {}
        for k in self.outs[self.symbol]:
            self.at_out.add((self.label, name, k))
        return {k: f"*{cname}" for k, (_, cname) in zip(self.outs[self.symbol], outs)}

    def returned_alone(self, name: str) -> bool:
        """The value `name` of the current block is what it returns, directly or through return joins, and
        the block reads it nowhere else."""
        block = self.blocks[self.label]
        term = block.term
        if isinstance(term, Ret):
            returned = term.value
        elif isinstance(term, Jmp) and term.edge.target in self.plan.return_index:
            returned = term.edge.args[self.plan.return_index[term.edge.target]]
        else:
            return False
        reads = [n for instr in block.instrs for n in instr_reads(instr)] + term_reads(term) + \
            [n for edge, _ in term_edges(term) for n in edge.args]
        return returned == name and reads.count(name) == 1

    def destruct(self, instr: Instr) -> None:
        arg = instr.args[0]
        if arg.startswith("&"):
            source, ty = self.place(arg)
        else:
            source, ty = self.value_name(arg), self.type_of(arg)
        representation = erase(ty, self.types)
        pointer = isinstance(representation, Ptr)
        component_type = representation.inner if isinstance(representation, Ptr) else ty
        s = self.shape(component_type)
        if isinstance(s, Array):
            access = [(f"items[{k}]", s.elem) for k in range(s.length)]
        else:
            access = self.members(s)
        for name, (member, t) in zip(instr.lhs, access):
            if not self.is_live(name) or self.erased(self.type_of(name)) or (self.label, name) in self.plan.resident:
                continue
            # A pointer to a zero-size component is the pointer to the whole: they may share an address.
            if pointer:
                self.line(f"{self.value_name(name)} = (void*){source};" if self.erased(t)
                          else f"{self.value_name(name)} = KUR_FIELD({source}, {member});")
            else:
                self.line(f"{self.value_name(name)} = {source}.{member};")

    def convert(self, expr: str, dst: Type) -> str:
        """Integer conversion by truncation or sign extension."""
        assert isinstance(dst, Prim)
        wide = "uintptr_t" if dst.name in POINTER_SIZED else "uint64_t"
        if dst.name in SIGNED:
            return f"kur_dec_i{SIGNED[dst.name]}(({wide})({expr}))"
        return f"({C_PRIMS[dst.name]})(({wide})({expr}))"

    def cast(self, dst: str, expr: str, src: Type, dst_ty: Type) -> None:
        s, d = self.shape(src), self.shape(dst_ty)
        cd = self.ctype(dst_ty)
        if isinstance(s, (Ptr, NonNull)) or isinstance(d, (Ptr, NonNull)):
            if isinstance(s, (Ptr, NonNull)) and isinstance(d, (Ptr, NonNull)):
                self.line(f"{dst} = ({cd}){expr};")
                if isinstance(d, NonNull) and not isinstance(s, NonNull):
                    # The cast's obligation: the pointer is not null. Telling the C compiler lets it fold
                    # the niche checks of an enum made from it.
                    self.line(f"if ({dst} == 0) __builtin_unreachable();")
            elif (isinstance(s, Ptr) and isinstance(d, Prim) and d.name in INTEGERS or
                  isinstance(d, Ptr) and isinstance(s, Prim) and s.name in INTEGERS):
                value = f"(uintptr_t){expr}"
                converted = self.convert(value, d) if isinstance(d, Prim) else f"({cd}){value}"
                self.line(f"{dst} = {converted};")
            else:
                raise KursError("backend cast supports raw pointers only with pointers or integers")
        elif isinstance(s, Prim) and isinstance(d, Prim):
            if s.name == "bool" or d.name == "bool" or s.name in FLOATS and d.name in FLOATS:
                self.line(f"{dst} = ({cd}){expr};")
            elif (s.name in FLOATS) != (d.name in FLOATS):
                # A pointer-sized integer's width is the target's: the assertion below checks it.
                widths = {**WIDTHS, "f32": 32, "f64": 64}
                if s.name in widths and d.name in widths and widths[s.name] != widths[d.name]:
                    raise KursError("backend integer/float reinterpretation requires equal widths")
                self.reinterpret(dst, expr)
            else:
                self.line(f"{dst} = {self.convert(expr, d)};")
        else:
            # Layout belongs to C. Never silently truncate a source or leave a
            # destination tail uninitialized; exact size is a target obligation.
            self.reinterpret(dst, expr)

    def reinterpret(self, dst: str, expr: str) -> None:
        self.line(f'_Static_assert(sizeof({dst}) == sizeof({expr}), "KURS cast requires equal representation sizes");')
        self.line(f"__builtin_memcpy(&{dst}, &{expr}, sizeof({dst}));")

    def primitive(self, dst: str, op: str, args: tuple[str, ...]) -> None:
        x, y = (self.value_name(a) for a in args)
        t = self.shape(self.type_of(args[0]))
        if isinstance(t, (Ptr, NonNull)):
            self.line(f"{dst} = ((uintptr_t){x} {C_OPS[op]} (uintptr_t){y});")
            return
        assert isinstance(t, Prim)
        name = t.name
        if name not in INTEGERS or op in ("eq", "ne", "lt", "le", "gt", "ge"):
            self.line(f"{dst} = ({x} {C_OPS[op]} {y});")
            return
        ct = C_PRIMS[name]
        signed = name in SIGNED
        # The arithmetic is done unsigned, in `wide`, of `bits` bits: 64, or a pointer's width for
        # `usize` and `isize`.
        if name in POINTER_SIZED:
            wide, wide_max, bits, mask = "uintptr_t", "UINTPTR_MAX", "KUR_PTR_BITS", "(KUR_PTR_BITS - 1)"
            bits_of = f"(uintptr_t)({x})"
        else:
            width = WIDTHS[name]
            wide, wide_max, bits, mask = "uint64_t", "UINT64_MAX", str(width), str(width - 1)
            bits_of = f"(uint64_t)(uint{width}_t)({x})" if width < 64 or signed else f"(uint64_t)({x})"
        def wrap(expr: str) -> str:
            return f"kur_dec_i{SIGNED[name]}({expr})" if signed else f"({ct})({expr})"
        if op in ("div", "mod"):
            self.line(f"if ({y} == 0) KUR_TRAP();")
            if signed:
                minus = wrap(f"({wide})0 - ({wide})({x})") if op == "div" else "0"
                self.line(f"{dst} = ({y} == -1) ? {minus} : ({ct})({x} {C_OPS[op]} {y});")
            else:
                self.line(f"{dst} = ({ct})(({wide}){x} {C_OPS[op]} ({wide}){y});")
            return
        if op in ("shl", "shr"):
            amount = f"(({wide})({y}) & {mask})"
            if op == "shl":
                self.line(f"{dst} = {wrap(f'{bits_of} << {amount}')};")
            elif not signed:
                self.line(f"{dst} = {wrap(f'{bits_of} >> {amount}')};")
            else:
                fill = f"({x} < 0 && {amount} ? ({wide_max} << ({bits} - {amount})) : 0)"
                self.line(f"{dst} = {wrap(f'({bits_of} >> {amount}) | {fill}')};")
            return
        self.line(f"{dst} = {wrap(f'({wide})({x}) {C_OPS[op]} ({wide})({y})')};")

    def call(self, instr: Instr) -> None:
        callee, substitute_type = instr.args[0], self.substitute_type
        if callee.startswith("%"):
            fn = erase(self.type_of(callee), self.types)
            assert isinstance(fn, Fn)
            self.lend(None, instr.args[1:])
            self.emit_call(instr, self.value_name(callee), fn.ret, {}, instr.args[1:])
        elif callee in self.program.externs:
            ext = self.program.externs[callee]
            assert ext.ret is not None
            self.emit_call(instr, callee, ext.ret, {}, instr.args[1:])
        else:
            fun = self.program.funs[callee]
            sub = {p.name: substitute_type(t) for p, t in zip(fun.params, instr.tys)}
            symbol = self.request(fun, sub)
            self.lend(symbol, instr.args[1:])
            self.emit_call(instr, symbol, fun.ret, sub, instr.args[1:], self.by_ref[symbol], self.outs[symbol])

    def trait_call(self, instr: Instr) -> None:
        a = instr.args
        fun, sub = self.resolve_trait(a[0], a[1], tuple(self.substitute_type(t) for t in instr.tys))
        symbol = self.request(fun, sub)
        self.lend(symbol, a[2:])
        self.emit_call(instr, symbol, fun.ret, sub, a[2:], self.by_ref[symbol], self.outs[symbol])

    def lend_in_place(self, instr: Instr, paths: tuple[str, ...]) -> None:
        """A lend adapter's call (`Plan.residences`) as its own call, lending the callee the places
        `paths` the borrowed values lie in instead of the adapter's places, its result assigned to the
        result of the destruct that follows."""
        entry = self.adapters[instr.args[0]]
        assert entry is not None
        adapter, fun = entry
        block = fun.blocks[0]
        inner = block.instrs[len(fun.locals)]
        actual = {name: arg for (name, _), arg in zip(block.params, instr.args[1:])}
        actual.update((slot, path if path.startswith("&") else "&" + path) for (slot, _), path in zip(fun.locals, paths))
        sub = {p.name: t for p, t in zip(fun.params, instr.tys)}
        after = self.blocks[self.label].instrs[self.index + 1]
        call = Instr((after.lhs[-1],) if adapter.returns else instr.lhs, inner.op,
                     tuple(actual.get(arg, arg) for arg in inner.args), tys=tuple(subst(t, sub) for t in inner.tys),
                     unsafe=inner.unsafe, pos=inner.pos)
        if call.op == "call":
            self.call(call)
        else:
            self.trait_call(call)

    def handed_back(self, instr: Instr, paths: tuple[str, ...]) -> None:
        """The destruct after a lend in place: the call assigned the result, and each value handed back
        lies in its place, or is copied out of it when it cannot be read there."""
        for name, path in zip(instr.lhs, paths):
            if self.is_live(name) and not self.erased(self.type_of(name)) and (self.label, name) not in self.plan.resident:
                self.line(f"{self.value_name(name)} = {self.place(path)[0]};")

    def store_in_place(self, target: str, construct: Instr) -> None:
        """A store of the construction just before it (`Plan.fused`): each component written into its
        member of the place, except the ones that lie there already."""
        dst, _ = self.place(target)
        ty = self.type_operand(construct)
        labels = self.labels_of(ty)
        assert labels is not None
        for (member, t), arg, part in zip(self.members(self.shape(ty)), construct.args, labels):
            if self.erased(t) or self.plan.resident.get((self.label, arg)) == f"{target}.{part}":
                continue
            if root(target) in self.params:
                self.written.add((self.symbol, self.params.index(root(target))))
            self.line(f"{dst}.{member} = {self.value_name(arg)};")

    def operands(self, args: Sequence[str], refs: Sequence[bool] = ()) -> str:
        """The C arguments of a call: those that pass something; an aggregate the callee takes by
        pointer (`refs`) as the address of where it lies (`by_address`)."""
        return ", ".join(self.by_address(a, args) if k < len(refs) and refs[k] else self.operand(a)
                         for k, a in enumerate(args) if not self.erased_arg(a))

    def by_address(self, arg: str, args: Sequence[str]) -> str:
        """The address of the value `arg` for a callee that reads it through a pointer: where it lies,
        when nothing can write there while the callee runs, else a copy of it. A C variable is written by
        its own function alone, which is suspended; a local place by its function or a lend of it, so
        not one the same call lends; memory under a place parameter is copied."""
        expr = self.value_name(arg)
        path = self.plan.resident.get((self.label, arg), "")
        if path.startswith("&") and (root(path) in self.params or
                                     any(a.startswith("&") and not a.startswith("&*") and overlap(a, path) for a in args) or
                                     any(overlap(q, path) for q in self.out_paths)):
            copy = allocate(f"arg_{self.scratch}", self.taken)
            self.scratch += 1
            self.temps.append(f"{self.ctype(self.type_of(arg))} {copy};")
            self.line(f"{copy} = {expr};")
            return f"&{copy}"
        return f"&{expr}"

    def emit_call(self, instr: Instr, callee: str, ret: Type, sub: Mapping[str, Type], args: tuple[str, ...],
                  refs: Sequence[bool] = (), outs: Sequence[int] = ()) -> None:
        """The call, its result assigned. A callee writing elements of its result through out pointers is
        passed, for each: the place path a homed component ends up at, the caller's own out pointer for
        that element when the caller returns the result alone (`returning`), else the member of the
        result's variable (or a scratch local when the result is dead); the rest of the result is the C
        result."""
        name = instr.lhs[0]
        result = subst(ret, sub)
        live = not self.erased(result) and self.is_live(name)
        dst = self.value_name(name) if live else ""
        homed = self.away(name) if live and (self.label, name) in self.plan.made_home else {}
        members = self.members(self.shape(result)) if outs else []
        pointers = []
        own = dict(zip(self.outs[self.symbol], self.out_params.get(self.symbol, ()))) if self.returning else {}
        for k in outs:
            if k in homed:
                pointers.append(f"&{self.place(homed[k])[0]}")
                if root(homed[k]) in self.params:
                    self.written.add((self.symbol, self.params.index(root(homed[k]))))
            elif k in own and own[k][0] == self.ctype(self.element(result, k)):
                pointers.append(own[k][1])
                self.at_out.add((self.label, name, k))
            elif live:
                pointers.append(f"&{dst}.{members[k][0]}")
            else:
                temp = allocate(f"arg_{self.scratch}", self.taken)
                self.scratch += 1
                self.temps.append(f"{self.ctype(self.element(result, k))} {temp};")
                pointers.append(f"&{temp}")
        self.out_paths = [homed[k] for k in outs if k in homed]
        operands = self.operands(args, refs)
        self.out_paths = []
        call = f"{callee}({', '.join(x for x in [operands, *pointers] if x)})"
        if outs:
            reduced = self.reduced(result, outs)
            rest = [(m, t) for k, (m, t) in enumerate(members) if k not in outs]
            if not live or self.erased(reduced):
                self.line(f"{call};")
            else:
                temp = allocate(f"ret_{self.scratch}", self.taken)
                self.scratch += 1
                self.temps.append(f"{self.ctype(reduced)} {temp};")
                self.line(f"{temp} = {call};")
                for (r, _), (m, t) in zip(self.members(self.shape(reduced)), rest):
                    if not self.erased(t):
                        self.line(f"{dst}.{m} = {temp}.{r};")
        elif not live:
            self.line(f"{call};")
        else:
            self.line(f"{dst} = {call};")
        # A homed component the callee does not write through a pointer goes to its place after the call.
        for k, path in homed.items():
            if k not in outs:
                self.write_home(path, f"{dst}.{self.members(self.shape(result))[k][0]}", "")

    def lend(self, callee: Optional[str], args: Sequence[str]) -> None:
        """Records where each place lent to `callee` lies, for `qualifiers`; `None` for a call through a
        function value, whose place parameters are neither `const` nor `restrict`. A place of a zero-size
        type passes nothing."""
        for k, arg in enumerate(args):
            if not arg.startswith("&") or self.erased_arg(arg):
                continue
            if arg.startswith("&*"):
                kind, j = "raw", -1
            elif root(arg) in self.params:
                kind, j = "param", self.params.index(root(arg))
            else:
                kind, j = "local", -1
            if callee is not None:
                self.lends.append((callee, k, kind, self.symbol, j))
            elif kind == "param":
                self.written.add((self.symbol, j))

    def atomic(self, instr: Instr, dst: Optional[str]) -> None:
        assert instr.lit is not None
        kind, args = ATOMIC_OPS[instr.op], [self.value_name(a) for a in instr.args]
        if kind != "fence":
            pointer = self.type_of(instr.args[0])
            assert isinstance(pointer, Ptr)
            item = self.shape(pointer.inner)
            integer = isinstance(item, Prim) and item.name in INTEGERS
            transfer = isinstance(item, Ptr) or isinstance(item, Prim) and item.name == "bool"
            if not integer and (kind.startswith("fetch_") or not transfer):
                raise KursError(f"backend atomic {kind} does not support this pointee type; "
                                "fetch operations require integers, transfers require integers, bool or raw pointers",
                                instr.pos)
        if kind == "fence":
            self.line(f"__atomic_thread_fence({C_ORDER[instr.lit]});")
        elif kind == "load":
            self.line(f"{dst} = __atomic_load_n({args[0]}, {C_ORDER[instr.lit]});")
        elif kind == "store":
            self.line(f"__atomic_store_n({args[0]}, {args[1]}, {C_ORDER[instr.lit]});")
        elif kind == "swap":
            self.line(f"{dst} = __atomic_exchange_n({args[0]}, {args[1]}, {C_ORDER[instr.lit]});")
        elif kind == "compare_exchange":
            success, _, failure = instr.lit.partition("/")
            self.line(f"{dst}.e0 = {args[1]};")
            self.line(f"{dst}.e1 = __atomic_compare_exchange_n({args[0]}, &{dst}.e0, {args[2]}, 0, "
                      f"{C_ORDER[success]}, {C_ORDER[failure]});")
        else:
            self.line(f"{dst} = __atomic_{kind}({args[0]}, {args[1]}, {C_ORDER[instr.lit]});")

    def foreach(self, instr: Instr, dst: Optional[str]) -> None:
        """Each component of the result is the mapper applied to the source's component: a component
        of a zero-size type is passed as nothing, and a result component of one is not assigned (the call
        is still made)."""
        original = instr.ty
        assert isinstance(original, Foreach)
        t, renamed = subst_foreach(original, self.sub)
        source = "" if self.erased(self.type_of(instr.args[0])) else self.value_name(instr.args[0])
        mapper = self.program.funs[instr.args[1]]
        binders = []
        for name in foreach_binders(original):
            binder = renamed.get(name, Named(name))
            assert isinstance(binder, Named) and not binder.args
            binders.append(binder.name)
        own = [self.substitute_type(x) for x in instr.tys]
        if len(mapper.params) != len(binders) + len(own):
            raise KursError("foreach mapper parameter partition disagrees", instr.pos)
        # A captured value the mapper takes by pointer (an aggregate of more than four words, as `by_ref`
        # decides for its parameter of that type) is passed by its address.
        caps = [self.by_address(c, ()) if self.by_pointer(self.type_of(c)) else self.value_name(c)
                for c in instr.args[2:] if not self.erased(self.type_of(c))]
        values = [self.value_name(c) for c in instr.args[2:] if not self.erased(self.type_of(c))]
        skeleton, rows = self.types.rows(t.source)
        pointer = isinstance(self.shape(t.source), Ptr) and not isinstance(t.source, Foreach)
        out_shape = self.shape(t)

        def apply(row: ForeachRow, component: str) -> str:
            _, item, extra = row
            bound = self.types.bind(t, item, extra)
            sub = {p.name: bound[b] for p, b in zip(mapper.params, binders)}
            sub.update(zip((p.name for p in mapper.params[len(binders):]), own))
            symbol = self.request(mapper, sub)
            if self.outs[symbol]:
                # A mapper writing elements of its result through out pointers is called through its by-value
                # entry, which returns the whole result.
                self.address_taken.add(symbol)
                if symbol not in self.thunks:
                    self.thunks[symbol] = allocate(f"{symbol}_value", self.taken)
                arguments = ([] if not pointer and self.erased(item) else [component]) + values
                return f"{self.thunks[symbol]}({', '.join(arguments)})"
            passed = f"&{component}" if self.by_ref[symbol][0] else component
            arguments = ([] if not pointer and self.erased(item) else [passed]) + caps
            return f"{symbol}({', '.join(arguments)})"

        def assign(target: str, result: Type, call: str) -> str:
            return f"{call};" if self.erased(result) else f"{target} = {call};"

        if isinstance(skeleton, Enum):
            assert isinstance(out_shape, Enum) and dst is not None
            source_type = self.type_of(instr.args[0])
            self.line(f"switch ({self.tag(source, source_type)}) {{")
            for k, (row, (_, result)) in enumerate(zip(rows, out_shape.variants)):
                call = apply(row, self.payload(source, source_type, row[0]))
                if self.erased(result):
                    self.line(f"case {k}: {call}; {' '.join(self.set_variant(dst, t, row[0], None))} break;")
                elif self.niche_layout(t) is not None:
                    self.line(f"case {k}: {dst} = {call}; break;")
                else:
                    self.line(f"case {k}: {' '.join(self.set_variant(dst, t, row[0], call))} break;")
            self.line("default: KUR_TRAP();\n  }")
            return
        out = [(f"items[{k}]", out_shape.elem) for k in range(len(rows))] if isinstance(out_shape, Array) \
            else self.members(out_shape)
        if skeleton is None:
            component = f"{source}.e0" if isinstance(t.source, Foreach) else source
            self.line(assign(f"{dst}.{out[0][0]}", out[0][1], apply(rows[0], component)))
            return
        if isinstance(skeleton, Array):
            src_access = [f"items[{k}]" for k in range(len(rows))]
        else:
            src_access = [m for m, _ in self.members(skeleton)]
        if isinstance(t.source, Foreach):
            inner_shape = self.shape(t.source)
            src_access = ([f"items[{k}]" for k in range(len(rows))] if isinstance(inner_shape, Array)
                          else [m for m, _ in self.members(inner_shape)])
        for row, s_member, (d_member, result) in zip(rows, src_access, out):
            # A pointer to a zero-size component is the pointer to the whole, as `destruct` makes it.
            if pointer:
                component = f"(void*){source}" if self.erased(row[1]) else f"KUR_FIELD({source}, {s_member})"
            else:
                component = f"{source}.{s_member}"
            self.line(assign(f"{dst}.{d_member}", result, apply(row, component)))

    # -------------------------------------------------------- terminators --

    def term(self, term: Terminator) -> None:
        match term:
            case Ret():
                if self.erased(self.type_of(term.value)):
                    self.line("return;")
                else:
                    self.give_back(term.value, self.value_name(term.value), "")
            case Jmp():
                self.edge(term.edge)
            case Jnz():
                self.line(f"if ({self.value_name(term.cond)}) {{")
                self.edge(term.then, indent="  ")
                self.line("} else {")
                self.edge(term.other, indent="  ")
                self.line("}")
            case Switch():
                self.switch(term)
            case Unreachable():
                instrs = self.blocks[self.label].instrs
                if not instrs or instrs[-1].op != "trap":  # a `trap` has written its own
                    self.line("KUR_TRAP();")

    def edge(self, edge: Edge, payload: Optional[str] = None, source: str = "", indent: str = "") -> None:
        """A jump assigns the target's live parameters (`source` is the C variable `payload` reads); a
        jump to a return join returns the value it passes there. A value of a zero-size type is `""`:
        nothing is assigned or returned."""
        target = self.blocks[edge.target]
        if payload is not None and (target.label, target.params[0][0]) in self.plan.arrived:
            payload = ""
        values = ([(payload, source)] if payload is not None else []) + \
            [("", "") if self.erased(self.type_of(a)) else (self.value_name(a), self.value_base(a)) for a in edge.args]
        if target.label in self.plan.return_index:
            index = self.plan.return_index[target.label]
            value = values[index][0]
            names = ([None] if payload is not None else []) + list(edge.args)
            if value:
                self.give_back(names[index], value, indent)
            else:
                self.line(f"{indent}return;")
            return
        moves, homes = [], []
        for (n, t), (value, base), name in zip(target.params, values, ([None] if payload is not None else []) + list(edge.args)):
            if not value or not self.plan.is_live(target.label, n):
                continue
            homed = self.plan.homed.get((target.label, n), {})
            if not homed:
                moves.append((self.cname(target.label, n), t, False, value, base))
                continue
            # The components written where they end up (`Plan.home_params`): the others are assigned one by
            # one, and the homed ones after every other assignment, unless the value's making wrote them.
            for k, (member, mt) in enumerate(self.members(self.shape(self.substitute_type(t)))):
                if k not in homed and not self.erased(mt):
                    moves.append((f"{self.cname(target.label, n)}.{member}", mt, False, f"{value}.{member}", base))
                elif k in homed and (self.label, name) not in self.plan.made_home:
                    homes.append((homed[k], f"{value}.{member}"))
        self.assign(moves, indent)
        for path, value in homes:
            self.write_home(path, value, indent)
        self.line(f"{indent}goto {self.labels[target.label]};")

    def write_home(self, path: str, value: str, indent: str) -> None:
        """Writes `value` at the place path `path` where a homed component ends up, unless it lies there."""
        target = self.place(path)[0]
        if target != value:
            if root(path) in self.params:
                self.written.add((self.symbol, self.params.index(root(path))))
            self.line(f"{indent}{target} = {value};")

    def give_back(self, name: Optional[str], value: str, indent: str) -> None:
        """Returns the value `value` (of the SSA value `name`, if known): each element written through an
        out pointer is assigned to its pointee, unless it is there already, and the rest returned."""
        outs = self.out_params.get(self.symbol)
        if not outs:
            self.line(f"{indent}return {value};")
            return
        ks = self.outs[self.symbol]
        members = self.members(self.shape(self.ret_type))
        for k, (_, cname) in zip(ks, outs):
            if (self.label, name, k) not in self.at_out:
                self.line(f"{indent}*{cname} = {value}.{members[k][0]};")
        reduced = self.reduced(self.ret_type, ks)
        if self.erased(reduced):
            self.line(f"{indent}return;")
            return
        rest = [m for k, m in enumerate(members) if k not in ks]
        parts = [f".{r} = {value}.{m}" for (r, _), (m, t) in zip(self.members(self.shape(reduced)), rest) if not self.erased(t)]
        self.line(f"{indent}return ({self.ctype(reduced)}){{ {', '.join(parts)} }};")

    def assign(self, moves: Sequence[tuple[str, Type, bool, str, str]], indent: str) -> None:
        """Assigns each `(variable, type, whether a place's pointer, value, the variable it reads)` at once:
        one after another when no assignment overwrites what a later one reads, else through scratch locals."""
        moves = [move for move in moves if move[0] != move[3]]
        if any(moves[k][0].split(".")[0] == later[4] for k in range(len(moves)) for later in moves[k + 1:]):
            temps: list[str] = []
            for _, t, place, value, _ in moves:
                temp = allocate(f"edge_{self.scratch}", self.taken)
                self.scratch += 1
                self.temps.append(f"{self.ctype(self.substitute_type(t))}{'*' if place else ''} {temp};")
                self.line(f"{indent}{temp} = {value};")
                temps.append(temp)
            for move, temp in zip(moves, temps):
                self.line(f"{indent}{move[0]} = {temp};")
        else:
            for dst, _, _, value, _ in moves:
                self.line(f"{indent}{dst} = {value};")

    def switch(self, term: Switch) -> None:
        arg = term.scrutinee
        if arg.startswith("&"):
            source, _ = self.place(arg)
            base = source
        else:
            source, base = self.value_name(arg), self.value_base(arg)
        ty = self.substitute_type(term.ty)
        variants = self.types.variants(erase(ty, self.types))
        names = [n for n, _ in variants]
        self.line(f"switch ({self.tag(source, ty)}) {{")
        for arm in term.arms:
            index = names.index(arm.variant)
            self.line(f"case {index}:")
            if arg.startswith("&"):
                payload = None
            elif self.erased(variants[index][1]):
                payload = ""
            else:
                payload = self.payload(source, ty, arm.variant)
            self.line("  {")
            self.edge(arm.edge, payload, base, indent="    ")
            self.line("  }")
        self.line("default: KUR_TRAP();")
        self.line("}")

    # ---------------------------------------------------------- tail calls --

    def tail_call(self, call: Instr) -> bool:
        """A call whose result its block returns (`Plan.tail_call`), as a jump when the C allows one:

        - a call of the function being emitted, with the same specialization, assigns the call's
          arguments to the parameters and jumps to the entry block, unless it lends a local place, whose
          storage the next round would reuse;
        - a call of another Kursal function is `KUR_MUSTTAIL return f(...)` when Clang's `musttail`
          holds without question: the callee's C parameter and result types are the caller's, each a
          scalar (a C integer, float or pointer type), and the call lends no local place and the caller
          has no `alloca`, so no argument points into the caller's frame, which the tail call frees.

        Returns whether it wrote the call; otherwise the block is written as usual."""
        a = call.args
        if call.op == "call":
            if a[0].startswith("%") or a[0] in self.program.externs:
                return False
            fun = self.program.funs[a[0]]
            sub = {p.name: self.substitute_type(t) for p, t in zip(fun.params, call.tys)}
            args = a[1:]
        else:
            fun, sub = self.resolve_trait(a[0], a[1], tuple(self.substitute_type(t) for t in call.tys))
            args = a[2:]
        if any(arg.startswith("&") and not arg.startswith("&*") and root(arg) not in self.params for arg in args):
            return False
        symbol = self.request(fun, sub)
        if symbol == self.symbol:
            self.lend(symbol, args)
            entry = self.fun.blocks[0]
            moves = []
            for (name, ty), arg, (ctype, _, _) in zip(entry.params, args, self.heads[self.symbol][2]):
                if ctype == "void":
                    continue
                if name.startswith("&") and arg != name:
                    self.looping.add(self.symbol)
                    base = self.value_name(arg[2:]) if arg.startswith("&*") else self.cname(entry.label, root(arg))
                    moves.append((self.cname(entry.label, name), ty, True, self.operand(arg), base))
                elif not name.startswith("&") and self.plan.is_live(entry.label, name):
                    moves.append((self.cname(entry.label, name), ty, False, self.value_name(arg), self.value_base(arg)))
            self.assign(moves, "")
            self.line(f"goto {self.labels[entry.label]};")
            return True
        ret, _, params = self.heads[self.symbol]
        callee_ret = subst(fun.ret, sub)
        callee_ptypes = [(self.ctype(subst(t, sub)), k in fun.places) for k, t in enumerate(fun.ptypes)]
        callee_params = [ct + ("*" if place else "") for ct, place in callee_ptypes if ct != "void"]
        scalar = set(C_PRIMS.values())
        same = (not isinstance(callee_ret, Never) and self.ctype(callee_ret) == ret and
                callee_params == [ctype + ("*" if place else "") for ctype, _, place in params if ctype != "void"])
        if not same or self.plan.has_alloca() or \
                not all(t in scalar or t.endswith("*") for t in [ret, *callee_params]):
            return False
        self.lend(symbol, args)
        self.line(f"KUR_MUSTTAIL return {symbol}({self.operands(args)});")
        return True

    # ---------------------------------------------------------- qualifiers --

    def qualifiers(self) -> tuple[set[tuple[str, int]], set[tuple[str, int]]]:
        """The place parameters that are `const` and that are `restrict`, as the greatest sets closed under
        every lend in the program.

        A place parameter is `const` when its function stores into no path under it and lends it only to
        `const` place parameters. It is `restrict` when nothing reaches the place it designates during the
        call except through it: the place is a local of some caller, or under a `restrict` parameter of
        the caller, at every call. The core language makes that so for places lent by path: a place is no
        value, so a local place's C object has no address but the ones lends pass, and the places one call
        lends are disjoint (an enum's payloads are lent only under a known variant, so never two of them).
        It is not relied on for a raw target `&*p`, whose exclusivity while lent is the unsafe lender's
        unchecked obligation (lib/std/CONTRACTS.md, 2.0d); and it is not so for a function whose
        address is taken, which a call through a function value may lend anything; and for a function whose
        tail call to itself moves a place parameter to another place (`tail_call`), which would reach two
        places through one pointer in one execution of the C function."""
        heads = self.heads
        const = {(s, k) for s, (_, _, params) in heads.items() for k, (_, _, place) in enumerate(params)
                 if place and s not in self.address_taken and (s, k) not in self.written}
        restrict = {(s, k) for s, (_, _, params) in heads.items() for k, (_, _, place) in enumerate(params)
                    if place and s not in self.address_taken and s not in self.looping}
        changed = True
        while changed:
            changed = False
            for callee, k, kind, caller, j in self.lends:
                if (callee, k) in restrict and (kind == "raw" or kind == "param" and (caller, j) not in restrict):
                    restrict.discard((callee, k))
                    changed = True
                if kind == "param" and (caller, j) in const and (callee, k) not in const:
                    const.discard((caller, j))
                    changed = True
        return const, restrict


def emit_c(program: Program, checker: Checker, entry: Optional[str] = None) -> str:
    return Backend(program, checker).emit_program(entry)
