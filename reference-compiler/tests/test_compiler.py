"""The self-hosted compiler in compiler/ agrees with this one: built by the reference compiler, its parser
produces the same syntax tree for every source file in the repository, and rejects bad syntax with the
same message; its type modules give the same results as these on small cases; its resolver resolves
every package in the repository to the same names, and rejects bad packages with the same message;
it gives every package the same signatures, types and constant values, and rejects bad declarations
with the same message; it infers every function body to the same types, calls and bindings, and
rejects ill-typed bodies with the same message; it lowers every package, the packages this compiler's own
tests lower and small rejected programs to the same core program, or rejects them with the same message,
and its `build` writes the same C as this compiler's pipeline, freeing every byte it allocates (under
valgrind, when it is installed); with KURSAL_SELFHOST=1, it builds itself, and the result builds itself to
the same C, and builds it under valgrind freeing every byte;
its KURS reader and writer read every lowered program and write it back as this compiler's writer
does, and reject malformed KURS with the same message; its checker accepts and rejects, with the same
message, every core program this compiler's own tests check; and from every lowered program it writes
the same C and the same readable KURS."""

import dataclasses
import hashlib
import importlib
import itertools
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from kursal import core as C
from kursal import types as T
from kursal.coherence import check_coherence
from kursal.formation import Formation
from kursal.kurs import parse_type
from kursal.layout import check_type_expansion
from kursal.literals import valid_literal
from kursal.parser import parse
from kursal.sexpr import KursError, List, read
from kursal.termination import check_instance_termination, check_mono_termination
from kursal.unify import Meta, match_head, prune, unify, zonk
from kursal import infer as I
from kursal import syntax as S
from kursal.core import ModuleId
from kursal.resolve import LIB, Decl, Resolver, resolve
from kursal.kurs import (_form, _write_ptypes, parse_program, write_atom, write_forall, write_program, write_type,
                         write_type_decl)
from kursal.cgen import emit_c
from kursal.check import check_program
from kursal import lower as lower_module
from kursal.lower import lower
from kursal.readable import emit_kurs
from kursal.sigs import ConstValue, Sig, Signatures, signatures
from tests.infer_cases import INFER_ERRORS, INFERENCES
from tests.lower_cases import LOWER_ERRORS
from tests.test_kurs import CANONICAL, VARIANTS, packages

ROOT = Path(__file__).resolve().parents[2]
COMPILER = ROOT / "compiler"
CACHE = Path(__file__).resolve().parents[1] / ".build"
TESTS = Path(__file__).resolve().parent
# Every run of the Kursal compiler is bounded: it keeps everything in one arena until the command ends,
# so a runaway would take the machine's memory.
MEMORY_LIMIT = 6 << 30
TIME_LIMIT = 600


def limit_memory() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))


# Under valgrind the compiler's memory grows by valgrind's own; never run two such self-builds at once.
VALGRIND_MEMORY_LIMIT = 14_000_000 << 10
VALGRIND = ["valgrind", "--leak-check=full", "--show-leak-kinds=all", "--errors-for-leak-kinds=all",
            "--error-exitcode=99", "--trace-children=no"]


def limit_valgrind_memory() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (VALGRIND_MEMORY_LIMIT, VALGRIND_MEMORY_LIMIT))


def run_valgrind(binary: Path, *args: str, timeout: int) -> subprocess.CompletedProcess[str]:
    """`binary ARGS` under valgrind's leak check: status 99 on any error or any byte not freed, whether
    lost or still reachable. `cc`, which `build` runs, is not traced."""
    return subprocess.run([*VALGRIND, str(binary), *args], capture_output=True, text=True, timeout=timeout,
                          preexec_fn=limit_valgrind_memory, env=dict(os.environ, KURSAL_LIB=str(LIB)))


def run_kursal(binary: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=TIME_LIMIT,
                          preexec_fn=limit_memory, env=dict(os.environ, KURSAL_LIB=str(LIB)))


def digest_of(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def cached(stem: str, key: str, make: Callable[[], list]) -> list:
    """`make()`, a JSON value, kept in .build/ as STEM-KEY.json until KEY changes."""
    path = CACHE / f"{stem}-{key}.json"
    if path.exists():
        return json.loads(path.read_text())
    value = make()
    CACHE.mkdir(exist_ok=True)
    for old in CACHE.glob(f"{stem}-*.json"):
        old.unlink()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)
    return value


def self_built_compiler(kc1: Path) -> Path:
    """kc2, the compiler kc1 builds from compiler/, kept in .build/ beside kc1; its C is kc2.c."""
    kc2 = kc1.with_name(kc1.name.replace("kursal-", "kursal-self-"))
    if not kc2.exists():
        started = time.monotonic()
        result = run_kursal(kc1, "build", str(COMPILER), "-o", str(kc2), "--times")
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        print(f"\nkc2 built in {time.monotonic() - started:.1f}s:\n{result.stderr}", file=sys.stderr)
    return kc2


def build_compiler() -> Path:
    """The compiler binary, rebuilt when its sources or the library's change."""
    sources = sorted([*COMPILER.rglob("*.kur"), *COMPILER.rglob("*.toml"), *(ROOT / "lib").rglob("*.kur"),
                      *(ROOT / "reference-compiler/kursal").glob("*.py")])
    digest = hashlib.sha256()
    for path in sources:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    binary = CACHE / f"kursal-{digest.hexdigest()[:16]}"
    if not binary.exists():
        CACHE.mkdir(exist_ok=True)
        for old in CACHE.glob("kursal-*"):
            old.unlink()
        built = subprocess.run([sys.executable, str(ROOT / "reference-compiler/tools/build.py"), str(COMPILER), str(binary)],
                               capture_output=True, text=True)
        if built.returncode != 0:
            raise RuntimeError(built.stdout + built.stderr)
    return binary


def dump(node: object) -> str:
    """The canonical text of a syntax tree, as compiler/syntax/dump.kur writes it."""
    if node is None:
        return "_"
    if isinstance(node, bool):
        return "#t" if node else "#f"
    if isinstance(node, int):
        return str(node)
    if isinstance(node, str):
        out = []
        for b in node.encode():
            if b in (ord('"'), ord("\\")):
                out.append("\\" + chr(b))
            elif 32 <= b < 127:
                out.append(chr(b))
            else:
                out.append(f"\\x{b:02x}")
        return '"' + "".join(out) + '"'
    if isinstance(node, (tuple, list)):
        return "[" + " ".join(dump(x) for x in node) + "]"
    if dataclasses.is_dataclass(node):
        parts = [type(node).__name__]
        for f in dataclasses.fields(node):
            if not f.init:
                continue
            value = getattr(node, f.name)
            if f.name == "pos":
                parts.append(f"{value[0]}:{value[1]}" if value else "_")
            else:
                parts.append(dump(value))
        return "(" + " ".join(parts) + ")"
    raise TypeError(f"cannot dump {type(node).__name__}")


def normalized(text: str) -> str:
    """`text` without a space right after an opening bracket, outside string literals."""
    out, in_string, i = [], False, 0
    while i < len(text):
        c = text[i]
        out.append(c)
        if in_string:
            if c == "\\":
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c in "([" and text[i + 1:i + 2] == " ":
            i += 1
        i += 1
    return "".join(out)


def reference_dump(path: Path) -> str:
    return "".join(dump(item) + "\n" for item in parse(path.read_text(), str(path)))


def module_text(module: ModuleId) -> str:
    return f"{module.package}:{'::'.join(module.path)}"


def decl_text(decl: Decl) -> str:
    return f"{decl.kind} {module_text(decl.module)}:{decl.name}"


def ref_text(ref: S.NameRef) -> str:
    match ref:
        case ("decl", decl):
            return f"decl {decl_text(decl)}"
        case ("variant" | "trait_method" as kind, decl, name):
            return f"{kind} {decl_text(decl)} {name}"
        case ("assoc", param, name):
            return f"assoc {param} {name}"
        case (kind, name):
            return f"{kind} {name}"
    raise TypeError(f"cannot print {ref!r}")


def ref_lines(node: object, out: list[str]) -> None:
    """The references in `node`, in the order of its dataclass fields, as compiler/resolve/dump.kur
    writes them. A declaration's `forall`, when it is not a field, comes last."""
    if isinstance(node, (list, tuple)):
        for child in node:
            ref_lines(child, out)
        return
    if not dataclasses.is_dataclass(node):
        return
    if isinstance(node, S.ResolvedName) and node.ref is not None:
        match node:
            case S.TName():
                label = "TName " + "::".join(node.path)
            case S.Path():
                label = "Path " + "::".join(node.names)
            case _:
                label = type(node).__name__
        out.append(f"  {label} -> {ref_text(node.ref)}")
    names = [f.name for f in dataclasses.fields(node) if f.init]
    for name in names:
        ref_lines(getattr(node, name), out)
    if isinstance(node, S.Declaration) and "forall" not in names:
        ref_lines(node.forall, out)


def resolve_dump(resolver: Resolver) -> str:
    """The canonical text of a resolved program, as compiler/resolve/dump.kur writes it."""
    out = []
    for package in resolver.packages:
        out.append(f"package {package.id} {package.name}")
        out.append(f"  source_root {package.source_root}")
        out.append("  entry " + ("_" if package.entry is None else "::".join(package.entry)))
        out.append("  entry_decl " + ("_" if package.entry_decl is None else decl_text(package.entry_decl)))
        out.extend(f"  dep {alias} {target}" for alias, target in package.deps.items())
        out.extend(f"  file {module_text(module)} {file}" for module, file in package.files.items())
    for scope in resolver.scopes.values():
        out.append(f"scope {module_text(scope.module)}")
        out.extend(f"  decl {name} {decl_text(decl)}" for name, decl in scope.decls.items())
        out.extend(f"  import {alias} {'::'.join(path)}" for alias, path in scope.imports.items())
        out.extend(f"  glob {'::'.join(path)}" for path in scope.globs)
        out.extend(f"  export {name}" for name in sorted(scope.exports))
    for scope in resolver.scopes.values():
        out.append(f"refs {module_text(scope.module)}")
        ref_lines(scope.items, out)
    return "".join(line + "\n" for line in out)


def reference_resolution(path: Path) -> str:
    """The reference resolver's dump of the package at `path`, or its error."""
    try:
        return resolve_dump(resolve(path))
    except KursError as error:
        return f"error: {error}"


