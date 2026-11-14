"""What the C backend leaves out of one function, decided from its checked KURS before any C is written.

The canonical translation gives every SSA value a C variable, every place a C object, and every edge
a parallel assignment through scratch variables. `plan` finds where less does the same:

- Live values. A value is live when the C reads it: an instruction that is kept reads it, a terminator
  tests or returns it, or an edge passes it to a live parameter of its target. A block parameter is live
  when its own block reads it, so liveness is a fixpoint over the blocks. A pure instruction (`PURE`)
  whose results are all dead is left out; a store's unit result and a call's result that are dead are
  not assigned. A dead parameter is assigned by no edge.
- Copies. A `copy` reads its source's C variable instead of having one of its own. Within a block every
  C variable is assigned once before it is read, and a block's parameters only on the edges into it, so
  the source holds the value wherever the copy is read.
- Forwarded places. A local place named by exactly two instructions of the whole function, a store of
  the whole place followed in the same block by a load of the whole place, holds the stored value from
  one to the other and is read nowhere else: the load reads the stored value's C variable, the store
  writes nothing, and the place has no C object. Lowering makes such a place for every operand that
  waits while the next one is evaluated.
- Return joins. A block with no instructions that returns one of its parameters, or jumps to another
  return join passing its parameters on, is a return join (`return_index`): an edge into it returns the
  value it passes at that position.
- Tail calls. A call, the last instruction of its block, whose result the block returns, directly or
  through return joins (`tail_call`). cgen.py decides whether its C can be a tail call.

The plan only reads the program; the KURS the checker accepted is what cgen.py translates."""

from __future__ import annotations

from typing import Optional

from .core import Block, Edge, Fun, Instr, Jmp, Jnz, Ret, Switch, Terminator

# Instructions whose C writes only their results: left out when every result is dead.
PURE = frozenset({"const", "copy", "construct", "construct_variant", "destruct", "variant_tag", "load",
                  "builtin::int_cast", "builtin::as_pointer", "pack", "open", "builtin::unsafe::static_brand",
                  "builtin::fresh",
                  "add", "sub", "mul", "bit_and", "bit_or", "bit_xor", "shl", "shr",
                  "eq", "ne", "lt", "le", "gt", "ge"})
# Instructions whose dead result the C does not assign: unit results of stores, and call results.
UNASSIGNED = frozenset({"store", "builtin::unsafe::store", "builtin::unsafe::store_volatile",
                        "builtin::unsafe::atomic_store", "builtin::unsafe::atomic_fence", "call", "trait_call"})


def value_read(arg: str) -> Optional[str]:
    """The SSA value an operand reads: `%x`, or the pointer of a raw target `&*%x`; none for a place."""
    if arg.startswith("%"):
        return arg
    if arg.startswith("&*"):
        return arg[2:]
    return None


def instr_reads(instr: Instr) -> list[str]:
    return [v for v in map(value_read, instr.args) if v is not None]


def term_reads(term: Terminator) -> list[str]:
    """What a terminator reads besides its edges' arguments."""
    if isinstance(term, Ret):
        return [term.value]
    if isinstance(term, Jnz):
        return [term.cond]
    if isinstance(term, Switch) and not term.scrutinee.startswith("&"):
        return [term.scrutinee]
    return []


def term_edges(term: Terminator) -> list[tuple[Edge, int]]:
    """Each edge of a terminator, with the number of target parameters before its arguments: a switch
    on a value passes the variant's payload first."""
    if isinstance(term, Jmp):
        return [(term.edge, 0)]
    if isinstance(term, Jnz):
        return [(term.then, 0), (term.other, 0)]
    if isinstance(term, Switch):
        payload = 0 if term.scrutinee.startswith("&") else 1
        return [(arm.edge, payload) for arm in term.arms]
    return []


def root(path: str) -> str:
    return path.split(".")[0]


