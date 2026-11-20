"""Read the KURS S-expression serialization into the core IR, and write it back.

This adapter is separate from the strictly typed IR and type operations.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import NoReturn, TypeGuard, Optional
from .sexpr import Atom, Form, KursError, List, Position, Str, read
from .core import (ANY, ATOMIC_OPS, BRAND, COPY, LITERAL_BITS, PRIMS, Array, Arm, Block, Brand, Data,
                   Edge, Enum, Exists, Exposure, Extern, Fn, Foreach, Fun, Ignored, Instance,
                   Instr, Jmp, Jnz, Method, ModuleId, Named, Never, Param, Premise,
                   NonNull, Prim, Program, Ptr, Ret, Skolem, Struct, Switch, Trait, Tuple,
                   Type, TypeDecl, Terminator, Unreachable)


# ------------------------------------------------------------------ parsing --

def _fail(message: str, form: Form) -> NoReturn:
    raise KursError(message, form.pos)


def _is(form: Optional[Form], head: str, length: Optional[int] = None) -> TypeGuard[List]:
    return (isinstance(form, List) and bool(form) and form[0] == head
            and (length is None or len(form) == length))


def _list(form: Form) -> List:
    if not isinstance(form, List):
        _fail("expected a list", form)
    return form


def _text(form: Form) -> Atom:
    if not isinstance(form, Atom):
        _fail("expected an atom", form)
    return form


def _brand(atom: Form) -> str:
    if isinstance(atom, List) or not atom.startswith("$") or len(atom) < 2:
        _fail(f"expected a brand such as $k, got {write_form(atom)}", atom)
    return atom[1:]


def parse_type(form: Form) -> Type:
    if not isinstance(form, List):
        if form == "_":
            return Ignored()
        if form == "!":
            return Never()
        if form.startswith("$"):
            return Skolem(_brand(form))
        if form in PRIMS:
            return Prim(form)
        return Named(form)
    if not form:
        _fail("empty type", form)
    head, rest = _text(form[0]), form[1:]
    if head == "struct":
        return Struct(tuple((str(f[0]), parse_type(f[1])) for f in map(_list, rest)))
    if head == "enum":
        return Enum(tuple((str(f[0]), parse_type(f[1])) for f in map(_list, rest)))
    if head == "tuple":
        return Tuple(tuple(parse_type(f) for f in rest))
    if head == "array":
        return Array(parse_type(rest[0]), _int(rest[1]))
    if head == "ptr":
        return Ptr(parse_type(rest[0]))
    if head == "non_null":
        return NonNull(parse_type(rest[0]))
    if head == "brand":
        return Brand(Skolem(_brand(rest[0])))
    if head == "exists":
        return Exists(tuple(_brand(b) for b in _list(rest[0])), parse_type(rest[1]))
    if head == "foreach":
        pattern, source = _list(rest[0])
        indirect = _is(pattern, "ptr", 2)
        binder = str(_list(pattern)[1] if indirect else pattern)
        retained = None
        if len(rest) == 3:
            annotation = _list(rest[2])
            if not annotation or annotation[0] != "retain":
                _fail("expected foreach retained-binder annotation", rest[2])
            retained = tuple(map(str, annotation[1:]))
        elif len(rest) != 2:
            _fail("foreach requires source and body", form)
        return Foreach(binder, indirect, parse_type(source), parse_type(rest[1]), retained)
    if head == "fn":
        unsafe = _is(rest[0], "unsafe", 1)
        rest = rest[1:] if unsafe else rest
        header = rest[0]
        if _is(header, "forall", 5):
            params, premises = parse_forall(header[1], header[2])
            if premises:
                _fail("a function type has no premises", header)
            ptypes, places = _ptypes(header[4])
            return Fn(ptypes, parse_type(header[3]), params, unsafe, places)
        ptypes, places = _ptypes(rest[0])
        return Fn(ptypes, parse_type(rest[1]), unsafe=unsafe, places=places)
    return Named(head, tuple(parse_type(t) for t in rest))


def _ptypes(form: Form) -> tuple[tuple[Type, ...], frozenset[int]]:
    """A function type's or trait method's `(PTYPE*)`, where `(& T)` is a place parameter of type T."""
    forms = _list(form)
    places = frozenset(i for i, f in enumerate(forms) if _is(f, "&", 2))
    return tuple(parse_type(f[1] if _is(f, "&", 2) else f) for f in forms), places


def parse_forall(
    params_form: Form, premises_form: Form
) -> tuple[tuple[Param, ...], tuple[Premise, ...]]:
    """A `(forall (PARAM*) (PREMISE*) ...)` header; `(Copy T)` sets T's kind."""
    premises = tuple(Premise(str(p[0]), tuple(parse_type(a) for a in p[1:])) for p in map(_list, _list(premises_form)))
    copy: set[str] = set()
    for p in premises:
        if p.trait == "Copy":
            if len(p.args) != 1 or not isinstance(p.args[0], (Named, Prim)):
                _fail("Copy requires a type parameter name", premises_form)
            copy.add(p.args[0].name)
    names = {str(a) for a in map(_text, _list(params_form))}
    if copy - names:
        _fail("Copy requires a type parameter name", premises_form)
    params = tuple(Param(a[1:], BRAND) if a.startswith("$") else Param(str(a), COPY if a in copy else ANY)
                   for a in map(_text, _list(params_form)))
    return params, tuple(p for p in premises if p.trait != "Copy")


