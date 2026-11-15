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
- Residences. A value loaded from a place, a component taken apart from one, and a value a lend
  adapter hands back are read where they lie, in the place, while nothing writes it (`residences`).
  A lend adapter whose borrowed values lie in places lends those places to its callee itself; a store
  of a value back where it lies writes nothing, and a construction stored at once into a place writes
  only the components that are not already there.
- Shared variables. A value read by one edge alone is assigned to the target parameter's variable, and
  a payload parameter that one arm alone enters reads the payload where the scrutinee lies (`share`).

The plan only reads the program; the KURS the checker accepted is what cgen.py translates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .check import overlap
from .core import ATOMIC_OPS, Block, Edge, Fun, Instr, Jmp, Jnz, Ret, Switch, Terminator, Type

# Instructions whose C writes only their results: left out when every result is dead.
PURE = frozenset({"const", "copy", "construct", "construct_variant", "destruct", "variant_tag", "load",
                  "int_cast", "to_ptr", "pack", "open", "static_brand",
                  "fresh",
                  "add", "sub", "mul", "bit_and", "bit_or", "bit_xor", "shl", "shr",
                  "eq", "ne", "lt", "le", "gt", "ge"})
# Instructions whose dead result the C does not assign: unit results of stores, and call results.
UNASSIGNED = frozenset({"store", "ptr_store", "ptr_store_volatile",
                        "ptr_atomic_store", "fence", "call", "trait_call"})
# Instructions that may write memory through a raw pointer, or run code that may: a place parameter
# lent from a raw target is reached so too.
OPAQUE = frozenset({"call", "trait_call", "foreach", "ptr_store", "ptr_store_volatile",
                    *(op for op in ATOMIC_OPS if op not in ("ptr_atomic_load", "fence"))})


@dataclass(frozen=True)
class Adapter:
    """A lend adapter, which lowering makes for a lend of a value: it stores its parameters at
    `borrowed` (in the order it hands them back) into places of its own, lends those to one call, and
    returns them reloaded beside the call's result in a tuple, or never returns (`returns` false)."""
    borrowed: tuple[int, ...]
    returns: bool


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


def is_place(arg: str) -> bool:
    return arg.startswith("&") and not arg.startswith("&*")


