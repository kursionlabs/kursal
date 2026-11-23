"""Readable rendering of typed KURS; the interchange reader remains kurs.py.

This is a display format, not an alternate checker or a reconstruction from C.
"""

import json
from typing import Optional

from . import core as C
from .sexpr import Form, List, Str
from .types import subst


def brand(name: str) -> str:
    return "'" + name


def arguments(types: tuple[C.Type, ...], depth: int = 0) -> str:
    return "[" + ", ".join(type_text(ty, depth) for ty in types) + "]" if types else ""


def premise_text(premise: C.Premise) -> str:
    return premise.trait + "[" + ", ".join(type_text(ty) for ty in premise.args) + "]"


def quantifier(params: tuple[C.Param, ...], premises: tuple[C.Premise, ...]) -> str:
    names = [brand(param.name) if param.kind == C.BRAND else param.name for param in params]
    bounds = [f"Copy[{param.name}]" for param in params if param.kind == C.COPY]
    bounds.extend(premise_text(premise) for premise in premises)
    if not names and not bounds:
        return ""
    return "forall " + ", ".join(names) + (" where " + ", ".join(bounds) if bounds else "") + ". "


def type_text(ty: C.Type, depth: int = 0) -> str:
    match ty:
        case C.Prim(name=name) | C.Named(name=name, args=()):
            return name
        case C.Named(name=name, args=args):
            return name + arguments(args, depth)
        case C.Never():
            return "Never"
        case C.Skolem(label=label):
            return brand(label)
        case C.Brand(brand=witness):
            return "Brand[" + type_text(witness, depth) + "]"
        case C.NonNull(inner=inner):
            return "NonNull[" + type_text(inner, depth) + "]"
        case C.Ptr(inner=inner):
            text = type_text(inner, depth)
            return "*(" + text + ")" if isinstance(inner, (C.Exists, C.Foreach, C.Fn)) else "*" + text
        case C.Tuple(elems=elems):
            if not elems:
                return "unit"
            return "(" + ", ".join(type_text(elem, depth) for elem in elems) + ("," if len(elems) == 1 else "") + ")"
        case C.Array(elem=elem, length=length):
            return f"[{type_text(elem, depth)}; {length}]"
        case C.Struct(fields=fields) | C.Enum(variants=fields):
            kind = "struct" if isinstance(ty, C.Struct) else "enum"
            if not fields:
                return kind + " {}"
            body = "\n".join("    " * (depth + 1) + f"{name}: {type_text(field, depth + 1)};" for name, field in fields)
            return kind + " {\n" + body + "\n" + "    " * depth + "}"
        case C.Exists(binders=binders, body=body):
            return "exists " + ", ".join(map(brand, binders)) + ". " + type_text(body, depth)
        case C.Foreach(binder=binder, indirect=indirect, source=source, body=body):
            return f"foreach {'*' if indirect else ''}{binder} of {type_text(source, depth)} => {type_text(body, depth)}"
        case C.Fn(params=params, ret=ret, unsafe=unsafe, places=places):
            modes = ", ".join(("&" if index in places else "") + type_text(param, depth) for index, param in enumerate(params))
            return ("unsafe " if unsafe else "") + f"fn({modes}) -> {type_text(ret, depth)}"
    raise ValueError(f"unsupported readable KURS type: {ty!r}")


def edge_text(edge: C.Edge) -> str:
    return edge.target + arguments(edge.tys) + "(" + ", ".join(edge.args) + ")"