def sig_lines(sig: Sig, indent: str, out: list[str]) -> None:
    out.append(f"{indent}sig {write_atom(sig.name)} {'unsafe' if sig.unsafe else 'safe'}")
    out.append(f"{indent}  decl {'_' if sig.decl is None else decl_text(sig.decl)}")
    out.append(f"{indent}  module {'_' if sig.module is None else module_text(sig.module)}")
    out.append(f"{indent}  {write_forall(sig.params, sig.premises)}")
    params = [_form([*(["&"] if i in sig.places else []), write_atom(n), write_type(t)])
              for i, (n, t) in enumerate(zip(sig.pnames, sig.ptypes))]
    out.append(f"{indent}  params {_form(params)}")
    out.append(f"{indent}  ret {write_type(sig.ret)}")


def const_text(value: ConstValue) -> str:
    kind, v, ty = value
    if kind == "int":
        spelled = str(v)
    elif kind == "bool":
        spelled = "true" if v else "false"
    elif kind == "float":
        assert isinstance(v, str)
        spelled = v
    else:
        spelled = dump(v)
    return f"{kind} {spelled} {'_' if ty is None else write_type(ty)}"


def signatures_dump(sigs: Signatures) -> str:
    """The canonical text of a program's signatures, as compiler/sigs/dump.kur writes it."""
    out = [f"type {write_type_decl(decl)}" for decl in sigs.types.values()]
    for trait in sigs.traits.values():
        params, kinds = _form([write_atom(p) for p in trait.params]), _form(list(trait.param_kinds))
        out.append(f"trait {write_atom(trait.name)} {params} {kinds}")
        out.append(f"  module {'_' if trait.module is None else module_text(trait.module)}")
        out.extend(f"  method {write_atom(m.name)} {'unsafe ' if m.unsafe else ''}{_write_ptypes(m.ptypes, m.places)} "
                   f"{write_type(m.ret)}" for m in trait.methods)
    for sig in sigs.sigs.values():
        sig_lines(sig, "", out)
    for inst, _, module in sigs.instances:
        pos = f"{inst.pos[0]}:{inst.pos[1]}" if inst.pos else "_"
        out.append(f"instance {write_atom(inst.trait)} {module_text(module)} {pos}")
        out.append(f"  {write_forall(inst.params, inst.premises)} {_form([write_type(t) for t in inst.head])}")
        for method in inst.methods:
            sig_lines(method, "  ", out)
    out.extend(f"const {decl_text(decl)} {const_text(value)}" for decl, value in sigs.consts.items())
    return "".join(line + "\n" for line in out)


def reference_signatures(path: Path) -> str:
    """The reference compiler's signatures of the package at `path`, or its error."""
    sys.setrecursionlimit(20000)
    try:
        return signatures_dump(signatures(resolve(path)))
    except KursError as error:
        return f"error: {error}"


def inferred_type(ty: C.Type) -> str:
    """`ty` as KURS writes it, an unsolved metavariable written `?` (`$?` for a brand)."""
    def filled(t: C.Type) -> C.Type:
        t = prune(t)
        if isinstance(t, Meta):
            return C.Skolem("?") if t.kind == "brand" else C.Named("?")
        return T.map_children(t, filled)
    return write_type(filled(ty))


def inferred_types(types: Sequence[C.Type]) -> str:
    return _form([inferred_type(t) for t in types])


def receiver_text(receiver: Optional[I.Receiver]) -> str:
    return "" if receiver is None else " &recv" if receiver.lend else " recv"


def trait_call_text(call: I.TraitCall) -> str:
    return (f"(trait {write_atom(call.trait)} {write_atom(call.method)} {inferred_types(call.targs)}"
            f"{receiver_text(call.receiver)})")


def callee_text(call: I.Callee) -> str:
    match call:
        case I.FnCall():
            return (f"(fn {write_atom(call.name)} {write_atom(call.sig.name)} {inferred_types(call.targs)}"
                    f"{receiver_text(call.receiver)})")
        case I.TraitCall():
            return trait_call_text(call)
        case I.ValueCall():
            return f"(value {inferred_types(call.targs)})"
        case I.ClosureCall():
            return "(closure)"
        case I.VariantCall():
            return f"(variant {write_atom(call.variant)})"
        case I.TupleCall():
            return "(tuple)"
        case I.BuiltinCall():
            return f"(builtin {write_atom(call.name)})"
        case I.AllocaCall():
            return f"(alloca {inferred_type(call.target)})"
        case I.PointerCall():
            return f"(pointer {write_atom(call.name)})"
        case I.FStringWrite():
            return "(fwrite)"
    raise TypeError(f"cannot print {call!r}")


def position(node: object) -> str:
    pos = getattr(node, "pos")
    return f"{pos[0]}:{pos[1]}" if pos else "_"


def inferred_line(tag: str, node: object, positioned: bool = True) -> str:
    """One node's line, as compiler/infer/dump.kur writes it: tag, position, type and annotations."""
    parts = [tag, *([position(node)] if positioned else []), inferred_type(node.ty) if hasattr(node, "ty") else "-"]
    binder = getattr(node, "name_id" if isinstance(node, S.Foreach) else "binder", None)
    if isinstance(binder, int):
        parts.append(f"binder {binder}")
    if getattr(node, "folded", None) is not None:
        parts.append(f"folded ({const_text(node.folded)})")
    if getattr(node, "call", None) is not None:
        parts.append(f"call {callee_text(node.call)}")
    if hasattr(node, "target"):
        parts.append(f"target {inferred_type(node.target)}")
    if hasattr(node, "field_ids"):
        parts.append("fields " + _form([f"({write_atom(n)} {i})" for n, i in node.field_ids.items()]))
    if hasattr(node, "spread_id"):
        parts.append(f"spread {node.spread_id}")
    if hasattr(node, "binder_ids"):
        parts.append("binders " + _form(["_" if i is None else str(i) for i in node.binder_ids]))
    if hasattr(node, "lent"):
        parts.append("lent #t" if node.lent else "lent #f")
    if isinstance(node, S.Closure) and hasattr(node, "param_ids"):
        parts.append("params " + _form([str(i) for i in node.param_ids]))
    if hasattr(node, "captures"):
        parts.append("captures " + _form([f"({write_atom(c.name)} {c.binder_id})" for c in node.captures]))
    if hasattr(node, "witnesses"):
        parts.append(f"witnesses {inferred_types(node.witnesses)}")
    if isinstance(node, S.Try) and hasattr(node, "kind"):
        parts.append(f"kind {node.kind}")
    if hasattr(node, "opened_brands"):
        parts.append("opened " + _form([write_atom(b) for b in node.opened_brands]))
    if hasattr(node, "option"):
        parts.append(f"option {inferred_type(node.option)}")
    if hasattr(node, "next_call"):
        parts.append(f"next {trait_call_text(node.next_call)}")
    if hasattr(node, "rest_id"):
        parts.append(f"rest {node.rest_id}")
    return "  " + " ".join(parts)


# Each syntax node with a body's tag, and its children in the order compiler/infer/dump.kur walks them.
WALKED = {
    S.Block: ("block", lambda n: (*n.stmts, n.tail)), S.Lit: ("lit", lambda n: ()),
    S.FStr: ("fstr", lambda n: n.parts), S.Path: ("path", lambda n: ()), S.Tuple: ("tuple", lambda n: n.elems),
    S.Array: ("array", lambda n: n.elems), S.Sizeof: ("sizeof", lambda n: ()),
    S.StructLit: ("struct", lambda n: tuple(v for _, v in n.fields)), S.If: ("if", lambda n: (n.cond, n.then, n.other)),
    S.Closure: ("closure", lambda n: (n.body,)), S.Return: ("return", lambda n: (n.value,)),
    S.Pack: ("pack", lambda n: (n.value,)), S.Foreach: ("foreach", lambda n: (n.source, n.body)),
    S.Binop: ("binop", lambda n: (n.left, n.right)), S.Unary: ("unary", lambda n: (n.operand,)),
    S.Cast: ("cast", lambda n: (n.value,)), S.UnsafeStore: ("store", lambda n: (n.pointer, n.value)),
    S.Field: ("field", lambda n: (n.value,)), S.Index: ("index", lambda n: (n.value,)),
    S.Call: ("call", lambda n: (n.callee, *n.args)), S.Try: ("try", lambda n: (n.value,)),
    S.While: ("while", lambda n: (n.cond, n.body)), S.For: ("for", lambda n: (n.iterator, n.body)),
    S.Let: ("let", lambda n: (n.pattern, n.value)), S.Var: ("var", lambda n: (n.value,)),
    S.Const: ("const", lambda n: (n.value,)),
}
PATTERNS = {
    S.PBind: ("pbind", lambda n: ()), S.PWild: ("pwild", lambda n: ()), S.PTuple: ("ptuple", lambda n: n.elems),
    S.PArray: ("parray", lambda n: n.elems), S.PStruct: ("pstruct", lambda n: tuple(q for _, q in n.fields)),
}


def inferred_body(node: object, out: list[str]) -> None:
    """The lines of a body's nodes, in syntax order, as compiler/infer/dump.kur walks them."""
    def walk(children: Sequence[object]) -> None:
        for child in children:
            if child is not None and not isinstance(child, str):
                inferred_body(child, out)
    if isinstance(node, S.Match):
        out.append(inferred_line("match", node))
        walk((node.scrutinee,))
        for arm in node.arms:
            out.extend(inferred_line("case", case) for case in arm.cases)
            walk((arm.body,))
    elif isinstance(node, S.Assign):
        out.append(f"  assign {position(node)}")
        walk((node.target, node.value))
    elif isinstance(node, S.ExprStmt):
        walk((node.value,))
    elif type(node) in PATTERNS:
        tag, children = PATTERNS[type(node)]
        out.append(inferred_line(tag, node, positioned=False))
        walk(children(node))
    else:
        tag, children = WALKED[type(node)]
        out.append(inferred_line(tag, node))
        walk(children(node))


def inference_dump(sigs: Signatures) -> str:
    """The canonical text of what inference found, as compiler/infer/dump.kur writes it."""
    out: list[str] = []

    def function(name: str, node: S.Fn) -> None:
        out.append(f"fn {write_atom(name)} {position(node)} params {_form([str(i) for i in node.param_ids])}")
        inferred_body(node.body, out)
    for decl, sig in sigs.sigs.items():
        if isinstance(decl.node, S.Fn) and decl.node.body is not None:
            function(sig.name, decl.node)
    for inst, item, _ in sigs.instances:
        for node in item.methods:
            function(f"{inst.trait}::{node.name}", node)
    return "".join(line + "\n" for line in out)