def _edge(target: Form, args: Sequence[Form]) -> Edge:
    if isinstance(target, List):
        return Edge(str(target[0]), tuple(map(str, args)), tuple(parse_type(t) for t in target[1:]))
    return Edge(str(target), tuple(map(str, args)))


def parse_term(form: Form) -> Terminator:
    form = _list(form)
    head = form[0]
    if head == "ret":
        return Ret(str(form[1]), form.pos)
    if head == "unreachable":
        return Unreachable(form.pos)
    if head == "jmp":
        return Jmp(_edge(form[1], form[2:]), form.pos)
    if head == "jnz":
        return Jnz(str(form[1]), _edge(_list(form[2])[0], _list(form[2])[1:]), _edge(_list(form[3])[0], _list(form[3])[1:]), form.pos)
    if head == "switch":
        arms = tuple(Arm(_text(arm[0]), _edge(arm[1], arm[2:])) for arm in map(_list, form[3:]))
        return Switch(str(form[1]), parse_type(form[2]), arms, form.pos)
    _fail(f"unknown terminator {write_form(head)}", form)


def _applied(form: Form) -> tuple[str, tuple[Type, ...]]:
    """`name` or `(name T*)`: a callee, mapper or function-value name."""
    if isinstance(form, List):
        return str(form[0]), tuple(parse_type(t) for t in form[1:])
    return str(form), ()


def parse_instr(form: Form) -> Instr:
    form = _list(form)
    if _is(form, "unsafe", 2):
        return replace(parse_instr(form[1]), unsafe=True)
    if len(form) == 4 and _is(form[3], "evidence"):
        trimmed = List(form[:3])  # selected evidence is not consumed yet
        trimmed.pos = form.pos
        form = trimmed
    if not _is(form, "=", 3) or not isinstance(form[2], List):
        _fail("an instruction is (= lhs (op arg*))", form)
    lhs_form, operation = form[1], _list(form[2])
    lhs = tuple(map(str, lhs_form)) if isinstance(lhs_form, List) else (str(lhs_form),)
    op, rest, pos = str(operation[0]), operation[1:], form.pos
    if op == "const":
        return Instr(lhs, op, pos=pos, ty=parse_type(rest[0]), lit=str(rest[1]))
    if op in ("construct",):
        return Instr(lhs, op, pos=pos, ty=parse_type(rest[0]), args=tuple(map(str, rest[1:])))
    if op in ("destruct", "variant_tag", "cast", "int_cast", "float_cast", "to_ptr"):
        return Instr(lhs, op, pos=pos, args=(str(rest[0]),), ty=parse_type(rest[1]))
    if op == "construct_variant":
        return Instr(lhs, op, pos=pos, ty=parse_type(rest[0]), lit=str(rest[1]), args=(str(rest[2]),))
    if op in ("alloca", "sizeof"):
        return Instr(lhs, op, pos=pos, ty=parse_type(rest[0]))
    if op == "pack":
        witnesses = rest[1] if isinstance(rest[1], List) else [rest[1]]
        return Instr(lhs, op, pos=pos, ty=parse_type(rest[0]), tys=tuple(parse_type(w) for w in witnesses), args=(str(rest[2]),))
    if op == "open":
        return Instr(lhs, op, pos=pos, args=(str(rest[0]),), binders=tuple(_brand(b) for b in _list(rest[1])))
    if op in ("call", "fn"):
        callee, tys = _applied(rest[0])
        return Instr(lhs, op, pos=pos, args=(callee, *map(str, rest[1:])), tys=tys)
    if op == "foreach":
        mapper, tys = _applied(rest[2])
        return Instr(lhs, op, pos=pos, ty=parse_type(rest[0]), args=(str(rest[1]), mapper, *map(str, rest[3:])), tys=tys)
    if op == "trait_call":
        return Instr(lhs, op, pos=pos, args=(str(rest[0]), str(rest[1]), *map(str, rest[3:])),
                    tys=tuple(parse_type(t) for t in _list(rest[2])))
    if op in ATOMIC_OPS:
        return Instr(lhs, op, pos=pos, lit=str(rest[0]), args=tuple(map(str, rest[1:])))
    return Instr(lhs, op, pos=pos, args=tuple(map(str, rest)))


