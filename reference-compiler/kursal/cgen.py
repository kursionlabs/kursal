"""Checked KURS to C, by the canonical profile: no optimization, one C local
per SSA definition, places as fixed C objects, parallel edge assignment.

Generic functions are specialized on demand by logical type arguments.
Representation erasure is separate from code-specialization identity."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Optional

from .core import (FLOATS, INTEGERS, UNIT, Array, Brand, Enum, Exists, Fn, Foreach, Ignored, Jmp, Jnz,
                   Named, Never, Param, Prim, Ptr, Ret, Skolem, Struct, Switch, Tuple, Unreachable,
                   Type, Program, Fun, Data, Instr, Edge, Terminator)
from .check import Checker
from .sexpr import Form, KursError, List, Str
from .types import foreach_binders, ForeachRow, Types, equal, map_children, subst, subst_foreach
from .termination import check_mono_termination
from .mangle import encode, mangle, path_segments, type_key

PRELUDE = """#include <stddef.h>
#include <stdint.h>
typedef struct { unsigned char dummy; } KurUnit;
#define KUR_TRAP() __builtin_trap()
/* A component's address by integer arithmetic: projection never dereferences, so it is defined for any
   pointer value, null and dangling included. The `__typeof__` operands are not evaluated. */
#define KUR_FIELD(p, m) ((__typeof__(&(p)->m))((uintptr_t)(p) + offsetof(__typeof__(*(p)), m)))
static inline int8_t kur_dec_i8(uint64_t b) { b &= 0xFF; return b < 0x80 ? (int8_t)b : (int8_t)((int64_t)b - 0x100); }
static inline int16_t kur_dec_i16(uint64_t b) { b &= 0xFFFF; return b < 0x8000 ? (int16_t)b : (int16_t)((int64_t)b - 0x10000); }
static inline int32_t kur_dec_i32(uint64_t b) { b &= 0xFFFFFFFF; return b < 0x80000000 ? (int32_t)b : (int32_t)((int64_t)b - 0x100000000); }
static inline int64_t kur_dec_i64(uint64_t b) { return b <= (uint64_t)INT64_MAX ? (int64_t)b : -INT64_C(1) - (int64_t)(UINT64_MAX - b); }
"""

C_PRIMS = {"bool": "_Bool", "f32": "float", "f64": "double", "usize": "uint64_t", "isize": "int64_t",
           **{n: f"{'u' if n[0] == 'u' else ''}int{w}_t" for n, w in INTEGERS.items() if n[1:].isdigit()}}
SIGNED = {n: (64 if n == "isize" else INTEGERS[n]) for n in INTEGERS if n[0] == "i"}
C_ORDER = {o: f"__ATOMIC_{o.upper()}" for o in ("relaxed", "acquire", "release", "acq_rel", "seq_cst")}
C_OPS = {"add": "+", "sub": "-", "mul": "*", "div": "/", "mod": "%", "bit_and": "&", "bit_or": "|",
         "bit_xor": "^", "eq": "==", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}


C_KEYWORDS = set("auto break case char const continue default do double else enum extern float for goto if "
                 "inline int long register restrict return short signed sizeof static struct switch typedef "
                 "union unsigned void volatile while _Alignas _Alignof _Atomic _Bool _Complex _Generic "
                 "_Imaginary _Noreturn _Static_assert _Thread_local".split())
RUNTIME_NAMES = {"KurUnit", "KUR_TRAP", *(f"kur_dec_i{n}" for n in (8, 16, 32, 64)),
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
        self.pending: dict[Type, Tuple | Struct | Array | Enum] = {}      # named but not yet defined aggregates
        self.forward: list[str] = []
        self.defs: list[str] = []
        self.symbols: dict[tuple[int, tuple[Type, ...]], str] = {}      # specialization key -> C symbol
        self.queue: list[tuple[Fun, Mapping[str, Type], str]] = []
        self.bodies: list[str] = []
        self.protos: list[str] = []
        self.datas: dict[str, tuple[str, Data]] = {}
        # One conservative pool for ordinary identifiers, including function
        # locals: later global declarations must not be shadowed by earlier locals.
        self.taken = C_KEYWORDS | RUNTIME_NAMES | {"main"} | {e.name for e in program.externs.values()}
        self.component_names: dict[tuple[str, str], str] = {}
        self.component_taken: set[str] = set()
        self.foreign_adapters: dict[str, str] = {}
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
        if isinstance(ty, Ptr):
            inner = erase(ty.inner, self.types)
            return "void*" if isinstance(inner, Never) else self.ctype(inner, False) + "*"
        if ty not in self.names:
            self.register(ty)
        if defined and ty in self.pending:
            self.define(ty)
        return self.names[ty]

    def register(self, ty: Type) -> None:
        s = self.types.shape(ty) if isinstance(ty, Named) else ty
        match s:
            case Tuple(elems=()) | Struct(fields=()):
                self.names[ty] = "KurUnit"
            case Fn(params=params, ret=ret, places=places):
                name = self.names[ty] = self.fresh_name("fn")
                # Function declarations may name forward-declared aggregates.
                # Define their fields after this typedef, which they may use.
                result = "void" if isinstance(ret, Never) else self.ctype(ret, False)
                plist = ", ".join(self.ctype(p, False) + ("*" if k in places else "") for k, p in enumerate(params)) or "void"
                self.defs.append(f"typedef {result} (*{name})({plist});")
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
                body = f"{self.ctype(elem)} items[{length}];" if length else "unsigned char dummy;"
            case Enum(variants=variants):
                members = " ".join(f"{self.ctype(f)} {self.variant(n)};" for n, f in variants)
                body = f"uint32_t tag; union {{ {members} }} payload;"
            case _:
                body = " ".join(f"{self.ctype(f)} {m};" for m, f in self.members(s))
        self.defs.append(f"struct {self.names[ty]} {{ {body} }};")

    def members(self, shape: Type) -> list[tuple[str, Type]]:
        """(C member, component type) of a struct or tuple shape, in order."""
        if isinstance(shape, Tuple):
            return [(f"e{i}", e) for i, e in enumerate(shape.elems)]
        assert isinstance(shape, Struct)
        return [(self.component(n, "f"), f) for n, f in shape.fields]

    def component(self, name: str, prefix: str) -> str:
        key = (prefix, name)
        if key not in self.component_names:
            self.component_names[key] = allocate(f"{prefix}_{readable(name)}", self.component_taken)
        return self.component_names[key]

    def variant(self, name: str) -> str:
        return self.component(name, "v")

    def shape(self, ty: Type) -> Type:
        return self.types.shape(erase(ty, self.types))

    def is_unit(self, ty: Type) -> bool:
        return self.ctype(ty) == "KurUnit"

    # ---------------------------------------------------- specialization --

    def request(self, fun: Fun, sub: Mapping[str, Type]) -> str:
        params = self.instance_params.get(id(fun), fun.params)
        if set(sub) != {p.name for p in params}:
            raise KursError(f"specialization arguments disagree for {fun.name}")
        args = tuple(sub[p.name] for p in params)
        # Logical alpha-equivalence, not representation equality. Capture-avoiding
        # substitution may freshen bound names on recursive requests.
        for (identity, previous), symbol in self.symbols.items():
            if identity == id(fun) and all(equal(a, b) for a, b in zip(args, previous)):
                return symbol
        key = (id(fun), args)
        path, function_id = self.function_ids[id(fun)]
        signature = Fn(fun.ptypes, fun.ret, params, fun.unsafe, fun.places)
        symbol = self.function_name(path, encode((function_id, type_key(signature),
                                                  *(type_key(t) for t in args))))
        self.symbols[key] = symbol
        self.queue.append((fun, dict(sub), symbol))
        return symbol

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
            return self.foreign_address(name)
        fun = self.program.funs[name]
        # Stable distinct rigid identities: not a wildcard and not static.
        sub = {p.name: Skolem(encode(("#retained", fun.name, str(k)))) if isinstance(t, Ignored)
               else self.substitute_type(t) for k, (p, t) in enumerate(zip(fun.params, instr.tys))}
        return self.request(fun, sub)

    def foreign_address(self, name: str) -> str:
        ext = self.program.externs[name]
        assert ext.ret is not None and ext.ptypes is not None
        if not self.is_unit(ext.ret):
            return name
        if name not in self.foreign_adapters:
            signature = Fn(ext.ptypes, ext.ret, unsafe=True)
            symbol = self.function_name(("adapter", *path_segments(name)),
                                        encode(("adapter", name, type_key(signature))))
            self.foreign_adapters[name] = symbol
            names = [allocate(f"a{k}", self.taken) for k in range(len(ext.ptypes))]
            params = ", ".join(f"{self.ctype(t)} {n}" for t, n in zip(ext.ptypes, names)) or "void"
            args = ", ".join(names)
            result = allocate("result", self.taken)
            head = f"static KurUnit {symbol}({params})"
            self.protos.append(head + ";")
            self.bodies.append(head + " {\n  " + name + "(" + args + ");\n"
                               f"  KurUnit {result} = {{ 0 }};\n  return {result};\n}}")
            return symbol
        return self.foreign_adapters[name]

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
                raise KursError(f"backend: foreign symbol {ext.name!r} is not a C identifier", ext.pos)
            if ext.name in RUNTIME_NAMES or ext.name.startswith(("__builtin_", "__atomic_")) or ext.name == "main" and entry is not None:
                raise KursError(f"backend: foreign symbol {ext.name!r} conflicts with the C runtime prelude or entry wrapper", ext.pos)
            assert ext.ret is not None
            if ext.ptypes is None:
                externs.append(f"extern {self.ctype(ext.ret)} {ext.name};")
                continue
            ret = "void" if self.is_foreign_void(ext.ret) else self.ctype(ext.ret)
            noreturn = "_Noreturn " if isinstance(ext.ret, Never) else ""
            params = ", ".join(self.ctype(p) for p in ext.ptypes) or "void"
            externs.append(f"{noreturn}{ret} {ext.name}({params});")
        main: Optional[str] = None
        if entry is not None:
            fun = self.program.funs.get(entry)
            i32 = Prim("i32")
            if fun is None or fun.params or fun.premises or fun.unsafe or fun.places or not equal(fun.ret, i32) or \
                    fun.ptypes not in ((), (i32, Ptr(Ptr(Prim("u8"))))):
                raise KursError(f"backend: the entry {entry} must be a safe, non-generic "
                                "`fn() -> i32` or `fn(i32, *(*u8)) -> i32` without premises")
            main = self.request(fun, {})
        else:
            # The roots are the functions with nothing left to choose or discharge. One with premises gets code
            # only when a checked call, which discharged them, requests it.
            for fun in self.program.funs.values():
                if not fun.params and not fun.premises:
                    self.request(fun, {})
        while self.queue:
            self.emit_function(*self.queue.pop(0))
        datas = [self.emit_data(d) for d in self.datas.values()]
        while self.pending:
            self.define(next(iter(self.pending)))
        out = [PRELUDE, *self.forward, *self.defs, *externs, *datas, *self.protos, *self.bodies]
        if entry is not None:
            assert main is not None
            fun = self.program.funs[entry]
            if fun.ptypes:
                out.append(f"int main(int argc, char **argv) {{ return (int){main}((int32_t)argc, "
                           f"({self.ctype(fun.ptypes[1])})argv); }}")
            else:
                out.append(f"int main(void) {{ return (int){main}(); }}")
        return "\n".join(out) + "\n"

    def is_foreign_void(self, ty: Type) -> bool:
        return isinstance(ty, Never) or self.is_unit(ty)

    # -------------------------------------------------------------- data --

    def data_symbol(self, name: str) -> str:
        if name not in self.datas:
            self.datas[name] = (allocate(f"d{len(self.datas)}_{readable(name)[-40:]}", self.taken), self.program.datas[name])
        return self.datas[name][0]

    def emit_data(self, entry: tuple[str, Data]) -> str:
        symbol, data = entry
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
            return "{ { " + ", ".join(values) + " } }" if s.length else "{ 0 }"
        if self.is_unit(ty):
            return "{ 0 }"
        return "{ " + ", ".join(self.initializer(f, part) for (_, f), part in zip(self.members(s), form)) + " }"

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
        self.fun, self.sub = fun, sub
        typed = self.checker.typed[id(fun)]
        substitute_type = self.substitute_type
        self.locals: dict[tuple[str, str], str] = {}
        self.lines: list[str] = []
        self.scratch = 0
        label_taken: set[str] = set()
        self.labels = {b.label: allocate(f"L_{readable(b.label)}", label_taken) for b in fun.blocks}
        self.decls: list[str] = []
        decls = self.decls
        entry = fun.blocks[0]
        self.place_c: dict[str, str] = {}
        cparams = []
        for name, ty in entry.params:
            cname = self.cname(entry.label, name)
            if name.startswith("&"):
                self.place_c[name] = f"(*{cname})"
                cparams.append(f"{self.ctype(substitute_type(ty))}* {cname}")
            else:
                cparams.append(f"{self.ctype(substitute_type(ty))} {cname}")
        for name, ty in fun.locals:
            cname = allocate("p_" + readable(name), self.taken)
            self.place_c[name] = cname
            decls.append(f"{self.ctype(substitute_type(ty))} {cname};")
        self.roots = {**{n: substitute_type(t) for n, t in entry.params if n.startswith("&")},
                      **{n: substitute_type(t) for n, t in fun.locals}}
        self.typed = typed
        for (label, name), ty in typed.items():
            if label == entry.label and any(name == n for n, _ in entry.params) or isinstance(substitute_type(ty), Never):
                continue
            decls.append(f"{self.ctype(substitute_type(ty))} {self.cname(label, name)};")
        self.blocks = {b.label: b for b in fun.blocks}
        for block in fun.blocks:
            self.label = block.label
            self.line(f"{self.labels[block.label]}: ;")
            for instr in block.instrs:
                self.instr(instr)
            self.term(block.term)
        ret = "void" if isinstance(substitute_type(fun.ret), Never) else self.ctype(substitute_type(fun.ret))
        head = f"{'_Noreturn ' if ret == 'void' else ''}static {ret} {symbol}({', '.join(cparams) or 'void'})"
        self.protos.append(head + ";")
        self.bodies.append(head + " {\n" + "".join(f"  {d}\n" for d in decls) +
                           "".join(f"  {line}\n" for line in self.lines) + "}\n")

    def substitute_type(self, ty: Type) -> Type:
        return subst(ty, self.sub)

    def cname(self, label: str, name: str) -> str:
        key = (label, name)
        if key not in self.locals:
            base = f"{readable(label)}_{readable(name)}"
            self.locals[key] = allocate(base, self.taken)
        return self.locals[key]

    def value_name(self, name: str) -> str:
        return self.cname(self.label, name)

    def type_of(self, name: str) -> Type:
        return self.substitute_type(self.typed[self.label, name])

    def line(self, text: str) -> None:
        self.lines.append(text)

    # -------------------------------------------------------------- places --

    def place(self, path: str) -> tuple[str, Type]:
        """The C lvalue and type of a place path."""
        parts = path.split(".")
        expr, ty = self.place_c[parts[0]], self.roots[parts[0]]
        for seg in parts[1:]:
            ty = self.view(ty)
            s = self.shape(ty)
            if isinstance(s, Enum):
                expr, ty = f"{expr}.payload.{self.variant(seg)}", dict(s.variants)[seg]
            else:
                assert isinstance(s, (Struct, Tuple))
                names = [n for n, _ in s.fields] if isinstance(s, Struct) else [str(i) for i in range(len(s.elems))]
                member, ty = dict(zip(names, self.members(s)))[seg]
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
        dst = self.value_name(instr.lhs[0]) if len(instr.lhs) == 1 else None
        substitute_type = self.substitute_type
        if op == "const":
            assert isinstance(instr.ty, Prim) and instr.lit is not None
            self.line(f"{dst} = {self.literal(instr.ty, instr.lit)};")
        elif op == "copy":
            self.line(f"{dst} = {self.value_name(a[0])};")
        elif op == "construct":
            assert dst is not None
            self.construct(dst, self.type_operand(instr), [self.value_name(x) for x in a])
        elif op == "destruct":
            self.destruct(instr)
        elif op == "construct_variant":
            assert instr.lit is not None
            variants = self.types.variants(erase(self.type_operand(instr), self.types))
            index = [n for n, _ in variants].index(instr.lit)
            self.line(f"{dst}.payload.{self.variant(instr.lit)} = {self.value_name(a[0])};")
            self.line(f"{dst}.tag = {index};")
        elif op == "variant_tag":
            source = self.place(a[0])[0] if a[0].startswith("&") else self.value_name(a[0])
            self.line(f"{dst} = {source}.tag;")
        elif op == "builtin::int_cast":
            self.line(f"{dst} = {self.convert(self.value_name(a[0]), self.type_operand(instr))};")
        elif op == "builtin::unsafe::cast":
            assert dst is not None
            self.cast(dst, self.value_name(a[0]), self.type_of(a[0]), self.type_operand(instr))
        elif op in C_OPS or op in ("shl", "shr"):
            assert dst is not None
            self.primitive(dst, op, a)
        elif op == "call":
            self.call(instr)
        elif op == "trait_call":
            fun, sub = self.resolve_trait(a[0], a[1], tuple(substitute_type(t) for t in instr.tys))
            symbol = self.request(fun, sub)
            self.emit_call(instr, symbol, fun.ret, sub, a[2:], foreign=False)
        elif op == "fn":
            self.line(f"{dst} = {self.function_address(instr)};")
        elif op == "alloca":
            self.has_memory_form(self.type_operand(instr), instr)
            ct = self.ctype(self.type_operand(instr))
            self.line(f"{dst} = ({ct}*)__builtin_alloca(sizeof({ct}));")
        elif op == "builtin::sizeof":
            self.has_memory_form(self.type_operand(instr), instr)
            self.line(f"{dst} = (uint64_t)sizeof({self.ctype(self.type_operand(instr))});")
        elif op == "data":
            self.line(f"{dst} = &{self.data_symbol(a[0])};")
        elif op in ("builtin::unsafe::static_brand", "builtin::fresh"):
            self.line(f"{dst}.dummy = 0;")
        elif op in ("pack", "open"):
            if a[0].startswith("&"):
                if dst:
                    self.line(f"{dst}.dummy = 0;")
            else:
                self.line(f"{dst} = {self.value_name(a[0])};")
        elif op == "builtin::unsafe::deref":
            pointer = self.type_of(a[0])
            assert isinstance(pointer, Ptr)
            self.has_memory_form(pointer.inner, instr)
            self.line(f"{dst} = *{self.value_name(a[0])};")
        elif op == "builtin::unsafe::load_volatile":
            pointer = self.type_of(a[0])
            assert isinstance(pointer, Ptr)
            self.has_memory_form(pointer.inner, instr)
            self.line(f"{dst} = *({self.ctype(pointer.inner)} volatile*){self.value_name(a[0])};")
        elif op in ("builtin::unsafe::store", "builtin::unsafe::store_volatile"):
            pointer = self.type_of(a[0])
            assert isinstance(pointer, Ptr)
            self.has_memory_form(pointer.inner, instr)
            qual = f"({self.ctype(pointer.inner)} volatile*)" if op.endswith("volatile") else ""
            self.line(f"*{qual}{self.value_name(a[0])} = {self.value_name(a[1])};")
            self.unit(dst)
        elif op.startswith("builtin::unsafe::atomic_"):
            self.atomic(instr, dst)
        elif op == "load":
            self.line(f"{dst} = {self.place(a[0])[0]};")
        elif op == "store":
            self.line(f"{self.place(a[0])[0]} = {self.value_name(a[1])};")
            self.unit(dst)
        elif op == "foreach":
            assert dst is not None
            self.foreach(instr, dst)
        else:
            raise KursError(f"no C recipe for {op}")

    def has_memory_form(self, ty: Type, instr: Instr) -> None:
        """The checker's zero-size rule, for types a specialization chose."""
        if self.types.zero_size(ty):
            raise KursError("a zero-size type has no memory form yet", instr.pos)

    def type_operand(self, instr: Instr) -> Type:
        assert instr.ty is not None
        return self.substitute_type(instr.ty)

    def unit(self, dst: Optional[str]) -> None:
        if dst:
            self.line(f"{dst}.dummy = 0;")

    def construct(self, dst: str, ty: Type, values: Sequence[str]) -> None:
        s = self.shape(ty)
        if self.is_unit(ty):
            self.line(f"{dst}.dummy = 0;")
        elif isinstance(s, Array):
            if not s.length:
                self.line(f"{dst}.dummy = 0;")
            for k in range(s.length):
                self.line(f"{dst}.items[{k}] = {values[min(k, len(values) - 1)]};")
        else:
            for (member, _), value in zip(self.members(s), values):
                self.line(f"{dst}.{member} = {value};")

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
            access = [f"items[{k}]" for k in range(s.length)]
        else:
            access = [m for m, _ in self.members(s)] if not self.is_unit(component_type) else []
        for name, member in zip(instr.lhs, access):
            self.line(f"{self.value_name(name)} = KUR_FIELD({source}, {member});" if pointer
                      else f"{self.value_name(name)} = {source}.{member};")

    def convert(self, expr: str, dst: Type) -> str:
        """Integer conversion by truncation or sign extension."""
        assert isinstance(dst, Prim)
        if dst.name in SIGNED:
            return f"kur_dec_i{SIGNED[dst.name]}((uint64_t)({expr}))"
        return f"({C_PRIMS[dst.name]})((uint64_t)({expr}))"

    def cast(self, dst: str, expr: str, src: Type, dst_ty: Type) -> None:
        s, d = self.shape(src), self.shape(dst_ty)
        cd = self.ctype(dst_ty)
        if isinstance(s, Never) or isinstance(d, Never):
            raise KursError("backend cast does not support a never representation")
        if isinstance(s, Ptr) or isinstance(d, Ptr):
            if isinstance(s, Ptr) and isinstance(d, Ptr):
                self.line(f"{dst} = ({cd}){expr};")
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
                widths = {**INTEGERS, "f32": 32, "f64": 64}
                if widths[s.name] != widths[d.name]:
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
        if isinstance(t, Ptr):
            self.line(f"{dst} = ((uintptr_t){x} {C_OPS[op]} (uintptr_t){y});")
            return
        assert isinstance(t, Prim)
        name = t.name
        if name not in INTEGERS or op in ("eq", "ne", "lt", "le", "gt", "ge"):
            self.line(f"{dst} = ({x} {C_OPS[op]} {y});")
            return
        bits = 64 if name in ("usize", "isize") else INTEGERS[name]
        ct = C_PRIMS[name]
        signed = name in SIGNED
        def wrap(expr: str) -> str:
            return f"kur_dec_i{bits}({expr})" if signed else f"({ct})({expr})"
        if op in ("div", "mod"):
            self.line(f"if ({y} == 0) KUR_TRAP();")
            if signed:
                minus = wrap(f"(uint64_t)0 - (uint64_t)({x})") if op == "div" else "0"
                self.line(f"{dst} = ({y} == -1) ? {minus} : ({ct})({x} {C_OPS[op]} {y});")
            else:
                self.line(f"{dst} = ({ct})((uint64_t){x} {C_OPS[op]} (uint64_t){y});")
            return
        if op in ("shl", "shr"):
            amount = f"((uint64_t)({y}) & {bits - 1})"
            bits_of = f"(uint64_t)(uint{bits}_t)({x})" if bits < 64 or signed else f"(uint64_t)({x})"
            if op == "shl":
                self.line(f"{dst} = {wrap(f'{bits_of} << {amount}')};")
            elif not signed:
                self.line(f"{dst} = {wrap(f'{bits_of} >> {amount}')};")
            else:
                fill = f"({x} < 0 && {amount} ? (UINT64_MAX << ({bits} - {amount})) : 0)"
                self.line(f"{dst} = {wrap(f'({bits_of} >> {amount}) | {fill}')};")
            return
        self.line(f"{dst} = {wrap(f'(uint64_t)({x}) {C_OPS[op]} (uint64_t)({y})')};")

    def call(self, instr: Instr) -> None:
        callee, substitute_type = instr.args[0], self.substitute_type
        if callee.startswith("%"):
            fn = erase(self.type_of(callee), self.types)
            assert isinstance(fn, Fn)
            self.emit_call(instr, self.value_name(callee), fn.ret, {}, instr.args[1:], foreign=False)
        elif callee in self.program.externs:
            ext = self.program.externs[callee]
            assert ext.ret is not None
            self.emit_call(instr, callee, ext.ret, {}, instr.args[1:], foreign=True)
        else:
            fun = self.program.funs[callee]
            sub = {p.name: substitute_type(t) for p, t in zip(fun.params, instr.tys)}
            self.emit_call(instr, self.request(fun, sub), fun.ret, sub, instr.args[1:], foreign=False)

    def emit_call(self, instr: Instr, callee: str, ret: Type, sub: Mapping[str, Type],
                  args: tuple[str, ...], foreign: bool) -> None:
        call = f"{callee}({', '.join(self.operand(a) for a in args)})"
        ret = subst(ret, sub)
        dst = self.value_name(instr.lhs[0])
        if isinstance(ret, Never):
            self.line(f"{call};")
        elif foreign and self.is_unit(ret):
            self.line(f"{call};")
            self.line(f"{dst}.dummy = 0;")
        else:
            self.line(f"{dst} = {call};")

    def atomic(self, instr: Instr, dst: Optional[str]) -> None:
        assert instr.lit is not None
        kind, args = instr.op[len("builtin::unsafe::atomic_"):], [self.value_name(a) for a in instr.args]
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
            self.unit(dst)
        elif kind == "load":
            self.line(f"{dst} = __atomic_load_n({args[0]}, {C_ORDER[instr.lit]});")
        elif kind == "store":
            self.line(f"__atomic_store_n({args[0]}, {args[1]}, {C_ORDER[instr.lit]});")
            self.unit(dst)
        elif kind == "swap":
            self.line(f"{dst} = __atomic_exchange_n({args[0]}, {args[1]}, {C_ORDER[instr.lit]});")
        elif kind == "compare_exchange":
            success, _, failure = instr.lit.partition("/")
            self.line(f"{dst}.e0 = {args[1]};")
            self.line(f"{dst}.e1 = __atomic_compare_exchange_n({args[0]}, &{dst}.e0, {args[2]}, 0, "
                      f"{C_ORDER[success]}, {C_ORDER[failure]});")
        else:
            self.line(f"{dst} = __atomic_{kind}({args[0]}, {args[1]}, {C_ORDER[instr.lit]});")

    def foreach(self, instr: Instr, dst: str) -> None:
        original = instr.ty
        assert isinstance(original, Foreach)
        t, renamed = subst_foreach(original, self.sub)
        source = self.value_name(instr.args[0])
        mapper = self.program.funs[instr.args[1]]
        binders = []
        for name in foreach_binders(original):
            binder = renamed.get(name, Named(name))
            assert isinstance(binder, Named) and not binder.args
            binders.append(binder.name)
        own = [self.substitute_type(x) for x in instr.tys]
        if len(mapper.params) != len(binders) + len(own):
            raise KursError("foreach mapper parameter partition disagrees", instr.pos)
        caps = [self.value_name(c) for c in instr.args[2:]]
        skeleton, rows = self.types.rows(t.source)
        pointer = isinstance(self.shape(t.source), Ptr) and not isinstance(t.source, Foreach)
        out_shape = self.shape(t)

        def apply(row: ForeachRow, component: str) -> str:
            _, item, extra = row
            bound = self.types.bind(t, item, extra)
            sub = {p.name: bound[b] for p, b in zip(mapper.params, binders)}
            sub.update(zip((p.name for p in mapper.params[len(binders):]), own))
            return f"{self.request(mapper, sub)}({', '.join([component, *caps])})"

        if isinstance(skeleton, Enum):
            self.line(f"switch ({source}.tag) {{")
            for k, row in enumerate(rows):
                member = self.variant(row[0])
                self.line(f"case {k}: {dst}.payload.{member} = {apply(row, f'{source}.payload.{member}')}; "
                          f"{dst}.tag = {k}; break;")
            self.line("default: KUR_TRAP();\n  }")
            return
        if skeleton is None:
            component = f"{source}.e0" if isinstance(t.source, Foreach) else source
            self.line(f"{dst}.e0 = {apply(rows[0], component)};")
            return
        if isinstance(skeleton, Array):
            src_access = [f"items[{k}]" for k in range(len(rows))]
            dst_access = src_access
        else:
            src_access = [m for m, _ in self.members(skeleton)]
            dst_access = [m for m, _ in self.members(out_shape)]
        if isinstance(t.source, Foreach):
            inner_shape = self.shape(t.source)
            src_access = ([f"items[{k}]" for k in range(len(rows))] if isinstance(inner_shape, Array)
                          else [m for m, _ in self.members(inner_shape)])
        for row, s_member, d_member in zip(rows, src_access, dst_access):
            component = f"KUR_FIELD({source}, {s_member})" if pointer else f"{source}.{s_member}"
            self.line(f"{dst}.{d_member} = {apply(row, component)};")
        if not rows:
            self.line(f"{dst}.dummy = 0;")

    # -------------------------------------------------------- terminators --

    def term(self, term: Terminator) -> None:
        match term:
            case Ret():
                self.line(f"return {self.value_name(term.value)};")
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
                self.line("KUR_TRAP();")

    def edge(self, edge: Edge, payload: Optional[str] = None, indent: str = "") -> None:
        """A jump assigns every target parameter at once, through scratch locals."""
        target = self.blocks[edge.target]
        values = ([payload] if payload is not None else []) + [self.value_name(a) for a in edge.args]
        params = [(self.cname(target.label, n), t) for n, t in target.params]
        temps: list[str] = []
        for (_, t), value in zip(params, values):
            temp = allocate(f"edge_{self.scratch}", self.taken)
            self.scratch += 1
            self.decls.append(f"{self.ctype(self.substitute_type(t))} {temp};")
            self.line(f"{indent}{temp} = {value};")
            temps.append(temp)
        for (cname, _), temp in zip(params, temps):
            self.line(f"{indent}{cname} = {temp};")
        self.line(f"{indent}goto {self.labels[target.label]};")

    def switch(self, term: Switch) -> None:
        arg = term.scrutinee
        if arg.startswith("&"):
            source, _ = self.place(arg)
        else:
            source = self.value_name(arg)
        variants = [n for n, _ in self.types.variants(erase(self.substitute_type(term.ty), self.types))]
        self.line(f"switch ({source}.tag) {{")
        for arm in term.arms:
            self.line(f"case {variants.index(arm.variant)}:")
            payload = None if arg.startswith("&") else f"{source}.payload.{self.variant(arm.variant)}"
            self.line("  {")
            self.edge(arm.edge, payload, indent="    ")
            self.line("  }")
        self.line("default: KUR_TRAP();")
        self.line("}")


def emit_c(program: Program, checker: Checker, entry: Optional[str] = None) -> str:
    return Backend(program, checker).emit_program(entry)