def reference_inference(path: Path) -> tuple[str, bool]:
    """The reference compiler's inference of the package at `path`, or its error; and whether
    resolution and signatures succeeded, so that an error is inference's own."""
    sys.setrecursionlimit(20000)
    T._fresh = itertools.count()   # the Kursal compiler numbers fresh names and bindings from 0
    I._ids = itertools.count()
    try:
        resolver = resolve(path)
        sigs = signatures(resolver)
    except KursError as error:
        return f"error: {error}", False
    try:
        I.Infer(resolver, sigs).run()
        return inference_dump(sigs), True
    except KursError as error:
        return f"error: {error}", True


def reference_inference_text(path: Path) -> str:
    return reference_inference(path)[0]


def in_deep_thread[R](make: Callable[[], R]) -> R:
    """`make()`, on a thread with room to recurse through a large package, as tools/build.py runs it."""
    sys.setrecursionlimit(1_000_000)
    threading.stack_size(1 << 30)
    result: list[R] = []
    failure: list[BaseException] = []

    def run() -> None:
        try:
            result.append(make())
        except BaseException as error:  # noqa: BLE001 - re-raised on the calling thread
            failure.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join()
    if failure:
        raise failure[0]
    return result[0]


def reference_build(path: Path) -> dict[str, Optional[str]]:
    """What the reference makes of the package at `path`, numbering fresh names and bindings from 0 as one
    run of the Kursal compiler does: its lowered program as KURS (`kursal lower`), or the error of an
    earlier stage or of lowering; and, when that succeeds, the C of the whole pipeline as tools/build.py
    runs it (`kursal build`), entered through the package's entry, or none if checking or C rejects it."""
    def make() -> dict[str, Optional[str]]:
        T._fresh = itertools.count()
        I._ids = itertools.count()
        try:
            resolver = resolve(path)
            sigs = signatures(resolver)
            program = lower(resolver, sigs)
            text = write_program(program)
        except KursError as error:
            return {"lower": f"error: {error}", "c": None}
        entry_decl = resolver.packages[0].entry_decl
        try:
            c: Optional[str] = emit_c(program, check_program(program),
                                      sigs.name(entry_decl) if entry_decl is not None else None)
        except KursError:
            c = None
        return {"lower": text, "c": c}
    return in_deep_thread(make)


def reference_lowering(path: Path) -> str:
    return reference_build(path)["lower"]


def built_paths() -> list[Path]:
    """Every package in the repository, every bad example, and the compiler itself."""
    packages = sorted({p.parent for top in ("examples", "lib") for p in (ROOT / top).glob("*/kur.toml")})
    return [*packages, *sorted((ROOT / "examples/bad").glob("*.kur")), COMPILER]


def reference_builds() -> list[dict[str, Optional[str]]]:
    """`reference_build` for each of `built_paths`, computed once per change to the sources."""
    paths = built_paths()
    sources = [*(ROOT / "lib").rglob("*.kur"), *(ROOT / "lib").rglob("*.toml"), *(ROOT / "examples").rglob("*.kur"),
               *(ROOT / "examples").rglob("*.toml"), *COMPILER.rglob("*.kur"), *COMPILER.rglob("*.toml"),
               *(ROOT / "reference-compiler/kursal").glob("*.py")]

    def make() -> list:
        with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            return list(pool.map(reference_build, paths))

    return cached("built", digest_of(sources), make)


def place_case(directory: Path, index: int, case: str | dict[str, str]) -> Path:
    """Where an inference case is written (see tests/infer_cases.py)."""
    root = directory / f"case{index}"
    if isinstance(case, dict):
        for rel, text in case.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        return root / "a"
    if "std::" in case:
        path = directory / f"case{index}.kur"
        path.write_text(case)
        return path
    root.mkdir()
    (root / "kur.toml").write_text(manifest("p"))
    (root / "lib.kur").write_text(case)
    return root


def manifest(name: str, deps: tuple[str, ...] = (), entry: str | None = None) -> str:
    text = f'[package]\nname="{name}"\nsource_root="."\n'
    if entry:
        text += f'kind="executable"\nentry="{entry}"\n'
    for dep in deps:
        text += f'[dependencies.{dep}]\npath="../{dep}"\n'
    return text


def case_files(case: str | dict[str, str]) -> tuple[dict[str, str], str]:
    """A case of tests/lower_cases.py as files, by relative path, and the path to lower (see `place_case`)."""
    if isinstance(case, dict):
        return case, "a"
    if "std::" in case:
        return {"case.kur": case}, "case.kur"
    return {"kur.toml": manifest("p"), "lib.kur": case}, "."


LOWER_CASES = tuple(case_files(case) for case, _ in LOWER_ERRORS)


def recorded_lowerings(module_name: str) -> list[tuple[dict[str, str], str]]:
    """The packages a test module lowers, outside the repository: its tests run with `lower` watched. Each
    is the files of the directory holding the packages lowered, by relative path, and the path lowered."""
    sys.setrecursionlimit(20000)
    real_lower = lower_module.lower
    found: list[tuple[dict[str, str], str]] = []

    def watched(resolver, *args, **kwargs):
        try:
            roots = [p.root for p in resolver.packages if not p.root.is_relative_to(LIB)]
            first = resolver.packages[0]
            single = not (first.root / "kur.toml").exists()
            target = next(iter(first.files.values())) if single else first.root
            base = Path(os.path.commonpath(roots))
            files = [f for f in base.rglob("*") if f.is_file() and f.suffix in (".kur", ".toml")]
            if not base.is_relative_to(ROOT) and len(files) <= 20:
                found.append(({str(f.relative_to(base)): f.read_text() for f in files}, str(target.relative_to(base))))
        except Exception:  # noqa: BLE001 - a package that cannot be recorded is left out
            pass
        return real_lower(resolver, *args, **kwargs)

    module = importlib.import_module(module_name)
    patched = [m for name, m in list(sys.modules.items())
               if (name.startswith("tests.") or name == module_name) and getattr(m, "lower", None) is real_lower]
    for m in patched:
        m.lower = watched
    try:
        unittest.TestLoader().loadTestsFromModule(module).run(unittest.TestResult())
    finally:
        for m in patched:
            m.lower = real_lower
    return found


def materialize(directory: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (directory / rel).parent.mkdir(parents=True, exist_ok=True)
        (directory / rel).write_text(text)


def reference_case_lowering(case: tuple[dict[str, str], str]) -> str:
    """The reference's lowering of a recorded or written package, its directory spelled `<DIR>`."""
    files, target = case
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory).resolve()
        materialize(base, files)
        return reference_lowering(base / target).replace(str(base), "<DIR>")


def lowering_cases() -> list[dict]:
    """Every distinct package the reference's own tests lower outside the repository, and the cases of
    tests/lower_cases.py, each with the reference's lowering: {files, target, lower}."""
    modules = sorted(path.stem for path in TESTS.glob("test_*.py")
                     if path.stem not in ("test_compiler", "test_examples", "test_library", "test_kurs", "test_parallel_runner"))

    def make() -> list:
        with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            found = pool.map(recorded_lowerings, [f"tests.{module}" for module in modules])
            cases: dict[str, tuple[dict[str, str], str]] = {}
            for files, target in [case for group in found for case in group] + list(LOWER_CASES):
                cases.setdefault(json.dumps([files, target], sort_keys=True), (files, target))
            unique = list(cases.values())
            return [{"files": files, "target": target, "lower": text}
                    for (files, target), text in zip(unique, pool.map(reference_case_lowering, unique, chunksize=4))]

    sources = [*TESTS.glob("*.py"), *(ROOT / "reference-compiler/kursal").glob("*.py"), *(ROOT / "lib").rglob("*.kur")]
    return cached("lowerings", digest_of(sources), make)


SHAPES = {
    "lib/kur.toml": manifest("lib"),
    "lib/lib.kur": "use package::shapes;\nexport shapes;\n",
    "lib/shapes.kur": "exposed struct Point { x: u32 }\nfn make(a: u32) -> Point { Point.{ x: a } }\n"
                      "enum Shape { Dot(Point), Empty }\nimpl Point { fn get(self) -> u32 { 0u32 } }\n"
                      "trait Show { fn show(x: u32) -> u32; }\n",
    "lib/hidden.kur": "fn secret() -> u32 { 0u32 }\n",
    "app/kur.toml": manifest("app", ("lib",)),
}


def app(source: str, files: dict[str, str] | None = None) -> dict[str, str]:
    """A package `app`, depending on a package `lib`, whose lib.kur is `source`; `files` adds or replaces files."""
    return {**SHAPES, "app/lib.kur": source, **(files or {})}