class Plan:
    def __init__(self, fun: Fun, components: Optional[dict[tuple[str, int], tuple[str, ...]]] = None,
                 adapters: Optional[dict[str, Adapter]] = None, params: frozenset[str] = frozenset(),
                 shapes: Optional[dict[tuple[str, int], Type]] = None) -> None:
        """`components`: for each (block, index) of a `destruct` or `construct` of a struct or tuple, the
        labels of its components in a place path, and in `shapes` its type with brands erased; `adapters`: the lend adapters the function calls;
        `params`: the place parameters, whose memory a raw pointer may reach."""
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
        # (block, value) -> the place path it is read from
        self.resident: dict[tuple[str, str], str] = {}
        # (block, index) of a lend adapter's call -> the paths it lends its callee, in the order handed back
        self.in_place: dict[tuple[str, int], tuple[str, ...]] = {}
        # (block, index) of each store that writes nothing, and of each construction its store writes in place
        self.unwritten: set[tuple[str, int]] = set()
        self.fused: set[tuple[str, int]] = set()
        for block in self.emitted:
            self.residences(block, components or {}, adapters or {}, params, shapes or {})
        self.live = self.liveness()
        # (block, value) -> (block, parameter) whose C variable the value is assigned to (`share`)
        self.shared: dict[tuple[str, str], tuple[str, str]] = {}
        # (block, payload parameter) -> (block, scrutinee, variant) whose payload the parameter reads (`share`)
        self.arrived: dict[tuple[str, str], tuple[str, str, str]] = {}
        self.share()
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
            out.update((block.label, name) for name, _ in block.params
                       if (block.label, name) in self.live and (block.label, name) not in self.arrived)
            for i, instr in enumerate(block.instrs):
                if instr.op == "copy" or self.is_forwarded_load(instr) or (block.label, i) in self.fused or \
                        (block.label, i) in self.in_place:
                    continue
                for name in instr.lhs:
                    if (block.label, name) in self.resident or (block.label, name) in self.shared:
                        continue
                    if (block.label, name) in self.live or instr.op not in PURE and instr.op not in UNASSIGNED:
                        out.add((block.label, name))
        return out

    # ----------------------------------------------------------------- sharing --

    def share(self) -> None:
        """Values that move along one edge share the C variable they move into.

        - A value an instruction of a block assigns, which one edge of the block reads and nothing else
          does, as the argument of a live parameter of another block, is assigned to the parameter's C
          variable (`shared`), and the edge assigns nothing. Only the edges into the parameter's block
          and that block itself touch the variable, and the edge enters the block after every read of
          the value.
        - The payload parameter of a block that one arm of a switch on a value enters, and no other
          edge, reads the payload where the scrutinee lies (`arrived`), when that is a C variable: only
          the switch's block assigns its variables, and it runs again only after the arm's block has
          left. A scrutinee lying in a place is not read so, since the arm's block may write the place."""
        incoming: dict[str, int] = {}
        for block in self.emitted:
            for edge, _ in term_edges(block.term):
                incoming[edge.target] = incoming.get(edge.target, 0) + 1
        for block in self.emitted:
            label, term = block.label, block.term
            # Nothing moves along an edge without arguments, nor a payload but a switch's.
            if not (isinstance(term, Switch) and not term.scrutinee.startswith("&")) and \
                    not any(edge.args for edge, _ in term_edges(term)):
                continue
            reads: dict[str, int] = {}
            for name in [name for instr in block.instrs for name in instr_reads(instr)] + term_reads(term) + \
                    [name for edge, _ in term_edges(term) for name in edge.args]:
                reads[name] = reads.get(name, 0) + 1
            assigned = {name for i, instr in enumerate(block.instrs) for name in instr.lhs
                        if instr.op != "copy" and not self.is_forwarded_load(instr) and (label, i) not in self.fused
                        and (label, i) not in self.in_place}
            for edge, skip in term_edges(term):
                target = self.blocks[edge.target]
                if target.label in self.return_index or target.label == label:
                    continue
                for (param, _), arg in zip(target.params[skip:], edge.args):
                    if arg in assigned and reads.get(arg) == 1 and (label, arg) not in self.resident and \
                            (target.label, param) in self.live:
                        self.shared[label, arg] = (target.label, param)
            if isinstance(term, Switch) and not term.scrutinee.startswith("&") and \
                    not self.resident.get((label, term.scrutinee), "%").startswith("&"):
                for arm in term.arms:
                    target = self.blocks[arm.edge.target]
                    if target.label not in self.return_index and target.label != label and \
                            incoming.get(target.label) == 1 and target.params:
                        self.arrived[target.label, target.params[0][0]] = (label, term.scrutinee, arm.variant)

    # -------------------------------------------------------------- residences --

    def residences(self, block: Block, components: dict[tuple[str, int], tuple[str, ...]],
                   adapters: dict[str, Adapter], params: frozenset[str], shapes: dict[tuple[str, int], Type]) -> None:
        """Which values of `block` are read where they lie, and what that saves.

        A value lies at a path: in a place P when it was loaded from P; in the C variable of a value `%v`
        (the path `%v`) when it is `v`; and at `Q.label` when it is a component taken apart from the place
        Q or from a value lying at Q; and at Q when it is constructed of components each lying at its own
        member of Q (and read so). Values that copy, open, pack or forward one lie where it does (one
        class), as does the value a lend adapter hands back when the call lends its callee the path the
        borrowed value lies at. A class is read where it lies when, from the instruction that took its
        first value out of the path to the class's last read in the block, nothing writes a path
        overlapping it: no store, no lend, no adapter that may lend such a path for another class, and,
        under a place parameter, no call or raw write at all (memory lent from a raw target may be reached
        through a pointer). A C variable is assigned once in a block, so only a lend in place writes it. A
        call evaluates its arguments before it runs, so the call that reads the class last may write it.

        A lend adapter's call lends its callee the paths its borrowed values lie at, instead of copying
        them into places of its own, when each is read there and read last by the call, the paths overlap
        neither each other nor another place the call lends, and the destruct taking its result apart
        follows it; a value lying in a C variable is read no more after the call, and a place has the
        value handed back stored into it again before anything else names it, so nothing sees the place
        between the lend and the store but the callee."""
        label, instrs = block.label, block.instrs
        n = len(instrs)
        # Nothing lies anywhere, nor is stored in place, in a block with no load, destruct, construct or call.
        if not any(instr.op in ("load", "destruct", "construct", "call") for instr in instrs):
            return
        alias: dict[str, str] = {}

        def find(name: str) -> str:
            while name in alias:
                name = alias[name]
            return name

        stored: dict[str, str] = {}
        # path -> the type of the value there that a destruct took apart
        held: dict[str, Type] = {}
        # class -> (path, index its first value came out of the path, adapter call it waits on or -1)
        candidates: dict[str, tuple[str, int, int]] = {}
        # adapter call index -> (class, path) of each borrowed value, in the order handed back
        lent: dict[int, list[tuple[str, str]]] = {}
        for i, instr in enumerate(instrs):
            op, a, lhs = instr.op, instr.args, instr.lhs
            if op in ("copy", "open", "pack") and a[0].startswith("%"):
                alias[lhs[0]] = find(a[0])
            elif op == "store" and a[0] in self.forwarded:
                stored[a[0]] = a[1]
            elif op == "load" and a[0] in self.forwarded:
                alias[lhs[0]] = find(stored[a[0]])
            elif op == "load" and is_place(a[0]):
                candidates[lhs[0]] = (a[0], i, -1)
            elif op == "destruct" and (label, i) in components and i - 1 not in lent:
                base = (a[0], i, -1) if is_place(a[0]) else candidates.get(find(a[0]), (find(a[0]), i, -1))
                held[base[0]] = shapes[label, i]
                for name, part in zip(lhs, components[label, i]):
                    candidates[name] = (f"{base[0]}.{part}", base[1], base[2])
            elif op == "call" and a[0] in adapters:
                adapter = adapters[a[0]]
                actuals = [find(a[1 + m]) for m in adapter.borrowed]
                if not all(x.startswith("%") for x in actuals):
                    continue
                if adapter.returns and (i + 1 >= n or instrs[i + 1].op != "destruct" or instrs[i + 1].args != (lhs[0],)):
                    continue
                for x in actuals:
                    candidates.setdefault(x, (x, -1, -1))
                lent[i] = [(x, candidates[x][0]) for x in actuals]
                if adapter.returns:
                    for name, x in zip(instrs[i + 1].lhs, actuals):
                        candidates[name] = (candidates[x][0], i, i)

        last: dict[str, int] = {}
        reads: dict[str, int] = {}
        for i, instr in enumerate(instrs):
            for name in instr_reads(instr):
                last[find(name)] = i
                reads[find(name)] = reads.get(find(name), 0) + 1
        for name in term_reads(block.term) + [name for edge, _ in term_edges(block.term) for name in edge.args]:
            last[find(name)] = n

        def kept(c: str, path: str, origin: int, until: Optional[int] = None) -> bool:
            """Nothing writes a path overlapping `path` after `origin` up to the class's last read, or
            up to `until`."""
            end = last.get(c, origin) if until is None else until
            under_param = root(path) in params
            for j in range(origin + 1, min(end, n - 1) + 1):
                op, a = instrs[j].op, instrs[j].args
                if op == "store":
                    if overlap(a[0], path) and not (j == end and a[0] == path and find(a[1]) == c):
                        return False
                elif op in ("call", "trait_call") and j == end:
                    continue
                elif under_param and op in OPAQUE:
                    return False
                elif op in ("call", "trait_call") and (
                        any(is_place(arg) and overlap(arg, path) for arg in a) or
                        any(x != c and overlap(q, path) for x, q in lent.get(j, ()))):
                    return False
            return True

        def stored_back(j: int, k: int, path: str) -> bool:
            """The value the adapter call at `j` hands back `k`th is stored into the place `path` again
            before any other instruction names a path overlapping it."""
            back = find(instrs[j + 1].lhs[k])
            for s in range(j + 2, n):
                instr = instrs[s]
                if not any(is_place(arg) and overlap(arg, path) for arg in instr.args):
                    continue
                if instr.op != "store":
                    return False
                target, value = instr.args
                if target == path:
                    return find(value) == back
                made = instrs[s - 1]
                if made.op != "construct" or made.lhs[0] != value or (label, s - 1) not in components:
                    return False
                return any(f"{target}.{part}" == path and find(arg) == back
                           for arg, part in zip(made.args, components[label, s - 1]))
            return False

        valid = {c for c, (path, origin, _) in candidates.items() if kept(c, path, origin)}
        for i in sorted(lent):
            paths = [q for _, q in lent[i]]
            others = [arg for arg in instrs[i].args[1:] if is_place(arg)]
            if all(x in valid and last.get(x) == i and
                   (candidates[x][2] == -1 or (label, candidates[x][2]) in self.in_place) and
                   (last.get(root(q), -1) <= i if q.startswith("%") else stored_back(i, k, q))
                   for k, (x, q) in enumerate(lent[i])) and \
                    not any(overlap(p, q) for k, p in enumerate(paths) for q in paths[k + 1:]) and \
                    not any(overlap(p, q) for p in paths for q in others):
                self.in_place[label, i] = tuple(paths)

        def lies(c: str, path: str, at: int) -> bool:
            """Class `c` lies at `path` when instruction `at` runs (it may be read elsewhere later)."""
            if c not in candidates or candidates[c][0] != path:
                return False
            _, origin, waits = candidates[c]
            return (waits == -1 or (label, waits) in self.in_place) and kept(c, path, origin, at)

        # A construction whose components each lie at their own member of one path when it is made, a
        # path where a destruct took apart a value of the construction's type, lies at that path.
        for i, instr in enumerate(instrs):
            if instr.op != "construct" or (label, i) not in components or not instr.args or \
                    len(components[label, i]) != len(instr.args):
                continue
            parts, first = components[label, i], find(instr.args[0])
            if first not in candidates or not candidates[first][0].endswith("." + parts[0]):
                continue
            whole = candidates[first][0][:-len(parts[0]) - 1]
            if held.get(whole) == shapes[label, i] and \
                    all(lies(find(arg), f"{whole}.{part}", i) for arg, part in zip(instr.args, parts)):
                candidates[instr.lhs[0]] = (whole, i, -1)
                if kept(instr.lhs[0], whole, i):
                    valid.add(instr.lhs[0])
        for instr in instrs:
            for name in instr.lhs:
                c = find(name)
                if c in valid and (candidates[c][2] == -1 or (label, candidates[c][2]) in self.in_place) and \
                        candidates[c][0] != name:
                    self.resident[label, name] = candidates[c][0]

        for i, instr in enumerate(instrs):
            a = instr.args
            if instr.op == "store" and a[0] not in self.forwarded and self.resident.get((label, a[1])) == a[0]:
                self.unwritten.add((label, i))
            elif instr.op == "construct" and (label, i) in components and i + 1 < n and \
                    (label, instr.lhs[0]) not in self.resident:
                store, made = instrs[i + 1], find(instr.lhs[0])
                if store.op != "store" or store.args[1] != instr.lhs[0] or root(store.args[0]) in self.forwarded \
                        or reads.get(made) != 1 or last.get(made) != i + 1:
                    continue
                target = store.args[0]
                if all(not overlap(self.resident.get((label, arg), "%"), target) or
                       self.resident[label, arg] == f"{target}.{part}" for arg, part in zip(a, components[label, i])):
                    self.fused.add((label, i))


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
