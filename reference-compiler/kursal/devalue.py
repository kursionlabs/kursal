"""Translate a checked place program into the value-only fragment: a verification oracle, not a pass of the
build. Every program `check_program` accepts has a translation with no places (no `&` argument or parameter,
no locals, no block `empty`/`known`/`opened` contract, no `load`/`store`) that `check_program` accepts too;
the translated program is the fragment the Lean proof covers. Nothing in lowering, cgen or the self-hosted
compiler uses this module: tests run it and check its result.

Within a block, each place root holds a tree mirroring the checker's `Places`: a `Full` leaf is one SSA
value, `Empty` holds nothing, a `Split` product holds one tree per component, `Known` an enum's payload tree
and `Opened` an opened package's body tree. A block takes its contract's tree as extra parameters, one per
`Full` leaf; an edge rebuilds and takes apart the source's trees into the target's. A function's place
parameters become value parameters, and it returns their final values after its result, in a tuple."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Optional
from .check import NOWHERE, Checker, check_program
from .core import (U32, UNIT, Arm, Block, Edge, Exists, Extern, Fn, Fun, Instance, Instr, Jmp, Jnz, Kind,
                   Method, Never, Param, Premise, Program, Ptr, Ret, Skolem, Struct, Switch, Trait, Tuple, Type,
                   TypeDecl, Unreachable, Data, Terminator)
from .sexpr import KursError, Position
from .types import map_children, subst


class DevalueError(KursError):
    """The translation met a state the checker should have ruled out."""


# ------------------------------------------------------------------ trees --

@dataclass(frozen=True)
class Full:
    value: str
    ty: Type


@dataclass(frozen=True)
class Empty:
    ty: Type


@dataclass(frozen=True)
class Split:
    ty: Type
    parts: tuple[tuple[str, Node], ...]


@dataclass(frozen=True)
class Known:
    ty: Type
    variant: str
    child: Node


@dataclass(frozen=True)
class Opened:
    ty: Exists
    brands: tuple[str, ...]
    child: Node   # the body's tree, its types at the view brands


type Node = Full | Empty | Split | Known | Opened
type Trees = dict[str, Node]


def leaves(node: Node) -> Iterator[Full]:
    """The tree's values, depth first, components in declaration order: the order a block takes them in."""
    match node:
        case Full():
            yield node
        case Split(parts=parts):
            for _, part in parts:
                yield from leaves(part)
        case Known(child=child) | Opened(child=child):
            yield from leaves(child)


def inside(change: Callable[[Node], Node]) -> Callable[[Node], Node]:
    """`change` applied to what a path holds, under the view opened at the path itself, which stays."""
    return lambda node: replace(node, child=change(node.child)) if isinstance(node, Opened) else change(node)


def content(node: Node) -> Node:
    return node.child if isinstance(node, Opened) else node


# ------------------------------------------------------------------ types --

def devalue_type(ty: Type) -> Type:
    """A function type with place parameters takes them by value and returns their final values after its
    result: `(tuple R P1 ... Pn)`."""
    if isinstance(ty, Fn) and ty.places:
        params = tuple(devalue_type(p) for p in ty.params)
        ret = Tuple((devalue_type(ty.ret), *(params[i] for i in sorted(ty.places))))
        return Fn(params, ret, ty.forall, ty.unsafe)
    return map_children(ty, devalue_type)


def devalue_premise(premise: Premise) -> Premise:
    return Premise(premise.trait, tuple(devalue_type(a) for a in premise.args))


def devalue_signature(ptypes: Sequence[Type], ret: Type, places: frozenset[int]) -> tuple[tuple[Type, ...], Type]:
    params = tuple(devalue_type(t) for t in ptypes)
    return params, Tuple((devalue_type(ret), *(params[i] for i in sorted(places)))) if places else devalue_type(ret)


# -------------------------------------------------------------- functions --

