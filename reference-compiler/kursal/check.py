"""The KURS assertion checker: every block is checked alone against its
declared contract, using only signatures of its successors and callees."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence, Set
from typing import Never as NoReturn, Optional
from dataclasses import dataclass, field
from .core import (BOOL, BRAND, COPY, FLOATS, INTEGERS, U32, UNIT, USIZE, Array, Brand, Enum, Exists, Fn,
                   Foreach, Ignored, Jmp, Jnz, Named, Never, Prim, Ptr, Ret, Skolem, Struct, Switch, Tuple,
                   Unreachable, Type, Kind, Program, Fun, Instance, Param, Premise, Block, Instr, Terminator, Edge, ModuleId)
from .sexpr import Form, KursError, List, Position, Str
from .types import Kinds, children, foreach_binders, foreach_item, Types, equal, fits, free_brands, subst
from .termination import check_instance_termination
from .formation import Formation
from .layout import check_type_expansion
from .coherence import check_coherence
from .literals import valid_literal
from .unify import match_head

ARITH = {"add", "sub", "mul", "div", "mod"}
ORDER = {"lt", "le", "gt", "ge"}
EQUALITY = {"eq", "ne"}
BITS = {"bit_and", "bit_or", "bit_xor", "shl", "shr"}
ORDERINGS = {"relaxed", "acquire", "release", "acq_rel", "seq_cst"}
CAS_FAILURE = {"relaxed": {"relaxed"}, "acquire": {"relaxed", "acquire"}, "release": {"relaxed"},
               "acq_rel": {"relaxed", "acquire"}, "seq_cst": {"relaxed", "acquire", "seq_cst"}}
ATOMIC_ARITY = {"load": 1, "store": 2, "swap": 2, "compare_exchange": 3,
                "fetch_add": 2, "fetch_sub": 2, "fetch_and": 2, "fetch_or": 2,
                "fetch_xor": 2, "fence": 0}
STATIC = "static"
# The module of a declaration that names none: inside no exposure scope, so it has no shape authority over
# restricted declarations.
NOWHERE = ModuleId(-1, ("<no module>",))
SEALED = ("Copy", "builtin::")      # compiler-reserved trait names: no user trait or instance


def contains(ty: Type, pred: Callable[[Type], bool]) -> bool:
    return pred(ty) or any(contains(t, pred) for t in children(ty))


class CheckError(KursError):
    pass


def segments(path: str) -> list[str]:
    return path.split(".")


def join(parts: Sequence[str]) -> str:
    return ".".join(parts)


def canonical_tuple_index(segment: str, arity: int) -> Optional[int]:
    """Resolve a bounded decimal tuple component without parsing huge atoms."""
    if (arity == 0 or not segment.isascii() or not segment.isdecimal()
            or len(segment) > 1 and segment[0] == "0"):
        return None
    largest = str(arity - 1)
    if len(segment) > len(largest) or len(segment) == len(largest) and segment > largest:
        return None
    return int(segment)


def prefix(parent: str, other_path: str) -> bool:
    """`parent` is an ancestor of `other_path`, or the path itself."""
    a, b = segments(parent), segments(other_path)
    return b[:len(a)] == a


def overlap(path: str, other_path: str) -> bool:
    return prefix(path, other_path) or prefix(other_path, path)


@dataclass
class Places:
    """P = (Z, K, O): empty paths, known variants, opened views."""
    empty: set[str] = field(default_factory=set)
    known: dict[str, str] = field(default_factory=dict)
    opened: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def copy(self) -> Places:
        return Places(set(self.empty), dict(self.known), dict(self.opened))

    def covered(self, path: str) -> bool:
        return any(prefix(z, path) for z in self.empty)

    def full(self, path: str) -> bool:
        return not any(overlap(z, path) for z in self.empty)

    def forget(self, path: str, keep_own_view: bool = True) -> None:
        self.known = {q: v for q, v in self.known.items() if not prefix(path, q)}
        self.opened = {q: b for q, b in self.opened.items() if not prefix(path, q) or (keep_own_view and q == path)}

    def keep_opened(self, wanted: dict[str, tuple[str, ...]]) -> None:
        """Retain views only when all their opened ancestors are retained too."""
        self.opened = {p: b for p, b in wanted.items()
                       if all(q in wanted for q in self.opened if q != p and prefix(q, p))}


class Checker:
    def __init__(self, program: Program) -> None:
        check_type_expansion(program)
        self.formation = Formation(program)
        self.formation.declarations()
        check_coherence((Premise(inst.trait, inst.head), inst.params, inst.pos)
                        for instances in program.instances.values() for inst in instances)
        check_instance_termination((Premise(inst.trait, inst.head), inst.premises, None)
                                   for instances in program.instances.values() for inst in instances)
        self.program = program
        self.types = Types(program)
        self.typed: dict[int, dict[tuple[str, str], Type]] = {}  # id(fun) -> {(block label, SSA name): type}, for the backend
        self.module: Optional[ModuleId] = None
        self.kinds: dict[str, Kind] = {}
        self.brands: set[str] = {STATIC}
        self.premises: tuple[Premise, ...] = ()
        self.formation.discharge = self.formation_holds
        self.formation.declarations()

    def formation_holds(self, premise: Premise, kinds: Kinds, premises: tuple[Premise, ...],
                        module: Optional[ModuleId]) -> bool:
        saved = self.kinds, self.premises, self.module, self.brands
        self.kinds, self.premises, self.module = dict(kinds), premises, module
        self.brands = {n for n, k in kinds.items() if k == BRAND}
        try:
            return self.holds(premise.trait, premise.args)
        finally:
            self.kinds, self.premises, self.module, self.brands = saved


    def fail(self, message: str, pos: Optional[Position] = None) -> NoReturn:
        raise CheckError(f"{self.where}: {message}", pos)

    # ---------------------------------------------------------- program --

    def check(self) -> None:
        for ext in self.program.externs.values():
            self.where = f"extern {ext.name}"
            for t in ext.ptypes or ():
                self.has_memory_form(t)
        for trait in self.program.traits.values():
            self.where = f"trait {trait.name}"
            if trait.name.startswith(SEALED):
                self.fail("trait_name_is_reserved")
            if len({m.name for m in trait.methods}) != len(trait.methods):
                self.fail("duplicate_trait_method")
        for fun in self.program.funs.values():
            self.check_fun(fun)
        for instances in self.program.instances.values():
            for inst in instances:
                self.check_instance(inst)
        for data in self.program.datas.values():
            self.where = data.name
            self.module = data.module or NOWHERE
            self.check_data(data.ty, data.init)

    def check_instance(self, inst: Instance) -> None:
        trait = self.program.traits.get(inst.trait)
        self.where = f"instance {inst.trait}"
        if inst.trait.startswith(SEALED):
            self.fail("trait_name_is_reserved")
        if trait is None or len(trait.params) != len(inst.head):
            self.fail("instance_unknown_trait_wrong")
        head = dict(zip(trait.params, inst.head))
        by_name = {m.name: m for m in inst.methods}
        if len(by_name) != len(inst.methods):
            self.fail("duplicate_instance_method")
        if set(by_name) - {sig.name for sig in trait.methods}:
            self.fail("instance_method_not_in_trait")
        for sig in trait.methods:
            fun = by_name.get(sig.name)
            if fun is None:
                self.fail("instance_lacks_method")
            if fun.params or fun.premises:
                self.fail("instance_method_has_own_quantifier")
            want = Fn(tuple(subst(t, head) for t in sig.ptypes), subst(sig.ret, head), places=sig.places)
            got = Fn(fun.ptypes, fun.ret, places=fun.places)
            if not equal(got, want) or (fun.unsafe and not sig.unsafe):
                self.fail("method_does_match_trait")
            self.check_fun(fun, inst.params, inst.premises)

    def check_data(self, ty: Type, form: Form) -> None:
        s = self.types.shape(ty, self.module)
        if isinstance(s, Prim):
            if isinstance(form, (List, Str)):
                self.fail("data_scalar_requires_atom")
            self.scalar_literal(s, str(form))
            return
        if not isinstance(s, (Tuple, Struct, Array)):
            self.fail("data_initializer_type")
        if not isinstance(form, List):
            self.fail("data_product_requires_list")
        if isinstance(s, Array):
            count = 0
            for part in form:
                if s.elem == Prim("u8") and isinstance(part, Str):
                    count += len(part.encode())
                else:
                    self.check_data(s.elem, part)
                    count += 1
            if count != s.length:
                self.fail("data_initializer_wrong_number")
            return
        parts = self.types.fields(ty, self.module)
        if len(form) != len(parts):
            self.fail("data_initializer_wrong_number")
        for component, part in zip(parts, form):
            self.check_data(component, part)

    # --------------------------------------------------------- function --

    def check_fun(self, fun: Fun, outer_params: tuple[Param, ...] = (),
                  outer_premises: tuple[Premise, ...] = ()) -> None:
        self.formation.function(fun, outer_params, outer_premises)
        self.fun = fun
        self.where = fun.name
        self.params = outer_params + fun.params
        self.kinds = {p.name: p.kind for p in self.params}
        self.premises = outer_premises + fun.premises
        self.module = fun.module or NOWHERE
        self.blocks = {b.label: b for b in fun.blocks}
        self.record = self.typed.setdefault(id(fun), {})
        if len(self.blocks) != len(fun.blocks):
            self.fail("duplicate_block_label")
        entry = fun.blocks[0]
        if len(entry.params) != len(fun.ptypes) or entry.brands:
            self.fail("entry_parameters_disagree_signature")
        self.roots = dict(fun.locals)
        if len(self.roots) != len(fun.locals):
            self.fail("duplicate_place_root")
        for (name, ty), declared in zip(entry.params, fun.ptypes):
            if not equal(ty, declared):
                self.fail("entry_parameter_disagrees_signature")
            if name.startswith("&"):
                if name in self.roots:
                    self.fail("duplicate_place_root")
                self.roots[name] = ty
        self.param_roots = {n for n, _ in entry.params if n.startswith("&")}
        for block in fun.blocks:
            self.check_block(block, block is entry)

    def check_block(self, block: Block, is_entry: bool) -> None:
        self.formation.pos = block.pos
        self.where = f"{self.fun.name}/{block.label}"
        self.label = block.label
        self.brands = {p.name for p in self.params if p.kind == BRAND} | set(block.brands) | {STATIC}
        self.env: dict[str, Type] = {}
        self.used: set[str] = set()
        for name, ty in block.params:
            if name.startswith("&"):
                if not is_entry:
                    self.fail("only_entry_declares_place")
                continue
            self.bind(name, ty)
        if is_entry:
            if block.opened:
                self.fail("entry_cannot_assume_opened_views")
            self.places = Places(empty={n for n, _ in self.fun.locals})
        else:
            self.places = Places(empty=set(block.empty), known=dict(block.known))
            if len(dict(block.opened)) != len(block.opened):
                self.fail("duplicate_opened_view")
            for path, brands in sorted(block.opened):
                ty = self.path_type(path)
                if not isinstance(ty, Exists) or len(ty.binders) != len(brands) or len(set(brands)) != len(brands):
                    self.fail("opened_contract_requires_package_brands")
                if not set(brands) <= set(block.brands):
                    self.fail("opened_contract_requires_block_brands")
                if any(set(brands) & set(other) for other in self.places.opened.values()):
                    self.fail("opened_contract_brand_names_one_view")
                self.places.opened[path] = brands
            for path in (*block.empty, *(p for p, _ in block.known)):
                self.path_type(path)
        last = None
        for instr in block.instrs:
            self.pos = instr.pos
            self.check_instr(instr)
            last = instr
        self.check_term(block.term, last)

    # ------------------------------------------------------- SSA values --

    def bind(self, name: str, ty: Type) -> None:
        self.formation.fits(ty, "any", {**self.kinds, **{b: BRAND for b in self.brands}})
        if name in self.env:
            self.fail("defined_twice")
        self.env[name] = ty
        self.record[self.label, name] = ty

    def use(self, name: str) -> Type:
        ty = self.peek(name)
        self.used.add(name)
        return ty

    def peek(self, name: str) -> Type:
        if name not in self.env:
            self.fail("defined")
        if name in self.used:
            self.fail("was_already_consumed")
        return self.env[name]

    def copy_type(self, ty: Type) -> bool:
        return self.types.is_copy(ty, self.kinds)

    def accounted(self, used: Set[str]) -> None:
        for name, ty in self.env.items():
            if name not in used and not self.copy_type(ty):
                self.fail("linear_value_never_consumed")

    # ------------------------------------------------------------ places --

    def path_type(self, path: str) -> Type:
        return self.view(path, self.storage_type(path))

    def storage_type(self, path: str) -> Type:
        """The final slot's stored type, applying views only to its ancestors."""
        parts = segments(path)
        if parts[0] not in self.roots:
            self.fail("place_function")
        ty, sofar = self.roots[parts[0]], parts[0]
        for seg in parts[1:]:
            ty = self.view(sofar, ty)
            match self.types.shape(ty, self.module):
                case Enum(variants=variants):
                    if self.places.known.get(sofar) != seg:
                        self.fail("known")
                    component = dict(variants).get(seg)
                case Tuple(elems=elems):
                    index = canonical_tuple_index(seg, len(elems))
                    component = elems[index] if index is not None else None
                case Struct(fields=fields):
                    component = dict(fields).get(seg)
                case _:
                    component = None
            if component is None:
                self.fail("names_component")
            sofar = f"{sofar}.{seg}"
            ty = component
        return ty

    def view(self, path: str, ty: Type) -> Type:
        brands = self.places.opened.get(path)
        if brands is None:
            return ty
        if not isinstance(ty, Exists):
            self.fail("opened_view_requires_existential")
        return subst(ty.body, {b: Skolem(c) for b, c in zip(ty.binders, brands)})

    def no_linear(self, path: str, ty: Type) -> bool:
        """Whether p may be discarded; ty is its stored type, before its own view."""
        if self.copy_type(ty) or self.places.covered(path):
            return True
        if path in self.places.opened:
            return self.no_linear_parts(path, self.view(path, ty))
        return self.no_linear_parts(path, ty)

    def no_linear_parts(self, path: str, ty: Type) -> bool:
        if self.copy_type(ty):
            return True
        decomposed = any(prefix(path, z) and z != path for z in self.places.empty)
        if not decomposed and path not in self.places.known:
            # A full non-Copy value cannot be discarded, irrespective of shape.
            # In particular, abstract parameters have no nominal body to unfold.
            return False
        match self.types.shape(ty, self.module):
            case Enum(variants=variants) if path in self.places.known:
                variant = self.places.known[path]
                return self.no_linear(f"{path}.{variant}", dict(variants)[variant])
            case Struct(fields=rows) if decomposed:
                pass
            case Tuple(elems=elems) if decomposed:
                rows = tuple((str(i), e) for i, e in enumerate(elems))
            case _:
                return False
        return all(self.no_linear(f"{path}.{n}", f) for n, f in rows)

    def nested_view_closed(self, path: str) -> None:
        """A view opened strictly inside `path` is closed (packed) before `path` is loaded or lent."""
        if any(prefix(path, q) and q != path for q in self.places.opened):
            self.fail("nested_view_open")

    def load(self, path: str) -> Type:
        ty = self.path_type(path)
        if not self.places.full(path):
            self.fail("full")
        self.nested_view_closed(path)
        if not self.copy_type(ty):
            self.places.empty.add(path)
            self.places.forget(path)
        return ty

    def store(self, path: str, ty: Type) -> None:
        want = self.path_type(path)
        if not fits(ty, want):
            self.fail("storing_into")
        if any(prefix(z, path) and z != path for z in self.places.empty):
            self.fail("empty_ancestor_store_whole")
        # What the store overwrites holds no linear contents: a Copy value, or a place whose linear parts
        # were moved out (dropping that empty linear place is its destruct in the value-only translation).
        if not self.no_linear(path, self.storage_type(path)):
            self.fail("storing_into_discard_linear")
        self.places.empty = {z for z in self.places.empty if not prefix(path, z)}
        self.places.forget(path)

    def lend(self, paths: Sequence[str]) -> None:
        """Any number of whole roots, and at most two paths below roots, which part at a struct or tuple."""
        for i, p in enumerate(paths):
            if not self.places.full(p):
                self.fail("lent_place_full")
            self.nested_view_closed(p)
            if any(overlap(p, q) for q in paths[i + 1:]):
                self.fail("lent_places_overlap")
        nested = [p for p in paths if "." in p]
        if len(nested) > 2:
            self.fail("at_most_two_nested_lends")
        if len(nested) == 2 and not self.product_branches(nested):
            self.fail("nested_lends_part_at_a_product")

    def common_parent(self, paths: Sequence[str]) -> str:
        parts = [segments(p) for p in paths]
        common = 0
        while all(len(s) > common + 1 for s in parts) and len({s[common] for s in parts}) == 1:
            common += 1
        return join(parts[0][:common])

    def product_branches(self, paths: Sequence[str]) -> bool:
        """The paths part at a struct or tuple, and reach it through structs and tuples only, past no
        known variant or opened view."""
        parts = [segments(p) for p in paths]
        common = 0
        while all(len(s) > common + 1 for s in parts) and len({s[common] for s in parts}) == 1:
            common += 1
        if len({s[common] for s in parts}) == 1:
            return False
        for s in parts:
            for depth in range(1, len(s)):
                step = join(s[:depth])
                if step in self.places.known or step in self.places.opened or \
                        not isinstance(self.types.shape(self.path_type(step), self.module), (Struct, Tuple)):
                    return False
        return True

    def after_call(self, paths: Iterable[str]) -> None:
        for p in paths:
            self.places.forget(p)

    # ---------------------------------------------------------- premises --

    def holds(self, trait: str, args: tuple[Type, ...]) -> bool:
        """A premise is discharged by an assumption, or by an instance whose
        head matches and whose own premises hold."""
        if trait == "builtin::Exposed":
            return len(args) == 1 and self.types.exposure_available(args[0], self.module, self.kinds, self.premises)
        for p in self.premises:
            if p.trait == trait and len(p.args) == len(args) and all(equal(a, b) for a, b in zip(p.args, args)):
                return True
        return self.find_instance(trait, args) is not None

    def matching_instance(self, trait: str, args: tuple[Type, ...]) -> Optional[tuple[Instance, dict[str, Type]]]:
        """Select the unique coherent head; this does not authorize its use."""
        for inst in self.program.instances.get(trait, ()):
            sub = match_head(inst.params, inst.head, args, require_extractable=True)
            if sub is not None:
                return inst, sub
        return None

    def find_instance(self, trait: str, args: tuple[Type, ...]) -> Optional[tuple[Instance, dict[str, Type]]]:
        found = self.matching_instance(trait, args)
        if found is None:
            return None
        inst, sub = found
        if any(p.kind == COPY and not self.copy_type(sub[p.name]) for p in inst.params):
            return None
        # A matched type can have been formed under a function type's own
        # premises. Those assumptions do not escape into this instance call.
        saved = self.formation.premises, self.formation.module
        self.formation.premises, self.formation.module = self.premises, self.module
        try:
            scope: dict[str, Kind] = {**self.kinds, **{b: BRAND for b in self.brands}}
            self.formation.arguments(inst.params, tuple(sub[p.name] for p in inst.params), scope)
        finally:
            self.formation.premises, self.formation.module = saved
        return found if all(self.holds(p.trait, tuple(subst(a, sub) for a in p.args))
                            for p in inst.premises) else None

    # -------------------------------------------------------- signatures --

    def instantiate(self, params: Sequence[Param], premises: Sequence[Premise],
                    tys: tuple[Type, ...], what: str) -> dict[str, Type]:
        if len(tys) != len(params):
            self.fail("takes_type_arguments_got")
        sub = {}
        for p, t in zip(params, tys):
            if p.kind == BRAND:
                if isinstance(t, Ignored):
                    continue
                if not isinstance(t, Skolem) or t.label not in self.brands:
                    self.fail("brand_argument_scope")
            elif p.kind == COPY and not self.copy_type(t):
                self.fail("copy")
            sub[p.name] = t
        for premise in premises:
            args = tuple(subst(a, sub) for a in premise.args)
            if not self.holds(premise.trait, args):
                self.fail("unsatisfied_premise")
        return sub

    def signature(self, instr: Instr) -> tuple[list[Type], Type, bool, frozenset[int]]:
        """(param types, result, unsafe, place indices) of a call's callee."""
        callee, tys = instr.args[0], instr.tys
        if callee.startswith("%"):
            ty = self.use(callee)
            if not isinstance(ty, Fn):
                self.fail("function_value")
            sub = self.instantiate(ty.forall, (), tys, callee)
            return [subst(t, sub) for t in ty.params], subst(ty.ret, sub), ty.unsafe, ty.places
        if callee in self.program.externs:
            ext = self.program.externs[callee]
            if ext.ptypes is None or ext.ret is None:
                self.fail("extern_value_not_callable")
            return list(ext.ptypes), ext.ret, True, frozenset()
        fun = self.program.funs.get(callee)
        if fun is None:
            self.fail("unknown_function")
        sub = self.instantiate(fun.params, fun.premises, tys, callee)
        return [subst(t, sub) for t in fun.ptypes], subst(fun.ret, sub), fun.unsafe, fun.places

    def arguments(self, ptypes: Sequence[Type], args: tuple[str, ...], places: frozenset[int]) -> list[str]:
        if len(ptypes) != len(args):
            self.fail("expected_arguments_got")
        lent = []
        for k, (want, arg) in enumerate(zip(ptypes, args)):
            if (k in places) != arg.startswith("&"):
                self.fail("argument_mode")
            if arg.startswith("&*"):
                self.authorize(self.instr, True)
                ty = self.use(arg[2:])
                if not isinstance(ty, Ptr) or not equal(ty.inner, want):
                    self.fail("raw_target_lend_wrong")
            elif arg.startswith("&"):
                if not equal(self.path_type(arg), want):
                    self.fail("lent_place_type_expected")
                lent.append(arg)
            else:
                got = self.use(arg)
                if not fits(got, want):
                    self.fail("argument_expected")
        self.lend(lent)
        return lent

    def authorize(self, instr: Instr, unsafe: bool) -> None:
        if unsafe and not (self.fun.unsafe or instr.unsafe):
            self.fail("unsafe_authorization")

    # ------------------------------------------------------ instructions --

    def check_instr(self, instr: Instr) -> None:
        self.formation.pos = instr.pos
        op = instr.op
        self.instr = instr
        result = OPERATIONS.get(op)
        if result is not None:
            out = result(self, instr)
        elif op in ARITH | ORDER | EQUALITY | BITS:
            out = self.primitive(instr)
        elif op.startswith("builtin::unsafe::atomic_"):
            out = self.atomic(instr)
        else:
            self.fail("unknown_instruction")
        outs = out if isinstance(out, list) else [out]
        if len(outs) != len(instr.lhs):
            self.fail("produces_results_named")
        for name, ty in zip(instr.lhs, outs):
            self.bind(name, ty)

    def op_const(self, instr: Instr) -> Type:
        t, lit = instr.ty, instr.lit
        if lit is None:
            self.fail("const_requires_literal")
        if not isinstance(t, Prim):
            self.fail("const_scalar_type")
        self.scalar_literal(t, lit)
        return t

    def scalar_literal(self, t: Prim, lit: str) -> None:
        if not valid_literal(t, lit):
            self.fail("invalid")

    def op_copy(self, instr: Instr) -> Type:
        t = self.peek(instr.args[0])
        if not self.copy_type(t):
            self.fail("copy_linear")
        return t

    def op_construct(self, instr: Instr) -> Type:
        if instr.ty is None:
            self.fail("instruction_requires_type")
        s = self.types.shape(instr.ty, self.module)
        if isinstance(s, Array):
            n = len(instr.args)
            if not (n == s.length or 0 < n < s.length and self.copy_type(s.elem)):
                self.fail("array_construct_element_copy")
            want = (s.elem,) * n
        else:
            want = self.types.fields(instr.ty, self.module)
            if len(want) != len(instr.args):
                self.fail("components_given")
        for arg, w in zip(instr.args, want):
            got = self.use(arg)
            if not fits(got, w):
                self.fail("component_expected")
        return instr.ty

    def op_destruct(self, instr: Instr) -> list[Type]:
        if instr.ty is None:
            self.fail("instruction_requires_type")
        arg = instr.args[0]
        if arg.startswith("&"):
            got = self.load(arg)
        else:
            got = self.use(arg)
        if isinstance(got, Ptr):
            if not equal(got, instr.ty):
                self.fail("destruct")
            return [Ptr(f) for f in self.types.fields(got.inner, self.module)]
        if not equal(got, instr.ty):
            self.fail("destruct")
        return list(self.types.fields(instr.ty, self.module))

    def op_construct_variant(self, instr: Instr) -> Type:
        if instr.ty is None:
            self.fail("instruction_requires_type")
        variants = dict(self.types.variants(instr.ty, self.module))
        if instr.lit is None or instr.lit not in variants:
            self.fail("variant")
        got = self.use(instr.args[0])
        if not fits(got, variants[instr.lit]):
            self.fail("payload_expected")
        return instr.ty

    def op_variant_tag(self, instr: Instr) -> Type:
        if instr.ty is None:
            self.fail("instruction_requires_type")
        arg = instr.args[0]
        if arg.startswith("&"):
            if not self.places.full(arg):
                self.fail("readable_tag")
            got = self.path_type(arg)
        else:
            got = self.peek(arg)
        if not equal(got, instr.ty):
            self.fail("variant_tag")
        self.types.variants(instr.ty, self.module)
        return U32

    def op_int_cast(self, instr: Instr) -> Type:
        got = self.use(instr.args[0])
        if not (isinstance(got, Prim) and got.name in INTEGERS and isinstance(instr.ty, Prim) and instr.ty.name in INTEGERS):
            self.fail("int_cast_converts_between")
        return instr.ty

    def op_unsafe_cast(self, instr: Instr) -> Type:
        if instr.ty is None:
            self.fail("instruction_requires_type")
        self.authorize(instr, True)
        got = self.use(instr.args[0])
        if any(contains(t, self.uncastable) for t in (got, instr.ty)):
            self.fail("unsafe_cast_cannot_produce")
        return instr.ty

    def uncastable(self, ty: Type) -> bool:
        """What no unsafe operation may conjure: a package, a brand witness, a function value, a transform,
        a value of an abstract type parameter, or Never."""
        return isinstance(ty, (Exists, Brand, Fn, Foreach, Never)) or \
            isinstance(ty, Named) and not ty.args and ty.name in self.kinds

    def primitive(self, instr: Instr) -> Type:
        if len(instr.args) != 2:
            self.fail("takes_two_operands")
        x, y = self.use(instr.args[0]), self.use(instr.args[1])
        if not equal(x, y):
            self.fail("operands_differ")
        name = x.name if isinstance(x, Prim) else None
        integer, floating = name in INTEGERS, name in FLOATS
        if instr.op in ORDER or instr.op in ARITH - {"mod"}:
            ok = integer or floating
        elif instr.op in BITS | {"mod"}:
            ok = integer
        else:
            ok = isinstance(x, (Prim, Ptr))
        if not ok:
            self.fail("does_apply")
        return BOOL if instr.op in ORDER | EQUALITY else x

    def op_call(self, instr: Instr) -> Type:
        ptypes, ret, unsafe, places = self.signature(instr)
        self.authorize(instr, unsafe)
        self.after_call(self.arguments(ptypes, instr.args[1:], places))
        return ret

    def op_trait_call(self, instr: Instr) -> Type:
        trait = self.program.traits.get(instr.args[0])
        if trait is None or len(trait.params) != len(instr.tys):
            self.fail("unknown_trait_wrong_arity")
        sig = next((m for m in trait.methods if m.name == instr.args[1]), None)
        if sig is None:
            self.fail("trait_method")
        if not self.holds(trait.name, instr.tys):
            self.fail("evidence")
        sub = dict(zip(trait.params, instr.tys))
        self.authorize(instr, sig.unsafe)
        self.after_call(self.arguments([subst(t, sub) for t in sig.ptypes], instr.args[2:], sig.places))
        return subst(sig.ret, sub)

    def op_fn(self, instr: Instr) -> Type:
        name = instr.args[0]
        if name in self.program.externs:
            ext = self.program.externs[name]
            if ext.ptypes is None or ext.ret is None:
                self.fail("extern_value_not_callable")
            return Fn(ext.ptypes, ext.ret, unsafe=True)
        fun = self.program.funs.get(name)
        if fun is None:
            self.fail("unknown_function")
        retained = tuple(p for p, t in zip(fun.params, instr.tys) if p.kind == BRAND and isinstance(t, Ignored))
        retained_brands = {param.name for param in retained}
        # A function value may keep brands quantified, but no premise on them: every premise is discharged
        # where the value is made.
        if any(retained_brands & free_brands(arg) for premise in fun.premises for arg in premise.args):
            self.fail("retained_brand_with_premise")
        sub = self.instantiate(fun.params, fun.premises, instr.tys, name)
        return Fn(tuple(subst(t, sub) for t in fun.ptypes), subst(fun.ret, sub), retained, fun.unsafe, fun.places)

    def has_memory_form(self, ty: Type) -> None:
        """How a zero-size type is laid out in memory is not decided yet, so nothing may observe it:
        `sizeof`, `alloca`, unsafe memory operations and extern parameters reject zero-size types."""
        if self.types.zero_size(ty):
            self.fail("zero_size_type_has_no_memory_form")

    def op_alloca(self, instr: Instr) -> Type:
        if instr.ty is None:
            self.fail("instruction_requires_type")
        self.authorize(instr, True)
        self.has_memory_form(instr.ty)
        return Ptr(instr.ty)

    def op_sizeof(self, instr: Instr) -> Type:
        if instr.ty is None:
            self.fail("instruction_requires_type")
        self.layout_visible(instr.ty)
        self.has_memory_form(instr.ty)
        return USIZE

    def layout_visible(self, ty: Type) -> None:
        """`sizeof` observes a layout: each nominal type stored by value in `ty` exposes its shape here.
        Pointers, function values and brand witnesses have a fixed size; a type parameter is answered by
        whoever instantiates it."""
        match ty:
            case Ptr() | Fn() | Brand() | Prim() | Never():
                return
            case Named(name=name, args=()) if name in self.kinds:
                return
        for part in children(self.types.shape(ty, self.module)):
            self.layout_visible(part)

    def op_data(self, instr: Instr) -> Type:
        data = self.program.datas.get(instr.args[0])
        if data is None:
            self.fail("unknown_data")
        return Ptr(data.ty)

    def op_unsafe_static_brand(self, instr: Instr) -> Type:
        self.authorize(instr, True)
        return Brand(Skolem(STATIC))

    def op_fresh(self, instr: Instr) -> Type:
        return Exists(("k",), Brand(Skolem("k")))

    def op_pack(self, instr: Instr) -> Type:
        t = instr.ty
        if not isinstance(t, Exists) or len(instr.tys) != len(t.binders):
            self.fail("pack_existential_type_brand")
        witnesses: list[str] = []
        for w in instr.tys:
            if not isinstance(w, Skolem) or w.label not in self.brands:
                self.fail("pack_witness_scope")
            witnesses.append(w.label)
        payload = subst(t.body, {b: w for b, w in zip(t.binders, instr.tys)})
        arg = instr.args[0]
        if arg.startswith("&"):
            root_view = self.places.opened.get(arg)
            if root_view is None or tuple(witnesses) != root_view:
                self.fail("open_at_those_brands")
            if not equal(self.path_type(arg), payload):
                self.fail("packing")
            if not self.places.full(arg):
                self.fail("pack_place_full")
            self.places.forget(arg, keep_own_view=False)
            if not equal(self.path_type(arg), t):
                self.fail("pack_place_type")
            return UNIT
        got = self.use(arg)
        if not fits(got, payload):
            self.fail("packing")
        return t

    def op_open(self, instr: Instr) -> Type:
        arg = instr.args[0]
        for b in instr.binders:
            if b in self.brands:
                self.fail("open_binder_already_scope")
            self.brands.add(b)
        if arg.startswith("&"):
            if arg in self.places.opened:
                self.fail("place_already_open")
            t = self.path_type(arg)
            if not isinstance(t, Exists) or len(t.binders) != len(instr.binders) or not self.places.full(arg):
                self.fail("full_package_brands")
            self.places.opened[arg] = instr.binders
            return UNIT
        t = self.use(arg)
        if not isinstance(t, Exists) or len(t.binders) != len(instr.binders):
            self.fail("open_binders")
        return subst(t.body, {b: Skolem(c) for b, c in zip(t.binders, instr.binders)})

    def op_unsafe_deref(self, instr: Instr) -> Type:
        self.authorize(instr, True)
        t = self.use(instr.args[0])
        if not isinstance(t, Ptr):
            self.fail("deref_non_pointer")
        if isinstance(t.inner, Never):
            self.fail("unsafe_read_cannot_produce_never")
        self.has_memory_form(t.inner)
        return t.inner

    op_unsafe_load_volatile = op_unsafe_deref

    def op_unsafe_store(self, instr: Instr) -> Type:
        self.authorize(instr, True)
        p, v = self.use(instr.args[0]), self.use(instr.args[1])
        if not isinstance(p, Ptr) or not fits(v, p.inner):
            self.fail("store_through_pointer_another")
        self.has_memory_form(p.inner)
        return UNIT

    op_unsafe_store_volatile = op_unsafe_store

    def atomic(self, instr: Instr) -> Type:
        self.authorize(instr, True)
        kind = instr.op[len("builtin::unsafe::atomic_"):]
        if kind not in ATOMIC_ARITY:
            self.fail("unknown_atomic_operation")
        if len(instr.args) != ATOMIC_ARITY[kind]:
            self.fail("atomic_operand_count")
        order = instr.lit
        if order is None:
            self.fail("atomic_requires_ordering")
        if kind == "compare_exchange":
            success, _, failure = order.partition("/")
            if success not in CAS_FAILURE or failure not in CAS_FAILURE[success]:
                self.fail("bad_compare_exchange_ordering")
        elif order not in ORDERINGS or kind == "load" and order in ("release", "acq_rel") or \
                kind == "store" and order in ("acquire", "acq_rel") or kind == "fence" and order == "relaxed":
            self.fail("bad_ordering")
        if kind == "fence":
            return UNIT
        p = self.use(instr.args[0])
        if not isinstance(p, Ptr):
            self.fail("atomic_operation_non_pointer")
        integer = isinstance(p.inner, Prim) and p.inner.name in INTEGERS
        if kind.startswith("fetch_") and not integer or \
                not (integer or p.inner == BOOL or isinstance(p.inner, Ptr)):
            self.fail("atomic_pointee_type")
        for arg in instr.args[1:]:
            if not equal(self.use(arg), p.inner):
                self.fail("atomic_operand_wrong_type")
        return {"load": p.inner, "store": UNIT, "compare_exchange": Tuple((p.inner, BOOL))}.get(kind, p.inner)

    def op_load(self, instr: Instr) -> Type:
        return self.load(instr.args[0])

    def op_store(self, instr: Instr) -> Type:
        self.store(instr.args[0], self.use(instr.args[1]))
        return UNIT

    def op_foreach(self, instr: Instr) -> Type:
        t = instr.ty
        if not isinstance(t, Foreach):
            self.fail("foreach_builds_foreach_type")
        got = self.use(instr.args[0])
        if not equal(got, t.source):
            self.fail("foreach_source")
        if not self.holds("builtin::Exposed", (t.source,)):
            self.fail("foreach_source_not_exposed")
        if not self.types.pattern_available(t, self.module, self.kinds):
            self.fail("foreach_pointer_pattern_requires_pointer_components")
        if not self.concrete_product(t.source):
            self.fail("foreach_source_is_a_concrete_struct_tuple_or_enum")
        mapper = self.program.funs.get(instr.args[1])
        if mapper is None:
            self.fail("unknown_mapper")
        if mapper.premises or len(instr.args) > 2:
            self.fail("foreach_mapper_without_premises_or_captures")
        binders = foreach_binders(t)
        if len(mapper.params) != len(binders) + len(instr.tys) or mapper.places:
            self.fail("mapper_takes_traversal_binders")
        abstract = {p.name: Named(b) for p, b in zip(mapper.params, binders)}
        own = dict(zip((p.name for p in mapper.params[len(binders):]), instr.tys))
        sub = {**abstract, **own}
        # The mapper is checked for an arbitrary component, not only today's rows.
        saved_kinds = self.kinds
        self.kinds = {**self.kinds, **{binder: "any" for binder in binders}}
        try:
            self.instantiate(mapper.params, mapper.premises,
                             tuple(sub[param.name] for param in mapper.params), mapper.name)
        finally:
            self.kinds = saved_kinds
        item = foreach_item(t)
        ptypes = [subst(p, sub) for p in mapper.ptypes]
        if not ptypes or not equal(ptypes[0], item) or not equal(subst(mapper.ret, sub), t.body):
            self.fail("mapper_does_map")
        for cap, want in zip(instr.args[2:], ptypes[1:]):
            got = self.use(cap)
            if not self.copy_type(got) or not fits(got, want):
                self.fail("foreach_capture_copy_fit")
        if len(instr.args) - 2 != len(ptypes) - 1:
            self.fail("foreach_capture_count_disagrees")
        self.authorize(instr, mapper.unsafe)
        return t

    def concrete_product(self, source: Type) -> bool:
        """A struct, tuple or enum whose shape is known here, or a pointer to one: not an array, a type
        parameter, or another transform."""
        if isinstance(source, Ptr):
            source = source.inner
        if isinstance(source, Foreach) or isinstance(source, Named) and source.name in self.kinds:
            return False
        return isinstance(self.types.shape(source, self.module), (Struct, Tuple, Enum))

    # ------------------------------------------------------- terminators --

    def check_term(self, term: Terminator, last: Optional[Instr]) -> None:
        match term:
            case Ret():
                got = self.use(term.value)
                if not fits(got, self.fun.ret):
                    self.fail("returning_function")
                self.accounted(self.used)
                for root, ty in self.roots.items():
                    if root in self.param_roots and any(prefix(root, path) for path in self.places.opened):
                        self.fail("place_parameter_sealed")
                    if root in self.param_roots and not self.places.full(root):
                        self.fail("place_parameter_restored")
                    if root not in self.param_roots and not self.no_linear(root, ty):
                        self.fail("local_place_still_holds")
                local = set().union(*(free_brands(t) for t in (got,))) - {
                    p.name for p in self.params if p.kind == BRAND} - {STATIC}
                if local and not isinstance(got, Never):
                    self.fail("result_mentions_local_brands")
            case Jmp():
                self.edge(term.edge, self.used, self.places)
            case Jnz():
                if self.use(term.cond) != BOOL:
                    self.fail("jnz_bool_condition")
                used = set(self.used)
                self.edge(term.then, set(used), self.places.copy())
                self.edge(term.other, set(used), self.places.copy())
            case Switch():
                self.switch(term)
            case Unreachable():
                if last is None or len(last.lhs) != 1 or not isinstance(self.env[last.lhs[0]], Never):
                    self.fail("unreachable_follow_instruction_producing")

    def edge(self, edge: Edge, used: set[str], places: Places, payload: Optional[Type] = None) -> None:
        target = self.blocks.get(edge.target)
        if target is None or target is self.fun.blocks[0]:
            self.fail("jump")
        if len(edge.tys) != len(target.brands):
            self.fail("takes_brand_arguments")
        sub = {}
        for b, t in zip(target.brands, edge.tys):
            if not isinstance(t, Skolem) or t.label not in self.brands:
                self.fail("brand_argument_scope")
            sub[b] = t
        args = ([payload] if payload is not None else []) + list(edge.args)
        if len(args) != len(target.params):
            self.fail("takes_arguments_got")
        saved = self.used
        self.used = used
        for arg, (name, want) in zip(args, target.params):
            got = arg if not isinstance(arg, str) else self.use(arg)
            if not fits(got, subst(want, sub)):
                self.fail("argument_expected")
        self.accounted(self.used)
        self.used = saved
        self.entails(places, target, sub)
        kept = {p.name for p in self.params if p.kind == BRAND} | {STATIC} | {
            b for b, t in sub.items() if t.label == b}
        for root, ty in self.roots.items():
            if root not in self.param_roots and not any(prefix(q, root) for q in target.empty) \
                    and not free_brands(ty) <= kept:
                self.fail("full_local_block_brand")

    def entails(self, places: Places, target: Block, sub: Mapping[str, Skolem]) -> None:
        saved, self.places = self.places, places
        try:
            wanted = {path: tuple(sub.get(b, Skolem(b)).label for b in brands)
                      for path, brands in target.opened}
            if places.opened != wanted:
                self.fail("opened_view_contract")
            for z in places.empty:
                if not any(prefix(q, z) for q in target.empty):
                    self.fail("empty_assumes_full")
            fresh = [q for q in target.empty if not places.covered(q)]
            for root in {segments(q)[0] for q in fresh}:
                own = [q for q in fresh if segments(q)[0] == root]
                # One newly empty path per root, or leaves of one full product tree.
                if len(own) > 1 and not (self.product_branches(own) and places.full(self.common_parent(own))):
                    self.fail("edge_empties_one_path_or_product_leaves")
            for q in target.empty:
                if not self.no_linear(q, self.storage_type(q)):
                    self.fail("holds_linear_contents_assumes")
            for path, variant in target.known:
                if places.known.get(path) != variant:
                    self.fail("assumes")
        finally:
            self.places = saved

    def switch(self, term: Switch) -> None:
        """An enum switch: one arm per variant, each variant once. A value switch passes each arm its
        variant's payload; a place switch tells each arm which variant the place holds."""
        arg, ty = term.scrutinee, term.ty
        place = arg.startswith("&")
        if place:
            got = self.path_type(arg)
            if not self.places.full(arg):
                self.fail("switch_which_full")
        else:
            got = self.use(arg)
        if not equal(got, ty):
            self.fail("switch")
        if isinstance(self.types.shape(ty, self.module), Prim):
            self.fail("switch_on_enum")
        variants = dict(self.types.variants(ty, self.module))
        arms = [arm.variant for arm in term.arms]
        if len(set(arms)) != len(arms) or set(arms) != set(variants):
            self.fail("switch_arm_per_variant")
        used = set(self.used)
        for arm in term.arms:
            places = self.places.copy()
            payload = None
            if place:
                places.known[arg] = arm.variant
            else:
                payload = variants[arm.variant]
            self.edge(arm.edge, set(used), places, payload)

