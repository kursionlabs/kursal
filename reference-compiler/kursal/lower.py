"""Lowering: inferred surface to KURS.

Ordinary local variables start in places, with loads/stores and explicit CFG
contracts computed by forward dataflow. A final frontend pass promotes roots
whose contents mention nongeneric brands to SSA block arguments, decomposing
projected products and generating generic value adapters for lending. Only
function-generic brands may remain in core local declarations.

Lowering.run first infers every function body in the resolved, signature-built
graph. FnLower consumes those annotated bodies after Infer.finish: local uses
and binders have matching IDs, calls have targets, and closures have captures.
Core types and call substitutions have their solved metavariables replaced.
Annotations conditional on syntax (such as a struct spread ID) are read only
for that form; lookup-only paths and folded constant subexpressions need no ty.

The result is a `core.Program`, ready for core checking before C emission.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Never, Optional
from functools import partial
from itertools import count
from . import core as C
from . import syntax as S
from .placeflow import OwnershipError, PlaceFlow
from .promote import promote_locals
from .literals import valid_literal
from .check import contains, overlap
from .infer import (POINTER_OPS, BuiltinCall, ClosureCall, FnCall, FStringWrite, Infer, PointerCall, TypedBuiltinCall,
                    TraitCall, TupleCall, ValueCall, VariantCall)
from .sexpr import KursError, List, Position, Str
from .sigs import OPERATORS, ConstValue, OpConst, Sig, Signatures
from .resolve import Resolver
from .types import brand_name, equal, foreach_binders, foreach_item, Types, free_brands, subst

ORDERINGS = {"Relaxed": "relaxed", "Acquire": "acquire", "Release": "release", "AcqRel": "acq_rel",
             "SeqCst": "seq_cst"}


# What a match arm covers: enum variant names, or integer ranges; empty for the wildcard.
type Cover = list[str | tuple[int, int]]


class LowerError(KursError):
    pass


def fail(message: str, pos: Optional[Position] = None) -> Never:
    raise LowerError(message, pos)


type BindingId = int | str       # source binder id, or a component of a ..rest bundle


@dataclass
class LoweredProgram(C.Program):
    errors: list[tuple[str, Exception]] = field(default_factory=list)


@dataclass
class Binding:
    name: str
    ty: C.Type
    path: Optional[str] = None       # the place that holds the variable (None for a `..rest` bundle)
    fields: Optional[dict[str, BindingId]] = None    # a `..rest` bundle: field -> binding id


class BlockB:
    """A block under construction."""

    def __init__(self, label: str, params: Sequence[tuple[str, C.Type]], brands: Sequence[str]) -> None:
        self.label, self.params, self.brands = label, list(params), tuple(brands)
        self.instrs: list[C.Instr] = []
        self.term: Optional[C.Terminator] = None


class Lowering:
    """The program-wide state."""

    def __init__(self, resolver: Resolver, sigs: Signatures) -> None:
        self.r, self.sigs = resolver, sigs
        self.program = LoweredProgram(types=sigs.types, traits=sigs.traits)
        self.types = Types(self.program)
        self.datas: dict[bytes, str] = {}
        self.closures = count()
        self.value_types: dict[int, dict[str, C.Type]] = {}

    def library_name(self, name: str, module: C.ModuleId) -> str:
        return self.sigs.name(self.sigs.library(module, name))

    def library_sig(self, key: str, module: C.ModuleId) -> Sig:
        return self.sigs.sigs[self.sigs.library(module, key)]

    def run(self, strict: bool = True) -> LoweredProgram:
        Infer(self.r, self.sigs).run()
        program = self.program
        for decl, sig in self.sigs.sigs.items():
            if decl.kind == "extern":
                with self.r.located(decl.module, decl.node.pos):
                    if any(self.types.zero_size(t) for t in sig.ptypes):
                        fail(f"extern {decl.node.name} takes a zero-size type, which has no memory form")
                program.externs[decl.node.name] = C.Extern(decl.node.name, sig.ret, sig.ptypes)
            elif isinstance(decl.node, S.Fn) and decl.node.body is not None:
                node = decl.node
                with self.r.located(decl.module, node.pos):
                    fun = self.attempt(strict, sig.name, partial(self.function, sig, node, sig.name))
                if fun is not None:
                    program.funs[sig.name] = fun
        for inst, item, module in self.sigs.instances:
            methods = []
            for sig, node in zip(inst.methods, item.methods):
                with self.r.located(module, node.pos):
                    fun = self.attempt(strict, f"{inst.trait}::{node.name}",
                                       partial(self.function, sig, node, node.name))
                if fun is not None:
                    fun.params = sig.params[len(inst.params):]
                    fun.premises = sig.premises[len(inst.premises):]
                    methods.append(fun)
            program.instances.setdefault(inst.trait, []).append(
                C.Instance(inst.trait, inst.params, inst.premises, inst.head, tuple(methods), inst.module, inst.pos))
        try:
            flow = PlaceFlow(program)
        except KursError as error:
            fail(error.message, error.pos)

        def plan(fun: C.Fun, instance: Optional[C.Instance] = None) -> None:
            try:
                flow.plan(fun, instance, diagnose=True)
                try:
                    helpers = promote_locals(fun, self.types, (instance.params if instance else ()) + fun.params,
                                             (instance.premises if instance else ()) + fun.premises,
                                             self.value_types.pop(id(fun), {}))
                    for helper in helpers:
                        program.funs[helper.name] = helper
                        flow.formation.function(helper)
                    flow.formation.function(fun, instance.params if instance else (),
                                            instance.premises if instance else ())
                except KursError as error:
                    fail(error.message, error.pos)
            except (OwnershipError, LowerError) as error:
                if strict:
                    raise
                program.errors.append((fun.name, error))

        for fun in list(program.funs.values()):
            with self.located(fun):
                plan(fun)
        for insts in program.instances.values():
            for lowered in insts:
                for method in lowered.methods:
                    with self.located(method):
                        plan(method, lowered)
        return program

    def located(self, fun: C.Fun) -> AbstractContextManager[None]:
        return self.r.located(fun.module, fun.pos) if fun.module is not None else nullcontext()

    def attempt(self, strict: bool, name: str, build: Callable[[], C.Fun]) -> Optional[C.Fun]:
        if strict:
            return build()
        try:
            return build()
        except Exception as e:  # not strict: record whatever went wrong and go on
            self.program.errors.append((name, e))
            return None

    def function(self, sig: Sig, node: S.Fn, name: str) -> C.Fun:
        return FnLower(self, sig, node, name).run()

    def data(self, text: str) -> tuple[str, int]:
        """A static byte array holding `text`; returns its name and length."""
        raw = text.encode()
        if raw not in self.datas:
            label = f"string#{len(self.datas)}"
            init = List([Str(text)])
            self.datas[raw] = label
            self.program.datas[label] = C.Data(label, C.Array(C.Prim("u8"), len(raw)), init)
        return self.datas[raw], len(raw)


class FnLower[N: S.Fn | S.Closure | S.Foreach]:
    """Translate one function, appending instructions to the current block.

    `cur` is None before entry and after a terminator. `start` selects an open
    block; `terminate` closes it. Expression rules return a value name on paths
    that continue, including a constructed unit value for unit expressions.
    They return None when no path continues, leaving `cur` as None.
    """

    def __init__(self, lowering: Lowering, sig: Sig, node: N, name: str) -> None:
        self.lowering, self.sig, self.node, self.name = lowering, sig, node, name
        self.types = lowering.types
        assert sig.module is not None
        self.module = sig.module
        self.kinds: dict[str, C.Kind] = {p.name: p.kind for p in sig.params}
        self.blocks: list[BlockB] = []
        self.cur: Optional[BlockB] = None
        self.vty: dict[str, C.Type] = {}
        self.bindings: dict[BindingId, Binding] = {}
        self.locals: list[tuple[str, C.Type]] = []
        self.open_brands: list[str] = []
        self.brand_map: dict[str, C.Type] = {}
        self.unsafe_nodes: set[int] = set()
        self.discharge_all = False   # inside an unsafe function's lifted closures every operation is discharged
        self.lend_frames: list[list[str]] = []
        self.n = 0
        self.pos: Optional[Position] = None

    # ---- machinery
    @property
    def current(self) -> BlockB:
        assert self.cur is not None
        return self.cur

    def fresh(self, hint: str = "v") -> str:
        self.n += 1
        return f"%{hint}{self.n}"

    def substitute_type(self, ty: C.Type) -> C.Type:
        return subst(ty, self.brand_map) if self.brand_map else ty

    def copy(self, ty: C.Type) -> bool:
        return self.types.is_copy(ty, self.kinds)

    def authorized(self, node: Optional[S.Expr] = None) -> bool:
        return self.sig.unsafe or self.discharge_all or node is not None and id(node) in self.unsafe_nodes

    def emit(self, op: str, rty: C.Type, args: Sequence[str] = (), node: Optional[S.Expr] = None,
             unsafe: bool = False, *, ty: Optional[C.Type] = None, tys: tuple[C.Type, ...] = (),
             lit: Optional[str] = None, binders: tuple[str, ...] = (), pos: Optional[Position] = None) -> str:
        pos = pos or (node.pos if node is not None else self.pos)
        if op == "const" and isinstance(ty, C.Prim) and lit is not None and not valid_literal(ty, lit):
            fail(f"literal {lit} does not fit {ty.name}", pos or self.pos)
        name = self.fresh()
        if op in {"call", "trait_call"}:
            for frame in self.lend_frames:
                frame.extend(a for a in args if a.startswith("&") and not a.startswith("&*"))
        discharged = unsafe or self.discharge_all or node is not None and id(node) in self.unsafe_nodes
        if op in C.UNSAFE_OPS and not (discharged or self.sig.unsafe):
            fail(f"{op} requires unsafe authorization", pos)
        if op in C.UNSAFE_OPS:
            self.memory_operation(op, args, ty, pos)
        self.current.instrs.append(C.Instr((name,), op, tuple(args), unsafe=discharged, pos=pos, ty=ty, tys=tys, lit=lit, binders=binders))
        self.vty[name] = rty
        return name

    def memory_operation(self, op: str, args: Sequence[str], ty: Optional[C.Type], pos: Optional[Position]) -> None:
        """The core's rules for an unsafe operation on memory, which an `unsafe` marker does not lift: a read
        through a pointer does not produce Never; a cast does not conjure what only a checked operation
        makes, nor reinterpret a layout the compiler chooses, nor take or produce a zero-size type, which has
        no representation to reinterpret; an atomic operation works on an integer, a bool or a pointer. A
        load or store of a zero-size type reads and writes nothing, and `alloca` of one is not null."""
        target = self.vty.get(args[0]) if args else None
        pointee = target.inner if isinstance(target, C.Ptr) else None
        if op in ("ptr_load", "ptr_load_volatile"):
            if isinstance(pointee, C.Never):
                fail("an unsafe read cannot produce Never", pos)
        elif op == "cast" and target is not None and ty is not None:
            if any(contains(self.types, t, self.uncastable) for t in (target, ty)):
                fail("an unsafe cast cannot produce or take apart a package, a brand witness, a function value, "
                     "a transform, a value of a type parameter or Never", pos)
            if self.types.zero_size(target) or self.types.zero_size(ty):
                fail("a zero-size type has no memory form", pos)
            if isinstance(target, C.NonNull) or isinstance(ty, C.NonNull):
                if not (isinstance(target, (C.Ptr, C.NonNull)) and isinstance(ty, (C.Ptr, C.NonNull))):
                    fail("an unsafe cast converts a NonNull only to or from a pointer", pos)
            elif self.types.chosen_layout(target) or self.types.chosen_layout(ty):
                fail("an unsafe cast cannot reinterpret an enum or a NonNull, whose layout the compiler chooses", pos)
        elif op in C.ATOMIC_OPS and pointee is not None:
            kind = C.ATOMIC_OPS[op]
            integer = isinstance(pointee, C.Prim) and pointee.name in C.INTEGERS
            if kind.startswith("fetch_") and not integer:
                fail(f"atomic {kind} works on a pointer to an integer", pos)
            if not (integer or pointee == C.BOOL or isinstance(pointee, C.Ptr)):
                fail(f"atomic {kind} works on a pointer to an integer, a bool or a pointer", pos)

    def uncastable(self, ty: C.Type) -> bool:
        return isinstance(ty, (C.Exists, C.Brand, C.Fn, C.Foreach, C.Never)) or \
            isinstance(ty, C.Named) and not ty.args and ty.name in self.kinds

    def unit(self) -> str:
        return self.emit("construct", C.UNIT, (), ty=C.UNIT)

    def destruct(self, value: str, ty: C.Type) -> list[tuple[str, C.Type]]:
        comps = self.types.fields(ty)
        names = [self.fresh("c") for _ in comps]
        self.current.instrs.append(C.Instr(tuple(names), "destruct", (value,), ty=ty, pos=self.pos))
        self.vty.update(zip(names, comps))
        return list(zip(names, comps))

    def new_block(self, params: Sequence[tuple[str, C.Type]] = (), hint: str = "b") -> BlockB:
        self.n += 1
        block = BlockB(f"{hint}{self.n}", params, self.open_brands)
        self.blocks.append(block)
        return block

    def start(self, block: BlockB) -> None:
        self.cur = block
        for name, ty in block.params:
            self.vty[name] = ty
        self.open_brands = list(block.brands)

    def terminate(self, term: C.Terminator) -> None:
        self.current.term = term
        self.cur = None

    def edge(self, block: BlockB, extra: Sequence[str] = ()) -> C.Edge:
        return C.Edge(block.label, tuple(extra), tuple(C.Skolem(b) for b in block.brands))

    def jump(self, block: BlockB, extra: Sequence[str] = ()) -> None:
        self.terminate(C.Jmp(self.edge(block, extra), self.pos))

    def is_plain_local(self, expr: S.Expr) -> bool:
        return isinstance(expr, S.Path) and expr.ref is not None and expr.ref[0] == "local"

    def temporary(self, ty: C.Type) -> str:
        root = f"&#tmp{len(self.locals)}"
        self.locals.append((root, ty))
        return root

    def value(self, item: S.Expr | Binding | str) -> Optional[str]:
        if isinstance(item, Binding):
            return self.read(item)
        return item if isinstance(item, str) else self.expr(item)

    def operands[Item: S.Expr | Binding | str](self, items: Sequence[Item],
                                            evaluate: Optional[Callable[[Item], Optional[str]]] = None) -> Optional[list[str]]:
        """Evaluate strictly left to right.

        Items are expressions, bindings to read, or already resolved place paths.
        A value preceding another operand waits in a temporary place, then is
        reloaded in the final block. Calls supply `operand` to interpret explicit lends.
        A plain read of a place that the same group lends is rejected, whatever its type:
        the program states the order with a binding before the expression.
        """
        reads = []
        for item in items:
            if isinstance(item, Binding):
                path, ty = item.path, item.ty
            elif isinstance(item, (S.Path, S.Field, S.Index)):
                path, ty = self.place_path(item), self.substitute_type(item.ty)
            else:
                continue
            if path is not None:
                reads.append(path)
        lends: list[str] = []
        self.lend_frames.append(lends)
        try:
            evaluate = evaluate or self.value
            order = range(len(items))
            values: dict[int, str] = {}
            saved: list[tuple[int, str, str, C.Type]] = []
            for n, k in enumerate(order):
                v = evaluate(items[k])
                if v is None:
                    assert self.cur is None
                    return None
                values[k] = v
                raw, name = ("&*", v[2:]) if v.startswith("&*") else ("", v)
                if not name.startswith("&") and n + 1 < len(order):
                    root = self.temporary(self.vty[name])
                    item = items[k]
                    origin = self.pos if isinstance(item, (Binding, str)) else item.pos
                    self.emit("store", C.UNIT, (root, name), pos=origin)
                    saved.append((k, raw, root, self.vty[name]))
            for k, raw, root, ty in saved:
                values[k] = raw + self.emit("load", ty, (root,))
            result = [values[k] for k in range(len(items))]
            lends.extend(v for v in result if v.startswith("&") and not v.startswith("&*"))
            for path in reads:
                if any(overlap(path, lent) for lent in lends):
                    fail("a plain read overlaps a lend in the same operand group; "
                         "bind the value before the expression", self.pos)
            return result
        finally:
            self.lend_frames.pop()

    def arms(self, arms: Sequence[tuple[BlockB, Callable[[], Optional[str]]]], ty: C.Type) -> Optional[str]:
        """Run each arm in its block; the arms that finish meet in a join block taking their results."""
        outer, mapping, ends = list(self.open_brands), dict(self.brand_map), []
        for block, body in arms:
            self.start(block)
            result = body()
            if self.cur is not None:
                assert result is not None
                ends.append((self.cur, result))
            self.open_brands, self.brand_map = list(outer), dict(mapping)
        if not ends:
            self.cur = None
            return None
        join = self.new_block([(self.fresh("r"), self.substitute_type(ty))], "join")
        for block, result in ends:
            self.cur = block
            self.jump(join, (result,))
        self.start(join)
        return join.params[-1][0]

    def scoped[Result](self, fn: Callable[[], Result]) -> Result:
        """Run `fn` in a lexical scope: the brands it opens go out of scope with it."""
        brands, mapping = list(self.open_brands), dict(self.brand_map)
        try:
            return fn()
        finally:
            self.brand_map = mapping
            if self.cur is not None:
                self.open_brands = brands

    # ---- bindings
    def declare(self, bid: BindingId, name: str, ty: C.Type, value: str) -> None:
        """Bind a variable: a new root of the function, holding `value`."""
        ty = self.substitute_type(ty)
        root = f"&{name}#{bid}"
        self.locals.append((root, ty))
        self.bindings[bid] = Binding(name, ty, root)
        self.emit("store", C.UNIT, (root, value))

    def read(self, binding: Binding) -> str:
        if binding.path is None:
            fail(f"{binding.name} is a ..rest bundle, not a value", self.pos)
        return self.emit("load", binding.ty, (binding.path,))

    def write(self, binding: Binding, value: str) -> None:
        assert binding.path is not None
        self.emit("store", C.UNIT, (binding.path, value))

    def place_path(self, expr: S.Expr) -> Optional[str]:
        """The place path `expr` names, or None if it is not rooted at a place."""
        if isinstance(expr, S.Path) and expr.ref is not None and expr.ref[0] == "local":
            return self.bindings[expr.binder].path
        if isinstance(expr, (S.Field, S.Index)):
            base = self.place_path(expr.value)
            if base is None:
                return None
            return f"{base}.{expr.name if isinstance(expr, S.Field) else expr.index}"
        return None

    # ---- the function
    def start_entry(self, params: Sequence[tuple[str, C.Type]] = ()) -> None:
        entry = self.new_block(params, "entry")
        entry.label = "entry"
        self.start(entry)

    def parameters(self, ids: Sequence[int], offset: int = 0, sources: Sequence[str] = ()) -> None:
        """Lent parameters are roots already; value parameters start in local places.

        An offset skips raw parameters such as a closure environment. Explicit
        sources preserve the SSA names used by generated functions.
        """
        sig = self.sig
        for k, (name, ty, bid) in enumerate(zip(sig.pnames[offset:], sig.ptypes[offset:], ids), offset):
            source = sources[k - offset] if sources else ("&" if k in sig.places else "%a_") + name
            self.current.params.append((source, ty))
            if k in sig.places:
                self.bindings[bid] = Binding(name, ty, source)
            else:
                self.vty[source] = ty
                self.declare(bid, name, ty, source)

    def run(self) -> C.Fun:
        self.start_entry()
        assert isinstance(self.node, S.Fn)
        self.parameters(self.node.param_ids)
        return self.finish_body(self.node)

    def finish_body(self, expr: S.Fn | S.Closure | S.Foreach) -> C.Fun:
        assert expr.body is not None
        value = self.expr(expr.body)
        if self.cur is not None:
            assert value is not None
            result_pos = expr.body.tail.pos if isinstance(expr.body, S.Block) and expr.body.tail is not None else expr.body.pos
            self.terminate(C.Ret(value, result_pos))
        return self.finish()

    def finish(self) -> C.Fun:
        sig = self.sig
        blocks: list[C.Block] = []
        for b in self.blocks:
            assert b.term is not None
            blocks.append(C.Block(b.label, tuple(b.params), tuple(b.instrs), b.term, b.brands))
        fun = C.Fun(self.name, sig.params, sig.premises, sig.ret, sig.ptypes, tuple(blocks), tuple(self.locals),
                    sig.unsafe, self.module, self.node.pos)
        brands = {p.name for p in sig.params if p.kind == C.BRAND}
        if any(not free_brands(t) <= brands for _, t in self.locals):
            self.lowering.value_types[id(fun)] = dict(self.vty)
        return fun

    def identity_args(self) -> tuple[C.Type, ...]:
        """The enclosing function's own parameters, as the type arguments that instantiate a lifted function."""
        return tuple(C.Skolem(p.name) if p.kind == C.BRAND else C.Named(p.name) for p in self.sig.params)

    def check_closed(self, ty: C.Type, local_brands: Iterable[str] = ()) -> None:
        """A lifted type may mention function parameters and brands opened inside that body."""
        allowed = {p.name for p in self.sig.params if p.kind == C.BRAND} | {"static"} | set(local_brands)
        local = free_brands(ty) - allowed
        if local:
            fail(f"a closure or foreach body cannot mention the block-local brand {brand_name(min(local))}", self.pos)

    # ---- expressions
    def expr(self, expr: S.Expr) -> Optional[str]:
        """Return the expression's value in the current block, or None if control cannot continue."""
        if self.cur is None:
            return None
        previous, self.pos = self.pos, expr.pos
        try:
            return self.lower_expr(expr)
        finally:
            self.pos = previous

    def lower_expr(self, expr: S.Expr) -> Optional[str]:
        match expr:
            case S.Lit(): return self.expr_lit(expr)
            case S.FStr(): return self.expr_fstring(expr)
            case S.Path(): return self.expr_path(expr)
            case S.Tuple(): return self.expr_tuple(expr)
            case S.Array(): return self.expr_tuple(expr)
            case S.StructLit(): return self.expr_struct_lit(expr)
            case S.Block(): return self.expr_block(expr)
            case S.If(): return self.expr_if(expr)
            case S.Match(): return self.expr_match(expr)
            case S.Closure(): return self.expr_closure(expr)
            case S.Return(): return self.expr_return(expr)
            case S.Pack(): return self.expr_pack(expr)
            case S.Foreach(): return self.expr_foreach(expr)
            case S.Binop(): return self.expr_binop(expr)
            case S.Unary(): return self.expr_unary(expr)
            case S.Cast(): return self.expr_cast(expr)
            case S.UnsafeStore(): return self.expr_unsafe_store(expr)
            case S.Field(): return self.expr_field(expr)
            case S.Index(): return self.expr_field(expr)
            case S.Call(): return self.expr_call(expr)
            case S.Try(): return self.expr_try(expr)
            case S.While(): return self.expr_while(expr)
            case S.For(): return self.expr_for(expr)
        fail(f"cannot lower {type(expr).__name__}", self.pos)

    def expr_lit(self, expr: S.Lit) -> Optional[str]:
        ty = self.substitute_type(expr.ty)
        if expr.kind == "str":
            assert isinstance(expr.value, str)
            return self.string(expr.value)
        if expr.kind == "bool":
            return self.emit("const", ty, ty=ty, lit="true" if expr.value else "false")
        return self.emit("const", ty, ty=ty, lit=str(expr.value))

    def string(self, text: str) -> str:
        """A string literal: a `Slice[u8, 'static]` over static bytes."""
        data, n = self.lowering.data(text)
        u8, static = C.Prim("u8"), C.Skolem("static")
        d = self.emit("data", C.Ptr(C.Array(u8, n)), (data,))
        p = self.emit("cast", C.Ptr(u8), (d,), ty=C.Ptr(u8), unsafe=True)
        brand = self.emit("static_brand", C.Brand(static), unsafe=True)
        length = self.emit("const", C.USIZE, ty=C.USIZE, lit=str(n))
        sig = self.lowering.library_sig("Slice::from_raw_parts", self.module)
        slice_ty = C.Named(self.lowering.library_name("Slice", self.module), (u8, static))
        return self.emit("call", slice_ty, (sig.name, p, length, brand), tys=(u8, static), unsafe=True)

    def expr_tuple(self, expr: S.Tuple | S.Array) -> Optional[str]:
        vals = self.operands(list(expr.elems))
        if vals is None:
            return None
        ty = self.substitute_type(expr.ty)
        return self.emit("construct", ty, vals, ty=ty)

    def expr_block(self, expr: S.Block) -> Optional[str]:
        def body() -> Optional[str]:
            for s in expr.stmts:
                if self.cur is None:
                    break
                self.stmt(s)
            if self.cur is None:
                return None
            return self.expr(expr.tail) if expr.tail is not None else self.unit()
        return self.scoped(body)

    def expr_path(self, expr: S.Path) -> Optional[str]:
        ref = expr.ref
        assert ref is not None
        if ref[0] == "local":
            return self.read(self.bindings[expr.binder])
        if expr.folded is not None:
            return self.const(expr.folded)
        if isinstance(expr.call, FnCall):
            call = expr.call
            return self.emit("fn", self.substitute_type(expr.ty), (call.name,), tys=tuple(self.substitute_type(t) for t in call.targs))
        if isinstance(expr.call, VariantCall):
            return self.variant(expr.call.variant, expr.ty, None)
        fail(f"cannot lower the path {'::'.join(expr.names)}", expr.pos)

    def const(self, value: ConstValue) -> str:
        if value[0] == "str":
            return self.string(value[1])
        if value[0] == "op":
            return self.operation(value)
        kind, v, ty = value
        assert ty is not None
        lit = ("true" if v else "false") if kind == "bool" else str(v)
        return self.emit("const", ty, ty=ty, lit=lit)

    def operation(self, value: OpConst) -> str:
        """A constant operation the C compiler folds, as the instructions that compute it."""
        _, (op, operands), ty = value
        if op in ("and", "or"):
            c = self.const(operands[0])
            right = lambda: self.const(operands[1])
            constant = lambda: self.emit("const", C.BOOL, ty=C.BOOL, lit="false" if op == "and" else "true")
            result = self.branch(c, right, constant, C.BOOL) if op == "and" else self.branch(c, constant, right, C.BOOL)
            assert result is not None
            return result
        args = tuple(self.const(operand) for operand in operands)
        if op == "neg":
            return self.negate(args[0], ty)
        if op == "not":
            return self.complement(args[0], ty)
        if op in ("int_cast", "float_cast"):
            return self.emit(op, ty, args, ty=ty)
        return self.emit(op, ty, args)

    def expr_binop(self, expr: S.Binop) -> Optional[str]:
        if expr.op in ("&&", "||"):
            c = self.expr(expr.left)
            if c is None:
                return None
            lit = "false" if expr.op == "&&" else "true"
            constant = lambda: self.emit("const", C.BOOL, ty=C.BOOL, lit=lit)
            if expr.op == "&&":
                return self.branch(c, lambda: self.expr(expr.right), constant, C.BOOL)
            return self.branch(c, constant, lambda: self.expr(expr.right), C.BOOL)
        operands = self.operands([expr.left, expr.right])
        return None if operands is None else self.emit(OPERATORS[expr.op],self.substitute_type(expr.ty), operands)

    def expr_unary(self, expr: S.Unary) -> Optional[str]:
        op = expr.op
        if op == "unsafe":
            node = expr.operand
            while isinstance(node, S.Try) or isinstance(node, S.Unary) and node.op == "&":
                node = node.value if isinstance(node, S.Try) else node.operand
            self.unsafe_nodes.add(id(node))
            return self.expr(expr.operand)
        if op == "&":
            return self.expr(expr.operand)
        if op == "*":
            p = self.expr(expr.operand)
            return None if p is None else self.emit("ptr_load", self.substitute_type(expr.ty), (p,), node=expr)
        ty = self.substitute_type(expr.ty)
        if op == "-" and isinstance(expr.operand, S.Lit) and expr.operand.kind in ("int", "float"):
            return self.emit("const", ty, ty=ty, lit="-" + str(expr.operand.value))
        v = self.expr(expr.operand)
        if v is None:
            return None
        return self.negate(v, ty) if op == "-" else self.complement(v, ty)

    def negate(self, v: str, ty: C.Type) -> str:
        s = self.types.shape(ty)
        assert isinstance(s, C.Prim)
        if s.name in C.FLOATS:
            minus = self.emit("const", ty, ty=ty, lit="-1.0")
            return self.emit("mul", ty, (v, minus))
        zero = self.emit("const", ty, ty=ty, lit="0")
        return self.emit("sub", ty, (zero, v))

    def complement(self, v: str, ty: C.Type) -> str:
        """`!v`: a bool's negation, or an integer's bits flipped."""
        s = self.types.shape(ty)
        assert isinstance(s, C.Prim)
        if s.name == "bool":
            f = self.emit("const", ty, ty=ty, lit="false")
            return self.emit("eq", ty, (v, f))
        if s.name.startswith("i"):
            ones = self.emit("const", ty, ty=ty, lit="-1")
        elif s.name in C.POINTER_SIZED:
            # All ones at the target's width, `0 - 1`, which the C compiler folds.
            zero, one = self.emit("const", ty, ty=ty, lit="0"), self.emit("const", ty, ty=ty, lit="1")
            ones = self.emit("sub", ty, (zero, one))
        else:
            ones = self.emit("const", ty, ty=ty, lit=str((1 << C.WIDTHS[s.name]) - 1))
        return self.emit("bit_xor", ty, (v, ones))

    def expr_cast(self, expr: S.Cast) -> Optional[str]:
        v = self.expr(expr.value)
        if v is None:
            return None
        src, dst = self.vty[v], self.substitute_type(expr.ty)
        # A number to a number is a conversion by value, which needs no `unsafe`.
        numbers = C.INTEGERS | C.FLOATS
        if isinstance(src, C.Prim) and isinstance(dst, C.Prim) and src.name in numbers and dst.name in numbers:
            op = "int_cast" if src.name in C.INTEGERS and dst.name in C.INTEGERS else "float_cast"
            return self.emit(op, dst, (v,), ty=dst)
        # Forgetting that a pointer is not null is safe.
        if isinstance(src, C.NonNull) and isinstance(dst, C.Ptr) and equal(src.inner, dst.inner):
            return self.emit("to_ptr", dst, (v,), ty=dst)
        # `emit` holds a cast to the core's rules for it (`memory_operation`); a package is opened or packed.
        if isinstance(src, C.Exists) or isinstance(dst, C.Exists):
            fail("a cast cannot consume or produce an outer existential package; use opening or packing", expr.pos)
        return self.emit("cast", dst, (v,), ty=dst, node=expr)

    def expr_unsafe_store(self, expr: S.UnsafeStore) -> Optional[str]:
        operands = self.operands([expr.pointer, expr.value])
        return None if operands is None else self.emit("ptr_store", C.UNIT, operands, node=expr)

    def expr_field(self, expr: S.Field | S.Index) -> Optional[str]:
        path = self.place_path(expr)
        if path is not None:
            return self.emit("load", self.substitute_type(expr.ty), (path,))
        return self.project(expr)

    def project(self, expr: S.Field | S.Index) -> Optional[str]:
        """Read a field of a value that is not a place: take the value apart, once per step."""
        steps: list[str | int] = []
        base: S.Expr = expr
        while isinstance(base, (S.Field, S.Index)):
            steps.append(base.name if isinstance(base, S.Field) else base.index)
            base = base.value
        v = self.expr(base)
        if v is None:
            return None
        ty = self.vty[v]
        for step in reversed(steps):
            selected = self.field_index(ty, step)
            parts = self.destruct(v, ty)
            if any(not self.copy(t) for k, (_, t) in enumerate(parts) if k != selected):
                fail("a projection cannot discard a linear sibling; destructure the value explicitly", expr.pos)
            v, ty = parts[selected]
        return v

    def field_index(self, ty: C.Type, step: str | int) -> int:
        s = self.types.shape(ty)
        if isinstance(s, C.Struct):
            assert isinstance(step, str)
            return [n for n, _ in s.fields].index(step)
        assert isinstance(step, int)
        return step

    # ---- constructors
    def expr_struct_lit(self, expr: S.StructLit) -> Optional[str]:
        ty = self.substitute_type(expr.ty)
        shape = self.types.shape(ty)
        assert isinstance(shape, C.Struct)
        names = [n for n, _ in expr.fields]
        items = [value if value is not None else self.bindings[expr.field_ids[name]] for name, value in expr.fields]
        if expr.spread:
            rest = self.bindings[expr.spread_id]
            assert rest.fields is not None
            for name, _ in shape.fields:
                if name not in names:
                    names.append(name)
                    items.append(self.bindings[rest.fields[name]])
        results = self.operands(items)
        if results is None:
            return None
        values = dict(zip(names, results))
        return self.emit("construct", ty, [values[n] for n, _ in shape.fields], ty=ty)

    def variant(self, variant: str, ty: C.Type, args: Optional[tuple[S.Expr, ...]]) -> Optional[str]:
        enum = self.substitute_type(ty)
        shape = self.types.shape(enum)
        assert isinstance(shape, C.Enum)
        payload_ty = dict(shape.variants)[variant]
        payload: Optional[str]
        if not args:
            if payload_ty != C.UNIT:
                fail(f"variant {variant} used as a function value", self.pos)
            payload = self.emit("construct", C.UNIT, (), ty=C.UNIT)
        elif len(args) == 1:
            payload = self.expr(args[0])
        else:
            vals = self.operands(list(args))
            payload = None if vals is None else self.emit("construct", payload_ty, vals, ty=payload_ty)
        if payload is None:
            return None
        return self.emit("construct_variant", enum, (payload,), ty=enum, lit=variant)

    # ---- calls
    def expr_call(self, expr: S.Call) -> Optional[str]:
        call = expr.call
        assert call is not None
        match call:
            case VariantCall():
                return self.variant(call.variant, expr.ty, expr.args)
            case TupleCall():
                vals = self.operands(list(expr.args))
                ty = self.substitute_type(expr.ty)
                return None if vals is None else self.emit("construct", ty, vals, ty=ty)
            case TypedBuiltinCall():
                return self.emit(call.name, self.substitute_type(expr.ty), ty=self.substitute_type(call.target), node=expr)
            case BuiltinCall():
                return self.builtin(expr, call)
            case PointerCall():
                return self.pointer_method(expr, call)
            case FStringWrite():
                out = self.expr(expr.args[0])
                return None if out is None else self.fstring(call.string, out)
        items: list[S.Expr | str] = [expr.callee] if isinstance(call, (ValueCall, ClosureCall)) else []
        if isinstance(call, (FnCall, TraitCall)) and call.receiver is not None:
            receiver = call.receiver
            if receiver.lend:
                path = self.place_path(receiver.value)
                if path is None:
                    fail("a lent receiver is not a place", expr.pos)
                items.append(path)
            else:
                items.append(receiver.value)
        items.extend(expr.args)
        operands = self.operands(items, lambda arg: self.operand(arg, expr))
        if operands is None:
            return None
        callee_value = operands.pop(0) if isinstance(call, (ValueCall, ClosureCall)) else None
        if isinstance(call, ClosureCall):  # a closure is {env, code}: the code takes the environment first
            assert callee_value is not None
            (env, _), (callee_value, _) = self.destruct(callee_value, self.vty[callee_value])
            operands.insert(0, env)
        rty = self.substitute_type(expr.ty)
        target: tuple[str, ...]
        match call:
            case TraitCall():
                target = (call.trait, call.method)
                targs = call.targs
                requires_unsafe = next(m.unsafe for m in self.lowering.sigs.traits[call.trait].methods if m.name == call.method)
            case FnCall():
                target = (call.name,)
                targs = call.targs
                requires_unsafe = call.sig.unsafe
            case ValueCall() | ClosureCall():
                assert callee_value is not None
                target = (callee_value,)
                targs = call.targs if isinstance(call, ValueCall) else ()
                signature = self.vty[callee_value]
                assert isinstance(signature, C.Fn)
                requires_unsafe = signature.unsafe
        if requires_unsafe and not self.authorized(expr):
            fail("calling an unsafe function requires unsafe authorization", expr.pos)
        # A raw lend's explicit authorization discharges the core call instruction,
        # but must not authorize an independently unsafe callee in the surface.
        raw_lend = any(o.startswith("&*") for o in operands)
        result = self.emit("trait_call" if isinstance(call, TraitCall) else "call", rty, (*target, *operands),
                           tys=tuple(self.substitute_type(t) for t in targs), node=expr, unsafe=raw_lend)
        if isinstance(rty, C.Never):
            self.terminate(C.Unreachable(self.pos))
            return None
        return result

    def operand(self, arg: S.Expr | str, call: S.Call) -> Optional[str]:
        """A call argument: a value, a lent place's path, or a raw pointer's target."""
        if isinstance(arg, str):
            return arg
        node = arg
        explicit = isinstance(node, S.Unary) and node.op == "unsafe"
        if isinstance(node, S.Unary) and node.op == "unsafe":
            node = node.operand
        if isinstance(node, S.Unary) and node.op == "&":
            target = node.operand
            if isinstance(target, S.Unary) and target.op == "*":
                p = self.expr(target.operand)
                if p is not None and not (explicit or self.authorized(call)):
                    fail("lending a raw pointer target requires unsafe authorization", arg.pos)
                return None if p is None else "&*" + p
            path = self.place_path(target)
            if path is not None:
                return path
        return self.expr(arg)  # `&x.m().n`: the & lends the receiver of the first call, not the argument

    def builtin(self, expr: S.Call, call: BuiltinCall) -> Optional[str]:
        name = call.name
        rty = self.substitute_type(expr.ty)
        if name == "fresh":
            return self.emit("fresh", rty)
        if name == "static_brand":
            return self.emit("static_brand", rty, node=expr)
        if name == "trap":
            self.emit("trap", C.Never(), node=expr)
            self.terminate(C.Unreachable(self.pos))
            return None
        if name == "enum_variant":
            arg = expr.args[0]
            if isinstance(arg, S.Unary) and arg.op == "&":
                path = self.place_path(arg.operand)
                if path is None:
                    fail("enum_variant(&x) needs a local", expr.pos)
                return self.emit("variant_tag", C.U32, (path,), ty=self.substitute_type(arg.ty))
            v = self.expr(arg)
            if v is None:
                return None
            tag = self.emit("variant_tag", C.U32, (v,), ty=self.vty[v])
            return self.emit("construct", rty, (v, tag), ty=rty)
        if name == "fence":
            return self.emit("fence", C.UNIT,
                             lit=self.atomic_ordering("fence", expr.args), node=expr)
        operands = self.operands(list(expr.args))
        if operands is None:
            return None
        if name == "load_volatile":
            return self.emit("ptr_load_volatile", rty, operands, node=expr)
        return self.emit("ptr_store_volatile", C.UNIT, operands, node=expr)

    def ordering(self, expr: S.Expr) -> str:
        if isinstance(expr, S.Path) and expr.ref is not None and expr.ref[0] == "variant" and expr.ref[2] in ORDERINGS:
            return ORDERINGS[expr.ref[2]]
        fail("a memory ordering is written as an Ordering variant", expr.pos)

    def pointer_method(self, expr: S.Call, call: PointerCall) -> Optional[str]:
        name = call.name
        params, _ = POINTER_OPS[name]
        values = [a for a, param in zip(expr.args, params) if param != "O"]
        order_args = [a for a, param in zip(expr.args, params) if param == "O"]
        kind = name.removeprefix("atomic_")
        orders = self.atomic_ordering(kind, order_args) if order_args else None
        operands = self.operands([call.receiver, *values])
        if operands is None:
            return None
        op = "ptr_" + name if name in ("load", "store", "load_volatile", "store_volatile") else "ptr_atomic_" + kind
        return self.emit(op, self.substitute_type(expr.ty), operands, lit=orders, node=expr)

    def atomic_ordering(self, kind: str, args: Sequence[S.Expr]) -> str:
        orders = [self.ordering(arg) for arg in args]
        order = orders[0]
        if kind == "compare_exchange":
            failure = orders[1]
            allowed = {"relaxed"}
            if order in {"acquire", "acq_rel", "seq_cst"}:
                allowed.add("acquire")
            if order == "seq_cst":
                allowed.add("seq_cst")
            if failure not in allowed:
                fail(f"compare_exchange failure ordering {failure} is invalid for success ordering {order}", args[1].pos)
        elif (kind == "load" and order in {"release", "acq_rel"} or
              kind == "store" and order in {"acquire", "acq_rel"} or
              kind == "fence" and order == "relaxed"):
            fail(f"atomic {kind} does not support ordering {order}", args[0].pos)
        return "/".join(orders)

    # ---- control flow
    def branch(self, cond: str, then_fn: Callable[[], Optional[str]], else_fn: Callable[[], Optional[str]], ty: C.Type) -> Optional[str]:
        a, b = self.new_block(), self.new_block()
        self.terminate(C.Jnz(cond, self.edge(a), self.edge(b), self.pos))
        return self.arms([(a, then_fn), (b, else_fn)], ty)

    def expr_if(self, expr: S.If) -> Optional[str]:
        c = self.expr(expr.cond)
        if c is None:
            return None
        other_expr = expr.other
        other = (lambda: self.expr(other_expr)) if other_expr is not None else self.unit
        return self.branch(c, lambda: self.expr(expr.then), other, expr.ty)

    def expr_return(self, expr: S.Return) -> Optional[str]:
        v = self.expr(expr.value) if expr.value is not None else self.unit()
        if self.cur is not None:
            assert v is not None
            self.terminate(C.Ret(v, self.pos))
        return None

    def expr_while(self, expr: S.While) -> Optional[str]:
        head = self.new_block(hint="while")
        self.jump(head)
        self.start(head)
        c = self.expr(expr.cond)
        if c is None:
            return None
        body, exit_ = self.new_block(), self.new_block(hint="after")
        self.terminate(C.Jnz(c, self.edge(body), self.edge(exit_), self.pos))
        self.start(body)
        self.expr(expr.body)
        if self.cur is not None:
            self.jump(head)
        self.start(exit_)
        return self.unit()

    def expr_for(self, expr: S.For) -> Optional[str]:
        assert isinstance(expr.iterator, S.Unary)
        path = self.place_path(expr.iterator.operand)
        if path is None:
            fail("a for loop's iterator is not a place", expr.pos)
        call = expr.next_call
        iterator_ty, item = [self.substitute_type(a) for a in call.targs]
        option = self.substitute_type(expr.option)
        head = self.new_block(hint="for")
        self.jump(head)
        self.start(head)
        step = self.emit("trait_call", option, (call.trait, call.method, path), tys=(iterator_ty, item))
        item_ty = self.substitute_type(item)
        some = self.new_block([(self.fresh("item"), item_ty)], "some")
        none = self.new_block([(self.fresh("unit"), C.UNIT)], "none")
        self.terminate(C.Switch(step, option, (C.Arm("Some", self.edge(some)), C.Arm("None", self.edge(none))),
                                self.pos))
        self.start(some)

        def body() -> None:
            self.declare(expr.binder, expr.name, item_ty, some.params[0][0])
            self.expr(expr.body)
        self.scoped(body)
        if self.cur is not None:
            self.jump(head)
        self.start(none)
        return self.unit()

    def discard_pattern(self, ty: C.Type) -> None:
        if not self.copy(ty):
            fail("a pattern cannot discard a linear value", self.pos)

    def enum_switch(self, s: str, ty: C.Type, shape: C.Enum, covers: list[Cover], blocks: list[BlockB],
                    place: bool) -> None:
        """One core arm per variant. A value arm covering several variants, or none by name, gets one
        block per variant that takes the payload and jumps on; the coverage check discarded it."""
        payloads = dict(shape.variants)
        arms, hops = [], []
        for cover, block in zip(covers, blocks):
            for variant in cover:
                assert isinstance(variant, str)
                if place or block.params:
                    arms.append(C.Arm(variant, self.edge(block)))
                    continue
                hop = self.new_block([(self.fresh("pay"), payloads[variant])])
                arms.append(C.Arm(variant, self.edge(hop)))
                hops.append((hop, block))
        self.terminate(C.Switch(s, ty, tuple(arms), self.pos))
        for hop, block in hops:
            self.start(hop)
            self.jump(block)

    def integer_tests(self, s: str, ty: C.Type, covers: list[Cover], blocks: list[BlockB]) -> None:
        """An integer match as comparisons in arm order: each range is tested, and a miss goes on to the
        next test; the wildcard arm takes what is left. The tests run in several blocks, so a scrutinee
        value is kept in a temporary place and loaded by each."""
        if not s.startswith("&"):
            root = self.temporary(ty)
            self.emit("store", C.UNIT, (root, s))
            s = root

        def compare(op: str, bound: int) -> str:
            value = self.emit("load", ty, (s,))
            limit = self.emit("const", ty, ty=ty, lit=str(bound))
            return self.emit(op, C.BOOL, (value, limit))

        for cover, block in zip(covers, blocks):
            if not cover:
                self.jump(block)
                return
            for item in cover:
                assert isinstance(item, tuple)
                lo, hi = item
                miss = self.new_block()
                if lo == hi:
                    self.terminate(C.Jnz(compare("eq", lo), self.edge(block), self.edge(miss), self.pos))
                else:
                    upper = self.new_block()
                    self.terminate(C.Jnz(compare("ge", lo), self.edge(upper), self.edge(miss), self.pos))
                    self.start(upper)
                    self.terminate(C.Jnz(compare("le", hi), self.edge(block), self.edge(miss), self.pos))
                self.start(miss)

    def match_coverage(self, expr: S.Match, shape: C.Type, covers: list[Cover]) -> list[Cover]:
        """Check that the arms partition the scrutinee's values; returns each arm's cover with its wildcard
        resolved: an integer arm's ranges, or an enum arm's variants."""
        if isinstance(shape, C.Prim) and shape.name in C.INTEGERS:
            if not covers or covers[-1]:
                fail("an integer match must end with a wildcard", expr.pos)
            bits = C.LITERAL_BITS[shape.name]
            low, high = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if shape.name.startswith("i") else (0, (1 << bits) - 1)
            spans: list[tuple[int, int]] = []
            for k, cover in enumerate(covers):
                if not cover and k != len(covers) - 1:
                    fail("a match wildcard must come last", expr.pos)
                for item in cover:
                    assert isinstance(item, tuple)
                    lo, hi = item
                    if not low <= lo <= hi <= high or any(lo <= h and l <= hi for l, h in spans):
                        fail("match ranges must fit the integer type and not overlap", expr.pos)
                    spans.append((lo, hi))
            return covers
        if not isinstance(shape, C.Enum):
            fail("a match requires an integer or enum scrutinee", expr.pos)
        variants = dict(shape.variants)
        seen: set[str] = set()
        resolved: list[Cover] = []
        for k, (cover, source) in enumerate(zip(covers, expr.arms)):
            if not cover and k != len(covers) - 1:
                fail("a match wildcard must come last", expr.pos)
            names: Cover = [name for name in cover if isinstance(name, str)] or [n for n in variants if n not in seen]
            for name in names:
                assert isinstance(name, str)
                if name not in variants or name in seen:
                    fail("match variants must not repeat", expr.pos)
                seen.add(name)
            binds = len(source.cases) == 1 and source.cases[0].binders is not None
            if not expr.lent and not binds:
                for name in names:
                    assert isinstance(name, str)
                    self.discard_pattern(variants[name])
            resolved.append(names)
        if seen != set(variants):
            fail("a match must cover every enum variant", expr.pos)
        return resolved

    def expr_match(self, expr: S.Match) -> Optional[str]:
        if expr.lent:
            assert isinstance(expr.scrutinee, S.Unary)
            scrutinee = expr.scrutinee.operand
            place = self.place_path(scrutinee)
        else:
            scrutinee = expr.scrutinee
            place = None
        if expr.lent and place is None:
            fail("match & needs a place", expr.pos)
        s = place or self.expr(expr.scrutinee)
        if s is None:
            return None
        sty = self.substitute_type(scrutinee.ty)
        if isinstance(sty, C.Named) and sty.name in self.kinds:
            fail("a match requires an integer or enum scrutinee; an abstract type has no known matchable shape", expr.pos)
        shape = self.types.shape(sty)
        covers: list[Cover] = []
        blocks = []
        for arm in expr.arms:
            single = len(arm.cases) == 1 and arm.cases[0].kind == "variant"
            payload = [] if place or not single else [(self.fresh("pay"), self.substitute_type(arm.cases[0].ty))]
            blocks.append(self.new_block(payload))
            cover: Cover = []
            for case in arm.cases:
                if case.kind == "variant":
                    assert case.path is not None and case.path.ref is not None and case.path.ref[0] == "variant"
                    cover.append(case.path.ref[2])
                elif case.kind == "value":
                    assert case.low is not None and case.high is not None
                    assert isinstance(case.low.value, int) and isinstance(case.high.value, int)
                    cover.append((case.low.value, case.high.value))
            covers.append(cover)
        covers = self.match_coverage(expr, shape, covers)
        if isinstance(shape, C.Enum):
            self.enum_switch(s, sty, shape, covers, blocks, bool(place))
        else:
            self.integer_tests(s, sty, covers, blocks)

        def body(arm: S.Arm, block: BlockB) -> Optional[str]:
            case = arm.cases[0]
            if len(arm.cases) == 1 and case.kind == "variant" and case.binders is not None:
                self.bind_payload(case, place or block.params[0][0], lent=bool(place))
            return self.scoped(lambda: self.expr(arm.body))
        return self.arms([(block, partial(body, arm, block)) for arm, block in zip(expr.arms, blocks)],
                         expr.ty)

    def bind_payload(self, case: S.Case, payload: str, lent: bool = False) -> None:
        """Bind the whole payload or its components, by value or by place alias."""
        ty = self.substitute_type(case.ty)
        assert case.binders is not None
        whole = len(case.binders) == 1
        types = (ty,) if whole else self.types.fields(ty)
        if lent:
            assert case.path is not None and case.path.ref is not None and case.path.ref[0] == "variant"
            path = f"{payload}.{case.path.ref[2]}"
            sources = [path] if whole else [f"{path}.{k}" for k in range(len(types))]
        else:
            sources = [payload] if whole else [v for v, _ in self.destruct(payload, ty)]
        for bid, name, t, source in zip(case.binder_ids, case.binders, types, sources):
            if bid is None and not lent:
                self.discard_pattern(t)
            if bid is not None:
                if lent:
                    self.bindings[bid] = Binding(name, t, source)
                else:
                    self.declare(bid, name, t, source)

    def expr_try(self, expr: S.Try) -> Optional[str]:
        v = self.expr(expr.value)
        if v is None:
            return None
        enum_ty = self.vty[v]
        ok, err = ("Ok", "Err") if expr.kind == "result" else ("Some", "None")
        shape = self.types.shape(enum_ty)
        assert isinstance(shape, C.Enum)
        variants = dict(shape.variants)
        ok_block = self.new_block([(self.fresh("ok"), variants[ok])])
        err_block = self.new_block([(self.fresh("err"), variants[err])])
        self.terminate(C.Switch(v, enum_ty, (C.Arm(ok, self.edge(ok_block)), C.Arm(err, self.edge(err_block))),
                                self.pos))
        self.start(err_block)
        ret = self.substitute_type(self.sig.ret)
        failure = self.emit("construct_variant", ret, (err_block.params[0][0],), ty=ret, lit=err)
        self.terminate(C.Ret(failure, self.pos))
        self.start(ok_block)
        return ok_block.params[0][0]

    # ---- closures, foreach, f-strings
    def expr_closure(self, expr: S.Closure) -> Optional[str]:
        ty = self.substitute_type(expr.ty)
        self.check_closed(ty)
        assert isinstance(ty, C.Struct)
        env_ty, code_ty = ty.fields[0][1], ty.fields[1][1]
        vals = self.operands([self.bindings[c.binder_id] for c in expr.captures])
        if vals is None:
            return None
        env = self.emit("construct", env_ty, vals, ty=env_ty)
        name = ClosureLower(self, expr, ty).lower()
        code = self.emit("fn", code_ty, (name,), tys=self.identity_args())
        return self.emit("construct", ty, (env, code), ty=ty)

    def expr_foreach(self, expr: S.Foreach) -> Optional[str]:
        ty = self.substitute_type(expr.ty)
        self.check_closed(ty)
        vals = self.operands([expr.source] + [self.bindings[c.binder_id] for c in expr.captures])
        if vals is None:
            return None
        assert isinstance(ty, C.Foreach)
        name = MapperLower(self, expr, ty).lower()
        return self.emit("foreach", ty, (vals[0], name, *vals[1:]), ty=ty, tys=self.identity_args(), node=expr)

    def expr_fstring(self, expr: S.FStr) -> Optional[str]:
        sig = self.lowering.library_sig("Buffer!::new", self.module)
        return self.fstring(expr, self.emit("call", self.substitute_type(expr.ty), (sig.name,), tys=()))

    def fstring(self, expr: S.FStr, writer: str) -> Optional[str]:
        """Write each segment into the writer `w`: literals through `Writable::write_static`,
        `{expr}` through `Format::format`, which hands the value back (`{&x}` stores it again)."""
        module, wty = self.module, self.vty[writer]
        writable, fmt = self.lowering.library_name("Writable", module), self.lowering.library_name("Format", module)
        for part in expr.parts:
            if isinstance(part, str):
                if part:
                    writer = self.emit("trait_call", wty, (writable, "write_static", writer, self.string(part)), tys=(wty,))
                continue
            lend = isinstance(part, S.Unary) and part.op == "&"
            operand = part.operand if isinstance(part, S.Unary) and lend else part
            values = self.operands([writer, operand])
            if values is None:
                return None
            writer, v = values
            vty, pair = self.vty[v], C.Tuple((self.vty[v], wty))
            both = self.emit("trait_call", pair, (fmt, "format", v, writer), tys=(vty, wty))
            (v2, _), (writer, _) = self.destruct(both, pair)
            if lend:
                if isinstance(operand, S.Path) and self.is_plain_local(operand):
                    self.write(self.bindings[operand.binder], v2)
                else:
                    path = self.place_path(operand)
                    if path is None:
                        fail("{&x} in an f-string needs a local or a place", self.pos)
                    self.emit("store", C.UNIT, (path, v2))
        return writer

    # ---- brands
    def expr_pack(self, expr: S.Pack) -> Optional[str]:
        v = self.expr(expr.value)
        if v is None:
            return None
        witnesses = tuple(self.substitute_type(w) for w in expr.witnesses)
        ty = self.substitute_type(expr.ty)
        return self.emit("pack", ty, (v,), ty=ty, tys=witnesses)

    # ---- statements
    def stmt(self, stmt: S.Statement) -> None:
        previous, self.pos = self.pos, stmt.pos
        try:
            self.lower_stmt(stmt)
        finally:
            self.pos = previous

    def lower_stmt(self, stmt: S.Statement) -> None:
        if isinstance(stmt, S.Let):
            return self.let(stmt)
        if isinstance(stmt, S.Var):
            v = self.expr(stmt.value)
            if v is not None:
                self.declare(stmt.binder, stmt.name, self.vty[v], v)
            return
        if isinstance(stmt, S.Const):
            assert stmt.folded[2] is not None
            self.declare(stmt.binder, stmt.name, stmt.folded[2], self.const(stmt.folded))
            return
        if isinstance(stmt, S.Assign):
            return self.assign(stmt)
        if isinstance(stmt, S.ExprStmt):
            value = self.expr(stmt.value)
            if value is not None and not self.copy(self.vty[value]):
                fail("cannot discard a linear expression result; bind or consume it", stmt.value.pos)
            return
        fail(f"cannot lower statement {type(stmt).__name__}", self.pos)

    def let(self, stmt: S.Let) -> None:
        v = self.expr(stmt.value)
        if v is None:
            return
        if stmt.brands:
            package = self.types.shape(self.vty[v])
            assert isinstance(package, C.Exists)
            actual = tuple(f"{b}#{self.fresh('')[1:]}" for b in stmt.opened_brands)
            payload = subst(package.body, {b: C.Skolem(a) for b, a in zip(package.binders, actual)})
            v = self.emit("open", payload, (v,), binders=actual)
            for b, a in zip(stmt.opened_brands, actual):
                self.brand_map[b] = C.Skolem(a)
            self.open_brands.extend(actual)
        self.pattern(stmt.pattern, v)

    def pattern(self, pattern: S.Pattern, value: str) -> None:
        match pattern:
            case S.PBind():
                self.declare(pattern.binder, pattern.name, pattern.ty, value)
            case S.PWild():
                self.discard_pattern(self.vty[value])
            case S.PTuple(elems=elems) | S.PArray(elems=elems):
                for sub, (c, _) in zip(elems, self.destruct(value, self.vty[value])):
                    self.pattern(sub, c)
            case S.PStruct(fields=pfields, rest=prest):
                comps = self.destruct(value, self.vty[value])
                shape = self.types.shape(self.vty[value])
                assert isinstance(shape, C.Struct)
                names = [n for n, _ in shape.fields]
                named = dict(pfields)
                rest: dict[str, BindingId] = {}
                for name, (c, t) in zip(names, comps):
                    if name in named:
                        self.pattern(named[name], c)
                    elif prest:
                        rest[name] = f"{pattern.rest_id}_{name}"
                        self.declare(rest[name], name, t, c)
                    else:
                        self.discard_pattern(t)
                if prest:
                    self.bindings[pattern.rest_id] = Binding(prest, C.UNIT, fields=rest)

    def assign(self, stmt: S.Assign) -> None:
        v = self.expr(stmt.value)
        if v is None:
            return
        t = stmt.target
        if isinstance(t, S.Path):
            self.write(self.bindings[t.binder], v)
            return
        path = self.place_path(t)
        if path is None:
            fail("assignment through a field needs a place", stmt.pos)
        self.emit("store", C.UNIT, (path, v))