class Function:
    """The translation of one function: its blocks, each with its contract's trees as extra parameters,
    and the trampolines its branching edges need."""

    def __init__(self, checker: Checker, fun: Fun, outer: tuple[Param, ...]) -> None:
        self.fun = fun
        self.types = checker.types
        self.module = fun.module or NOWHERE
        self.kinds: dict[str, Kind] = {p.name: p.kind for p in (*outer, *fun.params)}
        entry = fun.blocks[0]
        self.roots: dict[str, Type] = {**dict(fun.locals), **{n: t for n, t in entry.params if n.startswith("&")}}
        self.place_params = [n for n, _ in entry.params if n.startswith("&")]
        self.typed: dict[str, dict[str, Type]] = {}
        for (label, name), ty in checker.typed[id(fun)].items():
            self.typed.setdefault(label, {})[name] = ty
        self.taken = {name for block in fun.blocks for name, _ in block.params} | \
            {name for block in fun.blocks for instr in block.instrs for name in instr.lhs} | \
            {block.label for block in fun.blocks}
        self.counter = 0
        self.canon = {block.label: self.contract(block) for block in fun.blocks[1:]}
        self.trampolines: list[Block] = []
        self.pos: Optional[Position] = None

    def fresh(self, stem: str = "%dv") -> str:
        while True:
            self.counter += 1
            name = f"{stem}{self.counter}"
            if name not in self.taken:
                self.taken.add(name)
                return name

    # ------------------------------------------------------- contracts --

    def contract(self, block: Block) -> Trees:
        """The trees a block's contract describes, with a fresh parameter at each `Full` leaf."""
        empty, known, opened = set(block.empty), dict(block.known), dict(block.opened)
        below = {path[:i] for path in (*empty, *known, *opened) for i, c in enumerate(path) if c == "."}

        def node(path: str, ty: Type) -> Node:
            brands = opened.get(path)
            if brands is None:
                return unviewed(path, ty)
            if not isinstance(ty, Exists):
                raise DevalueError(f"{path} is opened but holds no package")
            body = subst(ty.body, {b: Skolem(c) for b, c in zip(ty.binders, brands)})
            return Opened(ty, brands, unviewed(path, body))

        def unviewed(path: str, ty: Type) -> Node:
            if path in empty:
                return Empty(ty)
            if path in known:
                variant = known[path]
                return Known(ty, variant, node(f"{path}.{variant}", dict(self.types.variants(ty, self.module))[variant]))
            if path in below:
                return Split(ty, tuple((n, node(f"{path}.{n}", t)) for n, t in self.components(ty)))
            return Full(self.fresh(), ty)

        return {root: node(root, ty) for root, ty in self.roots.items()}

    def components(self, ty: Type) -> tuple[tuple[str, Type], ...]:
        match self.types.shape(ty, self.module):
            case Struct(fields=fields):
                return fields
            case Tuple(elems=elems):
                return tuple((str(i), e) for i, e in enumerate(elems))
        raise DevalueError("a place is taken apart only at a struct or tuple")

    # ---------------------------------------------------------- blocks --

    def translate(self) -> Fun:
        blocks = [self.block(block, i == 0) for i, block in enumerate(self.fun.blocks)]
        ptypes, ret = devalue_signature(self.fun.ptypes, self.fun.ret, self.fun.places)
        return replace(self.fun, premises=tuple(map(devalue_premise, self.fun.premises)),
                       ret=ret, ptypes=ptypes, blocks=(*blocks, *self.trampolines), locals=())

    def block(self, block: Block, is_entry: bool) -> Block:
        self.label = block.label
        self.out: list[Instr] = []
        self.rename: dict[str, str] = {}
        self.brands = list(block.brands)
        params = [(name, devalue_type(ty)) for name, ty in block.params]
        if is_entry:
            self.trees: Trees = {root: Empty(ty) for root, ty in self.fun.locals}
            for i, (name, ty) in enumerate(block.params):
                if name.startswith("&"):
                    value = self.fresh()
                    self.trees[name] = Full(value, ty)
                    params[i] = (value, devalue_type(ty))
        else:
            self.trees = dict(self.canon[block.label])
            params += [(leaf.value, devalue_type(leaf.ty)) for root in self.roots for leaf in leaves(self.trees[root])]
        term: Optional[Terminator] = None
        for instr in block.instrs:
            self.pos = instr.pos
            if self.instr(instr):
                term = Unreachable(instr.pos)
                break
        self.pos = block.term.pos
        if term is None:
            term = self.term(block.term, block.instrs[-1] if block.instrs else None)
        return Block(block.label, tuple(params), tuple(self.out), term, block.brands, pos=block.pos)

    def emit(self, op: str, args: Sequence[str] = (), ty: Optional[Type] = None, tys: Sequence[Type] = (),
             lit: Optional[str] = None, lhs: Optional[Sequence[str]] = None, unsafe: bool = False) -> str:
        names = tuple(lhs) if lhs is not None else (self.fresh(),)
        self.out.append(Instr(names, op, tuple(args), None if ty is None else devalue_type(ty),
                              tuple(devalue_type(t) for t in tys), lit, unsafe, pos=self.pos))
        return names[0]

    def sub(self, name: str) -> str:
        return self.rename.get(name, name)

    def copy_type(self, ty: Type) -> bool:
        return self.types.is_copy(ty, self.kinds)

    # ----------------------------------------------------------- trees --

    def split(self, node: Full) -> Split:
        """A whole product's value taken apart into its components."""
        rows = self.components(node.ty)
        names = [self.fresh() for _ in rows]
        self.emit("destruct", (node.value,), node.ty, lhs=names)
        return Split(node.ty, tuple((n, Full(v, t)) for (n, t), v in zip(rows, names)))

    def reach(self, node: Node, segments: Sequence[str], change: Callable[[Node], Node]) -> Node:
        """`node` with `change` applied to the node `segments` below it, taking apart whole products on the
        way."""
        if not segments:
            return change(node)
        if isinstance(node, Opened):
            return replace(node, child=self.reach(node.child, segments, change))
        if isinstance(node, Full):
            node = self.split(node)
        first, rest = segments[0], segments[1:]
        if isinstance(node, Split):
            return replace(node, parts=tuple((n, self.reach(c, rest, change) if n == first else c)
                                             for n, c in node.parts))
        if isinstance(node, Known) and node.variant == first:
            return replace(node, child=self.reach(node.child, rest, change))
        raise DevalueError(f"no place {first} in {type(node).__name__}")

    def at(self, path: str, change: Callable[[Node], Node]) -> None:
        root, *segments = path.split(".")
        self.trees[root] = self.reach(self.trees[root], segments, change)

    def rebuild(self, node: Node) -> str:
        """One value of everything the tree holds, consuming its leaves."""
        match node:
            case Full(value=value):
                return value
            case Split(ty=ty, parts=parts):
                return self.emit("construct", [self.rebuild(part) for _, part in parts], ty)
            case Known(ty=ty, variant=variant, child=child):
                return self.emit("construct_variant", (self.rebuild(child),), ty, lit=variant)
            case Opened(ty=ty, brands=brands, child=child):
                return self.emit("pack", (self.rebuild(child),), ty, tuple(Skolem(b) for b in brands))
        raise DevalueError("an empty place has no value")

    def duplicate(self, node: Node) -> str:
        """One value of a Copy tree, which keeps its leaves."""
        match node:
            case Full(value=value):
                return self.emit("copy", (value,))
            case Split(ty=ty, parts=parts):
                return self.emit("construct", [self.duplicate(part) for _, part in parts], ty)
            case Known(ty=ty, variant=variant, child=child):
                return self.emit("construct_variant", (self.duplicate(child),), ty, lit=variant)
        raise DevalueError("a Copy place is read whole, with no view open inside")

    def take(self, path: str) -> tuple[str, Type]:
        """The value at `path`, which becomes empty: a move out."""
        taken: list[tuple[str, Type]] = []

        def change(node: Node) -> Node:
            taken.append((self.rebuild(node), node.ty))
            return Empty(node.ty)
        self.at(path, inside(change))
        return taken[0]

    def read(self, path: str) -> tuple[str, Type]:
        """The value at `path`, moved out unless its type is Copy, when the place keeps it."""
        found: list[tuple[str, Type]] = []

        def change(node: Node) -> Node:
            if not self.copy_type(node.ty):
                found.append((self.rebuild(node), node.ty))
                return Empty(node.ty)
            found.append((self.duplicate(node), node.ty))
            return node
        self.at(path, inside(change))
        return found[0]

    def put(self, path: str, value: str) -> None:
        self.at(path, inside(lambda node: Full(value, node.ty)))

    def look(self, path: str) -> Node:
        """What `path` holds, under its own view; whole products on the way are taken apart."""
        found: list[Node] = []

        def change(node: Node) -> Node:
            found.append(node)
            return node
        self.at(path, inside(change))
        return found[0]

    # ---------------------------------------------------- instructions --

    def instr(self, instr: Instr) -> bool:
        """Translate one instruction; True when the block cannot go on past it."""
        op, args = instr.op, instr.args
        if op in ("call", "trait_call"):
            self.call(instr)
            return False
        if not any(a.startswith("&") for a in args):
            self.plain(instr)
            return False
        path = args[0]
        if op in ("load", "destruct"):
            value, _ = self.read(path)
            if op == "load":
                self.rename[instr.lhs[0]] = value
            else:
                self.emit("destruct", (value,), instr.ty, lhs=instr.lhs)
        elif op == "store":
            value = self.sub(args[1])
            if isinstance(self.typed.get(self.label, {}).get(args[1]), Never):
                # Code after a Never value never runs: the place need not hold it.
                self.emit("copy", (value,))
                return True
            self.put(path, value)
            self.unit(instr)
        elif op == "variant_tag":
            node = content(self.look(path))
            if isinstance(node, Known):
                names = [n for n, _ in self.types.variants(node.ty, self.module)]
                self.emit("const", ty=U32, lit=str(names.index(node.variant)), lhs=instr.lhs)
            elif isinstance(node, Full):
                self.emit("variant_tag", (node.value,), instr.ty, lhs=instr.lhs)
            else:
                raise DevalueError("a tag is read from a whole enum")
        elif op == "open":
            node = self.look(path)
            if not isinstance(node, Full) or not isinstance(node.ty, Exists):
                raise DevalueError("a place opens a whole package")
            package = node.ty
            body = subst(package.body, {b: Skolem(c) for b, c in zip(package.binders, instr.binders)})
            opened = self.fresh()
            self.out.append(Instr((opened,), "open", (node.value,), binders=instr.binders, pos=instr.pos))
            self.at(path, lambda _: Opened(package, instr.binders, Full(opened, body)))
            self.brands += instr.binders
            self.unit(instr)
        elif op == "pack":
            def close(node: Node) -> Node:
                if not isinstance(node, Opened):
                    raise DevalueError("a place packs its open view")
                return Full(self.rebuild(node), node.ty)
            self.at(path, close)
            self.unit(instr)
        else:
            raise DevalueError(f"{op} takes no place")
        return False

    def unit(self, instr: Instr) -> None:
        """The unit result of an instruction on a place."""
        self.emit("construct", (), UNIT, lhs=instr.lhs)

    def plain(self, instr: Instr) -> None:
        self.out.append(replace(instr, args=tuple(map(self.sub, instr.args)),
                                ty=None if instr.ty is None else devalue_type(instr.ty),
                                tys=tuple(devalue_type(t) for t in instr.tys)))
        if instr.op == "open":
            self.brands += instr.binders

    def call(self, instr: Instr) -> None:
        """Each place argument is passed by value and comes back after the result. A raw-target lend `&*q`
        reads its target through `q` before the call and writes the new value back after it, both under
        the call's unsafe marker."""
        fixed = 1 if instr.op == "call" else 2
        args = [self.sub(a) for a in instr.args[:fixed]]
        back: list[tuple[str, Type]] = []
        for arg in instr.args[fixed:]:
            if arg.startswith("&*"):
                pointer = self.sub(arg[2:])
                ty = self.typed[self.label][arg[2:]]
                if not isinstance(ty, Ptr):
                    raise DevalueError("a raw-target lend is through a pointer")
                reader = self.emit("copy", (pointer,))
                args.append(self.emit("ptr_load", (reader,), unsafe=True))
                back.append((pointer, ty.inner))
            elif arg.startswith("&"):
                value, ty = self.take(arg)
                args.append(value)
                back.append((arg, ty))
            else:
                args.append(self.sub(arg))
        if not back:
            self.out.append(replace(instr, args=tuple(args), tys=tuple(devalue_type(t) for t in instr.tys)))
            return
        result = self.fresh()
        self.out.append(replace(instr, lhs=(result,), args=tuple(args), tys=tuple(devalue_type(t) for t in instr.tys)))
        returned = [self.fresh() for _ in back]
        ret = self.typed[self.label][instr.lhs[0]]
        self.emit("destruct", (result,), Tuple((ret, *(ty for _, ty in back))), lhs=(instr.lhs[0], *returned))
        for (where, _), value in zip(back, returned):
            if where.startswith("&"):
                self.put(where, value)
            else:
                self.emit("ptr_store", (where, value), unsafe=True)

    # ----------------------------------------------------- terminators --

    def term(self, term: Terminator, last: Optional[Instr]) -> Terminator:
        match term:
            case Ret():
                value = self.sub(term.value)
                if self.place_params:
                    finals = [self.rebuild(self.trees[root]) for root in self.place_params]
                    _, ret = devalue_signature(self.fun.ptypes, self.fun.ret, self.fun.places)
                    value = self.emit("construct", (value, *finals), ret)
                return Ret(value, term.pos)
            case Jmp():
                return Jmp(self.edge(term.edge), term.pos)
            case Jnz():
                cond = self.sub(term.cond)
                then, other = self.branch([term.then, term.other], None)
                return Jnz(cond, then, other, term.pos)
            case Switch() if term.scrutinee.startswith("&"):
                return self.place_switch(term)
            case Switch():
                scrutinee = self.sub(term.scrutinee)
                variants = dict(self.types.variants(term.ty, self.module))
                edges = self.branch([arm.edge for arm in term.arms], [variants[arm.variant] for arm in term.arms])
                return Switch(scrutinee, devalue_type(term.ty),
                              tuple(Arm(arm.variant, e) for arm, e in zip(term.arms, edges)), term.pos)
            case Unreachable():
                if last is None:
                    raise DevalueError("unreachable follows a Never result")
                never = self.sub(last.lhs[0])
                if not self.out or self.out[-1].lhs != (never,):
                    self.emit("copy", (never,))
                return term
        raise DevalueError("unknown terminator")

    def edge(self, edge: Edge) -> Edge:
        """A jump from the current trees: they are reshaped to the target's here."""
        values = self.normalized(self.trees, edge.target)
        return Edge(edge.target, (*map(self.sub, edge.args), *values), edge.tys)

    def normalized(self, trees: Trees, target: str) -> list[str]:
        """The target's leaf values, made from `trees` by instructions emitted here."""
        canon = self.canon.get(target)
        if canon is None:
            raise DevalueError("a jump goes to a block other than the entry")
        out: list[str] = []
        for root in self.roots:
            self.normalize(trees[root], canon[root], out)
        return out

    def normalize(self, source: Node, target: Node, out: list[str]) -> None:
        match target:
            case Empty():
                # The checker proved the source holds no linear value here: every leaf left behind is Copy.
                return
            case Full():
                out.append(self.rebuild(source))
                return
            case Opened(child=child) if isinstance(source, Opened):
                self.normalize(source.child, child, out)
                return
            case Known(variant=variant, child=child) if isinstance(source, Known) and source.variant == variant:
                self.normalize(source.child, child, out)
                return
            case Split(parts=parts):
                if isinstance(source, Full):
                    source = self.split(source)
                if isinstance(source, Split):
                    for (_, mine), (_, theirs) in zip(source.parts, parts):
                        self.normalize(mine, theirs, out)
                    return
        raise DevalueError(f"no edge from {type(source).__name__} to {type(target).__name__}")

    def branch(self, edges: Sequence[Edge], payloads: Optional[Sequence[Type]]) -> list[Edge]:
        """The edges of a terminator with several: each arm whose reshaping takes instructions runs them in
        a trampoline of its own, since a terminator runs none."""
        out = []
        for i, edge in enumerate(edges):
            saved, self.out = self.out, []
            values = self.normalized(self.trees, edge.target)
            code, self.out = self.out, saved
            if not code:
                out.append(Edge(edge.target, (*map(self.sub, edge.args), *values), edge.tys))
                continue
            payload = None if payloads is None else (self.fresh(), payloads[i])
            out.append(self.trampoline(edge, self.trees, code, values, payload, payload is not None))
        return out

    def trampoline(self, edge: Edge, trees: Trees, code: list[Instr], values: list[str],
                   payload: Optional[tuple[str, Type]], forward: bool) -> Edge:
        """A block that takes the source's leaves and the edge's arguments (after the switch payload, if
        any), under the source's brands and names, runs `code` and jumps on, passing the payload on when
        `forward`. `code` was made from `trees`."""
        label = self.fresh("dv")
        held = [(leaf.value, leaf.ty) for root in self.roots for leaf in leaves(trees[root])]
        args = [(self.sub(a), self.typed[self.label][a]) for a in edge.args]
        params = ([payload] if payload is not None else []) + held + args
        passed = ([payload[0]] if payload is not None and forward else []) + [a for a, _ in args] + values
        self.trampolines.append(Block(label, tuple((n, devalue_type(t)) for n, t in params), tuple(code),
                                      Jmp(Edge(edge.target, tuple(passed), edge.tys), self.pos),
                                      tuple(self.brands), pos=self.pos))
        return Edge(label, tuple(name for name, _ in held + args), tuple(Skolem(b) for b in self.brands))

    def place_switch(self, term: Switch) -> Terminator:
        """A switch on a place: on its value, whose payload each arm's trampoline puts back as the place's
        known variant. A place whose variant is already known jumps to that arm."""
        path = term.scrutinee
        node = content(self.look(path))
        if isinstance(node, Known):
            arm = next(a for a in term.arms if a.variant == node.variant)
            return Jmp(self.edge(arm.edge), term.pos)
        if not isinstance(node, Full):
            raise DevalueError("a switch reads a whole enum")
        self.at(path, inside(lambda n: Empty(n.ty)))
        base = self.trees
        variants = dict(self.types.variants(node.ty, self.module))
        arms = []
        for arm in term.arms:
            payload = (self.fresh(), variants[arm.variant])
            self.trees = dict(base)
            self.at(path, inside(lambda n: Known(n.ty, arm.variant, Full(payload[0], payload[1]))))
            saved, self.out = self.out, []
            values = self.normalized(self.trees, arm.edge.target)
            code, self.out = self.out, saved
            arms.append(Arm(arm.variant, self.trampoline(arm.edge, base, code, values, payload, False)))
        self.trees = base
        return Switch(node.value, devalue_type(term.ty), tuple(arms), term.pos)


