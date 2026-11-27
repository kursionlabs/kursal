"""Regenerate and verify the Counter walkthrough. Run from any directory.

The private serialization writer handles only the forms in this example.
Readable output is rendered by kursal.readable from the same core program.
The emitted KURS is parsed and checked independently, and both C programs run.
"""

import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE.parents[1]))

from kursal import core as C, syntax as S  # noqa: E402
from kursal.cc import FLAGS, c_compiler  # noqa: E402
from kursal.cgen import emit_c  # noqa: E402
from kursal.check import check_program  # noqa: E402
from kursal.infer import FnCall  # noqa: E402
from kursal.kurs import parse_program  # noqa: E402
from kursal.readable import emit_kurs  # noqa: E402
from kursal.lower import lower  # noqa: E402
from kursal.resolve import resolve  # noqa: E402
from kursal.sigs import signatures  # noqa: E402
from kursal.types import show  # noqa: E402


def type_text(ty: C.Type) -> str:
    match ty:
        case C.Prim(name=name) | C.Named(name=name, args=()):
            return name
        case C.Tuple(elems=()):
            return "(tuple)"
        case C.Struct(fields=fields):
            return "(struct " + " ".join(f"({name} {type_text(ty)})" for name, ty in fields) + ")"
    raise ValueError(f"type outside the example: {ty}")


def bindings(items: tuple[tuple[str, C.Type], ...]) -> str:
    return "(" + " ".join(f"({name} {type_text(ty)})" for name, ty in items) + ")"


def edge_text(edge: C.Edge) -> str:
    assert not edge.tys
    return " ".join((edge.target, *edge.args))


def instruction_text(instr: C.Instr) -> str:
    assert len(instr.lhs) == 1 and not instr.tys and not instr.binders and not instr.unsafe
    match instr.op:
        case "const":
            assert instr.ty is not None and instr.lit is not None
            operands = [type_text(instr.ty), instr.lit]
        case "construct":
            assert instr.ty is not None
            operands = [type_text(instr.ty), *instr.args]
        case "int_cast":
            assert instr.ty is not None
            operands = [*instr.args, type_text(instr.ty)]
        case "load" | "store" | "copy" | "add" | "eq" | "call":
            assert instr.ty is None and instr.lit is None
            operands = list(instr.args)
        case _:
            raise ValueError(f"instruction outside the example: {instr.op}")
    return f"(= {instr.lhs[0]} ({' '.join([instr.op, *operands])}))"


def serialized_kurs(program: C.Program) -> str:
    assert not program.traits and not program.instances and not program.externs and not program.datas
    lines = []
    for name, decl in program.types.items():
        assert not decl.params and not decl.premises and not decl.linear and decl.exposure is None
        lines.append(f"(type {name} {type_text(decl.body)})")
    for fun in program.funs.values():
        assert not fun.params and not fun.premises and not fun.unsafe
        params = " ".join(type_text(ty) for ty in fun.ptypes)
        lines.extend(["", f"(fun {fun.name} {type_text(fun.ret)} ({params})"])
        if fun.locals:
            lines.append(f"  (locals {bindings(fun.locals)[1:-1]})")
        for block in fun.blocks:
            assert not block.brands and not block.known and not block.opened
            lines.append(f"  ({block.label} {bindings(block.params)}")
            if block.empty:
                lines.append(f"    (empty {' '.join(block.empty)})")
            lines.extend(f"    {instruction_text(instr)}" for instr in block.instrs)
            match block.term:
                case C.Ret(value=value):
                    term = f"(ret {value})"
                case C.Jmp(edge=edge):
                    term = f"(jmp {edge_text(edge)})"
                case C.Jnz(cond=cond, then=then, other=other):
                    term = f"(jnz {cond} (({edge_text(then)})) (({edge_text(other)})))"
                case _:
                    raise ValueError(f"terminator outside the example: {block.term}")
            lines.append(f"    {term})")
        lines.append(")")
    return "\n".join(lines) + "\n"


def annotations(bump: S.Fn, main: S.Fn) -> str:
    assert bump.body is not None and main.body is not None
    counter, result = main.body.stmts
    assert isinstance(counter, S.Let) and counter.mutable and isinstance(counter.pattern, S.PBind)
    assert isinstance(result, S.Let)
    assert isinstance(result.pattern, S.PBind) and isinstance(result.value, S.If)
    branch = result.value
    call = branch.then.tail
    assert isinstance(call, S.Call) and isinstance(call.call, FnCall)
    argument = call.args[0]
    assert isinstance(argument, S.Unary) and argument.op == "&" and isinstance(argument.operand, S.Path)
    return "\n".join([
        f"bump parameter IDs: {bump.param_ids}",
        f"bump parameter types: {[show(ty) for ty in call.call.sig.ptypes]}; lent indices: {sorted(call.call.sig.places)}",
        f"bump body type: {show(bump.body.ty)}",
        f"main parameter IDs: {main.param_ids}",
        f"counter binding ID: {counter.pattern.binder}; initializer type: {show(counter.value.ty)}",
        f"if condition type: {show(branch.cond.ty)}",
        f"if result type: {show(branch.ty)}",
        f"then call: {type(call.call).__name__}; result type: {show(call.ty)}",
        f"then call target: {call.call.name}; type arguments: {[show(ty) for ty in call.call.targs]}",
        f"lent argument binding ID: {argument.operand.binder}",
        f"result binding ID: {result.pattern.binder}; type: {show(result.pattern.ty)}",
        f"main body type: {show(main.body.ty)}",
    ]) + "\n"


def generate(output: Path) -> tuple[str, str]:
    """Write documentation artifacts and check the serialization round-trip."""
    output.mkdir(parents=True, exist_ok=True)
    resolver = resolve(PACKAGE)
    sigs = signatures(resolver)
    program = lower(resolver, sigs)
    checker = check_program(program)
    entry = resolver.packages[0].entry_decl
    assert entry is not None
    c_source = emit_c(program, checker, sigs.name(entry))
    kurs = serialized_kurs(program)
    restored = parse_program(kurs)
    restored_c = emit_c(restored, check_program(restored), sigs.name(entry))
    assert restored_c == c_source, "KURS round-trip changed generated C"
    functions = {decl.name: decl.node for decl in sigs.sigs}
    bump, main = functions["bump"], functions["main"]
    assert isinstance(bump, S.Fn) and isinstance(main, S.Fn)
    (output / "annotations.txt").write_text(annotations(bump, main))
    (output / "program.kurs").write_text(emit_kurs(program))
    (output / "program.sexpr").write_text(kurs)
    (output / "program.c").write_text(c_source)
    return c_source, restored_c


class Arguments(argparse.Namespace):
    output: Path
    write_only: bool


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path, default=PACKAGE / "generated")
    parser.add_argument("--write-only", action="store_true", help="regenerate artifacts without compiling or running C")
    args = parser.parse_args(namespace=Arguments())
    c_source, restored_c = generate(args.output)
    print(f"Wrote annotations.txt, program.kurs, program.sexpr and program.c to {args.output}")
    if args.write_only:
        return
    with tempfile.TemporaryDirectory(prefix="kursal-counter-") as temporary:
        for name, source in (("surface", c_source), ("kurs", restored_c)):
            binary = Path(temporary) / name
            subprocess.run([c_compiler(), *FLAGS, "-x", "c", "-o", str(binary), "-"], input=source, text=True, check=True)
            status = subprocess.run([str(binary)], check=False).returncode
            assert status == 82, f"{name}: expected 82, got {status}"
    print("Core checking and KURS round-trip passed; both executables exited 82 (41 + 41).")


if __name__ == "__main__":
    run()