def parse_block(form: Form) -> Block:
    form = _list(form)
    label, rest = str(form[0]), form[1:]
    brands: tuple[str, ...] = ()
    first = rest[0] if rest else None
    if isinstance(first, List) and first and all(
            not isinstance(a, List) and a.startswith("$") for a in first):
        brands, rest = tuple(_brand(b) for b in first), rest[1:]
    params = tuple((str(p[0]), parse_type(p[1])) for p in map(_list, _list(rest[0])))
    rest = rest[1:]
    empty: list[str] = []
    known: list[tuple[str, str]] = []
    opened: list[tuple[str, tuple[str, ...]]] = []
    while len(rest) > 1 and any(_is(rest[0], head) for head in ("empty", "known", "opened")):
        clause = _list(rest[0])
        if clause[0] == "empty":
            empty.extend(map(str, clause[1:]))
        elif clause[0] == "known":
            known.append((str(clause[1]), str(clause[2])))
        else:
            if len(clause) != 3:
                _fail("an opened contract is (opened PLACE ($brand*))", clause)
            opened.append((str(_text(clause[1])), tuple(_brand(b) for b in _list(clause[2]))))
        rest = rest[1:]
    return Block(label, params, tuple(parse_instr(i) for i in rest[:-1]), parse_term(rest[-1]),
                 brands, tuple(empty), tuple(known), tuple(opened), form.pos)


def parse_fun(forms: Sequence[Form], name: str, module: Optional[ModuleId] = None,
              unsafe: bool = False, pos: Optional[Position] = None) -> Fun:
    """`RET (PARAM-TYPES) (locals ...)? BLOCK+`, possibly forall-wrapped."""
    params: tuple[Param, ...] = ()
    premises: tuple[Premise, ...] = ()
    header = forms[0]
    if _is(header, "forall"):
        if len(forms) > 1:
            _fail("a generic function's blocks are inside its forall", forms[1])
        params, premises = parse_forall(header[1], header[2])
        forms = header[3:]
    body = forms[2:]
    locals_: tuple[tuple[str, Type], ...] = ()
    local_form = body[0] if body else None
    if _is(local_form, "locals"):
        locals_ = tuple((str(p[0]), parse_type(p[1])) for p in map(_list, local_form[1:]))
        body = body[1:]
    if not body:
        raise KursError("a function needs an entry block", pos)
    return Fun(name, params, premises, parse_type(forms[0]), tuple(parse_type(t) for t in _list(forms[1])),
               tuple(parse_block(b) for b in body), locals_, unsafe, module, pos)


def _module(form: Form) -> ModuleId:
    form = _list(form)
    return ModuleId(_int(form[1]), tuple(map(str, form[2:])))