# ---------------------------------------------------------------- program --

def devalue(program: Program) -> Program:
    """The value-only translation of a program `check_program` accepts."""
    checker = check_program(program)

    def method(m: Method) -> Method:
        ptypes, ret = devalue_signature(m.ptypes, m.ret, m.places)
        return Method(m.name, ret, ptypes, m.unsafe)

    def instance(inst: Instance) -> Instance:
        return replace(inst, premises=tuple(map(devalue_premise, inst.premises)),
                       head=tuple(devalue_type(t) for t in inst.head),
                       methods=tuple(Function(checker, f, inst.params).translate() for f in inst.methods))

    def extern(ext: Extern) -> Extern:
        return replace(ext, ret=None if ext.ret is None else devalue_type(ext.ret),
                       ptypes=None if ext.ptypes is None else tuple(devalue_type(t) for t in ext.ptypes))

    def decl(d: TypeDecl) -> TypeDecl:
        return replace(d, premises=tuple(map(devalue_premise, d.premises)), body=devalue_type(d.body))

    def data(d: Data) -> Data:
        return replace(d, ty=devalue_type(d.ty))

    def trait(t: Trait) -> Trait:
        return replace(t, methods=tuple(map(method, t.methods)))

    return Program(
        types={name: decl(d) for name, d in program.types.items()},
        funs={name: Function(checker, f, ()).translate() for name, f in program.funs.items()},
        externs={name: extern(e) for name, e in program.externs.items()},
        datas={name: data(d) for name, d in program.datas.items()},
        traits={name: trait(t) for name, t in program.traits.items()},
        instances={name: [instance(i) for i in insts] for name, insts in program.instances.items()})