# Packages that resolution rejects: their files, by path, and the path to resolve.
RESOLUTION_ERRORS: tuple[tuple[dict[str, str], str], ...] = (
    (app("use nothing::*;\nfn f() -> u32 { g() }\n"), "app"),
    (app("fn f() -> u32 { nothing() }\n"), "app"),
    (app("fn f(x: Nothing) -> u32 { 0u32 }\n"), "app"),
    (app("fn f() -> u32 { 0u32 }\nfn f() -> u32 { 1u32 }\n"), "app"),
    (app("use lib::shapes::Point;\nuse lib::shapes::Point;\n"), "app"),
    (app("use lib::shapes::make;\nfn make() -> u32 { 0u32 }\n"), "app"),
    (app("use lib::shapes::Point as Copy;\n"), "app"),
    (app("use lib::shapes as package;\n"), "app"),
    (app("use package::a::X;\nfn f() -> u32 { X.() }\n", {"app/a.kur": "use package::X;\n"}), "app"),
    (app("fn f() -> u32 { lib::hidden::secret() }\n"), "app"),
    (app("fn f() -> u32 { lib::shapes }\n"), "app"),
    (app("fn f() -> u32 { lib::shapes::Point::nope() }\n"), "app"),
    (app("use lib::shapes::Shape;\nfn f() -> u32 { Shape::Dot::x() }\n"), "app"),
    (app("use lib::shapes::Shape::Dot;\nfn f() -> u32 { Dot::x() }\n"), "app"),
    (app("use package::f::*;\nfn f() -> u32 { g() }\n"), "app"),
    (app("use lib::shapes::make;\nfn f(x: make) -> u32 { 0u32 }\n"), "app"),
    (app("use lib::shapes::make;\nfn f() -> u32 { make.{ x: 1u32 } }\n"), "app"),
    (app("use lib::shapes::Point;\nfn f() -> Point { Point.{ x } }\n"), "app"),
    (app("use lib::shapes::Point;\nfn f(p: Point) -> u32 { match p { Point(x) => 0u32 } }\n"), "app"),
    (app("fn f(x: builtin) -> () { () }\n"), "app"),
    (app("fn f() -> u32 { u32::nope() }\n"), "app"),
    (app("forall T where lib::shapes::make[T]. fn f(x: T) -> u32 { 0u32 }\n"), "app"),
    (app("impl (u32, u32) { fn f(self) -> u32 { 0u32 } }\n"), "app"),
    (app("use lib::shapes;\nimpl shapes { fn f(self) -> u32 { 0u32 } }\n"), "app"),
    (app("struct P { x: u32 }\nimpl P { fn f(self) -> u32 { 0u32 } }\nimpl P { fn f(self) -> u32 { 1u32 } }\n"), "app"),
    (app("module x;\n"), "app"),
    (app("", {"app/kur.toml": manifest("app", ("lib",), "package::Nope")}), "app"),
    (app("struct Main {}\n", {"app/kur.toml": manifest("app", ("lib",), "Main")}), "app"),
    (app("", {"app/kur.toml": manifest("app", ("lib",), "lib::shapes::make")}), "app"),
    ({"p/kur.toml": manifest("p"), "p/a.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname="p"\nsource_root="missing"\n'}, "p"),
    ({"p/kur.toml": manifest("p"), "p/m.kur": "", "p/m/lib.kur": ""}, "p"),
    ({"a/kur.toml": manifest("a", ("b",)), "a/lib.kur": "", "b/kur.toml": manifest("b", ("a",)), "b/lib.kur": ""}, "a"),
    ({"p/kur.toml": '[package]\nname=12\nsource_root="."\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname="p"\nsource_root=false\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname="p"\nsyntax=12\nversion="1"\n[extra]\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": 'dependencies=[]\n[package]\nname="p"\nsource_root="."\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": manifest("p") + '[dependencies.x]\npath=3\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": manifest("p") + '[dependencies]\nx="../x"\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": 'package="p"\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '# no name\n[package]\nsource_root="."\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname="p"\nsource_root="."\nkind="executable"\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname = "p" # the name\n"source_root" = "."\n[package.x]\n', "p/lib.kur": ""}, "p"),
    ({"one.kur": "fn main() -> i32 { nothing() }\n"}, "one.kur"),
    ({"one.kur": "fn f() -> Nothing { 0i32 }\n"}, "one.kur"),
    (app("use package::a;\n", {"app/a.kur": "fn f() -> u32 {\n    g()\n}\n"}), "app"),
    (app("use package::a;\n", {"app/a.kur": "fn f( {\n"}), "app"),
    (app("use package::b::c::X;\nfn f() -> u32 { X() }\n", {"app/b/lib.kur": "", "app/b/c.kur": "use package::b::c::X;\n"}), "app/kur.toml"),
    ({"p/kur.toml": manifest("p") + '[dependencies]\nx = { path = 3 }\n', "p/lib.kur": ""}, "p"),
)


# Packages that resolution accepts, spelled in other ways than the repository's.
RESOLVED: tuple[tuple[dict[str, str], str], ...] = (
    (app("use lib::shapes::*;\nfn f(a: u32) -> Point { make(a) }\n",
         {"app/kur.toml": "# app\n[package]\n'name' = 'app'   # literal\nsource_root = \"\"\"\n.\"\"\"\n"
                          "[dependencies]\nlib = { path = \"../lib/kur.toml\" }\n"}), "app/../app"),
    (app("use lib::shapes::{Point, Shape, Show, make as mk};\n"
         "forall T where Copy[T], Show[T]. fn f(a: u32, x: T) -> Point {\n"
         "    let p = mk(a); let n = p.x; let q = Point::get(p); let s = Show::show(n);\n"
         "    match Shape::Empty { Shape::Dot(d) => d, _ => Point.{ x: T::size } }\n}\n",
         {"app/kur.toml": manifest("app", ("lib",)).replace('"', "'") + "kind = \"executable\"\nentry = \"package::run::main\"\n",
          "app/run.kur": "fn main() -> i32 { 0i32 }\n"}), "app"),
)


# Packages of one module, without dependencies, for signatures: sources the reference accepts, and
# sources it rejects at the signature stage.
SIGNATURES = (
    "use std::slice::Slice;\nconst T: Slice[u8, 'static] = \"te\\\"xt\\n\";\n",
    "const A: u8 = 200u8 + 100u8;\nconst B: i8 = -128;\nconst C: i8 = -B;\nconst D: i32 = -7 / 2;\n"
    "const E: i32 = -7 % 2;\nconst F: i64 = -9223372036854775808 / -1;\nconst G: u64 = 18446744073709551615 * 3;\n"
    "const H: i16 = -1 >> 3;\nconst I: u32 = 1 << 35;\nconst J: i32 = 1i32 << -1i32;\nconst K: bool = -1i32 < 1i32;\n"
    "const L: bool = 18446744073709551615u64 > 1u64;\nconst M: u8 = (-1i64) as u8;\nconst N: i64 = 255u8 as i8 as i64;\n"
    "const O: f64 = -1.5;\nconst P: f32 = -(-2.5);\nconst Q: bool = !(A == 44u8) || true;\nconst R: u32 = !0u32;\n"
    "const S: bool = false && (1u32 / 0u32 == 0u32);\nconst U: usize = 3;\n"
    "const V: i8 = (-128i8) / (-1i8);\nconst W: u16 = 65535 + 1;\nconst X: i64 = -9223372036854775808 % -1;\n"
    "const Y: u64 = 1u64 >> 64u64;\nconst Z: i8 = -128 >> 1;\nconst AA: bool = 1 == 2u8;\nconst AB: u8 = b'a' ^ 255u8;\n"
    "const AC: i64 = -5 / 3 + -5 % 3 + 7 % -2 + -7 / -2;\nconst AD: bool = -1i8 >= -2i8 && 3u16 <= 3u16 && 1i32 != 2i32;\n"
    "const AE: u32 = 4294967295u32 as i64 as u32;\nconst AF: bool = true != (1u8 > 0u8);\n"
    "struct Arr { a: [u8; U], b: [u8; 2 * U], c: [u8; 2u8] }\n",
    "forall T where Copy[T]. trait Dup { fn dup(x: T) -> (T, T); }\ntrait Show { fn show(&x: Self) -> u32; }\n"
    "instance Dup[u32] { fn dup(x: u32) -> (u32, u32) { (x, x) } }\n"
    "forall T where Exposed[T], Show[T]. instance Show[(T, T)] { fn show(&x: (T, T)) -> u32 { 0u32 } }\n"
    "forall 'k. exposed struct Tag! { n: u32 }\nforall T, 'a. struct P { p: *T, w: Brand['a], e: exists 'b. Brand['b] }\n"
    "forall T. impl P[T, 'static] { fn get(self, f: closure(u32) -> u32, g: closure[u8](&_: u32) -> u32) -> u32 { 0u32 } }\n"
    "forall T where Copy[T]. fn pair(x: T, h: unsafe fn(&_: T) -> !, k: forall 'q. fn(Brand['q]) -> u32) -> (T, T) { (x, x) }\n"
    "extern \"C\" fn puts(s: *u8) -> i32;\nenum E { A, B(u32), C(u8, u16) }\ntuple Pt(u32, i64);\n"
    "struct F { f: foreach F of (u32, u8) => *F }\nfn h(g: fn(closure(u32) -> u32) -> u32, k: closure(u8) -> u8) -> u32 { 0u32 }\n",
)

SIGNATURE_ERRORS = (
    "struct S { a: [u8; -1i32] }\n",
    "use std::slice::Slice;\nconst T: u32 = \"text\";\n",
    "const N: i64 = 0i64 - 2i64;\nstruct S { a: [u8; N] }\n",
    "struct S { a: Phantom }\n",
    "struct S { a: builtin::Foo }\n",
    "forall T. struct S { a: T[u32] }\n",
    "struct S { a: u32[u8] }\n",
    "struct S { a: Brand[u32] }\n",
    "struct S { a: Never[u32] }\n",
    "forall T. struct B { a: T }\nstruct S { b: B }\n",
    "forall 'a. struct B { x: Brand['a] }\nstruct S { b: B[u32] }\n",
    "struct S { a: Brand['k] }\n",
    "struct S { f: fn(closure(u32) -> u32) -> u32 }\n",
    "forall T. struct B { a: *T }\nstruct S { b: B[_] }\n",
    "struct S { f: forall 'a. u32 }\n",
    "forall T where Copy[(T, T)]. fn f(x: T) {}\n",
    "forall T where Copy[u32]. fn f(x: T) {}\n",
    "forall T where Copy[T]. struct B { a: T }\nstruct L! { x: u32 }\nstruct S { b: B[L] }\n",
    "forall T where Copy[T]. struct B { a: T }\nstruct L! { x: u32 }\nfn f(b: B[L]) {}\n",
    "trait Show { fn show(x: Self) -> u32; }\nforall T where Show[T]. trait Bad { fn bad(x: T) -> u32; }\n",
    "trait Show { fn show(x: Self, f: closure(u32) -> u32) -> u32; }\n",
    "fn f(self) -> u32 { 0u32 }\n",
    "fn g() {}\nexposed(package::g) struct S { a: u32 }\n",
    "forall T. struct R { r: *R[(T, T)] }\n",
    "struct S { s: S }\n",
    "const C: bool = 1;\n",
    "const C: u8 = 300;\n",
    "const C: i8 = -129;\n",
    "const C: f32 = 340282366920938463463374607431768211456.0;\n",
    "const C: u32 = 1.5;\n",
    "const B: u32 = 1u32;\nconst C: u32 = B[u32];\n",
    "fn g() -> u32 { 0u32 }\nconst C: u32 = g;\n",
    "const C: bool = -true;\n",
    "const C: u32 = true as u32;\n",
    "const C: bool = 1u32 && true;\n",
    "const C: bool = true || 1u32;\n",
    "const C: bool = true == 1u32;\n",
    "const C: u32 = 1u32 + 1u8;\n",
    "const C: u32 = 1u32 / 0u32;\n",
    "const C: u32 = 1u32 % (2u32 - 2u32);\n",
    "const C: u32 = C + 1u32;\n",
    "const A: u32 = B;\nconst B: u32 = A;\n",
    "const C: u64 = 1u32;\n",
    "const C: (u32, *u8) = 1u32;\n",
    "const C: u32 = 1 == 2;\n",
    "struct S { a: [u8; true] }\n",
    "const C: u32 = 1u32 + 1.5;\n",
    "fn g() -> u32 { 0u32 }\nconst C: u32 = g();\n",
    "trait Show { fn show(x: Self) -> u32; }\ninstance Show[u32, u8] { fn show(x: u32) -> u32 { x } }\n",
    "forall T where Copy[T]. trait Dup { fn dup(x: T) -> T; }\nstruct L! { x: u32 }\n"
    "instance Dup[L] { fn dup(x: L) -> L { x } }\n",
    "forall T where Nope[T]. fn f(x: T) {}\n",
    "struct S { a: (u32, [u8; 1u8 - 2u8 > 0u8]) }\n",
)

SYNTAX_ERRORS = (
    "fn f() -> Point { Point { x: 1u32 } }",
    "fn f(x: &u32) {}",
    "fn f() { let (x) = g(); }",
    "fn f() -> u32 { match x { _ => 1u32, A => 2u32 } }",
    "fn f() -> u32 { 1u32 < 2u32 < 3u32 }",
    "fn f() { let s = f\"a } b\"; }",
    "fn f() { let s = f\"a {b\"; }",
    "fn f() { let s = \"unterminated; }",
    "fn f() { let c = b'ab'; }",
    "fn f() { x # y }",
    "struct S { a: u32 b: u32 }",
    "fn f() { let s = \"\\q\"; }",
    "fn f() -> u32 { p.0.1 }",
    "fn f() -> u64 { 18446744073709551616 }",
)

KURS_ERRORS = (
    "garbage",
    "(fun main i32 () garbage)",
    "(type T (struct u32))",
    "(type T (forall (A) ((Copy (tuple u32))) A))",
    "(type T (forall (A) ((Copy B)) A))",
    "(widget x)",
    "((widget) x)",
    "(fun f u32 ()\n  (entry ()\n    (leap)))",
    "(fun f u32 () (entry () ((a 'b' \"c\\n\") c)))",
    "(fun f u32 () (entry () (= %x) (ret %x)))",
    "(type T (exists (a) u32))",
    "(type T (exists (($a it's)) u32))",
    "(type T $)",
    "(type T ())",
    "(type T ((tuple) u32))",
    "(type T (foreach (F u32) u32 (keep F)))",
    "(type T (foreach (F u32)))",
    "(type T (fn (forall () ((Eq u32)) u32 ())))",
    "(fun f u32 () (entry () (opened &p) (ret %x)))",
    "(type T u32",
    ")",
    '(data d u8 "\\q")',
    '(data d u8 "abc',
)


def fresh_result(make: Callable[[], str]) -> str:
    """`make()`, or `error: ` and the KursError it raises, with fresh names numbered from 0 as one run of
    the Kursal compiler numbers them."""
    T._fresh = itertools.count()
    try:
        return make()
    except KursError as error:
        return f"error: {error}"


def serialized(package: Path) -> Optional[dict[str, Optional[str]]]:
    """A package's lowered program as KURS, the function its executable enters, and what this compiler
    makes of that KURS: its C and its readable form. None if the package does not build."""
    sys.setrecursionlimit(20000)
    try:
        resolver = resolve(package)
        sigs = signatures(resolver)
        text = write_program(lower(resolver, sigs))
    except KursError:
        return None
    entry_decl = resolver.packages[0].entry_decl
    entry = sigs.name(entry_decl) if entry_decl is not None else None

    def c() -> str:
        program = parse_program(text)
        return emit_c(program, check_program(program), entry)

    return {"name": package.name, "kurs": text, "entry": entry, "c": fresh_result(c),
            "readable": fresh_result(lambda: emit_kurs(parse_program(text)))}


def lowered_programs() -> list[dict[str, Optional[str]]]:
    """`serialized` for every package that builds, computed once per change to the sources."""
    sources = [*(ROOT / "lib").rglob("*.kur"), *(ROOT / "lib").rglob("*.toml"), *(ROOT / "examples").rglob("*.kur"),
               *(ROOT / "examples").rglob("*.toml"), *(ROOT / "reference-compiler/kursal").glob("*.py")]

    def make() -> list:
        with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            return [p for p in pool.map(serialized, packages()) if p is not None]

    return cached("lowered", digest_of(sources), make)


def recorded_programs(module_name: str) -> list[str]:
    """The core programs a test module checks, as KURS: its tests run with `check_program` and
    `parse_program` watched. A program checked as it was read is its own text, so positions stay as the
    test wrote them; any other is written out."""
    sys.setrecursionlimit(20000)
    module = importlib.import_module(module_name)
    texts: dict[int, str] = {}
    found: list[str] = []
    real_parse, real_check = module.__dict__.get("parse_program"), module.__dict__.get("check_program")

    def parse(text, *args, **kwargs):
        program = real_parse(text, *args, **kwargs)
        texts[id(program)] = text
        return program

    def check(program, *args, **kwargs):
        try:
            written = write_program(program)
            original = texts.get(id(program))
            found.append(original if original is not None and write_program(parse_program(original)) == written
                         else written)
        except Exception:  # noqa: BLE001 - a program the writer cannot write is not recorded
            pass
        return real_check(program, *args, **kwargs)

    if real_parse is not None:
        module.parse_program = parse
    if real_check is not None:
        module.check_program = check
    unittest.TestLoader().loadTestsFromModule(module).run(unittest.TestResult())
    return found


def checked_case(text: str) -> Optional[dict[str, Optional[str]]]:
    """This compiler's verdict on a core program, with its message for one it rejects; for one it
    accepts, its C (of every root function, as without an entry) and its readable form. None if the
    program is not read, or checking it crashes rather than ending in a verdict."""
    try:
        program = parse_program(text)
    except KursError:
        return None
    T._fresh = itertools.count()
    try:
        checker = check_program(program)
    except KursError as error:
        # An array length is unsigned in the Kursal compiler's core, so it rejects a negative one as it
        # reads the program, with the reader's message.
        if str(error) == "type formation: negative array length":
            return None
        return {"kurs": text, "check": str(error), "c": None, "readable": None}
    except Exception:  # noqa: BLE001
        return None
    try:
        c: Optional[str] = fresh_result(lambda: emit_c(program, checker))
    except Exception:  # noqa: BLE001
        c = None
    try:
        readable: Optional[str] = fresh_result(lambda: emit_kurs(parse_program(text)))
    except Exception:  # noqa: BLE001
        readable = None
    return {"kurs": text, "check": None, "c": c, "readable": readable}


def core_cases() -> list[dict[str, Optional[str]]]:
    """Every distinct core program the other test modules check, with `checked_case`'s verdict."""
    modules = sorted(path.stem for path in TESTS.glob("test_*.py") if "check_program" in path.read_text()
                     and path.stem not in ("test_compiler", "test_examples", "test_library", "test_kurs"))

    def make() -> list:
        with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            found = pool.map(recorded_programs, [f"tests.{module}" for module in modules])
            texts = list(dict.fromkeys(text for group in found for text in group))
            return [case for case in pool.map(checked_case, texts, chunksize=16) if case is not None]

    sources = [*TESTS.glob("*.py"), *(ROOT / "reference-compiler/kursal").glob("*.py")]
    return cached("core", digest_of(sources), make)


# Cases for `kursal selftest-types`; compiler/types/selftest.kur describes their forms.
TYPE_CASES = (
    # show
    "(show (tuple))",
    "(show (tuple (struct) (enum (A u32)) (exists ($a $b) (brand $a)) (array u8 3)))",
    "(show (foreach (F (foreach (G (tuple u8 u16)) (ptr G))) (tuple F G)))",
    "(show (foreach (F (foreach (G (tuple u8)) G)) F (retain)))",
    "(show (foreach ((ptr F) (ptr (tuple u8))) F (retain X Y)))",
    "(show (fn (unsafe) (forall ($a) () ! ((Pair u32 $a) _))))",
    # subst: capture avoidance, numbering, foreach retained binders
    "(subst (exists ($a) (tuple $a $b)) ((b $a)))",
    "(subst (fn (forall ($a) () $b ($a))) ((b $a)))",
    "(subst (tuple (exists ($a) (tuple $a $b)) (exists ($a#7) (tuple $a#7 $b))) ((b (tuple $a $a#7))))",
    "(subst (exists ($a) $a) ((a $z)))",
    "(subst (T T) ((T u8)))",
    "(subst (foreach (F (foreach (G (tuple T T)) (tuple G U))) (tuple F G U)) ((T u8) (U G)))",
    "(subst (foreach (F (foreach (G (tuple T T)) (tuple G U))) (tuple F G U)) ((T u8) (U F)))",
    "(subst (foreach (F (tuple T)) (tuple F T)) ((T (tuple F)) (F u8)))",
    # free names
    "(free (foreach (F (tuple T $k)) (tuple F G)))",
    "(free (fn (forall ($a) () $a ($b T))))",
    "(brands (tuple $a (exists ($b) (tuple $b $c)) (P $d)))",
    "(extractable (tuple T (exists ($a) U) (fn (forall ($b) () V ())) (fn (W) X) (foreach (F Y) Z) $k))",
    # equality modulo bound names
    "(equal (exists ($a) (brand $a)) (exists ($b) (brand $b)))",
    "(equal (exists ($a $b) (tuple $a $b)) (exists ($b $a) (tuple $a $b)))",
    "(equal (exists ($a) (tuple $a $b)) (exists ($b) (tuple $b $b)))",
    "(equal (foreach (F (tuple u8)) F) (foreach (G (tuple u8)) G))",
    "(equal (foreach (F (tuple u8)) (tuple F G)) (foreach (G (tuple u8)) (tuple G G)))",
    "(equal (fn (forall ($p) () u8 ((brand $p)))) (fn (forall ($q) () u8 ((brand $q)))))",
    "(equal (fn (u8) u8) (fn (unsafe) (u8) u8))",
    "(equal (P T) (P T U))",
    "(fits ! u32)",
    "(fits u32 u8)",
    # unification
    "(unify ((a type) (i int)) (tuple ?a ?i) (tuple u32 u8))",
    "(unify ((i int)) ?i bool)",
    "(unify ((a type) (i int)) (tuple ?a ?a) (tuple ?i u8))",
    "(unify ((i int) (f float)) ?i ?f)",
    "(unify ((f float) (a type)) (tuple ?f ?a) (tuple ?a f32))",
    "(unify ((k brand)) (brand $?k) (brand $x))",
    "(unify ((k brand)) (tuple $?k) (tuple u32))",
    "(unify ((a type)) ?a $x)",
    "(unify ((a type)) ?a (ptr ?a))",
    "(unify ((a type)) (array ?a 2) (array u8 3))",
    "(unify ((a type) (b type)) (struct (x ?a) (y ?b)) (struct (x ?b) (y u8)))",
    "(unify ((a type)) (exists ($x) (tuple $x ?a)) (exists ($y) (tuple $y u8)))",
    "(unify ((k brand)) (exists ($x) (tuple $x $?k)) (exists ($y) (tuple $y $y)))",
    "(unify-closed ((k brand)) (exists ($x) (tuple $x $?k)) (exists ($y) (tuple $y $y)))",
    "(unify-closed ((a type)) (fn (forall ($p) () ?a ((brand $p)))) "
    "(fn (forall ($q) () u8 ((brand $q)))))",
    "(unify-closed ((k brand)) (tuple (exists ($x) (tuple $x $rigid#0)) (exists ($x) (tuple $x $?k))) "
    "(tuple (exists ($y) (tuple $y $rigid#0)) (exists ($y) (tuple $y $y))))",
    "(unify ((a type)) (foreach (F (tuple ?a)) (ptr F)) (foreach (G (tuple u8)) (ptr G)))",
    "(unify () (fn (u8) u8) (fn (unsafe) (u8) u8))",
    "(match-head (T $k) ((Pair T (brand $k))) ((Pair u8 (brand $z))) true)",
    "(match-head (T) ((Box (exists ($a) (P $a T)))) ((Box (exists ($a) (P $a u8)))) true)",
    "(match-head (T U) ((Box T)) ((Box u8)) false)",
    "(match-head (T) ((Box T)) ((Bag u8)) false)",
    # literals
    '(literal f32 "3.4028235e38")',
    '(literal f32 "3.40282356e38")',
    '(literal f32 "3.4028236e38")',
    '(literal f32 "1e39")',
    '(literal f64 "1e39")',
    '(literal f64 "1.7976931348623157e308")',
    '(literal f64 "1.7976931348623159e308")',
    '(literal f32 "1e-50")',
    '(literal f64 ".5")',
    '(literal f64 "5.")',
    '(literal f64 "+1.5e+3")',
    '(literal f64 "1e")',
    '(literal f64 "e5")',
    '(literal f64 ".")',
    '(literal f64 "inf")',
    '(literal f64 "1_0")',
    '(literal i8 "-128")',
    '(literal i8 "-129")',
    '(literal i8 "127")',
    '(literal i8 "128")',
    '(literal u8 "255")',
    '(literal u8 "256")',
    '(literal u8 "-1")',
    '(literal u32 "-0")',
    '(literal u32 "007")',
    '(literal i64 "-9223372036854775808")',
    '(literal i64 "9223372036854775808")',
    '(literal u64 "18446744073709551615")',
    '(literal u64 "18446744073709551616")',
    '(literal usize "99999999999999999999999999")',
    '(literal bool "true")',
    '(literal bool "True")',
    # declaration-aware queries
    "(copy ((type Node (T) (struct (v T) (next (ptr (Node T)))))) ((T copy)) (Node T))",
    "(copy ((type Node (T) (struct (v T) (next (ptr (Node T)))))) ((T any)) (Node T))",
    "(copy ((type L (T) (enum (Nil (tuple)) (Cons (tuple T (L T)))))) () (L u8))",
    "(copy ((linear R () u8)) () (tuple R))",
    "(copy () () (foreach (F (tuple u8)) (ptr F)))",
    "(copy () () (foreach (F (tuple u8)) u16))",
    "(zero ((type E () (struct (a (tuple)) (b (array u8 0))))) E)",
    "(zero () T)",
    "(zero () (exists ($a) (brand $a)))",
    "(expand ((type P (T) (struct (a T) (b u8)))) (foreach (F (P u16)) (ptr F)))",
    "(expand () (foreach ((ptr F) (ptr (tuple u8 u16))) (array F 2)))",
    "(expand () (foreach (G (foreach (F (struct (x u8) (y u16))) (tuple F F))) (tuple G F)))",
    "(expand () (foreach (F (array u32 0)) (ptr F)))",
    "(expand () (foreach ((ptr F) (ptr (enum (A u8)))) F))",
    "(expand () (foreach ((ptr F) (tuple u8)) F))",
    "(expand () (Q u8))",
    "(expand ((type P (T) T)) (P))",
    "(expand () (struct (a u8)))",
    "(fields ((type P (T) (struct (a T) (b u8)))) (P u16))",
    "(fields () (array u8 3))",
    "(fields () (enum (A u8)))",
    "(variants ((type O (T) (enum (Some T) (None (tuple))))) (O u8))",
    "(variants () (tuple u8))",
    "(exposure ((opaque H () (struct (a u8)))) () H)",
    "(exposure ((type P () (ptr (struct (a u8))))) () P)",
    "(exposure () () (ptr (enum (A u8))))",
    "(exposure () () (ptr (struct (a u8))))",
    "(exposure () ((T any)) T)",
    "(exposure () () (foreach (F (tuple u8 u16)) (ptr F)))",
    "(pattern () () (foreach ((ptr F) (tuple (ptr u8) (ptr u16))) F))",
    "(pattern () () (foreach ((ptr F) (tuple (ptr u8) u16)) F))",
    "(pattern ((opaque H () (tuple (ptr u8)))) () (foreach ((ptr F) H) F))",
    "(pattern () () (foreach ((ptr F) (foreach (G (tuple u8)) (ptr G))) F))",
    # layout
    "(layout (type L (T) (enum (Nil (tuple)) (Cons (ptr (L T))))))",
    "(layout (type L () (struct (next L))))",
    "(layout (type A () (ptr A)))",
    "(layout (type G (T) (enum (Leaf T) (Node (ptr (G (tuple T T)))))))",
    "(layout (type H (T) (enum (Leaf T) (Node (foreach (F T) (ptr (H F)))))))",
    "(layout (type S () (foreach (F S) F)))",
    "(layout (type A () (struct (b (ptr B)))) (type B () (enum (x A) (y (array B 0)))))",
    "(layout (type A ($k) (struct (b (brand $k)) (n (ptr (A $k))))))",
    # formation
    "(formation (type P (T $k) (struct (a T) (b (brand $k)))))",
    "(formation (type E () (exists ($a) u8)))",
    "(formation (type E () (exists ($a) (exists ($b) (tuple $a $b)))))",
    "(formation (type E () (exists () u8)))",
    "(formation (type B () (brand $z)))",
    "(formation (type R () (foreach (G (foreach (F (tuple u8)) F)) G (retain X))))",
    "(formation (type R () (foreach (G (foreach (F (tuple u8)) F)) (tuple G F))))",
    "(formation (type F () (fn (forall (T) () u8 ()))))",
    "(formation (type D () (struct (a u8) (a u16))))",
    "(formation (type I () (tuple _)))",
    "(formation (type V (T) (T u8)))",
    "(formation (type U () Nope))",
    "(formation (type W (T) u8) (type X () (W)))",
    "(formation (type K ($k) u8) (type X () (K u8)))",
    "(formation (type K (T) u8) (type X () (K $static)))",
    "(formation (type C (+T) u8) (linear L () u8) (type X () (C L)))",
    "(formation (type C (+T) u8) (type X () (C (ptr u8))))",
    "(formation (type S ($static) u8))",
    "(formation (type D (T T) u8))",
    "(kind () ((k brand)) $k)",
    "(kind () () (tuple u8 (fn (forall ($a) () u8 ((brand $a))))))",
    "(kind () () $k)",
    "(function () (f (T $k) (call f u8 $k) (fn f u16 _)))",
    "(function () (f (T $k) (call f u8 _)))",
    "(function () (f (T $k)\n  (call f u8)))",
    "(function ((type B ($k) u8)) (f ($k) (call f $k) (alloca x (B $k))))",
    "(function ((type B ($k) u8)) (f () (alloca x (B $q))))",
    "(function () (g ($b T)) (f () (foreach g (foreach (F (tuple u8)) F) u8)))",
    "(function () (f (T T)))",
    # termination
    "(instances ((Show (List T)) (Show T)))",
    "(instances ((Show T) (Show (List T))))",
    "(instances ((A (P T)) (B T))\n  ((B T) (A T)))",
    "(instances ((A) (A)))",
    "(instances ((S (exists ($a) (P $a))) (S (P $a))))",
    "(instances ((S (P T)) (E T)) ((E T) (Show T)))",
    "(mono (f (T) (call f T)))",
    "(mono (f (T) (call f (List T))))",
    "(mono (f (T) (call g T)) (g (T) (fn f (ptr T))))",
    "(mono (f ($k) (call f (brand $k))))",
    "(mono (f (T) (foreach f (foreach (F T) F) T)))",
    "(mono (f (T) (foreach f (foreach (F (tuple u8)) F) T)))",
    "(mono (f (T) (call g (List T))) (g (T)))",
    "(mono (f (T) (trait_call Show show (List T))))",
    # coherence
    "(coherence ((T) (Show (List T))) (() (Show (List u8))))",
    "(coherence (() (Show u8)) (() (Show u16)))",
    "(coherence (() (Show (exists ($a) (P $a u8)))) (() (Show (exists ($a) (P $a u16)))))",
    "(coherence ((T) (Show u8)))",
    "(coherence (($k) (Show (exists ($a) (brand $k)))))",
    "(coherence ((T) (Eq T u8)) ((T) (Eq u16 T)))",
    "(coherence ((T) (Eq T u8)) ((T) (Eq T u16)))",
)


def _params(form: List) -> tuple[C.Param, ...]:
    return tuple(C.Param(a[1:], C.BRAND) if a.startswith("$") else C.Param(a[1:], C.COPY) if a.startswith("+")
                 else C.Param(str(a)) for a in form)


def _kinds(form: List) -> dict[str, C.Kind]:
    return {str(name): str(kind) for name, kind in form}


def _premise(form: List) -> C.Premise:
    return C.Premise(str(form[0]), tuple(parse_type(t) for t in form[1:]))


def _program(decls: list) -> C.Program:
    opaque: C.Exposure = ("owner", C.ModuleId(0, ("m",)))
    types = {str(d[1]): C.TypeDecl(str(d[1]), d[0] == "linear", _params(d[2]), (), parse_type(d[3]),
                                   exposure=opaque if d[0] == "opaque" else None) for d in decls}
    return C.Program(types=types)


def _fun(form: List) -> C.Fun:
    instrs = []
    for f in form[2:]:
        op = str(f[0])
        if op == "trait_call":
            args, ty, first = (str(f[1]), str(f[2])), None, 3
        elif op == "foreach":
            args, ty, first = ("items", str(f[1])), parse_type(f[2]), 3
        else:
            args, ty, first = (str(f[1]),), None, 2
        instrs.append(C.Instr((), op, args, ty, tuple(parse_type(t) for t in f[first:]), pos=f.pos))
    block = C.Block("entry", (), tuple(instrs), C.Ret("x"))
    return C.Fun(str(form[0]), _params(form[1]), (), C.UNIT, (), (block,))


def _set(names: set[str]) -> str:
    return "[" + " ".join(sorted(names)) + "]"


def type_case(case: str) -> str:
    """What `kursal selftest-types CASE` prints for one case, computed with these modules."""
    form = read(case)[0]
    op = str(form[0])
    if op == "show":
        return T.show(parse_type(form[1]))
    if op == "subst":
        return T.show(T.subst(parse_type(form[1]), {str(n): parse_type(t) for n, t in form[2]}))
    if op == "free":
        return _set(T.free_names(parse_type(form[1])))
    if op == "brands":
        return _set(T.free_brands(parse_type(form[1])))
    if op == "extractable":
        return _set(T.extractable_names(parse_type(form[1])))
    if op in ("equal", "fits"):
        test = T.equal if op == "equal" else T.fits
        return str(test(parse_type(form[1]), parse_type(form[2]))).lower()
    if op in ("unify", "unify-closed"):
        metas: dict[str, C.Type] = {f"?{name}": Meta(str(kind)) for name, kind in form[1]}  # type: ignore[arg-type]
        a, b = T.subst(parse_type(form[2]), metas), T.subst(parse_type(form[3]), metas)
        ok = unify(a, b, closed=op == "unify-closed")
        solutions = "".join(f" {name}={T.show(zonk(m))}" for name, m in metas.items())
        return f"{str(ok).lower()} | {T.show(zonk(a))} | {T.show(zonk(b))} |{solutions}"
    if op == "match-head":
        found = match_head(_params(form[1]), tuple(map(parse_type, form[2])), tuple(map(parse_type, form[3])),
                           require_extractable=form[4] == "true")
        return "none" if found is None else "{" + " ".join(f"{n}={T.show(t)}" for n, t in found.items()) + "}"
    if op == "literal":
        return str(valid_literal(parse_type(form[1]), str(form[2]))).lower()  # type: ignore[arg-type]
    if op == "copy":
        return str(T.Types(_program(form[1])).is_copy(parse_type(form[3]), _kinds(form[2]))).lower()
    if op == "zero":
        return str(T.Types(_program(form[1])).zero_size(parse_type(form[2]))).lower()
    if op == "expand":
        return T.show(T.Types(_program(form[1])).shape(parse_type(form[2])))
    if op == "layout":
        check_type_expansion(_program(form[1:]))
        return "ok"
    if op == "formation":
        Formation(_program(form[1:])).declarations()
        return "ok"
    if op == "kind":
        return Formation(_program(form[1])).kind(parse_type(form[3]), _kinds(form[2]))
    if op == "instances":
        check_instance_termination((_premise(h[0]), tuple(map(_premise, h[1:])), h.pos) for h in form[1:])
        return "ok"
    if op == "coherence":
        check_coherence((_premise(h[1]), _params(h[0]), h.pos) for h in form[1:])
        return "ok"
    if op == "mono":
        check_mono_termination(C.Program(funs={str(f[0]): _fun(f) for f in form[1:]}))
        return "ok"
    if op == "function":
        program = _program(form[1])
        funs = [_fun(f) for f in form[2:]]
        program.funs = {f.name: f for f in funs}
        formation = Formation(program)
        for fun in funs:
            formation.function(fun)
        return "ok"
    if op == "fields":
        return "[" + " ".join(map(T.show, T.Types(_program(form[1])).fields(parse_type(form[2])))) + "]"
    if op == "variants":
        found = T.Types(_program(form[1])).variants(parse_type(form[2]))
        return "[" + " ".join(f"{n}:{T.show(t)}" for n, t in found) + "]"
    if op == "exposure":
        types, kinds, ty = T.Types(_program(form[1])), _kinds(form[2]), parse_type(form[3])
        shape = types.visible_shape(ty, None, kinds)
        return f"{'hidden' if shape is None else T.show(shape)} {str(types.concrete_exposure(ty, None, kinds)).lower()}"
    if op == "pattern":
        ty = parse_type(form[3])
        assert isinstance(ty, C.Foreach)
        return str(T.Types(_program(form[1])).pattern_available(ty, None, _kinds(form[2]))).lower()
    raise ValueError(f"unknown case {op}")


class CompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.binary = build_compiler()

    def run_compiler(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run_kursal(self.binary, *args)

    def run_all(self, runs: list[tuple[str, ...]]) -> list[subprocess.CompletedProcess[str]]:
        """Each run's result, in order; the runs go in parallel."""
        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            return list(pool.map(lambda args: self.run_compiler(*args), runs))

    @staticmethod
    def outcome(result: subprocess.CompletedProcess[str]) -> str:
        return result.stdout if result.returncode == 0 else f"error: {result.stderr.strip()}"

    def compiler_resolution(self, path: Path) -> str:
        result = self.run_compiler("resolve", str(path))
        if result.returncode != 0:
            return f"error: {result.stderr.strip()}"
        return result.stdout

    def test_resolver_matches_reference(self):
        packages = sorted({p.parent for top in ("examples", "lib") for p in (ROOT / top).glob("*/kur.toml")})
        singles = sorted((ROOT / "examples/bad").glob("*.kur"))
        self.assertGreater(len(packages), 40)
        for path in [*packages, *singles, COMPILER]:
            with self.subTest(path=str(path.relative_to(ROOT))):
                expected = reference_resolution(path)
                self.assertFalse(expected.startswith("error:"), expected)
                self.assertEqual(self.compiler_resolution(path), expected)

    def run_batches(self, command: str, paths: list[Path]) -> list[str]:
        """`kursal COMMAND PATH` for each path, a batch of processes at a time: its output, or its error."""
        workers = min(8, os.cpu_count() or 1)
        results: list[str] = []
        # Started from this thread, so that `limit_memory` runs in no threaded child.
        for start in range(0, len(paths), workers):
            running = [subprocess.Popen([str(self.binary), command, str(path)], stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, preexec_fn=limit_memory,
                                        env=dict(os.environ, KURSAL_LIB=str(LIB)))
                       for path in paths[start:start + workers]]
            for process in running:
                stdout, stderr = process.communicate(timeout=TIME_LIMIT)
                results.append(stdout if process.returncode == 0 else f"error: {stderr.strip()}")
        return results

    def compare_outputs(self, command: str, reference, paths: list[Path]) -> None:
        """Each package's result from the reference and from `kursal COMMAND`, both computed in parallel."""
        with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            expected = pool.map(reference, paths)
            results = self.run_batches(command, paths)
            for path, want, got in zip(paths, expected, results):
                with self.subTest(path=str(path)):
                    self.assertEqual(got, want)

    def compare_signatures(self, paths: list[Path]) -> None:
        self.compare_outputs("sigs", reference_signatures, paths)

    def test_signatures_match_reference(self):
        packages = sorted({p.parent for top in ("examples", "lib") for p in (ROOT / top).glob("*/kur.toml")})
        singles = sorted((ROOT / "examples/bad").glob("*.kur"))
        self.assertGreater(len(packages), 40)
        self.compare_signatures([*packages, *singles, COMPILER])

    def test_signature_cases_match_reference(self):
        """Small packages: SIGNATURES, which the reference accepts, and SIGNATURE_ERRORS, which it rejects."""
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index, source in enumerate((*SIGNATURES, *SIGNATURE_ERRORS)):
                root = Path(directory) / f"case{index}"
                root.mkdir()
                std = f'[dependencies.std]\npath="{LIB / "std"}"\n' if "std::" in source else ""
                (root / "kur.toml").write_text(manifest("p") + std)
                (root / "lib.kur").write_text(source)
                paths.append(root)
            for index, path in enumerate(paths):
                with self.subTest(source=(*SIGNATURES, *SIGNATURE_ERRORS)[index]):
                    expected = reference_signatures(path)
                    self.assertEqual(expected.startswith("error:"), index >= len(SIGNATURES), expected)
                    self.assertNotIn("syntax error", expected)
            self.compare_signatures(paths)

    def test_inference_matches_reference(self):
        packages = sorted({p.parent for top in ("examples", "lib") for p in (ROOT / top).glob("*/kur.toml")})
        singles = sorted((ROOT / "examples/bad").glob("*.kur"))
        self.assertGreater(len(packages), 40)
        self.compare_outputs("infer", reference_inference_text, [*packages, *singles, COMPILER])

    def test_inference_cases_match_reference(self):
        """Small programs: INFERENCES, which the reference infers, and INFER_ERRORS, which its inference
        rejects, each with a message containing the fragment given; resolution and signatures accept them all."""
        cases = [*INFERENCES, *(case for case, _ in INFER_ERRORS)]
        fragments = [None] * len(INFERENCES) + [fragment for _, fragment in INFER_ERRORS]
        with tempfile.TemporaryDirectory() as directory:
            paths = [place_case(Path(directory), index, case) for index, case in enumerate(cases)]
            with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
                expected = list(pool.map(reference_inference, paths))
            for case, fragment, (text, inferred) in zip(cases, fragments, expected):
                with self.subTest(case=case):
                    self.assertTrue(inferred, text)
                    self.assertNotIn("syntax error", text)
                    if fragment is None:
                        self.assertFalse(text.startswith("error:"), text)
                    else:
                        self.assertTrue(text.startswith("error:"), text[:200])
                        self.assertIn(fragment, text)
            for path, (want, _), got in zip(paths, expected, self.run_batches("infer", paths)):
                with self.subTest(path=str(path)):
                    self.assertEqual(got, want)

    def test_lowering_matches_reference(self):
        """Every package, bad example and the compiler itself: the same core program, or the same error."""
        paths, expected = built_paths(), reference_builds()
        self.assertGreater(len(paths), 50)
        for path, want, got in zip(paths, expected, self.run_batches("lower", paths)):
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual(got, want["lower"])

    def test_build_matches_reference(self):
        """Every package that builds: `kursal build` writes the C of the reference's whole pipeline."""
        paths, expected = built_paths(), reference_builds()
        buildable = [(path, want["c"]) for path, want in zip(paths, expected) if want["c"] is not None]
        self.assertGreater(len(buildable), 50)
        self.assertIn(COMPILER, [path for path, _ in buildable])
        results = self.run_batches("build", [path for path, _ in buildable])
        for (path, want), got in zip(buildable, results):
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual(got, want)

    def test_lowering_cases_match_reference(self):
        """The packages the reference's own tests lower, and the cases of tests/lower_cases.py, which the
        reference rejects with a message containing the fragment given: the same core program, or the same
        error. No case is a syntax error."""
        cases = lowering_cases()
        fragments = {json.dumps(list(case_files(case)), sort_keys=True): fragment for case, fragment in LOWER_ERRORS}
        self.assertGreater(len(cases), 300)
        written = 0
        for case in cases:
            with self.subTest(files=case["files"]):
                self.assertNotIn("syntax error", case["lower"])
                fragment = fragments.get(json.dumps([case["files"], case["target"]], sort_keys=True))
                if fragment is not None:
                    written += 1
                    self.assertTrue(case["lower"].startswith("error:"), case["lower"][:200])
                    self.assertIn(fragment, case["lower"])
        self.assertEqual(written, len(LOWER_ERRORS))
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            paths = []
            for index, case in enumerate(cases):
                materialize(base / f"case{index}", case["files"])
                paths.append(base / f"case{index}" / case["target"])
            results = self.run_batches("lower", paths)
        for index, (case, got) in enumerate(zip(cases, results)):
            with self.subTest(files=case["files"]):
                self.assertEqual(got.replace(str(base / f"case{index}"), "<DIR>"), case["lower"])

    @unittest.skipUnless(os.environ.get("KURSAL_SELFHOST") == "1", "set KURSAL_SELFHOST=1 to compile the compiler with itself")
    def test_self_compilation_reaches_a_fixpoint(self):
        """The compiler the reference built (kc1) builds compiler/ (kc2), which builds it again (kc3): kc2's C
        and kc3's are the same, and the reference's C for compiler/."""
        expected = reference_builds()[built_paths().index(COMPILER)]["c"]
        kc2 = self_built_compiler(self.binary)
        with tempfile.TemporaryDirectory() as directory:
            kc3 = Path(directory) / "kc3"
            started = time.monotonic()
            result = run_kursal(kc2, "build", str(COMPILER), "-o", str(kc3), "--times")
            self.assertEqual(result.returncode, 0, result.stderr)
            print(f"\nkc3 built in {time.monotonic() - started:.1f}s:\n{result.stderr}", file=sys.stderr)
            kc2_c, kc3_c = (Path(f"{stage}.c").read_text() for stage in (kc2, kc3))
        self.assertEqual(kc2_c, kc3_c)
        self.assertEqual(kc2_c, expected)

    @unittest.skipUnless(os.environ.get("KURSAL_SELFHOST") == "1", "set KURSAL_SELFHOST=1 to compile the compiler with itself")
    @unittest.skipUnless(shutil.which("valgrind"), "valgrind is not installed")
    def test_self_build_frees_everything(self):
        """kc2 builds compiler/ under valgrind with no error and every byte freed, and writes the reference's C."""
        expected = reference_builds()[built_paths().index(COMPILER)]["c"]
        kc2 = self_built_compiler(self.binary)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "kc3"
            started = time.monotonic()
            result = run_valgrind(kc2, "build", str(COMPILER), "-o", str(output), timeout=6 * 3600)
            print(f"\nkc3 built under valgrind in {time.monotonic() - started:.1f}s:\n"
                  + "\n".join(line for line in result.stderr.splitlines() if "in use at exit" in line or "ERROR SUMMARY" in line),
                  file=sys.stderr)
            self.assertEqual(result.returncode, 0, result.stderr[-4000:])
            self.assertIn("All heap blocks were freed -- no leaks are possible", result.stderr)
            self.assertIn("ERROR SUMMARY: 0 errors from 0 contexts", result.stderr)
            self.assertEqual(Path(f"{output}.c").read_text(), expected)

    @unittest.skipUnless(shutil.which("valgrind"), "valgrind is not installed")
    def test_build_frees_everything(self):
        """The compiler builds a program under valgrind with no error and every byte freed, and the program
        it builds runs."""
        package = ROOT / "examples/lisp"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "lisp"
            result = run_valgrind(self.binary, "build", str(package), "-o", str(output), timeout=TIME_LIMIT)
            self.assertEqual(result.returncode, 0, result.stderr[-4000:])
            self.assertIn("All heap blocks were freed -- no leaks are possible", result.stderr)
            self.assertIn("ERROR SUMMARY: 0 errors from 0 contexts", result.stderr)
            ran = subprocess.run([str(output)], input="(+ 1 (* 2 3))\n", capture_output=True, text=True, timeout=60)
            self.assertEqual((ran.stdout, ran.returncode), ("7\n", 0))

    def check_resolution(self, cases: tuple[tuple[dict[str, str], str], ...], fails: bool) -> None:
        for index, (files, target) in enumerate(cases):
            with self.subTest(case=index, files=files), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for rel, text in files.items():
                    (root / rel).parent.mkdir(parents=True, exist_ok=True)
                    (root / rel).write_text(text)
                expected = reference_resolution(root / target)
                self.assertEqual(expected.startswith("error:"), fails, expected)
                self.assertEqual(self.compiler_resolution(root / target), expected)

    def test_resolution_errors_match_reference(self):
        self.check_resolution(RESOLUTION_ERRORS, True)

    def test_manifests_and_names_match_reference(self):
        self.check_resolution(RESOLVED, False)

    def test_parser_matches_reference(self):
        paths = sorted(p for top in ("lib", "examples", "compiler", "reference-compiler/examples")
                       for p in (ROOT / top).rglob("*.kur"))
        accepted = []
        for path in paths:
            try:
                parse(path.read_text(), str(path))
                accepted.append(path)
            except KursError:
                pass
        self.assertGreater(len(accepted), 100)
        result = self.run_compiler("parse", *map(str, accepted))
        self.assertEqual(result.returncode, 0, result.stderr)
        sections: list[tuple[str, list[str]]] = []
        for line in result.stdout.splitlines():
            if line.startswith("== "):
                sections.append((line[3:], []))
            else:
                sections[-1][1].append(line)
        self.assertEqual(len(sections), len(accepted))
        for path, (name, got) in zip(accepted, sections):
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual(name, str(path))
                expected = reference_dump(path).splitlines()
                self.assertEqual(len(got), len(expected))
                for want, have in zip(expected, got):
                    self.assertEqual(normalized(have), normalized(want))

    def test_syntax_errors_match_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            for index, source in enumerate(SYNTAX_ERRORS):
                with self.subTest(source=source):
                    path = Path(directory) / f"case{index}.kur"
                    path.write_text(source)
                    with self.assertRaises(KursError) as raised:
                        parse(source, str(path))
                    result = self.run_compiler("parse", str(path))
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stderr.strip(), str(raised.exception))

    def test_kurs_matches_reference(self):
        """Each case is (name, KURS text, what the reference writer writes for it)."""
        cases = [("canonical", CANONICAL, CANONICAL)]
        cases += [(f"variant{i}", CANONICAL.replace(canonical, variant), CANONICAL)
                  for i, (canonical, variant) in enumerate(VARIANTS)]
        programs = [(p["name"], p["kurs"], p["kurs"]) for p in lowered_programs()]
        self.assertGreaterEqual(len(programs), 50)
        with tempfile.TemporaryDirectory() as directory:
            for name, text, expected in cases + programs:
                with self.subTest(case=name):
                    path = Path(directory) / f"{name}.kurs"
                    path.write_text(text)
                    result = self.run_compiler("kurs", str(path))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, expected)

    def test_c_and_readable_match_reference(self):
        """Every package that builds: the C of its lowered program, entered as its executable is, and
        its readable KURS."""
        programs = lowered_programs()
        self.assertGreaterEqual(len(programs), 50)
        with tempfile.TemporaryDirectory() as directory:
            runs: list[tuple[str, ...]] = []
            for program in programs:
                path = Path(directory) / f"{program['name']}.kurs"
                path.write_text(program["kurs"])
                entry = ("--entry", program["entry"]) if program["entry"] else ()
                runs += [("c", str(path), *entry), ("readable", str(path))]
            results = self.run_all(runs)
        for index, program in enumerate(programs):
            with self.subTest(package=program["name"]):
                self.assertEqual(self.outcome(results[2 * index]), program["c"])
                self.assertEqual(self.outcome(results[2 * index + 1]), program["readable"])

    def test_checker_matches_reference(self):
        """Every core program the reference's own tests check: the same verdict, with the same message
        for one it rejects; for one it accepts, the same C and readable KURS."""
        cases = core_cases()
        rejected = [case for case in cases if case["check"] is not None]
        self.assertGreater(len(rejected), 150)
        self.assertGreater(len(cases) - len(rejected), 100)
        with tempfile.TemporaryDirectory() as directory:
            runs: list[tuple[str, ...]] = []
            for index, case in enumerate(cases):
                path = Path(directory) / f"case{index}.kurs"
                path.write_text(case["kurs"])
                runs += [("check", str(path)), ("c", str(path)), ("readable", str(path))]
            results = self.run_all(runs)
        for index, case in enumerate(cases):
            with self.subTest(case=index, kurs=case["kurs"]):
                check, c, readable = results[3 * index: 3 * index + 3]
                if case["check"] is not None:
                    self.assertEqual((check.returncode, check.stderr.strip()), (1, case["check"]))
                    continue
                self.assertEqual((check.returncode, check.stdout, check.stderr), (0, "", ""))
                if case["c"] is not None:
                    self.assertEqual(self.outcome(c), case["c"])
                if case["readable"] is not None:
                    self.assertEqual(self.outcome(readable), case["readable"])

    def test_kurs_errors_match_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            for index, source in enumerate(KURS_ERRORS):
                with self.subTest(source=source):
                    path = Path(directory) / f"case{index}.kurs"
                    path.write_text(source)
                    with self.assertRaises(KursError) as raised:
                        parse_program(source)
                    result = self.run_compiler("kurs", str(path))
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stderr.strip(), str(raised.exception))


    def test_type_modules_match_reference(self):
        for case in TYPE_CASES:
            with self.subTest(case=case):
                T._fresh = itertools.count()   # the Kursal command numbers fresh names from 0
                try:
                    expected, status = type_case(case) + "\n", 0
                except KursError as error:
                    expected, status = str(error), 1
                result = self.run_compiler("selftest-types", case)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(result.stdout if status == 0 else result.stderr.strip(), expected)


if __name__ == "__main__":
    unittest.main()
