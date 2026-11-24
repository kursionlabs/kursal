"""Frontend SSA promotion for roots whose types contain nongeneric brands.

Place planning establishes which complete contents reach each edge. This pass
threads those contents explicitly; it does not relax core local formation.
Projected products are decomposed and rebuilt; lends use a generic value adapter.
A switch on an enum among the contents switches on its value: each arm's block, entered
by that arm alone, holds the variant's payload where its header knows the variant, and
the enum is rebuilt where a whole value is needed. Opened root views require a further
representation extension.
"""
from collections.abc import Sequence
from itertools import count

from . import core as C
from .sexpr import KursError
from .check import ancestor_in, overlap
from .placeflow import place_name
from .types import Types, free_brands, subst


def promote_locals(fun: C.Fun, types: Types, params: Sequence[C.Param],
                   premises: tuple[C.Premise, ...], value_types: dict[str, C.Type]) -> list[C.Fun]:
    allowed = {p.name for p in params if p.kind == C.BRAND}
    roots = {n: t for n, t in fun.locals if not free_brands(t) <= allowed}
    if not roots:
        return []
    kinds: dict[str, C.Kind] = {p.name: p.kind for p in params}
    taken = {n for b in fun.blocks for n, _ in b.params}
    taken.update(n for b in fun.blocks for i in b.instrs for n in i.lhs)
    serial = count()

    def fresh() -> str:
        while (name := f"%#contents{next(serial)}") in taken:
            pass
        taken.add(name)
        return name

    def restricted(path: str) -> bool:
        return path.split('.', 1)[0] in roots

    def unsupported(path: str) -> None:
        raise KursError(f"frontend limitation: locally branded value '{place_name(path)}' requires "
                        "structural SSA lowering; pass it by value to a generic function", fun.pos)

    helpers: list[C.Fun] = []
    shapes: dict[str, tuple[tuple[str, C.Type], ...]] = {}
    path_types = dict(roots)
    sums: set[str] = set()   # enum paths among the contents, whose variants' payloads are exposed

    def expose(path: str) -> None:
        if path in path_types:
            return
        parent, label = path.rsplit('.', 1)
        expose(parent)
        if parent not in shapes:
            shape = types.shape(path_types[parent], fun.module)
            if isinstance(shape, C.Enum) and label in dict(shape.variants):
                sums.add(parent)
                path_types[path] = dict(shape.variants)[label]
                return
            if isinstance(shape, C.Tuple):
                fields = tuple((str(k), t) for k, t in enumerate(shape.elems))
            elif isinstance(shape, C.Struct):
                fields = shape.fields
            else:
                unsupported(path)
                return
            shapes[parent] = tuple((parent + '.' + n, t) for n, t in fields)
            path_types.update(shapes[parent])
        if path not in path_types:
            unsupported(path)

    def is_sum(path: str) -> bool:
        return isinstance(types.shape(path_types[path], fun.module), C.Enum)

    # Validate before mutation; opened roots need their own representation.

    refined: dict[str, dict[str, str]] = {}    # block -> enum path known to hold a variant -> the variant
    arms: dict[str, tuple[str, str]] = {}      # an arm's block -> (enum path switched on, its variant)
    entries: dict[str, int] = {}
    for block in fun.blocks:
        for edge in (block.term.edge,) if isinstance(block.term, C.Jmp) else \
                (block.term.then, block.term.other) if isinstance(block.term, C.Jnz) else \
                tuple(arm.edge for arm in block.term.arms) if isinstance(block.term, C.Switch) else ():
            entries[edge.target] = entries.get(edge.target, 0) + 1
    for block in fun.blocks:
        refined[block.label] = {}
        for path in (*block.empty, *(p for p, _ in block.known), *(p for p, _ in block.opened)):
            if restricted(path):
                if path in dict(block.opened):
                    unsupported(path)
                expose(path)
                if path in dict(block.known):
                    variant = dict(block.known)[path]
                    if not is_sum(path) or any(path.startswith(q + '.') for q in refined[block.label]):
                        unsupported(path)
                    refined[block.label][path] = variant
                    expose(path + '.' + variant)
        for instr in block.instrs:
            for k, arg in enumerate(instr.args):
                if restricted(arg):
                    if not (k == 0 and instr.op in {'load', 'store', 'destruct'}
                            or instr.op in {'call', 'trait_call'}):
                        unsupported(arg)
                    expose(arg)
        if isinstance(block.term, C.Switch) and restricted(block.term.scrutinee):
            path = block.term.scrutinee
            expose(path)
            if not is_sum(path):
                unsupported(path)
            for arm in block.term.arms:
                if entries.get(arm.edge.target) != 1 or arm.edge.target == block.label:
                    unsupported(path)
                arms[arm.edge.target] = (path, arm.variant)

    def under_sum(path: str) -> bool:
        return any(path.startswith(q + '.') for q in sums)

    leaves = {p: t for p, t in path_types.items() if p not in shapes and not under_sum(p)}
    incoming: dict[str, dict[str, str]] = {}
    payloads: dict[str, tuple[str, str]] = {}   # an arm's block -> (payload path, the parameter holding it)
    for block in fun.blocks:
        held = dict(leaves)
        for q, variant in refined[block.label].items():
            if q in held:
                del held[q]
                held[q + '.' + variant] = path_types[q + '.' + variant]
        empty = set(block.empty)
        contents = {} if block is fun.blocks[0] else {p: fresh() for p in held if not ancestor_in(empty, p)}
        if block.label in arms:
            q, variant = arms[block.label]
            if refined[block.label].get(q) != variant:
                unsupported(q)
            payload = q + '.' + variant
            name = contents.pop(payload) if payload in contents else fresh()
            payloads[block.label] = (payload, name)
            block.params = ((name, path_types[payload]),) + block.params
        incoming[block.label] = contents
        block.params += tuple((v, held[p]) for p, v in contents.items())

    for block in fun.blocks:
        contents = dict(incoming[block.label])
        aliases: dict[str, str] = {}
        instructions: list[C.Instr] = []
        known = dict(refined[block.label])

        brands = list(block.brands)

        def value(name: str) -> str:
            if name.startswith('&*'):
                return '&*' + aliases.get(name[2:], name[2:])
            return aliases.get(name, name)

        def read(path: str) -> str:
            if path not in contents:
                raise KursError(f"locally branded value '{place_name(path)}' is unavailable", fun.pos)
            return contents[path]

        def materialize(path: str) -> None:
            """Puts `path` among the contents whole, building it from its parts or its variant's payload."""
            if path not in contents:
                contents[path] = extract(path)

        def install(path: str, operand: str) -> None:
            if path in shapes:
                children = shapes[path]
                names = tuple(fresh() for _ in children)
                instructions.append(C.Instr(names, 'destruct', (operand,), ty=path_types[path], pos=fun.pos))
                for (child, _), name in zip(children, names):
                    install(child, name)
            else:
                contents[path] = operand

        def extract(path: str, copy: bool = False) -> str:
            if path in shapes:
                names = tuple(extract(child, copy) for child, _ in shapes[path])
                name = fresh()
                instructions.append(C.Instr((name,), 'construct', names, ty=path_types[path], pos=fun.pos))
                return name
            if path not in contents and path in known:
                inner = extract(path + '.' + known[path], copy)
                name = fresh()
                instructions.append(C.Instr((name,), 'construct_variant', (inner,), ty=path_types[path],
                                            lit=known[path], pos=fun.pos))
                if not copy:
                    del known[path]
                return name
            name = read(path)
            if copy:
                copied = fresh()
                instructions.append(C.Instr((copied,), 'copy', (name,), pos=fun.pos))
                return copied
            del contents[path]
            return name

        def adapt_call(instr: C.Instr) -> None:
            offset = 2 if instr.op == 'trait_call' else 1
            operands = instr.args[offset:]
            borrowed = [(k, p) for k, p in enumerate(operands) if restricted(p)]
            paths = [p for p in operands if p.startswith('&') and not p.startswith('&*')]
            if any(overlap(p, q) for k, p in enumerate(paths) for q in paths[k + 1:]):
                raise KursError('lent places overlap', instr.pos)
            indirect = instr.op == 'call' and instr.args[0].startswith('%')
            if instr.op == 'trait_call':
                trait = types.program.traits[instr.args[0]]
                method = next(m for m in trait.methods if m.name == instr.args[1])
                sub = dict(zip(trait.params, instr.tys))
                ptypes = tuple(subst(t, sub) for t in method.ptypes)
            else:
                callee = instr.args[0]
                if indirect:
                    cty = value_types[callee]
                    assert isinstance(cty, C.Fn)
                    ptypes = cty.params
                elif callee in types.program.externs:
                    ptypes = types.program.externs[callee].ptypes
                else:
                    target = types.program.funs[callee]
                    sub = dict(zip((p.name for p in target.params), instr.tys))
                    ptypes = tuple(subst(t, sub) for t in target.ptypes)
            result = value_types[instr.lhs[0]]
            generic = tuple(params) + tuple(C.Param(b, C.BRAND) for b in brands)
            name = fun.name + '#lend' + fresh().removeprefix('%')
            while name in types.program.funs:
                name += '_'
            argument_names = tuple(('&' if p.startswith('&') and not restricted(p) else '%') + f'a{k}'
                                   for k, p in enumerate(operands))
            bindings = list(zip(argument_names, ptypes))
            actuals = [extract(p) if restricted(p) else p for p in operands]
            head = instr.args[:offset]
            if indirect:
                bindings.insert(0, ('%callee', value_types[instr.args[0]]))
                actuals.insert(0, instr.args[0])
                head = ('%callee',)
            locals_ = tuple((f'&slot{k}', ptypes[k]) for k, _ in borrowed)
            body = [C.Instr((f'%unit{k}',), 'store', (f'&slot{k}', argument_names[k]), pos=instr.pos)
                    for k, _ in borrowed]
            forwarded = list(argument_names)
            for k, _ in borrowed:
                forwarded[k] = f'&slot{k}'
            body.append(C.Instr(('%result',), instr.op, (*head, *forwarded), tys=instr.tys,
                                unsafe=instr.unsafe, pos=instr.pos))
            ret: C.Type
            if isinstance(result, C.Never):
                ret = result
                term: C.Terminator = C.Unreachable(instr.pos)
            else:
                restored = tuple(f'%restored{k}' for k, _ in borrowed)
                body.extend(C.Instr((n,), 'load', (f'&slot{k}',), pos=instr.pos)
                            for n, (k, _) in zip(restored, borrowed))
                ret = C.Tuple(tuple(ptypes[k] for k, _ in borrowed) + (result,))
                body.append(C.Instr(('%out',), 'construct', (*restored, '%result'), ty=ret, pos=instr.pos))
                term = C.Ret('%out', instr.pos)
            helper = C.Fun(name, generic, premises, ret, tuple(t for _, t in bindings),
                           (C.Block('entry', tuple(bindings), tuple(body), term),), locals_,
                           instr.unsafe, fun.module, instr.pos)
            helpers.append(helper)
            tys = tuple(C.Skolem(p.name) if p.kind == C.BRAND else C.Named(p.name) for p in generic)
            if isinstance(result, C.Never):
                instructions.append(C.Instr(instr.lhs, 'call', (name, *actuals), tys=tys,
                                            unsafe=instr.unsafe, pos=instr.pos))
            else:
                output = fresh()
                instructions.append(C.Instr((output,), 'call', (name, *actuals), tys=tys,
                                            unsafe=instr.unsafe, pos=instr.pos))
                replacements = tuple(fresh() for _ in borrowed)
                instructions.append(C.Instr((*replacements, *instr.lhs), 'destruct', (output,), ty=ret, pos=instr.pos))
                for (_, path), replacement in zip(borrowed, replacements):
                    install(path, replacement)

        if block.label in payloads:
            install(*payloads[block.label])
        for instr in block.instrs:
            instr.args = tuple(value(a) for a in instr.args)
            if instr.op == 'open':
                brands.extend(instr.binders)
            if instr.op == 'store' and restricted(instr.args[0]):
                path, operand = instr.args
                known.pop(path, None)
                install(path, operand)
                instructions.append(C.Instr(instr.lhs, 'construct', ty=C.UNIT, pos=instr.pos))
            elif instr.op in {'load', 'destruct'} and restricted(instr.args[0]):
                path = instr.args[0]
                operand = extract(path, types.is_copy(path_types[path], kinds))
                if instr.op == 'destruct':
                    instr.args = (operand,)
                    instructions.append(instr)
                else:
                    aliases[instr.lhs[0]] = operand
                    value_types[operand] = path_types[path]
            elif instr.op in {'call', 'trait_call'} and any(restricted(a) for a in instr.args):
                adapt_call(instr)
            else:
                instructions.append(instr)

        def pass_on(e: C.Edge) -> None:
            for p in incoming[e.target]:
                materialize(p)
            e.args = tuple(value(a) for a in e.args) + tuple(read(p) for p in incoming[e.target])

        match block.term:
            case C.Ret():
                block.term.value = value(block.term.value)
            case C.Jmp(edge=e):
                pass_on(e)
            case C.Jnz():
                block.term.cond = value(block.term.cond)
                pass_on(block.term.then)
                pass_on(block.term.other)
            case C.Switch():
                if restricted(block.term.scrutinee):
                    # The enum moves into the switch, and each arm's block holds its payload.
                    block.term.scrutinee = extract(block.term.scrutinee)
                else:
                    block.term.scrutinee = value(block.term.scrutinee)
                for arm in block.term.arms:
                    pass_on(arm.edge)
        block.instrs = tuple(instructions)
        block.empty = tuple(p for p in block.empty if not restricted(p))
        block.known = tuple((p, v) for p, v in block.known if not restricted(p))
    fun.locals = tuple((p, t) for p, t in fun.locals if p not in roots)
    return helpers