def instruction_text(instr: C.Instr, depth: int, omit_unit: bool = False) -> str:
    op, args = instr.op, instr.args
    ty = type_text(instr.ty, depth) if instr.ty is not None else ""
    literal = instr.lit if instr.lit is not None else ""
    lhs = instr.lhs[0] if len(instr.lhs) == 1 else "(" + ", ".join(instr.lhs) + ")"
    prefix = "unsafe " if instr.unsafe else ""
    if op == "open":
        binding = "" if omit_unit and args[0].startswith("&") else lhs + " = "
        return prefix + "exists " + ", ".join(map(brand, instr.binders)) + ". " + binding + "open " + args[0] + ";"
    if op == "pack":
        assert isinstance(instr.ty, C.Exists)
        payload = subst(instr.ty.body, dict(zip(instr.ty.binders, instr.tys)))
        rhs = "pack " + ", ".join(type_text(witness) for witness in instr.tys) + ". " + args[0] + " as " + type_text(payload, depth)
    elif op in ("call", "fn"):
        rhs = op + " " + args[0] + arguments(instr.tys, depth)
        if op == "call":
            rhs += "(" + ", ".join(args[1:]) + ")"
    elif op == "trait_call":
        rhs = args[0] + arguments(instr.tys, depth) + "::" + args[1] + "(" + ", ".join(args[2:]) + ")"
        rhs = "trait_call " + rhs
    elif op == "foreach":
        assert isinstance(instr.ty, C.Foreach)
        pattern = ("*" if instr.ty.indirect else "") + instr.ty.binder
        rhs = f"foreach {pattern} of {args[0]} : {type_text(instr.ty.source, depth)} => {type_text(instr.ty.body, depth)}"
        rhs += " using " + args[1] + arguments(instr.tys, depth) + "(" + ", ".join(args[2:]) + ")"
    else:
        operands = list(args)
        if op == "const":
            operands = [ty, literal]
        elif op in ("construct", "destruct", "variant_tag"):
            operands = [ty, *args]
        elif op == "construct_variant":
            operands = [ty, literal, *args]
        elif op in ("int_cast", "float_cast", "cast", "to_ptr"):
            operands = [*args, ty]
        elif op in ("alloca", "sizeof"):
            operands = [ty]
        elif instr.lit is not None:
            operands = [literal, *args]
        elif instr.ty is not None or instr.tys or instr.binders:
            raise ValueError(f"unsupported readable instruction metadata: {instr!r}")
        rhs = op + (" " + ", ".join(operands) if operands else "")
    return prefix + lhs + " = " + rhs + ";"


def terminator_text(term: C.Terminator) -> str:
    match term:
        case C.Ret(value=value):
            return "return " + value + ";"
        case C.Jmp(edge=edge):
            return "jump " + edge_text(edge) + ";"
        case C.Jnz(cond=cond, then=then, other=other):
            return f"branch {cond}, {edge_text(then)}, {edge_text(other)};"
        case C.Switch(scrutinee=scrutinee, ty=ty, arms=arms):
            lines = [f"switch {scrutinee} : {type_text(ty)} {{"]
            for arm in arms:
                lines.append(f"    {arm.variant} => {edge_text(arm.edge)};")
            return "\n".join([*lines, "}"])
        case C.Unreachable():
            return "unreachable;"


def metadata(module: Optional[C.ModuleId], depth: int = 0) -> list[str]:
    if module is None:
        return []
    return ["    " * depth + f"// owner: package {module.package}, module {json.dumps(module.path)}"]