class Plan:
    def __init__(self, fun: Fun) -> None:
        self.fun = fun
        self.blocks = {b.label: b for b in fun.blocks}
        self.forwarded = self.forwarded_places()
        self.return_index: dict[str, int] = {}
        for block in fun.blocks[1:]:
            index = self.returned(block.label, set())
            if index is not None:
                self.return_index[block.label] = index
        # The blocks the C has: every edge into a return join returns instead.
        self.emitted = [b for b in fun.blocks if b.label not in self.return_index]
        self.live = self.liveness()
        self.variables = self.c_variables()

    def is_live(self, label: str, name: str) -> bool:
        return (label, name) in self.live

    # ------------------------------------------------------------- forwarding --

    def forwarded_places(self) -> set[str]:
        """The local places a store and a load alone name, the load later in the store's block."""
        named: dict[str, list[tuple[int, int, Optional[Instr], str]]] = {}
        for b, block in enumerate(self.fun.blocks):
            for i, instr in enumerate(block.instrs):
                for arg in instr.args:
                    if arg.startswith("&") and not arg.startswith("&*"):
                        named.setdefault(root(arg), []).append((b, i, instr, arg))
            term = block.term
            if isinstance(term, Switch) and term.scrutinee.startswith("&"):
                named.setdefault(root(term.scrutinee), []).append((b, len(block.instrs), None, term.scrutinee))
        out: set[str] = set()
        for name, _ in self.fun.locals:
            uses = named.get(name, [])
            if len(uses) != 2:
                continue
            (b1, i1, first, path1), (b2, i2, second, path2) = uses
            if (first is not None and second is not None and first.op == "store" and second.op == "load"
                    and path1 == name and path2 == name and b1 == b2 and i1 < i2):
                out.add(name)
        return out

    def is_forwarded_load(self, instr: Instr) -> bool:
        return instr.op == "load" and instr.args[0] in self.forwarded

    # --------------------------------------------------------------- liveness --

    def liveness(self) -> set[tuple[str, str]]:
        live_params: set[tuple[str, str]] = set()
        changed = True
        while changed:
            changed = False
            for block in reversed(self.emitted):
                used = self.block_reads(block, live_params)
                for name, _ in block.params:
                    if name in used and (block.label, name) not in live_params:
                        live_params.add((block.label, name))
                        changed = True
        live: set[tuple[str, str]] = set()
        for block in self.emitted:
            live.update((block.label, name) for name in self.block_reads(block, live_params))
        return live

    def block_reads(self, block: Block, live_params: set[tuple[str, str]]) -> set[str]:
        """The names of `block` the C reads, given which parameters of other blocks are live."""
        used = set(term_reads(block.term))
        for edge, skip in term_edges(block.term):
            target = self.blocks[edge.target]
            if target.label in self.return_index:
                # The payload, when it is the value returned, is the scrutinee's, which the switch reads.
                if self.return_index[target.label] >= skip:
                    used.add(edge.args[self.return_index[target.label] - skip])
                continue
            for (param, _), arg in zip(target.params[skip:], edge.args):
                if (target.label, param) in live_params:
                    used.add(arg)
        for instr in reversed(block.instrs):
            if instr.op in PURE and not any(name in used for name in instr.lhs):
                continue
            used.update(instr_reads(instr))
        return used

    def c_variables(self) -> set[tuple[str, str]]:
        """The values that need a C variable: live parameters, and the results the C assigns."""
        out: set[tuple[str, str]] = set()
        for block in self.emitted:
            out.update((block.label, name) for name, _ in block.params if (block.label, name) in self.live)
            for instr in block.instrs:
                if instr.op == "copy" or self.is_forwarded_load(instr):
                    continue
                for name in instr.lhs:
                    if (block.label, name) in self.live or instr.op not in PURE and instr.op not in UNASSIGNED:
                        out.add((block.label, name))
        return out

    # ----------------------------------------------------------------- returns --

    def returned(self, label: str, seen: set[str]) -> Optional[int]:
        """The position of the parameter a return join returns."""
        block = self.blocks[label]
        if block.instrs or label in seen:
            return None
        seen.add(label)
        names = [name for name, _ in block.params]
        term = block.term
        if isinstance(term, Ret):
            return names.index(term.value) if term.value in names else None
        if isinstance(term, Jmp) and all(arg in names for arg in term.edge.args):
            index = self.returned(term.edge.target, seen)
            return None if index is None else names.index(term.edge.args[index])
        return None

    def tail_call(self, block: Block) -> Optional[Instr]:
        """The call whose result `block` returns, when it is the block's last instruction."""
        if not block.instrs:
            return None
        call = block.instrs[-1]
        if call.op not in ("call", "trait_call") or len(call.lhs) != 1:
            return None
        term = block.term
        if isinstance(term, Ret):
            returned = term.value
        elif isinstance(term, Jmp) and term.edge.target in self.return_index:
            returned = term.edge.args[self.return_index[term.edge.target]]
        else:
            return None
        return call if returned == call.lhs[0] else None

    def has_alloca(self) -> bool:
        return any(instr.op == "alloca" for block in self.fun.blocks for instr in block.instrs)
