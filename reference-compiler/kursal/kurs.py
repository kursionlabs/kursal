"""Read the KURS S-expression serialization into the core IR.

This input adapter is separate from the strictly typed IR and type operations.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import NoReturn, TypeGuard, Optional
from .sexpr import Atom, Form, KursError, List, Position, read
from .core import (ANY, BRAND, COPY, PRIMS, Array, Arm, Block, Brand, Data,
                   Edge, Enum, Exists, Exposure, Extern, Fn, Foreach, Fun, Ignored, Instance,
                   Instr, Jmp, Jnz, Method, ModuleId, Named, Never, Param, Premise,
                   Prim, Program, Ptr, Ret, Skolem, Struct, Switch, Trait, Tuple,
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
        _fail(f"expected a brand such as $k, got {atom}", atom)
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
        return Named(form.rstrip("!") if len(form) > 1 else form)
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
        return Array(parse_type(rest[0]), int(_text(rest[1])))
    if head == "builtin::Pointer":
        return Ptr(parse_type(rest[0]))
    if head == "builtin::Brand":
        return Brand(Skolem(_brand(rest[0])))
    if head == "exists":
        return Exists(tuple(_brand(b) for b in _list(rest[0])), parse_type(rest[1]))
    if head == "foreach":
        pattern, source = _list(rest[0])
        indirect = _is(pattern, "builtin::Pointer", 2)
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
    if head == "builtin::Fn":
        unsafe = _is(rest[0], "unsafe", 1)
        rest = rest[1:] if unsafe else rest
        header = rest[0]
        if _is(header, "forall", 5):
            params, premises = parse_forall(header[1], header[2])
            if premises:
                _fail("a function type has no premises", header)
            return Fn(tuple(parse_type(t) for t in _list(header[4])), parse_type(header[3]), params, unsafe)
        return Fn(tuple(parse_type(t) for t in _list(rest[0])), parse_type(rest[1]), unsafe=unsafe)
    return Named(head.rstrip("!"), tuple(parse_type(t) for t in rest))


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
    _fail(f"unknown terminator {head}", form)


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
    if op in ("destruct", "variant_tag", "builtin::unsafe::cast", "builtin::int_cast"):
        return Instr(lhs, op, pos=pos, args=(str(rest[0]),), ty=parse_type(rest[1]))
    if op == "construct_variant":
        return Instr(lhs, op, pos=pos, ty=parse_type(rest[0]), lit=str(rest[1]), args=(str(rest[2]),))
    if op in ("alloca", "builtin::sizeof"):
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
    if op.startswith("builtin::unsafe::atomic_"):
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
        params, premises = parse_forall(header[1], header[2])
        forms = header[3:]
    body = forms[2:]
    locals_: tuple[tuple[str, Type], ...] = ()
    local_form = body[0] if body else None
    if _is(local_form, "locals"):
        locals_ = tuple((str(p[0]), parse_type(p[1])) for p in map(_list, local_form[1:]))
        body = body[1:]
    return Fun(name, params, premises, parse_type(forms[0]), tuple(parse_type(t) for t in _list(forms[1])),
               tuple(parse_block(b) for b in body), locals_, unsafe, module, pos)


def _module(form: Form) -> ModuleId:
    form = _list(form)
    return ModuleId(int(_text(form[1])), tuple(map(str, form[2:])))


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
            name = raw.rstrip("!") if len(raw) > 1 else raw
            program.types[name] = TypeDecl(name, raw.endswith("!"), params, premises, parse_type(body),
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
                trait_methods.append(Method(str(m[0]), parse_type(m[1]), tuple(parse_type(t) for t in _list(m[2])), m_unsafe))
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
            _fail(f"unknown declaration {head}", form)
    return program
