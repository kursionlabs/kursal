"""Infer block place contracts after surface expressions have been lowered.

This pass follows the finished control-flow graph. It is separate from the
syntax-directed translation rules in lower.py and reuses the core checker's
path typing and place-state helpers to compute candidate block contracts.
Planning assumes the graph structure supplied by lowering; it does not validate
instructions or prove the contracts. With diagnose=True, a subsequent replay of
finalized headers reports named-place ownership errors. This does not check SSA
accounting or all contract obligations. Callers must still run check_program on
the lowered program before passing it and the resulting checker to emit_c.
"""

from typing import Never, Optional

from . import core as C
from .check import Checker, CheckError, Places, prefix
from .types import free_brands
from .sexpr import KursError, Position


class OwnershipError(KursError):
    """A source ownership diagnostic, not a core assertion failure."""


class PlaceFlow(Checker):
    """Computes each block's declared place state (`empty`, `known`, `opened`) by a
    forward dataflow over the finished graph, tracking the place effects of
    instructions: a block assumes empty whatever any incoming edge leaves
    empty. Known variants and opened views are retained only when incoming
    edges agree after brand renaming. This intersection is only a candidate:
    dropping an incoming view does not close it, and exact edge checking will
    reject that predecessor unless lowering explicitly closes it first."""

    def plan(self, fun: C.Fun, instance: Optional[C.Instance] = None, *, diagnose: bool = False) -> None:
        params = (instance.params if instance else ()) + fun.params
        self.fun, self.params, self.where = fun, params, fun.name
        self.kinds = {p.name: p.kind for p in params}
        self.module = fun.module
        self.roots = dict(fun.locals)
        entry = fun.blocks[0]
        for name, ty in entry.params:
            if name.startswith("&"):
                self.roots[name] = ty
        blocks = {b.label: b for b in fun.blocks}
        header: dict[str, Places] = {}
        global_brands = {p.name for p in params if p.kind == C.BRAND} | {"static"}
        self.places = Places()
        work = [(entry, Places(empty={n for n, _ in fun.locals}))]
        while work:
            block, incoming = work.pop()
            if block is not entry:
                state = header.get(block.label)
                if state is None:
                    header[block.label] = state = incoming.copy()
                else:
                    merged = Places(state.empty | incoming.empty,
                                    {p: v for p, v in state.known.items() if incoming.known.get(p) == v},
                                    dict(state.opened))
                    merged.keep_opened({p: b for p, b in state.opened.items() if incoming.opened.get(p) == b})
                    self.abstract_variants(merged, state, incoming)
                    if merged == state:
                        continue
                    header[block.label] = state = merged
                places = state.copy()
            else:
                places = incoming
            self.places = places
            self.simulate(block)
            for edge, arm_known in self.edges(block):
                target = blocks[edge.target]
                out = places.copy()
                if arm_known:
                    out.known[arm_known[0]] = arm_known[1]
                rename = {t.label: b for b, t in zip(target.brands, edge.tys) if isinstance(t, C.Skolem)}
                available = global_brands | set(target.brands)
                kept = global_brands | {b for b, t in zip(target.brands, edge.tys)
                                        if isinstance(t, C.Skolem) and t.label == b}
                # Existing edge entailment permits forgetting Copy contents.
                # A dead local brand need not survive in an unreadable slot.
                for root, ty in fun.locals:
                    if not free_brands(ty) <= kept and self.copy_type(ty):
                        if not any(prefix(root, p) for p in out.opened):
                            out.empty = {p for p in out.empty if not prefix(root, p)} | {root}
                            out.forget(root)
                opened = {p: tuple(rename.get(b, b) for b in brands) for p, brands in out.opened.items()}
                out.keep_opened({p: brands for p, brands in opened.items() if set(brands) <= available})
                work.append((target, out))
        for block in fun.blocks:
            state = header.get(block.label)
            if state is not None and block is not entry:
                block.empty = tuple(sorted(state.empty))
                block.known = tuple(sorted(state.known.items()))
                block.opened = tuple(sorted(state.opened.items()))

        if diagnose:
            self.diagnose_places(fun, set(header) | {entry.label})

    def ownership_error(self, path: str, message: str, pos: Optional[Position], *, origin: bool = False) -> Never:
        root, dot, fields = path.removeprefix("&").partition(".")
        temporary = root.startswith("#tmp")
        name = ("temporary expression result" if temporary else root.split("#", 1)[0]) + dot + fields
        if temporary or origin:
            pos = self.origins.get("&" + root) or self.fun.pos or pos
        raise OwnershipError(f"{self.fun.name}: {message.format(name=name)}", pos)

    def diagnose_places(self, fun: C.Fun, reachable: set[str]) -> None:
        """Replay finalized headers for source diagnostics, without checking SSA.

        Do not diagnose while the fixed point is evolving: a provisional header
        can still change. Reuse path/state helpers, but do not invoke the core
        instruction, edge, or function validators. Core checking remains required.
        """
        blocks = {b.label: b for b in fun.blocks}
        self.origins: dict[str, Optional[Position]] = {}
        for block in fun.blocks:
            for instr in block.instrs:
                if instr.op == "store":
                    self.origins.setdefault(instr.args[0], instr.pos)
        parameters = {name for name, _ in fun.blocks[0].params if name.startswith("&")}
        for block in fun.blocks:
            if block.label not in reachable:
                continue
            self.places = (Places(empty={n for n, _ in fun.locals}) if block is fun.blocks[0]
                           else Places(set(block.empty), dict(block.known), dict(block.opened)))
            self.simulate(block, diagnose=True)
            if isinstance(block.term, C.Ret):
                for root, ty in self.roots.items():
                    try:
                        if root in parameters:
                            if not self.places.full(root):
                                self.ownership_error(root, "lent value '{name}' must be restored before return", block.term.pos)
                        elif not self.no_linear(root, ty):
                            self.ownership_error(root, "linear value '{name}' is not consumed before return", block.term.pos, origin=True)
                    except CheckError:
                        pass  # A malformed path/view is still a core assertion.
            for edge, known in self.edges(block):
                saved = self.places
                self.places = saved.copy()
                if known:
                    self.places.known[known[0]] = known[1]
                try:
                    for path in blocks[edge.target].empty:
                        try:
                            if not self.no_linear(path, self.storage_type(path)):
                                self.ownership_error(path, "linear value '{name}' remains live on some paths "
                                                     f"to {edge.target}", block.term.pos)
                        except CheckError:
                            pass
                finally:
                    self.places = saved

    def abstract_variants(self, merged: Places, left: Places, right: Places) -> None:
        """Forget a lost variant's payload only if both states can discard it.

        Keep live linear contents and opened views for the core to diagnose;
        intersecting knowledge must never silently consume or close them.
        """
        lost = (left.known.keys() | right.known.keys()) - merged.known.keys()
        saved = self.places
        try:
            for path in sorted(lost):
                if not any(prefix(path, p) and path != p for p in merged.empty):
                    continue
                if any(prefix(path, p) for state in (left, right) for p in state.opened):
                    continue
                discardable = True
                for state in (left, right):
                    self.places = state
                    try:
                        discardable = discardable and self.no_linear(path, self.storage_type(path))
                    except CheckError:
                        discardable = False
                if discardable:
                    merged.empty = {p for p in merged.empty if not prefix(path, p)} | {path}
                    merged.forget(path)
            merged.empty = {p for p in merged.empty
                            if not any(q != p and prefix(q, p) for q in merged.empty)}
        finally:
            self.places = saved

    def simulate(self, block: C.Block, *, diagnose: bool = False) -> None:
        """Track moves, stores, lends and package views without checking SSA values.

        A load needs its path's type to decide whether it moves linear contents.
        If that query raises CheckError, leave the place state unchanged and
        continue planning. Unknown roots, unrefined enum projections and invalid
        opened views are diagnosed by the subsequent core checker. It checks
        every instruction and requires each edge to satisfy its target contract.
        """
        for i in block.instrs:
            try:
                if i.op == "load" or i.op == "destruct" and i.args[0].startswith("&"):
                    path = i.args[0]
                    if diagnose and not self.places.full(path):
                        self.ownership_error(path, "value '{name}' has been moved or is only partially available", i.pos)
                    if not self.copy_type(self.path_type(path)):
                        self.places.empty.add(path)
                        self.places.forget(path)
                elif i.op == "store":
                    path = i.args[0]
                    if diagnose and not self.no_linear(path, self.storage_type(path)):
                        self.ownership_error(path, "cannot overwrite live linear value '{name}'", i.pos)
                    self.places.empty = {z for z in self.places.empty if not prefix(path, z)}
                    self.places.forget(path)
                elif i.op == "variant_tag" and i.args[0].startswith("&"):
                    path = i.args[0]
                    if diagnose and self.places.covered(path):
                        self.ownership_error(path, "enum value '{name}' has been moved; its tag is unavailable", i.pos)
                elif i.op in ("call", "trait_call"):
                    for arg in i.args:
                        if arg.startswith("&") and not arg.startswith("&*"):
                            if diagnose and not self.places.full(arg):
                                self.ownership_error(arg, "value '{name}' has been moved and cannot be lent", i.pos)
                            self.places.forget(arg)
                elif i.op == "open" and i.args[0].startswith("&"):
                    self.places.opened[i.args[0]] = i.binders
                elif i.op == "pack" and i.args[0].startswith("&"):
                    self.places.forget(i.args[0], keep_own_view=False)
            except CheckError:
                pass  # Defer the failed load-path query to core checking.

    def edges(self, block: C.Block) -> list[tuple[C.Edge, Optional[tuple[str, str]]]]:
        t = block.term
        if isinstance(t, C.Jmp):
            return [(t.edge, None)]
        if isinstance(t, C.Jnz):
            return [(t.then, None), (t.other, None)]
        if isinstance(t, C.Switch):
            place = t.scrutinee.startswith("&")
            return [(a.edge, (t.scrutinee, a.variant) if place else None) for a in t.arms]
        return []