class Lifted[N: S.Closure | S.Foreach](FnLower[N]):
    """A function lifted out of another: a closure body or a foreach mapper."""

    def substitute_type(self, ty: C.Type) -> C.Type:
        # Signature checks miss type-only uses, e.g. sizeof[Brand['outer]]().
        # Locally opened brands are renamed by this body's own brand map.
        ty = super().substitute_type(ty)
        self.check_closed(ty, self.open_brands)
        return ty

    def lower(self) -> str:
        fun = self.run()
        self.lowering.program.funs[self.name] = fun
        return self.name



class ClosureLower(Lifted[S.Closure]):
    def __init__[P: S.Fn | S.Closure | S.Foreach](self, parent: FnLower[P], expr: S.Closure, ty: C.Struct) -> None:
        lowering, (env_ty, code_ty) = parent.lowering, (ty.fields[0][1], ty.fields[1][1])
        assert isinstance(code_ty, C.Fn)
        name = f"{parent.name}#closure{next(lowering.closures)}"
        places = frozenset(i + 1 for i, (lend, _, _) in enumerate(expr.params) if lend)
        sig = Sig(name, None, parent.sig.params, parent.sig.premises, ("env", *(n for _, n, _ in expr.params)),
                  tuple(code_ty.params), places, code_ty.ret, False, parent.sig.module)
        super().__init__(lowering, sig, expr, name)
        self.env_ty = env_ty
        self.discharge_all = parent.discharge_all or parent.sig.unsafe

    def run(self) -> C.Fun:
        e = self.node
        self.start_entry([("%env", self.env_ty)])
        self.parameters(e.param_ids, offset=1)
        for capture, (value, ty) in zip(e.captures, self.destruct("%env", self.env_ty)):
            self.declare(capture.binder_id, capture.name, ty, value)
        return self.finish_body(e)