def _int(form: Form) -> int:
    """A decimal atom no larger than `usize` holds, which literals take as 64-bit. A negative one is left
    to the checker."""
    text = _text(form)
    try:
        value: Optional[int] = int(text)
    except ValueError:
        value = None
    if value is None or value >= 1 << LITERAL_BITS["usize"]:
        _fail(f"{write_form(text)} is not a usize", form)
    return value


def _clause(
    form: List
) -> tuple[list[Form], Optional[ModuleId], bool, Optional[Exposure]]:
    """The optional `(unsafe)` or `(in OWNER POLICY?)` clause after a declaration head.

    Returns (rest, module, unsafe, exposure)."""
    rest, clause = form[1:], form[1] if len(form) > 1 else None
    if _is(clause, "unsafe", 1):
        return rest[1:], None, True, None
    if not _is(clause, "in"):
        return rest, None, False, None
    module, policy = _module(clause[1]), clause[2] if len(clause) > 2 else None
    exposure: Optional[Exposure] = (("owner", module) if policy == "abstract" else
                ("within", _module(policy[1])) if _is(policy, "exposed") else None)
    return rest[1:], module, policy == "unsafe", exposure


def parse_program(text: str) -> Program:
    program = Program()
    params: tuple[Param, ...]
    premises: tuple[Premise, ...]
    for raw_form in read(text):
        form = _list(raw_form)
        rest, module, unsafe, exposure = _clause(form)
        head = form[0]
        if head == "type":
            raw = str(rest[0])
            params = ()
            premises = ()
            body = rest[1]
            while _is(body, "forall"):
                more, more_premises = parse_forall(body[1], body[2])
                params, premises, body = params + more, premises + more_premises, body[3]
            program.types[raw] = TypeDecl(raw, params, premises, parse_type(body),
                                           module, exposure)
        elif head == "fun":
            program.funs[str(rest[0])] = parse_fun(rest[1:], str(rest[0]), module, unsafe, form.pos)
        elif head == "extern":
            kind, name = form[1], str(form[2])
            program.externs[name] = (Extern(name, parse_type(form[3]), tuple(parse_type(t) for t in _list(form[4])), form.pos)
                                     if kind == "fun" else Extern(name, parse_type(form[3]), None, form.pos))
        elif head == "trait":
            trait_methods: list[Method] = []
            for method in map(_list, rest[2:]):
                m_unsafe = _is(method[0], "unsafe", 1)
                m = method[1:] if m_unsafe else method
                ret = parse_type(m[1])
                ptypes, places = _ptypes(m[2])
                trait_methods.append(Method(str(m[0]), ret, ptypes, m_unsafe, places))
            params, _ = parse_forall(rest[1], List())
            program.traits[str(rest[0])] = Trait(str(rest[0]), tuple(p.name for p in params),
                                                tuple(trait_methods), module, tuple(p.kind for p in params))
        elif head == "instance":
            head_form, params, premises = rest[1], (), ()
            if _is(head_form, "forall"):
                params, premises = parse_forall(head_form[1], head_form[2])
                head_form = head_form[3]
            methods: list[Fun] = []
            for m in map(_list, rest[2:]):
                m_rest, _, m_unsafe, _ = _clause(m)
                methods.append(parse_fun(m_rest[1:], str(m_rest[0]), module, unsafe or m_unsafe, m.pos))
            program.instances.setdefault(str(rest[0]), []).append(Instance(
                str(rest[0]), params, premises, tuple(parse_type(t) for t in _list(head_form)),
                tuple(methods), module, form.pos))
        elif head == "data":
            program.datas[str(rest[0])] = Data(str(rest[0]), parse_type(rest[1]), rest[2], module)
        else:
            _fail(f"unknown declaration {write_form(head)}", form)
    return program


# ------------------------------------------------------------------ writing --
#
# `write_program` is the inverse of `parse_program`: one top-level form per declaration, and in a
# function one block header, clause, instruction or terminator per line. Every part has one spelling,
# so equal programs are written as equal text.

_ESCAPES = {"\n": "\\n", "\t": "\\t", "\r": "\\r", "\\": "\\\\", '"': '\\"'}