def function_lines(fun: C.Fun, depth: int = 0) -> list[str]:
    indent = "    " * depth
    lines = metadata(fun.module, depth)
    quant = quantifier(fun.params, fun.premises)
    if quant:
        lines.append(indent + quant.rstrip())
    params = ", ".join(f"{name}: {type_text(ty, depth)}" for name, ty in fun.blocks[0].params)
    lines.append(indent + ("unsafe " if fun.unsafe else "") + f"fn {fun.name}({params}) -> {type_text(fun.ret, depth)} {{")
    if fun.locals:
        lines.append(indent + "    locals {")
        lines.extend(indent + f"        {name}: {type_text(ty, depth + 2)};" for name, ty in fun.locals)
        lines.append(indent + "    }")
    for index, block in enumerate(fun.blocks):
        brands = "[" + ", ".join(map(brand, block.brands)) + "]" if block.brands else ""
        params = ", ".join(f"{name}: {type_text(ty, depth + 1)}" for name, ty in block.params)
        header = "entry" if index == 0 else f"block {block.label}{brands}({params})"
        if index == 0 and block.label != "entry":
            lines.append(indent + "    // entry label: " + block.label)
        lines.append(indent + "    " + header + " {")
        if block.empty or block.known or block.opened:
            lines.append(indent + "        state {")
            for path, witnesses in block.opened:
                lines.append(indent + "            open " + ", ".join(map(brand, witnesses)) + ". " + path + ";")
            lines.extend(indent + f"            empty {path};" for path in block.empty)
            lines.extend(indent + f"            known {path} = {variant};" for path, variant in block.known)
            lines.append(indent + "        }")
        # Preserve a unit binding if later code actually refers to it.
        used = {arg for instr in block.instrs for arg in instr.args}
        match block.term:
            case C.Ret(value=value):
                used.add(value)
            case C.Jmp(edge=edge):
                used.update(edge.args)
            case C.Jnz(cond=cond, then=then, other=other):
                used.update((cond, *then.args, *other.args))
            case C.Switch(scrutinee=scrutinee, arms=arms):
                used.add(scrutinee)
                for arm in arms:
                    used.update(arm.edge.args)
        for instr in block.instrs:
            text = instruction_text(instr, depth + 2, not any(name in used for name in instr.lhs))
            lines.append(indent + "        " + text)
        lines.extend(indent + "        " + line for line in terminator_text(block.term).splitlines())
        lines.append(indent + "    }")
    return [*lines, indent + "}"]


def initializer_text(form: Form) -> str:
    if isinstance(form, Str):
        return json.dumps(str(form), ensure_ascii=True)
    if isinstance(form, List):
        return "[" + ", ".join(initializer_text(item) for item in form) + "]"
    return str(form)


def emit_kurs(program: C.Program) -> str:
    """Render all declarations without changing or checking the program."""
    sections: list[str] = []
    for decl in program.types.values():
        lines = metadata(decl.module)
        if decl.exposure is not None:
            mode, owner = decl.exposure
            lines.append(f"// exposure: {mode}, package {owner.package}, module {json.dumps(owner.path)}")
        quant = quantifier(decl.params, decl.premises)
        if quant:
            lines.append(quant.rstrip())
        lines.append(f"type {decl.name} = {type_text(decl.body)};")
        sections.append("\n".join(lines))
    for external in program.externs.values():
        extern_params = ", ".join(type_text(param) for param in external.ptypes)
        sections.append(f"extern fn {external.name}({extern_params}) -> {type_text(external.ret)};")
    for data in program.datas.values():
        sections.append("\n".join([*metadata(data.module), f"data {data.name}: {type_text(data.ty)} = {initializer_text(data.init)};"]))
    for trait in program.traits.values():
        kinds = trait.param_kinds or (C.ANY,) * len(trait.params)
        params = tuple(C.Param(name, kind) for name, kind in zip(trait.params, kinds))
        names = ", ".join(brand(param.name) if param.kind == C.BRAND else param.name for param in params)
        bounds = [f"Copy[{param.name}]" for param in params if param.kind == C.COPY]
        header = f"trait {trait.name}[{names}]" + (" where " + ", ".join(bounds) if bounds else "")
        lines = [*metadata(trait.module), header + " {"]
        for method in trait.methods:
            modes = ", ".join(("&" if index in method.places else "") + type_text(ty, 1) for index, ty in enumerate(method.ptypes))
            lines.append("    " + ("unsafe " if method.unsafe else "") + f"fn {method.name}({modes}) -> {type_text(method.ret, 1)};")
        sections.append("\n".join([*lines, "}"]))
    for instances in program.instances.values():
        for instance in instances:
            lines = metadata(instance.module)
            quant = quantifier(instance.params, instance.premises)
            if quant:
                lines.append(quant.rstrip())
            lines.append("instance " + instance.trait + arguments(instance.head) + " {")
            for implementation in instance.methods:
                lines.extend(function_lines(implementation, 1))
            sections.append("\n".join([*lines, "}"]))
    sections.extend("\n".join(function_lines(fun)) for fun in program.funs.values())
    return "\n\n".join(sections) + "\n"