OPERATIONS: dict[str, Callable[[Checker, Instr], Type | list[Type]]] = {
    "const": Checker.op_const,
    "copy": Checker.op_copy,
    "construct": Checker.op_construct,
    "destruct": Checker.op_destruct,
    "construct_variant": Checker.op_construct_variant,
    "variant_tag": Checker.op_variant_tag,
    "builtin::int_cast": Checker.op_int_cast,
    "builtin::unsafe::cast": Checker.op_unsafe_cast,
    "call": Checker.op_call,
    "trait_call": Checker.op_trait_call,
    "fn": Checker.op_fn,
    "alloca": Checker.op_alloca,
    "builtin::sizeof": Checker.op_sizeof,
    "data": Checker.op_data,
    "builtin::unsafe::static_brand": Checker.op_unsafe_static_brand,
    "builtin::fresh": Checker.op_fresh,
    "pack": Checker.op_pack,
    "open": Checker.op_open,
    "builtin::unsafe::deref": Checker.op_unsafe_deref,
    "builtin::unsafe::load_volatile": Checker.op_unsafe_load_volatile,
    "builtin::unsafe::store": Checker.op_unsafe_store,
    "builtin::unsafe::store_volatile": Checker.op_unsafe_store_volatile,
    "load": Checker.op_load,
    "store": Checker.op_store,
    "foreach": Checker.op_foreach,
}


def check_program(program: Program) -> Checker:
    checker = Checker(program)
    checker.check()
    return checker