def _cannot(what: str) -> NoReturn:
    raise KursError(f"cannot write {what} as KURS")


def write_string(text: str) -> str:
    """`text` as a string literal."""
    out = []
    for c in text:
        if c in _ESCAPES:
            out.append(_ESCAPES[c])
        elif c < " " or c == "\x7f":
            out.append(f"\\x{ord(c):02x}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def write_atom(text: str) -> str:
    """`text` as an atom, or as a string literal when it is not a single word."""
    if not text or any(c <= " " or c in '();"\x7f' for c in text):
        return write_string(text)
    return text


def _form(parts: Sequence[str]) -> str:
    return "(" + " ".join(parts) + ")"


def write_form(form: Form) -> str:
    if isinstance(form, List):
        return _form([write_form(f) for f in form])
    if isinstance(form, Str):
        return write_string(form)
    return write_atom(form)


def write_type(ty: Type) -> str:
    match ty:
        case Ignored():
            return "_"
        case Never():
            return "!"
        case Skolem(label=label):
            return write_atom("$" + label)
        case Prim(name=name):
            return name
        case Named(name=name, args=()):
            return write_atom(name)
        case Named(name=name, args=args):
            return _form([write_atom(name), *map(write_type, args)])
        case Struct(fields=fields):
            return _form(["struct", *_bindings(fields)])
        case Enum(variants=variants):
            return _form(["enum", *_bindings(variants)])
        case Tuple(elems=elems):
            return _form(["tuple", *map(write_type, elems)])
        case Array(elem=elem, length=length):
            return _form(["array", write_type(elem), str(length)])
        case Ptr(inner=inner):
            return _form(["ptr", write_type(inner)])
        case NonNull(inner=inner):
            return _form(["non_null", write_type(inner)])
        case Brand(brand=Skolem(label=label)):
            return _form(["brand", write_atom("$" + label)])
        case Exists(binders=binders, body=body):
            return _form(["exists", _brands(binders), write_type(body)])
        case Foreach(binder=binder, indirect=indirect, source=source, body=body, retained=retained):
            pattern = _form(["ptr", write_atom(binder)]) if indirect else write_atom(binder)
            parts = ["foreach", _form([pattern, write_type(source)]), write_type(body)]
            inferred = (source.binder, *(source.retained or ())) if isinstance(source, Foreach) else ()
            if retained != inferred:
                parts.append(_form(["retain", *map(write_atom, retained or ())]))
            return _form(parts)
        case Fn(params=params, ret=ret, forall=forall, unsafe=unsafe, places=places):
            parts = ["fn", *(["(unsafe)"] if unsafe else [])]
            if forall:
                parts.append("(" + write_forall(forall, ()) + " " + write_type(ret) + " "
                             + _write_ptypes(params, places) + ")")
            else:
                parts += [_write_ptypes(params, places), write_type(ret)]
            return _form(parts)
    _cannot(f"the type {ty}")


def _brands(labels: Sequence[str]) -> str:
    return _form([write_atom("$" + label) for label in labels])


def _bindings(bindings: Sequence[tuple[str, Type]]) -> list[str]:
    """`(name TYPE)` for each pair: fields, variants, parameters, locals."""
    return [_form([write_atom(name), write_type(ty)]) for name, ty in bindings]


def _write_ptypes(ptypes: Sequence[Type], places: frozenset[int]) -> str:
    return _form([_form(["&", write_type(t)]) if i in places else write_type(t) for i, t in enumerate(ptypes)])


def write_forall(params: Sequence[Param], premises: Sequence[Premise]) -> str:
    """`forall (PARAM*) (PREMISE*)`, the start of a forall form; a Copy parameter has a `(Copy T)` premise."""
    names = [write_atom("$" + p.name if p.kind == BRAND else p.name) for p in params]
    copies = [_form(["Copy", write_atom(p.name)]) for p in params if p.kind == COPY]
    others = [_form([write_atom(p.trait), *map(write_type, p.args)]) for p in premises]
    return "forall " + _form(names) + " " + _form(copies + others)


def _applied_text(name: str, tys: Sequence[Type]) -> str:
    """`name` or `(name T*)`: a callee, mapper, function-value name or edge target."""
    return _form([write_atom(name), *map(write_type, tys)]) if tys else write_atom(name)


def _write_edge(edge: Edge) -> list[str]:
    return [_applied_text(edge.target, edge.tys), *map(write_atom, edge.args)]


def write_term(term: Terminator) -> str:
    match term:
        case Ret(value=value):
            return _form(["ret", write_atom(value)])
        case Unreachable():
            return "(unreachable)"
        case Jmp(edge=edge):
            return _form(["jmp", *_write_edge(edge)])
        case Jnz(cond=cond, then=then, other=other):
            return _form(["jnz", write_atom(cond), _form(_write_edge(then)), _form(_write_edge(other))])
        case Switch(scrutinee=scrutinee, ty=ty, arms=arms):
            return _form(["switch", write_atom(scrutinee), write_type(ty),
                          *(_form([write_atom(arm.variant), *_write_edge(arm.edge)]) for arm in arms)])


def _type_operand(instr: Instr) -> str:
    if instr.ty is None:
        _cannot(f"{instr.op} without a type operand")
    return write_type(instr.ty)


def _literal(instr: Instr) -> str:
    if instr.lit is None:
        _cannot(f"{instr.op} without a literal")
    return write_atom(instr.lit)


def write_instr(instr: Instr) -> str:
    op, args = instr.op, [write_atom(a) for a in instr.args]
    operation: list[str]
    if op == "const":
        operation = [op, _type_operand(instr), _literal(instr)]
    elif op == "construct":
        operation = [op, _type_operand(instr), *args]
    elif op in ("destruct", "variant_tag", "cast", "int_cast", "float_cast", "to_ptr"):
        operation = [op, args[0], _type_operand(instr)]
    elif op == "construct_variant":
        operation = [op, _type_operand(instr), _literal(instr), args[0]]
    elif op in ("alloca", "sizeof"):
        operation = [op, _type_operand(instr)]
    elif op == "pack":
        operation = [op, _type_operand(instr), _form([write_type(t) for t in instr.tys]), args[0]]
    elif op == "open":
        operation = [op, args[0], _brands(instr.binders)]
    elif op in ("call", "fn"):
        operation = [op, _applied_text(instr.args[0], instr.tys), *args[1:]]
    elif op == "foreach":
        operation = [op, _type_operand(instr), args[0], _applied_text(instr.args[1], instr.tys), *args[2:]]
    elif op == "trait_call":
        operation = [op, args[0], args[1], _form([write_type(t) for t in instr.tys]), *args[2:]]
    elif op in ATOMIC_OPS:
        operation = [op, _literal(instr), *args]
    else:
        operation = [op, *args]
    lhs = write_atom(instr.lhs[0]) if len(instr.lhs) == 1 else _form([write_atom(n) for n in instr.lhs])
    text = _form(["=", lhs, _form(operation)])
    return _form(["unsafe", text]) if instr.unsafe else text


def write_block(block: Block, indent: str) -> list[str]:
    """The block's lines: its header, then its clauses, instructions and terminator, indented further."""
    header = [write_atom(block.label), *([_brands(block.brands)] if block.brands else []),
              _form(_bindings(block.params))]
    inner = indent + "  "
    lines = [indent + "(" + " ".join(header)]
    if block.empty:
        lines.append(inner + _form(["empty", *map(write_atom, block.empty)]))
    lines += [inner + _form(["known", write_atom(path), write_atom(variant)]) for path, variant in block.known]
    lines += [inner + _form(["opened", write_atom(path), _brands(brands)]) for path, brands in block.opened]
    lines += [inner + write_instr(instr) for instr in block.instrs]
    lines.append(inner + write_term(block.term) + ")")
    return lines


def write_fun(fun: Fun, head: Sequence[str], indent: str = "") -> list[str]:
    """`(HEAD... NAME RET (PTYPES) (locals ...)? BLOCK+)`, forall-wrapped from RET on when generic."""
    signature = [write_type(fun.ret), _form([write_type(t) for t in fun.ptypes])]
    generic = bool(fun.params or fun.premises)
    if generic:
        signature = ["(" + write_forall(fun.params, fun.premises), *signature]
    lines = [indent + "(" + " ".join([*head, write_atom(fun.name), *signature])]
    inner = indent + "  "
    if fun.locals:
        lines.append(inner + _form(["locals", *_bindings(fun.locals)]))
    for block in fun.blocks:
        lines += write_block(block, inner)
    lines[-1] += "))" if generic else ")"
    return lines


def write_module(module: ModuleId) -> str:
    return _form(["module", str(module.package), *map(write_atom, module.path)])


def _in(module: Optional[ModuleId], policy: Optional[str] = None) -> list[str]:
    """The `(in MODULE POLICY?)` clause, if the declaration has a module."""
    if module is None:
        return []
    return [_form(["in", write_module(module), *([policy] if policy else [])])]


def _fun_clause(module: Optional[ModuleId], unsafe: bool) -> list[str]:
    """A function's `(unsafe)` or `(in MODULE unsafe?)` clause."""
    if module is None:
        return ["(unsafe)"] if unsafe else []
    return _in(module, "unsafe" if unsafe else None)


def write_type_decl(decl: TypeDecl) -> str:
    policy: Optional[str] = None
    if decl.exposure is not None:
        mode, scope = decl.exposure
        if decl.module is None or mode == "owner" and scope != decl.module:
            _cannot(f"the exposure of {decl.name}")
        policy = "abstract" if mode == "owner" else _form(["exposed", write_module(scope)])
    body = write_type(decl.body)
    if decl.params or decl.premises:
        body = "(" + write_forall(decl.params, decl.premises) + " " + body + ")"
    return _form(["type", *_in(decl.module, policy), write_atom(decl.name), body])


def write_extern(extern: Extern) -> str:
    if extern.ret is None:
        _cannot(f"the extern {extern.name} without a type")
    if extern.ptypes is None:
        return _form(["extern", "value", write_atom(extern.name), write_type(extern.ret)])
    return _form(["extern", "fun", write_atom(extern.name), write_type(extern.ret),
                  _form([write_type(t) for t in extern.ptypes])])


def write_data(data: Data) -> str:
    return _form(["data", *_in(data.module), write_atom(data.name), write_type(data.ty), write_form(data.init)])


def write_trait(trait: Trait) -> list[str]:
    kinds = trait.param_kinds or (ANY,) * len(trait.params)
    if COPY in kinds:
        _cannot(f"the Copy parameter of trait {trait.name}")
    params = [write_atom("$" + name if kind == BRAND else name) for name, kind in zip(trait.params, kinds)]
    lines = ["(" + " ".join(["trait", *_in(trait.module), write_atom(trait.name), _form(params)])]
    for method in trait.methods:
        lines.append("  " + _form([*(["(unsafe)"] if method.unsafe else []), write_atom(method.name),
                                   write_type(method.ret), _write_ptypes(method.ptypes, method.places)]))
    lines[-1] += ")"
    return lines


def write_instance(instance: Instance) -> list[str]:
    head = _form([write_type(t) for t in instance.head])
    if instance.params or instance.premises:
        head = "(" + write_forall(instance.params, instance.premises) + " " + head + ")"
    lines = ["(" + " ".join(["instance", *_in(instance.module), write_atom(instance.trait), head])]
    for method in instance.methods:
        if method.module != instance.module:
            _cannot(f"the method {method.name} of an instance of {instance.trait} from another module")
        lines += write_fun(method, ["fun", *(["(unsafe)"] if method.unsafe else [])], "  ")
    lines[-1] += ")"
    return lines


def write_program(program: Program) -> str:
    """The program as KURS text that `parse_program` reads back into the same program."""
    lines = [write_type_decl(decl) for decl in program.types.values()]
    for fun in program.funs.values():
        lines += write_fun(fun, ["fun", *_fun_clause(fun.module, fun.unsafe)])
    lines += [write_extern(extern) for extern in program.externs.values()]
    lines += [write_data(data) for data in program.datas.values()]
    for trait in program.traits.values():
        lines += write_trait(trait)
    for instances in program.instances.values():
        for instance in instances:
            lines += write_instance(instance)
    return "".join(line + "\n" for line in lines)