class MapperLower(Lifted[S.Foreach]):
    """The function a `foreach` applies to each component: the component, then the (Copy) captures."""

    def __init__[P: S.Fn | S.Closure | S.Foreach](self, parent: FnLower[P], expr: S.Foreach, ty: C.Foreach) -> None:
        lowering = parent.lowering
        name = f"{parent.name}#foreach{next(lowering.closures)}"
        binders = foreach_binders(ty)
        item = foreach_item(ty)
        captured = [parent.bindings[c.binder_id].ty for c in expr.captures]
        sig = Sig(name, None, tuple(C.Param(b, C.ANY) for b in binders) + parent.sig.params, parent.sig.premises,
                  (expr.name, *(c.name for c in expr.captures)), (item, *captured), frozenset(), ty.body, False, parent.sig.module)
        super().__init__(lowering, sig, expr, name)
        self.discharge_all = parent.discharge_all or parent.sig.unsafe

    def run(self) -> C.Fun:
        e = self.node
        self.start_entry()
        self.parameters((e.name_id, *(c.binder_id for c in e.captures)),
                        sources=("%item", *(f"%a_{c.name}" for c in e.captures)))
        return self.finish_body(e)


def lower(resolver: Resolver, sigs: Signatures, strict: bool = True) -> LoweredProgram:
    """The core program for a resolved, signature-built package graph.

    Not strict, a function that cannot be lowered is left out and listed in the returned program's `.errors`."""
    lowering = Lowering(resolver, sigs)
    program = lowering.run(strict)
    return program
