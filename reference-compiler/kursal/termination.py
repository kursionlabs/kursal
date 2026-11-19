"""SCC restrictions for instance search and function specialization.

For specialization inside a recursive component, pass parameters unchanged or choose fixed types.
Across components, arbitrary construction is finite because the SCC graph is a
DAG. Instance matching only extracts subterms of the trait arguments. Thus each
component draws from a finite set of incoming types, constants and subterms;
memoizing specialization tuples exhausts that set. This is conservative: a
rejected cycle need not actually grow (another edge might reset its arguments).
Instance search instead requires structural descent on every recursive edge.
"""

from collections.abc import Iterable, Sequence
from typing import Optional

from .core import BRAND, Foreach, Fun, Instr, Named, Param, Premise, Program
from .sexpr import KursError, Position, located
from .types import Surface, free_names, show
from .formation import strict_subterm

type Key = tuple[str, ...]  # (function,) or (trait, method)


def components(graph: dict[Key, set[Key]]) -> dict[Key, int]:
    """Tarjan's algorithm: mutually reachable nodes receive the same ID."""
    indices: dict[Key, int] = {}
    low: dict[Key, int] = {}
    stack: list[Key] = []
    active: set[Key] = set()
    result: dict[Key, int] = {}

    def visit(node: Key) -> None:
        indices[node] = low[node] = len(indices)
        stack.append(node)
        active.add(node)
        for target in graph[node]:
            if target not in indices:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in active:
                low[node] = min(low[node], indices[target])
        if low[node] == indices[node]:
            while True:
                member = stack.pop()
                active.remove(member)
                result[member] = indices[node]
                if member == node:
                    break

    for node in graph:
        if node not in indices:
            visit(node)
    return result


def check_instance_termination(
    instances: Iterable[tuple[Premise, tuple[Premise, ...], Optional[Position]]], files: Sequence[str] = (),
    surface: bool = False,
) -> None:
    """Every premise edge within a trait SCC decreases its first argument.

    A finite proof tree follows: each recursive component strictly decreases
    a finite type term, and edges between components follow a finite DAG.
    Headers are shared by surface inference and KURS, before either searches. `files` names the file of each
    header, when it has one. With `surface` set, a message writes trait names as a message about source does
    (`types.Surface`).
    """
    headers = list(instances)
    graph: dict[Key, set[Key]] = {}
    for head, premises, _ in headers:
        graph.setdefault((head.trait,), set())
        for premise in premises:
            graph.setdefault((premise.trait,), set())
            graph[(head.trait,)].add((premise.trait,))
    scc = components(graph)
    for k, (head, premises, pos) in enumerate(headers):
        for premise in premises:
            if scc[(head.trait,)] == scc[(premise.trait,)]:
                if not head.args or not premise.args or not strict_subterm(premise.args[0], head.args[0]):
                    spell = Surface((), {}, (head.trait, premise.trait)).name if surface else str
                    error = KursError(f"recursive instance premise {spell(head.trait)} -> {spell(premise.trait)}: "
                                      "first argument must be a strict structural subterm of the head's "
                                      "first argument", pos)
                    raise located(error, files[k], pos) if files else error


def check_mono_termination(program: Program) -> None:
    """Check every specialization request before starting the backend worklist.

    Trait-method nodes unite all their instance bodies. Indirect calls require
    no edge: their code was already requested at a statically named `fn` site.
    Lifted closures and foreach mappers are ordinary functions in this graph.
    """
    bodies: list[tuple[Key, Fun, tuple[Param, ...]]] = [
        ((fun.name,), fun, fun.params) for fun in program.funs.values()]
    bodies.extend(((inst.trait, fun.name), fun, inst.params + fun.params)
                  for instances in program.instances.values() for inst in instances for fun in inst.methods)
    graph: dict[Key, set[Key]] = {key: set() for key, _, _ in bodies}
    sites: list[tuple[Key, Key, set[str], Instr]] = []
    for key, fun, params in bodies:
        scope = {p.name for p in params if p.kind != BRAND}  # brands have no type constructors and cannot grow
        for block in fun.blocks:
            for instr in block.instrs:
                target: Key
                if instr.op in {"call", "fn"}:
                    target = (instr.args[0],)
                elif instr.op == "trait_call":
                    target = (instr.args[0], instr.args[1])
                elif instr.op == "foreach":
                    target = (instr.args[1],)
                else:
                    continue
                if target in graph:
                    graph[key].add(target)
                    sites.append((key, target, scope, instr))
    scc = components(graph)
    for caller, callee, scope, instr in sites:
        if scc[caller] != scc[callee]:
            continue
        context = f"recursive specialization {'::'.join(caller)} -> {'::'.join(callee)}"
        for ty in instr.tys:
            if isinstance(ty, Named) and not ty.args and ty.name in scope:
                continue
            if free_names(ty) & scope:
                raise KursError(f"{context}: {show(ty)} must be an unchanged parameter or independent of "
                                "the caller's parameters", instr.pos)
        # A nominal component need not be a subterm of its source: unfolding
        # Node[T] may yield Node[(tuple T T)]. Without a separate structural
        # descent proof, only parameter-independent traversal is safe in a cycle.
        if isinstance(instr.ty, Foreach) and instr.op == "foreach":
            if free_names(instr.ty.source) & scope:
                raise KursError(f"{context}: recursive foreach requires a parameter-independent source",
                                instr.pos)
