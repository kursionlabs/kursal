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
the same C and the same readable KURS. Its `doc` writes a Krayon site whose pages carry a package's
documentation comments and signatures, and whose links all lead somewhere."""

import dataclasses
import fcntl
import functools
import hashlib
import html
import importlib
import itertools
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
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


@functools.cache
def valgrind_unusable() -> Optional[str]:
    """Why valgrind cannot check a program here, or None when it can. It is not installed, or it cannot start
    one: on Linux it needs the C library's symbols (glibc's debug symbols, on some systems a separate
    package) and stops at startup without them."""
    if shutil.which("valgrind") is None:
        return "valgrind is not installed"
    probe = subprocess.run(["valgrind", "--error-exitcode=99", shutil.which("true") or "/bin/true"],
                           capture_output=True, text=True, timeout=300)
    if probe.returncode != 0:
        reason = next((line.lstrip("=0123456789 ") for line in probe.stderr.splitlines() if "Fatal error" in line),
                      f"status {probe.returncode}")
        return f"valgrind cannot start a program here ({reason}); install glibc's debug symbols"
    return None


def run_valgrind(binary: Path, *args: str, timeout: int) -> subprocess.CompletedProcess[str]:
    """`binary ARGS` under valgrind's leak check: status 99 on any error or any byte not freed, whether
    lost or still reachable. `cc`, which `build` runs, is not traced."""
    return subprocess.run([*VALGRIND, str(binary), *args], capture_output=True, text=True, timeout=timeout,
                          preexec_fn=limit_valgrind_memory, env=dict(os.environ, KURSAL_LIB=str(LIB)))


def run_kursal(binary: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(binary), *args], capture_output=True, text=True, timeout=TIME_LIMIT,
                          preexec_fn=limit_memory, env=dict(os.environ, KURSAL_LIB=str(LIB), KURSAL_DIAGNOSTICS="short",
                                                            KURSAL_JOBS="2"))


def digest_of(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def cached(stem: str, key: str, make: Callable[[], list]) -> list:
    """`make()`, a JSON value, kept in .build/ as STEM-KEY.json until KEY changes. Test modules running at
    once make it once: the first takes STEM.lock while it makes the value, and the others wait to read it."""
    path = CACHE / f"{stem}-{key}.json"
    if path.exists():
        return json.loads(path.read_text())
    CACHE.mkdir(exist_ok=True)
    with open(CACHE / f"{stem}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            return json.loads(path.read_text())
        value = make()
        for old in CACHE.glob(f"{stem}-*.json"):
            old.unlink()
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value))
        temporary.replace(path)
    return value


def self_built_compiler(kc1: Path) -> Path:
    """kc2, the compiler kc1 builds from compiler/, kept in .build/ beside kc1; its C, split, is in kc2.build."""
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
            if old.is_dir():
                shutil.rmtree(old)
            else:
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
    if kind == "op":
        assert isinstance(v, tuple)
        op, operands = v
        spelled = "(" + " ".join([op, *(f"({const_text(operand)})" for operand in operands)]) + ")"
    elif kind == "int":
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
            return "(value)"
        case I.ClosureCall():
            return "(closure)"
        case I.VariantCall():
            return f"(variant {write_atom(call.variant)})"
        case I.TupleCall():
            return "(tuple)"
        case I.BuiltinCall():
            return f"(builtin {write_atom(call.name)})"
        case I.TypedBuiltinCall():
            return f"({write_atom(call.name)} {inferred_type(call.target)})"
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
    S.Array: ("array", lambda n: n.elems),
    S.StructLit: ("struct", lambda n: tuple(v for _, v in n.fields)), S.If: ("if", lambda n: (n.cond, n.then, n.other)),
    S.Closure: ("closure", lambda n: (n.body,)), S.Return: ("return", lambda n: (n.value,)),
    S.Pack: ("pack", lambda n: (n.value,)), S.Foreach: ("foreach", lambda n: (n.source, n.body)),
    S.Binop: ("binop", lambda n: (n.left, n.right)), S.Unary: ("unary", lambda n: (n.operand,)),
    S.Cast: ("cast", lambda n: (n.value,)), S.UnsafeStore: ("store", lambda n: (n.pointer, n.value)),
    S.Field: ("field", lambda n: (n.value,)), S.Index: ("index", lambda n: (n.value,)),
    S.Call: ("call", lambda n: (n.callee, *n.args)), S.Try: ("try", lambda n: (n.value,)),
    S.While: ("while", lambda n: (n.cond, n.body)), S.For: ("for", lambda n: (n.iterator, n.body)),
    S.Let: ("let", lambda n: (n.pattern, n.value)),
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
                                      sigs.name(entry_decl) if entry_decl is not None else None, hosted=True)
        except KursError:
            c = None
        return {"lower": text, "c": c}
    return in_deep_thread(make)


ENTRY_IMPORTS = "use std::io::{Error, print};\nuse std::option::{Option, Result};\nuse std::process::Args;\n"

# An executable's entry functions: the six shapes it may have, then others it may not.
ENTRIES = (
    "fn main() { print(\"hello\"); }\n",
    "fn main(args: Args) { print(\"hello\"); }\n",
    "fn main() -> i32 { 3 }\n",
    "fn main(args: Args) -> i32 { args.len() as i32 }\n",
    "fn main() -> Result[(), Error] { print(\"hello\") }\n",
    "fn main(args: Args) -> Result[(), Error] { Result::Err.(Error.{ errno: args.len() as i32 }) }\n",
    "fn main() -> i64 { 0 }\n",
    "fn main() -> u8 { 0 }\n",
    "fn main() -> bool { true }\n",
    "fn main() -> ! { std::process::exit(0) }\n",
    "fn main() -> Result[(), u32] { Result::Ok.(()) }\n",
    "fn main() -> Result[i32, Error] { Result::Ok.(0) }\n",
    "fn main() -> Option[Error] { Option::None }\n",
    "fn main(code: i32) -> i32 { code }\n",
    "fn main(args: Args, more: Args) -> i32 { 0 }\n",
    "unsafe fn main() -> i32 { 0 }\n",
    "forall T. fn main() -> i32 { 0 }\n",
    "fn main(&args: Args) -> i32 { 0 }\n",
)


def reference_entry(path: Path) -> str:
    """The reference's C for the executable at `path`, as `kursal build` writes it, or its error."""
    T._fresh = itertools.count()
    I._ids = itertools.count()
    try:
        resolver = resolve(path)
        sigs = signatures(resolver)
        program = lower(resolver, sigs)
        return emit_c(program, check_program(program), sigs.name(resolver.packages[0].entry_decl), hosted=True)
    except KursError as error:
        return f"error: {error}"


def reference_lowering(path: Path) -> str:
    return reference_build(path)["lower"]


def built_paths() -> list[Path]:
    """Every package in the repository, every bad example, and the compiler itself."""
    packages = sorted({p.parent for top in ("examples", "reference-compiler/tests/fixtures", "lib")
                       for p in (ROOT / top).glob("*/kur.toml")})
    return [*packages, *sorted((ROOT / "examples/bad").glob("*.kur")), COMPILER]


def reference_builds() -> list[dict[str, Optional[str]]]:
    """`reference_build` for each of `built_paths`, computed once per change to the sources."""
    paths = built_paths()
    sources = [*(ROOT / "lib").rglob("*.kur"), *(ROOT / "lib").rglob("*.toml"), *(ROOT / "examples").rglob("*.kur"),
               *(ROOT / "examples").rglob("*.toml"), *(ROOT / "reference-compiler/tests/fixtures").rglob("*.kur"),
               *(ROOT / "reference-compiler/tests/fixtures").rglob("*.toml"), *COMPILER.rglob("*.kur"), *COMPILER.rglob("*.toml"),
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
    (root / "kur.toml").write_text(manifest("p", needs_core(case)))
    (root / "lib.kur").write_text(case)
    return root


def manifest(name: str, deps: tuple[str, ...] = (), entry: str | None = None) -> str:
    """`core` is the library's; any other dependency is a sibling directory."""
    text = f'[package]\nname="{name}"\nsource_root="."\n'
    if entry:
        text += f'kind="executable"\nentry="{entry}"\n'
    for dep in deps:
        text += f'[dependencies.{dep}]\npath="{LIB / "core" if dep == "core" else "../" + dep}"\n'
    return text


# A package for `kursal doc`: overview and item comments, a struct with methods, a trait, an instance of
# it, a constant, a function under a `forall` premise of several lines, and what the site leaves out: a
# plain comment and an extern.
DOC_PACKAGE = """\
//! Gizmos for testing the documentation site.
//! A second line of the overview.

// An implementation note, not published.
extern "C" fn abs(n: i32) -> i32;

/// A point on a grid.
exposed struct Point {
    x: u32,
    y: u32,
}

impl Point {
    /// The point at the origin.
    fn origin() -> Point {
        Point.{ x: 0u32, y: 0u32 }
    }

    /// The sum of both coordinates, e.g. 3 for `(1, 2)`, i.e. `x + y`. It stays
    /// below twice `LIMIT`.
    fn sum(self) -> u32 {
        self.x + self.y
    }
}

/// Things with a size.
forall Self.
trait Measure {
    fn size(self: Self) -> u32;
}

/// A point's size is the sum of its coordinates.
instance Measure[Point] {
    fn size(self: Point) -> u32 {
        self.sum()
    }
}

/// The largest coordinate.
const LIMIT: u32 = 100u32;

/// The first of two values.
forall T, U
    where Copy[T],
          Copy[U].
fn first(a: T, b: U) -> T {
    a
}
"""


# Packages for a site with a re-exported module: `gadget` declares a struct, an enum and a trait in
# `gadget::shapes`, with a method in another module and the instances in `gadget::measures`; `gizmo`
# re-exports `gadget::shapes` as `gizmo::shapes`, and its own `gizmo::tools` names `Circle`.
DOC_PACKAGES = {
    "gadget/kur.toml": manifest("gadget"),
    "gadget/lib.kur": "//! Gadgets: shapes and how big they are.\nuse package::{measures, shapes};\nexport measures;\nexport shapes;\n",
    "gadget/shapes.kur": """\
//! Shapes and their areas.

/// A circle. Its radius is whole.
exposed struct Circle {
    radius: u32,
}

/// A side of a square.
exposed enum Side {
    Top,
    Bottom,
}

/// Things with an area.
forall Self.
trait Area {
    /// The area, rounded down.
    fn area(self: Self) -> u32;
}
""",
    "gadget/measures.kur": """\
//! How big shapes are.
use package::shapes::{Area, Circle, Side};

impl Circle {
    /// A circle of radius one.
    fn unit() -> Circle {
        Circle.{ radius: 1u32 }
    }
}

/// A circle's area, roughly.
instance Area[Circle] {
    fn area(self: Circle) -> u32 {
        3u32 * self.radius * self.radius
    }
}

/// A side has no area.
instance Area[Side] {
    fn area(self: Side) -> u32 {
        0u32
    }
}

/// The larger of two areas.
forall T where Area[T], Copy[T].
fn larger(a: T, b: T) -> u32 {
    let x = a.area();
    let y = b.area();
    if x > y { x } else { y }
}
""",
    "gizmo/kur.toml": manifest("gizmo", ("gadget",)),
    "gizmo/lib.kur": "//! The gizmo facade.\nuse gadget::shapes;\nexport shapes;\nuse package::tools;\nexport tools;\n",
    "gizmo/tools.kur": "//! Tools for circles.\nuse gadget::shapes::Circle;\n\n/// The radius of a circle.\nfn radius(c: Circle) -> u32 {\n    c.radius\n}\n",
}


# Declarations added before the others of DOC_PACKAGES' `gadget`, to `shapes.kur` and to `measures.kur`.
EARLY_SHAPES = "/// Early.\nexposed struct Early {\n    n: u32,\n}\n\n/// Early too.\nfn early() -> u32 {\n    1u32\n}\n\n"
EARLY_MEASURES = ("impl Circle {\n    fn zero() -> Circle {\n        Circle.{ radius: 0u32 }\n    }\n}\n\n"
                  "instance Area[Early] {\n    fn area(self: Early) -> u32 {\n        self.n\n    }\n}\n\n")


def write_files(base: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (base / name).parent.mkdir(parents=True, exist_ok=True)
        (base / name).write_text(text)


def page_body(page: Path) -> str:
    """A generated page without its front matter."""
    return page.read_text().split("+++\n", 2)[2]


def page_url(src: Path, page: Path) -> str:
    """Where Krayon serves a generated page: its `permalink`, else its directory."""
    found = re.search(r'^permalink = "([^"]+)"', page.read_text(), re.M)
    if found:
        return found.group(1)
    relative = page.relative_to(src).as_posix()
    return "/" + relative.removesuffix("index.md")


def anchors_of(text: str) -> set[str]:
    return set(re.findall(r"\{#([\w:-]+)", text)) | set(re.findall(r'\sid="([^"]+)"', text))


def markdown_link_errors(src: Path) -> list[str]:
    """The links of a generated site's Markdown that lead nowhere: a target, root-relative or relative to
    the page's URL, names a page under SRC, and a fragment an anchor there (a heading's `{#id}` or an
    element's `id`). Markdown links, `href`s and front matter's `parent` and sidebar `url`s count;
    links in fenced code do not, nor Markdown's link syntax in a line of HTML, which Krayon copies as
    written."""
    pages = {page: page.read_text() for page in src.rglob("*.md")}
    by_url = {page_url(src, page): page for page in pages}
    anchors = {page: anchors_of(text) for page, text in pages.items()}
    errors = []
    for page, text in pages.items():
        here = page_url(src, page)
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        markdown = re.sub(r"^<.*$", "", text, flags=re.M)
        targets = re.findall(r"\]\(([^)\s]+)\)", markdown) + re.findall(r'href="([^"]+)"', text)
        targets += re.findall(r'^(?:url|parent) = "([^"]+)"', text, re.M)
        for target in targets:
            if re.match(r"[a-z]+:", target):
                continue
            resolved = urllib.parse.urljoin("http://site" + here, html.unescape(target))
            path, _, fragment = resolved.removeprefix("http://site").partition("#")
            dest = by_url.get(path)
            if dest is None or (fragment and fragment not in anchors[dest]):
                errors.append(f"{page.relative_to(src)}: {target}")
    return errors


def html_link_errors(dist: Path) -> list[str]:
    """The local `href`s and `src`s of a built site that name no file, or no `id` in it, and the ids a
    page has twice."""
    dist = dist.resolve()
    texts = {page: page.read_text() for page in dist.rglob("*.html")}
    ids = {page: re.findall(r'\sid="([^"]+)"', text) for page, text in texts.items()}
    errors = [f"{page.relative_to(dist)}: id {name} twice" for page, found in ids.items()
              for name in sorted({n for n in found if found.count(n) > 1})]
    for page, text in texts.items():
        for target in re.findall(r'(?:href|src)="([^"]+)"', text):
            if re.match(r"[a-z]+:|//", target):
                continue
            path, _, fragment = target.partition("#")
            path = path.partition("?")[0]
            if not path:
                dest = page
            elif path.startswith("/"):
                dest = dist / path.lstrip("/")
            else:
                dest = page.parent / path
            if dest.is_dir():
                dest = dest / "index.html"
            dest = dest.resolve()
            if not dest.is_file() or (fragment and fragment not in ids.get(dest, [])):
                errors.append(f"{page.relative_to(dist)}: {target}")
    return errors


def plain_text(markup: str) -> str:
    """HTML as text: without its tags, its entities decoded."""
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


def first_signature(body: str) -> str:
    """The text of the first signature in BODY."""
    block = re.search(r'<pre class="sig"><code class="language-kursal">(.*?)</code></pre>', body, re.S)
    assert block is not None
    return plain_text(block.group(1))


def signature_after(body: str, anchor: str) -> str:
    """The text of the first signature after the element with id ANCHOR."""
    return first_signature(body[body.index(f'id="{anchor}"'):])


def needs_core(source: str) -> tuple[str, ...]:
    """The dependencies of a one-module case: `core`, where the builtins are, when it names it."""
    return ("core",) if "core::" in source else ()


def case_files(case: str | dict[str, str]) -> tuple[dict[str, str], str]:
    """A case of tests/lower_cases.py as files, by relative path, and the path to lower (see `place_case`)."""
    if isinstance(case, dict):
        return case, "a"
    if "std::" in case:
        return {"case.kur": case}, "case.kur"
    return {"kur.toml": manifest("p", needs_core(case)), "lib.kur": case}, "."


LOWER_CASES = tuple(case_files(case) for case, _ in LOWER_ERRORS)


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
    def make() -> list:
        cases: dict[str, tuple[dict[str, str], str]] = {}
        for files, target in [*recordings()[1], *LOWER_CASES]:
            cases.setdefault(json.dumps([files, target], sort_keys=True), (files, target))
        unique = list(cases.values())
        with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            return [{"files": files, "target": target, "lower": text}
                    for (files, target), text in zip(unique, pool.map(reference_case_lowering, unique, chunksize=4))]

    return cached("lowerings", digest_of(RECORDED_SOURCES), make)


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
    (app("use lib::shapes::Nothing;\n"), "app"),
    (app("use lib::nowhere::*;\n"), "app"),
    (app("use lib::shapes::Point::*;\n"), "app"),
    (app("use lib::{shapes::{Point, Nothing}, nowhere::Thing};\n"), "app"),
    (app("export absent;\n"), "app"),
    (app("fn f(x: Nothing) -> u32 { 0u32 }\n"), "app"),
    (app("fn f() -> u32 { 0u32 }\nfn f() -> u32 { 1u32 }\n"), "app"),
    (app("use lib::shapes::Point;\nuse lib::shapes::Point;\n"), "app"),
    (app("use lib::shapes::make;\nfn make() -> u32 { 0u32 }\n"), "app"),
    (app("use lib::shapes::Point as Copy;\n"), "app"),
    (app("use lib::shapes as package;\n"), "app"),
    (app("use package::a::X;\nfn f() -> u32 { X.() }\n", {"app/a.kur": "use package::X;\n"}), "app"),
    (app("use package::a::*;\nfn f() -> u32 { X() }\n", {"app/a.kur": "use package::*;\n"}), "app"),
    (app("use package::a::*;\nfn f() -> u32 { X() }\n", {"app/a.kur": "use package::b::*;\n", "app/b.kur": "use package::a::*;\n"}), "app"),
    (app("fn f() -> u32 { lib::hidden::secret() }\n"), "app"),
    (app("fn f() -> u32 { lib::shapes }\n"), "app"),
    (app("fn f() -> u32 { lib::shapes::Point::nope() }\n"), "app"),
    (app("use lib::shapes::Shape;\nfn f() -> u32 { Shape::Dot::x() }\n"), "app"),
    (app("use lib::shapes::Shape::Dot;\nfn f() -> u32 { Dot::x() }\n"), "app"),
    (app("use package::f::*;\nfn f() -> u32 { g() }\n"), "app"),
    (app("use lib::shapes::make;\nfn f(x: make) -> u32 { 0u32 }\n"), "app"),
    (app("use lib::shapes::make;\nfn f() -> u32 { make.{ x: 1u32 } }\n"), "app"),
    (app("use lib::shapes::Point;\nfn f() -> Point { Point.{ x } }\n"), "app"),
    (app("use lib::shapes;\nuse lib::shapes::Point;\nfn f(p: Point) -> u32 { match p { shapes::Point(x) => 0u32 } }\n"), "app"),
    (app("fn f(x: builtin) -> () { () }\n"), "app"),
    # The whole of TOML, read before its keys are checked: a dependency has no key but `path`.
    (app("", {"app/kur.toml": "[package]\nname = \"app\"\nsource_root = \".\"\n[dependencies.lib]\npath = '../lib'\n"
                              "version = 1\nratio = 0.5\nflags = [true, false]\nwhen = 1979-05-27T07:32:00Z\n"
                              "[[dependencies.lib.authors]]\nname = \"a\"\n"}), "app"),
    # An inherent impl is in the package that declares its type; one of a primitive is in core.
    (app("use lib::shapes::Point;\nimpl Point { fn other(self) -> u32 { 0u32 } }\nimpl bool { fn no(self) -> bool { self } }\n"), "app"),
    # Construction is `Name.(args)`, and only a tuple or a variant is constructed.
    (app("use lib::shapes::{Shape, make};\nfn f() -> u32 { let s = Shape::Dot(make(1u32)); let p = make.(2u32); 0u32 }\n"), "app"),
    (app("fn f() -> u32 { u32::nope() }\n"), "app"),
    (app("forall T where lib::shapes::make[T]. fn f(x: T) -> u32 { 0u32 }\n"), "app"),
    (app("impl (u32, u32) { fn f(self) -> u32 { 0u32 } }\n"), "app"),
    (app("use lib::shapes;\nimpl shapes { fn f(self) -> u32 { 0u32 } }\n"), "app"),
    (app("struct P { x: u32 }\nimpl P { fn f(self) -> u32 { 0u32 } }\nimpl P { fn f(self) -> u32 { 1u32 } }\n"), "app"),
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
    ({"p/kur.toml": '[package]\nname="p"\nversion="1"\n[extra]\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname="p"\nversion="1"\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname="p"\nsource_root="."\nlink_flags=["-lm", ""]\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": 'dependencies=[]\n[package]\nname="p"\nsource_root="."\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": manifest("p") + '[dependencies.x]\npath=3\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": manifest("p") + '[dependencies]\nx="../x"\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": 'package="p"\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '# no name\n[package]\nsource_root="."\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname="p"\nsource_root="."\nkind="executable"\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname="p"\nsource_root="."\nkind="executabl"\nentry="package::main"\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname="p"\nsource_root="."\nkind="Library"\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname="p"\nsource_root="."\n[dependencies.x]\npath="../missing"\nversion="1"\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname="p"\nedition=1\n[dependencies.x]\npath="../missing"\noptional=true\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname="p"\nsource_root="."\nkind="library"\nentry="package::main"\n[dependencies.x]\npath="../missing"\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname="p"\nsource_root="."\nkind=1\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname = "p" # the name\n"source_root" = "."\n[package.x]\n', "p/lib.kur": ""}, "p"),
    ({"one.kur": "fn main() -> i32 { nothing() }\n"}, "one.kur"),
    ({"one.kur": "fn f() -> Nothing { 0i32 }\n"}, "one.kur"),
    (app("use package::a;\n", {"app/a.kur": "fn f() -> u32 {\n    g()\n}\n"}), "app"),
    (app("use package::a;\n", {"app/a.kur": "fn f( {\n"}), "app"),
    (app("use package::b::c::X;\nfn f() -> u32 { X() }\n", {"app/b/lib.kur": "", "app/b/c.kur": "use package::b::c::X;\n"}), "app/kur.toml"),
    ({"p/kur.toml": manifest("p") + '[dependencies]\nx = { path = 3 }\n', "p/lib.kur": ""}, "p"),
    # Manifests that are not TOML: the error names the line tomllib reports.
    ({"p/kur.toml": manifest("p") + '[package]\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": 'package = { name = "p" }\n[package]\nsource_root = "."\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": 'package.name = "p"\n[package]\nsource_root = "."\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": manifest("p") + '[dependencies.x]\npath = "x"\npath = "y"\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname = "p"\nsource_root = "."\nversion = 1.2.3\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\r\nname = "p\r\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": manifest("p") + '[dependencies.x\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": manifest("p") + 'x = """open', "p/lib.kur": ""}, "p"),
    # Values the manifest does not take.
    ({"p/kur.toml": '[package]\nname = ["p"]\nsource_root = "."\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[package]\nname = "p"\nsource_root = 1979-05-27\n', "p/lib.kur": ""}, "p"),
    ({"p/kur.toml": '[[package]]\nname = "p"\n', "p/lib.kur": ""}, "p"),
    # A linear type's `!` is part of its name: a name without it, or with one it does not have, names
    # nothing.
    (app("struct L! { x: u32 }\nfn f(l: L) -> u32 { 0u32 }\n"), "app"),
    (app("struct L! { x: u32 }\nfn f() -> L! { L.{ x: 0u32 } }\n"), "app"),
    (app("enum E! { A, B }\nfn f() -> E! { E!::A }\nfn g(e: E!) -> u32 { match e { E::A => 0u32, E!::B => 1u32 } }\n"), "app"),
    (app("struct L! { x: u32 }\nimpl L { fn get(self) -> u32 { 0u32 } }\n"), "app"),
    (app("use package::a::L;\nfn f(l: L) -> u32 { 0u32 }\n", {"app/a.kur": "struct L! { x: u32 }\n"}), "app"),
    (app("use lib::shapes::Point!;\nfn f(p: Point!) -> u32 { 0u32 }\n"), "app"),
    (app("fn f(p: lib::shapes::Point!) -> u32 { 0u32 }\n"), "app"),
    (app("struct P { x: u32 }\nfn f() -> P { P!.{ x: 0u32 } }\n"), "app"),
    (app("fn f() -> u32 { lib::shapes::make!(1u32).x }\n"), "app"),
    (app("use package::a::L! as M;\nfn f(l: M) -> u32 { 0u32 }\n", {"app/a.kur": "struct L! { x: u32 }\n"}), "app"),
    (app("use lib::shapes::Point as Q!;\n"), "app"),
    (app("struct P { x: u32 }\nstruct P! { x: u32 }\nfn f(p: P!) -> P { P.{ x: 0u32 } }\nfn g(p: Q) -> u32 { 0u32 }\n"), "app"),
    # Every error is reported in its file, at the name: an impl's target in a module other than lib.kur,
    # whose methods still reach the type its error names.
    (app("use package::a;\n", {"app/a.kur": "struct L! { x: u32 }\nimpl L { fn get(self) -> u32 { 0u32 } }\n"
                                            "fn f(l: L!) -> u32 { L!::get(l) }\n"}), "app"),
    (app("struct P { x: u32 }\nimpl (u32, P) { fn f(self) -> u32 { 0u32 } }\nimpl P { fn g(self) -> u32 { 0u32 } }\n"
         "impl P { fn g(self) -> u32 { 1u32 } }\n"), "app"),
    (app("", {"app/kur.toml": manifest("app", ("lib",), "package::Nope"), "app/b.kur": "fn f() -> u32 { g() }\n"}), "app"),
    (app("use package::b;\nuse builtin::fresh;\n", {"app/b.kur": "use lib::{shapes::Point as P!, shapes as Copy};\n"}), "app"),
    # Resolution reports every error, in source order: the modules in order, each by position.
    (app("use package::a;\nfn f(x: Nothing) -> u32 { nothing() }\nfn g() -> Other { 0u32 }\n",
         {"app/a.kur": "struct L! { x: u32 }\nstruct S { l: L, m: Missing }\n"
                       "forall T where Nope[T], Copy[T]. fn h(l: L) -> u32 { L::x + q + p::r }\n"
                       "fn k(p: lib::shapes::Point) -> u32 { match p { lib::shapes::make(a) => 0u32 } }\n"}), "app"),
    (app("use lib::shapes::{Point, make};\nfn f() -> u32 { Point.{ x, y: lib::shapes } + make.{ } + u32::nope() }\n"
         "fn g(x: builtin, y: make) -> lib::shapes { () }\nfn h() -> u32 { lib::hidden::secret() }\n"), "app"),
    (app("".join(f"fn f{i}() -> u32 {{ g{i}() }}\n" for i in range(21))), "app"),
    (app("".join(f"fn f{i}() -> u32 {{ g{i}() + g{i}() }}\n" for i in range(12))), "app"),
    # A `use` that imports the other spelling of a type: the hint names it.
    (app("use package::a::X;\nfn f(x: X!) -> u32 { X!::V }\nfn g(x: X) -> u32 { 0u32 }\n", {"app/a.kur": "enum X! { V }\n"}), "app"),
    (app("use package::a::{\n    Y,\n    P!,\n};\nfn f(p: P) -> P! { p }\n", {"app/a.kur": "struct P { x: u32 }\nfn Y() {}\n"}), "app"),
    (app("use package::a::X as Z;\nfn f(x: Z!) -> u32 { 0u32 }\n", {"app/a.kur": "enum X! { V }\n"}), "app"),
    (app("use lib::shapes::{Point as Q!, make as Copy};\nfn f(q: Q!) -> u32 { 0u32 }\n"), "app"),
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
         {"app/kur.toml": manifest("app").replace('"', "'") + "kind = \"executable\"\nentry = \"package::run::main\"\n"
                          "[dependencies.lib]\npath = '../lib'\n",
          "app/run.kur": "fn main() -> i32 { 0i32 }\n"}), "app"),
    # Every reference to a linear type spells its `!`, and so does an alias of it.
    (app("use package::a::{L!, E! as F!};\nfn f(l: L!) -> L! { let { x } = l; L!::make(x) }\n"
         "fn g(e: F!) -> u32 { match e { F!::A => 0u32, F!::B(l) => l.get() } }\n",
         {"app/a.kur": "exposed struct L! { x: u32 }\nenum E! { A, B(L!) }\n"
                       "impl L! { fn make(x: u32) -> L! { L!.{ x } } fn get(self) -> u32 { self.x } }\n"}), "app"),
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
    "const AG: usize = !0usize >> 1;\nconst AH: u32 = (AG as u32) + 1;\nconst AI: bool = AG > 3 && !(U == 3);\n"
    "const AJ: isize = -(AG as isize) / 2isize - 1;\nconst AK: bool = AI || true;\nconst AL: u8 = 300u32 as usize as u8;\n"
    "struct Arr { a: [u8; U], b: [u8; 2u64 * 3], c: [u8; 2u8] }\n",
    "forall T where Copy[T]. trait Dup { fn dup(x: T) -> (T, T); }\ntrait Show { fn show(&x: Self) -> u32; }\n"
    "instance Dup[u32] { fn dup(x: u32) -> (u32, u32) { (x, x) } }\n"
    "forall T where Exposed[T], Show[T]. instance Show[(T, T)] { fn show(&x: (T, T)) -> u32 { 0u32 } }\n"
    "forall 'k. exposed struct Tag! { n: u32 }\nforall T, 'a. struct P { p: *T, w: core::cap::Brand['a], e: exists 'b. core::cap::Brand['b] }\n"
    "forall T. impl P[T, 'static] { fn get(self, f: closure(u32) -> u32, g: closure[u8](&_: u32) -> u32) -> u32 { 0u32 } }\n"
    "forall T where Copy[T]. fn pair(x: T, h: unsafe fn(&_: T) -> !, k: fn(core::cap::Brand['static]) -> u32) -> (T, T) { (x, x) }\n"
    "extern \"C\" fn puts(s: *u8) -> i32;\nenum E { A, B(u32), C(u8, u16) }\ntuple Pt(u32, i64);\n"
    "fn h(g: fn(closure(u32) -> u32) -> u32, k: closure(u8) -> u8) -> u32 { 0u32 }\n",
    # A generic declaration linear only through its arguments has no `!`; one with a `!` is linear even
    # when every field is Copy.
    "forall T. enum Opt { None, Some(T) }\nstruct L! { x: u32 }\nstruct H! { o: Opt[L!], p: *L! }\n"
    "forall 'k. struct Cap! {}\nforall T. struct W { w: Opt[T] }\nfn f(o: Opt[L!], w: W[u32]) -> Opt[L!] { o }\n",
    # A foreach type is a whole parameter's or result's type, in a function type's too.
    "struct L { next: *L, v: u32 }\nfn m(f: foreach *F of *L => F) -> foreach *F of *L => F { f }\n"
    "forall T where Exposed[T]. fn g(x: foreach X of T => *X, k: fn(foreach X of (u32, u8) => X) -> u32) -> u32 { 0u32 }\n",
)

SIGNATURE_ERRORS = (
    # several errors, reported together in source order
    "fn f(x: u32[u8]) -> u32 { 0u32 }\nstruct S { a: u32 }\n"
    "fn g(y: core::cap::Brand[u32]) -> u32 { 0u32 }\nforall T. fn h(x: T[u32]) -> u32 { 0u32 }\n",
    "trait Show { fn show(x: Self) -> u32; }\n"
    "instance Show[u32] { fn show(x: u32) -> u32 { x } fn extra(x: u32) -> u32 { x } }\n"
    "instance Show[u8] { fn show(x: u8) -> u8 { x } }\n",
    "fn f(x: u32[u8]) -> u32 { 0u32 }\nstruct V { d: Never[u32] }\nstruct U { c: u32[u8] }\n",
    "struct U { c: u32[u8] }\nconst A: u32 = true;\nstruct S { a: [u8; A] }\nstruct T { b: [u8; A] }\n",
    "struct S { a: [u8; -1i32] }\n",
    "use std::slice::Slice;\nconst T: u32 = \"text\";\n",
    "const N: i64 = 0i64 - 2i64;\nstruct S { a: [u8; N] }\n",
    "const U: usize = 3;\nstruct S { a: [u8; 2 * U] }\n",
    "const U: usize = 1usize / 0usize;\n",
    # an exists type's binders are distinct and shadow no brand in scope
    "forall 'k. fn f(x: exists 'k. core::cap::Brand['k]) -> u32 { 0u32 }\n",
    "struct S { p: exists 'a. (core::cap::Brand['a], exists 'a. core::cap::Brand['a]) }\n",
    "struct S { p: exists 'a, 'a. core::cap::Brand['a] }\n",
    "struct S { p: exists 'static. core::cap::Brand['static] }\n",
    "struct S { a: Phantom }\n",
    "struct S { a: builtin::Foo }\n",
    "forall T. struct S { a: T[u32] }\n",
    "struct S { a: u32[u8] }\n",
    "struct S { a: core::cap::Brand[u32] }\n",
    "struct S { a: Never[u32] }\n",
    "forall T. struct B { a: T }\nstruct S { b: B }\n",
    "forall 'a. struct B { x: core::cap::Brand['a] }\nstruct S { b: B[u32] }\n",
    "struct S { a: core::cap::Brand['k] }\n",
    "struct S { f: fn(closure(u32) -> u32) -> u32 }\n",
    "forall T. struct B { a: *T }\nstruct S { b: B[_] }\n",
    "forall T where Copy[(T, T)]. fn f(x: T) {}\n",
    "forall T where Copy[u32]. fn f(x: T) {}\n",
    "forall T where Copy[T]. struct B { a: T }\nstruct L! { x: u32 }\nstruct S { b: B[L] }\n",
    "forall T where Copy[T]. struct B { a: T }\nstruct L! { x: u32 }\nfn f(b: B[L]) {}\n",
    "trait Show { fn show(x: Self) -> u32; }\nforall T where Show[T]. trait Bad { fn bad(x: T) -> u32; }\n",
    "trait Show { fn show(x: Self, f: closure(u32) -> u32) -> u32; }\n",
    'extern "C" fn take(f: closure(u32) -> u32) -> u32;\n',
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
    # An enum has at least one variant.
    "enum E {}\n",
    # `Sized` is a premise of functions and instances only.
    "forall T where Sized[T]. struct S { t: T }\n",
    # A declaration linear for every instantiation carries the `!`.
    "struct L! { x: u32 }\nstruct S { l: L! }\n",
    "struct L! { x: u32 }\nforall T. struct G { t: T, l: L! }\n",
    "struct L! { x: u32 }\nforall 'k. enum E { A, B(L!, core::cap::Brand['k]) }\n",
    "struct L! { x: u32 }\ntuple P(u32, (L!, u8));\n",
    "forall T. enum Opt { None, Some(T) }\nstruct L! { x: u32 }\nstruct S { o: Opt[L!] }\n",
    "struct S { a: (u32, [u8; 1u8 - 2u8 > 0u8]) }\n",
    # Errors at the type that has them, and a declaration's at its name, naming what holds the linear type.
    "struct L! { x: u32 }\nexposed struct S {\n    a: u32,\n    l: L!,\n}\n",
    "struct L! { x: u32 }\nforall T. enum E { A, B(u32, L!) }\n",
    "struct L! { x: u32 }\nexposed tuple P(u32, L!);\n",
    "struct S {\n    a: u32,\n    b: u32[u8],\n}\n",
    "forall T where Copy[T]. struct B { a: T }\nstruct L! { x: u32 }\nstruct S { b: B[L!] }\n",
    "forall T where Copy[T]. struct B { a: T }\nstruct L! { x: u32 }\nfn f(b: B[L!]) {}\n",
    "forall T. struct B { a: T }\nfn f(b: B[u32, u8]) {}\n",
    "trait Show { fn show(x: Self) -> u32; }\nforall T where Show[T, T]. fn f(x: T) {}\n",
    "forall T. struct R { r: R[(T, T)] }\n",
    # A foreach type is flat: no component, pointee, type argument, or foreach source or body.
    "struct T { f: foreach *F of *T => F }\n",
    "tuple P(u32, foreach X of (u32,) => X);\n",
    "enum E { A(foreach X of (u32,) => X), B }\n",
    "fn f(p: *foreach X of (u32,) => X) {}\n",
    "forall T. struct B { b: *T }\nfn f(b: B[foreach X of (u32,) => X]) {}\n",
    "forall T where Exposed[foreach X of T => X]. fn f(x: T) {}\n",
    "fn f(x: foreach X of (foreach Y of (u32,) => Y) => X) {}\n",
    "fn f(x: foreach X of (u32,) => foreach Y of (X,) => Y) {}\n",
    "fn f(x: exists 'k. foreach X of (core::cap::Brand['k],) => X) {}\n",
)

# The examples in examples/bad/ that resolution rejects.
UNRESOLVED = {"private_import.kur", "unused_unknown_import.kur", "glob_of_unknown_module.kur",
              "unknown_name_in_use_tree.kur", "constructor_call.kur"}

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
    # `\x` takes two hex digits; in a string, which is UTF-8, they are at most 7f.
    "fn f() { let s = \"A\\xZZB\"; }",
    "fn f() { let s = f\"{1u32}\\x4\"; }",
    "fn f() { let s = \"\\x80\"; }",
    "fn f() { let b = b'\\x'; }",
    "fn f() { let b = b'\u00e9'; }",
    "extern \"\\xC3\" fn g();",
    # Nesting is limited; a chain of binary operators is not nesting.
    "fn f() -> i32 { " + "-" * 129 + "1i32 }",
    "fn f(p: " + "*" * 129 + "u8) {}",
    "fn f() -> i32 { x" + ".g()" * 65 + " }",
    "fn f() -> i32 { 0i32" + " as i32" * 129 + " }",
    "fn f() -> i32 { " + " else ".join(["if c { 0i32 }"] * 129) + " else { 1i32 } }",
    "fn f() { let x = 1u32" + " + 1u32" * 3000 + " # }",
    "fn f() -> u32 { p.0.1 }",
    "fn f() -> u64 { 18446744073709551616 }",
    # Only a type's name ends with `!`.
    "fn f!() {}",
    "fn f() { let x! = 1u32; }",
    "forall T!. fn f(x: T!) {}",
    "struct S { a!: u32 }",
    "enum E { A! }",
    "fn f(&x!: u32) {}",
    "fn f() -> u32 { p.x! }",
    "fn f() -> bool { a!b }",
    # `forall` quantifies a declaration, never a type: a function type quantifies nothing.
    "fn f(k: forall 'q. fn(core::cap::Brand['q]) -> u32) {}",
    "struct S { f: forall 'a. u32 }",
    # A `return` at the end of a block ends with `;`.
    "fn f() -> u32 { return 1u32 }",
    # `module` is reserved, and `unsafe` marks only a function.
    "module x;",
    "unsafe instance Send[u32] {}",
)

KURS_ERRORS = (
    "garbage",
    "(fun main i32 () garbage)",
    # A function has an entry block, and a generic one's are inside its forall.
    "(fun g u32 ())",
    "(fun g (forall (T) () T (T))\n  (entry ((%x T)) (ret %x)))",
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
    # A function type quantifies nothing, and `fn` names every brand of its function.
    "(type T (fn (forall ($k) () u32 ((brand $k)))))",
    "(type T (fn (unsafe) (forall () () u32 ())))",
    "(type T (tuple _))",
    "(fun f (forall ($k) () u32 () (entry () (= %n (const u32 0)) (ret %n))))\n"
    "(fun main u32 () (entry () (= %p (fn (f _))) (= %r (const u32 0)) (ret %r)))",
    "(fun f u32 () (entry () (opened &p) (ret %x)))",
    "(type T u32",
    ")",
    '(data d u8 "\\q")',
    '(data d u8 "abc',
    "(fun f u32 () (entry () (= %x (const u32 1)) ((ret) %x)))",
    "(type T (exists ((a \"b c\")) u32))",
    "(type T (array u8 x))",
    "(type T (array u8 18446744073709551616))",
    # There is no extern value.
    "(extern value errno i32)",
    "(extern fun f u32)",
    # A \x escape is two hex digits of an ASCII character.
    '(data d (array u8 1) ("\\xzz"))',
    '(data d (array u8 1) ("\\x4"))',
    '(data d (array u8 1) ("\\x80"))',
    "(" * 300,
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
               *(ROOT / "examples").rglob("*.toml"), *(ROOT / "reference-compiler/tests/fixtures").rglob("*.kur"),
               *(ROOT / "reference-compiler/tests/fixtures").rglob("*.toml"), *(ROOT / "reference-compiler/kursal").glob("*.py")]

    def make() -> list:
        with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            return [p for p in pool.map(serialized, packages()) if p is not None]

    return cached("lowered", digest_of(sources), make)


# The modules recording leaves out: this one, which builds the cases from the others, the ones that check
# the repository's own packages (which the lowering cases leave out), and the parallel runner's.
UNRECORDED = ("test_compiler", "test_examples", "test_library", "test_kurs", "test_parallel_runner",
              "test_place_translation")


def recorded(module_name: str) -> tuple[list[str], list[tuple[dict[str, str], str]]]:
    """What a test module's tests check and lower, from one run of them with `parse_program`,
    `check_program` and `lower` watched: the core programs it checks, as KURS (a program checked as it was
    read is its own text, so positions stay as the test wrote them; any other is written out), and the
    packages it lowers outside the repository, each as the files of the directory holding the packages
    lowered, by relative path, and the path lowered."""
    sys.setrecursionlimit(20000)
    module = importlib.import_module(module_name)
    texts: dict[int, str] = {}
    programs: list[str] = []
    packages: list[tuple[dict[str, str], str]] = []
    real_parse, real_check = module.__dict__.get("parse_program"), module.__dict__.get("check_program")
    real_lower = lower_module.lower

    def parse(text, *args, **kwargs):
        program = real_parse(text, *args, **kwargs)
        texts[id(program)] = text
        return program

    def check(program, *args, **kwargs):
        try:
            written = write_program(program)
            original = texts.get(id(program))
            programs.append(original if original is not None and write_program(parse_program(original)) == written
                            else written)
        except Exception:  # noqa: BLE001 - a program the writer cannot write is not recorded
            pass
        return real_check(program, *args, **kwargs)

    def watched(resolver, *args, **kwargs):
        try:
            roots = [p.root for p in resolver.packages if not p.root.is_relative_to(LIB)]
            first = resolver.packages[0]
            single = not (first.root / "kur.toml").exists()
            target = next(iter(first.files.values())) if single else first.root
            base = Path(os.path.commonpath(roots))
            files = [f for f in base.rglob("*") if f.is_file() and f.suffix in (".kur", ".toml")]
            if not base.is_relative_to(ROOT) and len(files) <= 20:
                packages.append(({str(f.relative_to(base)): f.read_text() for f in files}, str(target.relative_to(base))))
        except Exception:  # noqa: BLE001 - a package that cannot be recorded is left out
            pass
        return real_lower(resolver, *args, **kwargs)

    if real_parse is not None:
        module.parse_program = parse
    if real_check is not None:
        module.check_program = check
    patched = [m for name, m in list(sys.modules.items())
               if (name.startswith("tests.") or name == module_name) and getattr(m, "lower", None) is real_lower]
    for m in patched:
        m.lower = watched
    try:
        unittest.TestLoader().loadTestsFromModule(module).run(unittest.TestResult())
    finally:
        for m in patched:
            m.lower = real_lower
    return programs, packages


RECORDED_SOURCES = [*TESTS.glob("*.py"), *(ROOT / "reference-compiler/kursal").glob("*.py"), *(ROOT / "lib").rglob("*.kur")]


def recordings() -> list:
    """`recorded` for every test module but the `UNRECORDED`, computed once per change to the sources: the
    distinct core programs, then every package lowered."""
    modules = sorted(path.stem for path in TESTS.glob("test_*.py") if path.stem not in UNRECORDED)

    def make() -> list:
        with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            found = list(pool.map(recorded, [f"tests.{module}" for module in modules]))
        return [list(dict.fromkeys(text for programs, _ in found for text in programs)),
                [package for _, packages in found for package in packages]]

    return cached("recorded", digest_of(RECORDED_SOURCES), make)


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
        if str(error).startswith("type formation: negative array length"):
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
    def make() -> list:
        texts = recordings()[0]
        with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            return [case for case in pool.map(checked_case, texts, chunksize=16) if case is not None]

    return cached("core", digest_of(RECORDED_SOURCES), make)


# Cases for `kursal selftest-types`; compiler/types/selftest.kur describes their forms.
TYPE_CASES = (
    # show
    "(show (tuple))",
    "(show (tuple (struct) (enum (A u32)) (exists ($a $b) (brand $a)) (array u8 3)))",
    "(show (foreach (F (tuple u8 u16)) (tuple F (ptr F))))",
    "(show (foreach ((ptr F) (ptr (tuple u8))) F))",
    "(show (fn (unsafe) ((Pair u32 $a) u8) !))",
    # subst: capture avoidance, numbering, a foreach binder
    "(subst (exists ($a) (tuple $a $b)) ((b $a)))",
    "(subst (fn ($a) $b) ((b $c) (a $d)))",
    "(subst (tuple (exists ($a) (tuple $a $b)) (exists ($a#7) (tuple $a#7 $b))) ((b (tuple $a $a#7))))",
    "(subst (exists ($a) $a) ((a $z)))",
    "(subst (T T) ((T u8)))",
    # apart: a package binder spelled as a brand in scope, or as an enclosing binder, is renamed
    "(apart (exists ($k) (brand $k)) (k))",
    "(apart (tuple (exists ($k $a) (tuple (brand $k) (brand $a))) (exists ($k#0) (brand $k#0))) (k k#0))",
    "(apart (exists ($a) (tuple (brand $a) (exists ($a) (brand $a)))) ())",
    "(apart (exists ($a) (tuple (brand $a) (brand $b))) (b))",
    "(subst (foreach (F (tuple T T)) (tuple F U)) ((T u8) (U F)))",
    "(subst (foreach (F (tuple T)) (tuple F T)) ((T (tuple F)) (F u8)))",
    # free names
    "(free (foreach (F (tuple T $k)) (tuple F G)))",
    "(free (fn ($b T) $a))",
    "(brands (tuple $a (exists ($b) (tuple $b $c)) (P $d)))",
    "(extractable (tuple T (exists ($a) U) (fn (W) X) (foreach (F Y) Z) $k))",
    # equality modulo bound names
    "(equal (exists ($a) (brand $a)) (exists ($b) (brand $b)))",
    "(equal (exists ($a $b) (tuple $a $b)) (exists ($b $a) (tuple $a $b)))",
    "(equal (exists ($a) (tuple $a $b)) (exists ($b) (tuple $b $b)))",
    "(equal (foreach (F (tuple u8)) F) (foreach (G (tuple u8)) G))",
    "(equal (foreach (F (tuple u8)) (tuple F G)) (foreach (G (tuple u8)) (tuple G G)))",
    "(equal (fn ((brand $p)) u8) (fn ((brand $q)) u8))",
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
    "(unify-closed ((a type)) (fn ((brand $p)) ?a) (fn ((brand $p)) u8))",
    "(unify-closed ((k brand)) (tuple (exists ($x) (tuple $x $rigid#0)) (exists ($x) (tuple $x $?k))) "
    "(tuple (exists ($y) (tuple $y $rigid#0)) (exists ($y) (tuple $y $y))))",
    "(unify ((k brand)) (exists ($x) (tuple $x $?k $x)) (exists ($y) (tuple $y $z $y)))",
    "(unify () (exists ($x) (tuple $x $x)) (exists ($y) (tuple $y $x)))",
    "(unify ((k brand) (a type)) (tuple ?a (exists ($x) (tuple $x $?k u8))) (tuple u16 (exists ($y) (tuple $y $y u16))))",
    "(unify ((a type)) (fn ((brand $p)) ?a) (fn ((brand $q)) (brand $q)))",
    "(unify ((a type)) (foreach (F (tuple ?a)) (ptr F)) (foreach (G (tuple u8)) (ptr G)))",
    "(unify ((a type)) (foreach (F (tuple u8)) ?a) (foreach (G (tuple u8)) (ptr G)))",
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
    "(copy ((type R! () u8)) () (tuple R!))",
    "(copy () () (foreach (F (tuple u8)) (ptr F)))",
    "(copy () () (foreach (F (tuple u8)) u16))",
    "(copy ((type L! () u8)) () (foreach (F (tuple u8 L!)) F))",
    "(copy ((type L! () u8)) () (foreach (F (tuple u8 L!)) (ptr F)))",
    "(copy ((type L! () u8)) () (foreach (F (array L! 0)) F))",
    "(copy ((type P (T) (struct (a T) (b u8)))) ((T any)) (foreach (F (P T)) (ptr F)))",
    "(copy () ((T copy)) (foreach (F (tuple T u8)) F))",
    "(copy () ((T copy)) (foreach (F T) (ptr F)))",
    "(copy () ((T copy)) (foreach (F (ptr T)) (ptr F)))",
    "(copy () ((T copy)) (foreach (F T) u8))",
    # a foreach holding itself again by value: an active query cannot justify itself
    "(copy ((type T () (struct (f (foreach ((ptr F) (ptr T)) F))))) () T)",
    "(copy () () (foreach (F G) (ptr F)))",
    "(zero ((type E () (struct (a (tuple)) (b (array u8 0))))) E)",
    "(zero () T)",
    "(zero () (exists ($a) (brand $a)))",
    "(expand ((type P (T) (struct (a T) (b u8)))) (foreach (F (P u16)) (ptr F)))",
    "(expand () (foreach ((ptr F) (ptr (tuple u8 u16))) (array F 2)))",
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
    "(pattern () (foreach ((ptr F) (tuple (ptr u8) (ptr u16))) F))",
    "(pattern () (foreach ((ptr F) (tuple (ptr u8) u16)) F))",
    "(pattern ((opaque H () (tuple (ptr u8)))) (foreach ((ptr F) H) F))",
    "(pattern ((type H () (tuple (ptr u8)))) (foreach ((ptr F) H) F))",
    "(pattern () (foreach ((ptr F) (array (ptr u8) 2)) F))",
    # layout
    "(layout (type L (T) (enum (Nil (tuple)) (Cons (ptr (L T))))))",
    "(layout (type L () (struct (next L))))",
    "(layout (type A () (ptr A)))",
    "(layout (type G (T) (enum (Leaf T) (Node (ptr (G (tuple T T)))))))",
    "(layout (type H (T) (foreach (F T) (ptr (H F)))))",
    "(layout (type S () (foreach (F S) F)))",
    "(layout (type A () (struct (b (ptr B)))) (type B () (enum (x A) (y (array B 0)))))",
    "(layout (type A ($k) (struct (b (brand $k)) (n (ptr (A $k))))))",
    # a foreach whose components are known is walked as its expansion
    "(layout (type T () (foreach ((ptr F) (ptr T)) F)))",
    "(layout (type S (P) (foreach ((ptr F) (ptr P)) (tuple F))) (type W () (S W)))",
    "(layout (type L () (struct (next (ptr L)) (v u32))) (type M () (foreach ((ptr F) (ptr L)) F)))",
    "(layout (type E () (enum (A (ptr T)) (B (ptr u32)))) (type T () (foreach ((ptr F) E) F)))",
    "(layout (type T () (struct (f (fn ((foreach ((ptr F) (ptr T)) (ptr F))) u32)))))",
    # formation
    "(formation (type P (T $k) (struct (a T) (b (brand $k)))))",
    "(formation (type E () (exists ($a) u8)))",
    "(formation (type E () (exists ($a) (exists ($b) (tuple $a $b)))))",
    "(formation (type E () (exists () u8)))",
    "(formation (type B () (brand $z)))",
    "(formation (type R () (foreach (G (foreach (F (tuple u8)) F)) G)))",
    "(formation (type R () (foreach (G (tuple u8)) (foreach (F (tuple G)) F))))",
    "(formation (type W () (foreach (F (tuple u8)) F)) (type P () (struct (w W))))",
    "(formation (type W () (foreach (F (tuple u8)) F)) (type V () W) (type P () (tuple u8 V)))",
    "(formation (type W () (foreach (F (tuple u8)) F)) (type O (T) (enum (S T))) (type P () (O W)))",
    "(formation (type W () (foreach (F (tuple u8)) F)) (type P () (ptr W)))",
    "(formation (type W () (foreach (F (tuple u8)) F)) (type P () (exists ($k) W)))",
    "(formation (type W () (foreach (F (tuple u8)) F)) (type P () (fn (W) W)))",
    "(formation (type D () (struct (a u8) (a u16))))",
    "(formation (type V (T) (T u8)))",
    "(formation (type U () Nope))",
    "(formation (type W (T) u8) (type X () (W)))",
    "(formation (type K ($k) u8) (type X () (K u8)))",
    "(formation (type K (T) u8) (type X () (K $static)))",
    "(formation (type C (+T) u8) (type L! () u8) (type X () (C L!)))",
    "(formation (type C (+T) u8) (type X () (C (ptr u8))))",
    "(formation (type L! () u8) (type S () (tuple u8 L!)))",
    "(formation (type L! () u8) (type O (T) (enum (N (tuple)) (S T))) (type S () (O L!)))",
    "(formation (type L! () u8) (type G (T) (tuple T L!)))",
    "(formation (type L! () u8) (type G ($k) (tuple (brand $k) L!)))",
    "(formation (type L! () u8) (type O (T) (enum (N (tuple)) (S T))) (type H! () (O L!)) (type C! ($k) u8) (type W (T) (O T)))",
    "(formation (type L! () u8) (type F () (foreach (X (tuple u32 L!)) (ptr X))))",
    "(formation (type L! () u8) (type F () (foreach (X (tuple u32 L!)) X)))",
    "(formation (type G (T) (foreach (X T) (ptr X))))",
    "(formation (type G! (T) (foreach (X T) (ptr X))))",
    "(formation (type S ($static) u8))",
    "(formation (type D (T T) u8))",
    "(formation (type E ($k) (exists ($k) (brand $k))))",
    "(formation (type E () (exists ($k) (tuple (brand $k) (exists ($k) (brand $k))))))",
    "(formation (type E () (exists ($k $k) (brand $k))))",
    "(formation (type E () (exists ($static) (brand $static))))",
    "(formation (type E ($k) (tuple (brand $k) (exists ($j) (brand $j)))))",
    "(kind () ((k brand)) $k)",
    "(kind () ((k brand)) (tuple u8 (fn ((brand $k)) u8)))",
    "(kind () () (tuple u8 (fn ((brand $a)) u8)))",
    "(kind () () $k)",
    "(function () (f (T $k) (call f u8 $k) (fn f u16 $k)))",
    "(function () (f (T $k) (fn f u16 $q)))",
    "(function () (f (T $k)\n  (call f u8)))",
    "(function ((type B ($k) u8)) (f ($k) (call f $k) (alloca x (B $k))))",
    "(function ((type B ($k) u8)) (f () (alloca x (B $q))))",
    "(function () (g ($b T)) (f () (foreach g (foreach (F (tuple u8)) F) u8)))",
    "(function () (f (T T)))",
    "(function () (f ($k) (alloca x (exists ($k) (brand $k)))))",
    "(function () (f ($k) (alloca x (exists ($j) (tuple (brand $j) (brand $k))))))",
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
    types = {str(d[1]): C.TypeDecl(str(d[1]), _params(d[2]), (), parse_type(d[3]),
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
    if op == "apart":
        return T.show(T.apart(parse_type(form[1]), {str(n) for n in form[2]}))
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
        ty = parse_type(form[2])
        assert isinstance(ty, C.Foreach)
        return str(T.Types(_program(form[1])).pattern_available(ty)).lower()
    raise ValueError(f"unknown case {op}")


class CompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.binary = build_compiler()

    def run_compiler(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run_kursal(self.binary, *args)

    def test_public_cli(self):
        for args, text in [(("--help",), "Commands:"), (("build", "--help"), "--emit-c"),
                           (("--version",), "kursal 0.1.0")]:
            with self.subTest(args=args):
                result = self.run_compiler(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(text, result.stdout)
                self.assertEqual(result.stderr, "")

        with tempfile.TemporaryDirectory() as directory:
            # a file an earlier build left, which this one does not write
            (Path(directory) / "a.out.build").mkdir()
            (Path(directory) / "a.out.build/stale.c").write_text("")
            result = subprocess.run([str(self.binary), "build", str(ROOT / "examples/hello")], cwd=directory,
                                    capture_output=True, text=True, timeout=TIME_LIMIT,
                                    preexec_fn=limit_memory, env=dict(os.environ, KURSAL_LIB=str(LIB)))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertTrue((Path(directory) / "a.out").is_file())
            self.assertTrue((Path(directory) / "a.out.build/kursal-decls.h").is_file())
            self.assertFalse((Path(directory) / "a.out.c").exists())
            self.assertFalse((Path(directory) / "a.out.build/stale.c").exists())
            running = subprocess.run([str(Path(directory) / "a.out")], capture_output=True, text=True)
            self.assertEqual((running.returncode, running.stdout), (0, "Hello, world!\n"))

        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([str(self.binary), "build", str(ROOT / "examples/hello"), "--single-c"], cwd=directory,
                                    capture_output=True, text=True, timeout=TIME_LIMIT,
                                    preexec_fn=limit_memory, env=dict(os.environ, KURSAL_LIB=str(LIB)))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(directory) / "a.out.c").is_file())
            self.assertFalse((Path(directory) / "a.out.build").exists())
            running = subprocess.run([str(Path(directory) / "a.out")], capture_output=True, text=True)
            self.assertEqual((running.returncode, running.stdout), (0, "Hello, world!\n"))

        bad = subprocess.run([str(self.binary), "build", str(ROOT / "examples/bad/type_mismatch.kur")],
                             capture_output=True, text=True, timeout=TIME_LIMIT,
                             env=dict(os.environ, KURSAL_LIB=str(LIB)))
        self.assertEqual(bad.returncode, 1)
        self.assertIn("type_mismatch.kur:6:22: type mismatch", bad.stderr)
        self.assertIn("let total: u32 = items > 0u32;", bad.stderr)
        self.assertIn("^", bad.stderr)
        short = self.run_compiler("build", str(ROOT / "examples/bad/type_mismatch.kur"), "--emit-c")
        self.assertEqual(short.returncode, 1)
        self.assertEqual(short.stderr.count("\n"), 1)
        self.assertIn("type_mismatch.kur:6:22: type mismatch", short.stderr)

    def test_misused_command_lines(self):
        """A command line no command takes: its usage on stderr, with status 2."""
        build = "usage: kursal build PATH [-o OUTPUT] [--emit-c] [--single-c] [--times]\n"
        cases = [((), "Kursal compiler\n\n" + build + "       kursal COMMAND --help\n\nRun kursal --help for commands.\n"),
                 (("frob",), "unknown command frob\nRun kursal --help for commands.\n"),
                 (("frob", "--help"), "unknown command frob\nRun kursal --help for commands.\n"),
                 (("parse",), "usage: kursal parse FILE...\n"),
                 (("build",), build),
                 (("build", "--bogus"), "unknown option --bogus\n" + build),
                 (("build", "a", "b"), build),
                 (("build", "a", "-o"), build),
                 (("build", "a", "--emit-c", "-o", "b"), "--emit-c and -o cannot be used together\n" + build),
                 (("c",), "usage: kursal c FILE [--entry NAME]\n"),
                 (("c", "a", "--entry"), "usage: kursal c FILE [--entry NAME]\n"),
                 (("c", "-x"), "unknown option -x\nusage: kursal c FILE [--entry NAME]\n"),
                 (("doc", "a"), "usage: kursal doc PATH -o DIR\n")]
        for command in ("resolve", "sigs", "infer", "lower"):
            cases += [((command,), f"usage: kursal {command} PATH\n"), ((command, "a", "b"), f"usage: kursal {command} PATH\n")]
        for command in ("kurs", "check", "readable"):
            cases += [((command,), f"usage: kursal {command} FILE\n"),
                      ((command, "--fast"), f"unknown option --fast\nusage: kursal {command} FILE\n")]
        for args, stderr in cases:
            with self.subTest(args=args):
                result = self.run_compiler(*args)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (2, "", stderr))

    def test_help_lists_the_commands_in_columns(self):
        listed = self.run_compiler("--help").stdout.partition("Commands:\n")[2].partition("\n\n")[0].splitlines()
        self.assertEqual(len(listed), 11)
        self.assertEqual({len(line) - len(line[2:].split("  ")[-1].lstrip()) for line in listed}, {25})

    def test_kurs_files_are_read_as_the_reference_reads_them(self):
        """`kursal check FILE` and `python -m kursal FILE`: nothing for a sound program; the same message, with
        status 1, for a file neither can read, one that is not UTF-8, and one each rejects."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "sound.kurs").write_text("(fun f u32 () (entry () (= %r (const u32 0)) (ret %r)))")
            (base / "unsound.kurs").write_text("(fun f u32 () (entry () (ret %r)))")
            (base / "latin1.kurs").write_bytes(b'(data d (array u8 1) ("\xe9"))')
            (base / "deep.kurs").write_text("(" * 100_000 + ")" * 100_000)
            for name in ("sound.kurs", "unsound.kurs", "missing.kurs", "latin1.kurs", "deep.kurs"):
                with self.subTest(file=name):
                    ours = self.run_compiler("check", str(base / name))
                    reference = subprocess.run([sys.executable, "-m", "kursal", str(base / name)], capture_output=True,
                                               text=True, timeout=TIME_LIMIT, cwd=ROOT / "reference-compiler")
                    self.assertEqual((ours.returncode, ours.stdout, ours.stderr),
                                     (reference.returncode, reference.stdout, reference.stderr))
                    self.assertEqual(ours.returncode, 0 if name == "sound.kurs" else 1)

    def test_dependency_native_sources_and_link_flags_reach_the_c_compiler(self):
        """A library's C source and link requirement are used by both executable build commands."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            app, dep = base / "app", base / "dep"
            app.mkdir()
            dep.mkdir()
            (app / "kur.toml").write_text('[package]\nname="app"\nsource_root="."\nkind="executable"\n'
                                          'entry="main"\n[dependencies.dep]\npath="../dep"\n')
            (app / "lib.kur").write_text('extern "C" fn native_cos(x: f64) -> f64;\n'
                                         'fn main() -> i32 { if unsafe native_cos(0.0f64) == 1.0f64 { 0i32 } else { 1i32 } }\n')
            (dep / "kur.toml").write_text('[package]\nname="dep"\nsource_root="."\n'
                                          'c_sources=["native.c"]\nlink_flags=["-lm"]\n')
            (dep / "lib.kur").write_text("")
            (dep / "native.c").write_text('#include <math.h>\ndouble native_cos(double x) { return cos(x); }\n')
            ours = self.run_compiler("build", str(app), "-o", str(base / "ours"))
            self.assertEqual(ours.returncode, 0, ours.stderr)
            reference = subprocess.run([sys.executable, str(ROOT / "reference-compiler/tools/build.py"),
                                        str(app), str(base / "reference")], capture_output=True, text=True,
                                       timeout=TIME_LIMIT, env=dict(os.environ, KURSAL_LIB=str(LIB)))
            self.assertEqual(reference.returncode, 0, reference.stderr)
            for name in ("ours", "reference"):
                with self.subTest(builder=name):
                    ran = subprocess.run([str(base / name)], capture_output=True, text=True)
                    self.assertEqual((ran.returncode, ran.stdout), (0, ""))

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
        packages = sorted({p.parent for top in ("examples", "reference-compiler/tests/fixtures", "lib")
                           for p in (ROOT / top).glob("*/kur.toml")})
        singles = sorted((ROOT / "examples/bad").glob("*.kur"))
        self.assertGreater(len(packages), 40)
        for path in [*packages, *singles, COMPILER]:
            with self.subTest(path=str(path.relative_to(ROOT))):
                expected = reference_resolution(path)
                # A rejected program may be rejected here: one in examples/bad/ reaches a private name, or
                # imports one that names nothing.
                self.assertEqual(expected.startswith("error:"), path.name in UNRESOLVED, expected)
                self.assertEqual(self.compiler_resolution(path), expected)

    def run_batches(self, command: str, paths: list[Path]) -> list[str]:
        """`kursal COMMAND PATH` for each path, a batch of processes at a time: its output, or its error."""
        workers = min(8, os.cpu_count() or 1)
        results: list[str] = []
        # Started from this thread, so that `limit_memory` runs in no threaded child.
        for start in range(0, len(paths), workers):
            running = [subprocess.Popen([str(self.binary), command, str(path),
                                         *(["--emit-c"] if command == "build" else [])], stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, preexec_fn=limit_memory,
                                        env=dict(os.environ, KURSAL_LIB=str(LIB), KURSAL_DIAGNOSTICS="short"))
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
        packages = sorted({p.parent for top in ("examples", "reference-compiler/tests/fixtures", "lib")
                           for p in (ROOT / top).glob("*/kur.toml")})
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
                (root / "kur.toml").write_text(manifest("p", needs_core(source)) + std)
                (root / "lib.kur").write_text(source)
                paths.append(root)
            for index, path in enumerate(paths):
                with self.subTest(source=(*SIGNATURES, *SIGNATURE_ERRORS)[index]):
                    expected = reference_signatures(path)
                    self.assertEqual(expected.startswith("error:"), index >= len(SIGNATURES), expected)
                    self.assertNotIn("syntax error", expected)
            self.compare_signatures(paths)

    def test_inference_matches_reference(self):
        packages = sorted({p.parent for top in ("examples", "reference-compiler/tests/fixtures", "lib")
                           for p in (ROOT / top).glob("*/kur.toml")})
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

    def test_entries_match_reference(self):
        """Every entry shape, with and without `Args`, and entries of other shapes: `kursal build` writes the
        reference's C, or fails with its error."""
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index, source in enumerate(ENTRIES):
                root = Path(directory) / f"entry{index}"
                root.mkdir()
                (root / "kur.toml").write_text(manifest("app", entry="main") + f'[dependencies.std]\npath="{LIB / "std"}"\n')
                (root / "lib.kur").write_text(ENTRY_IMPORTS + source)
                paths.append(root)
            expected = [in_deep_thread(lambda: reference_entry(path)) for path in paths]
            self.assertEqual(sum(want.startswith("error:") for want in expected), len(ENTRIES) - 6)
            for path, want, got in zip(paths, expected, self.run_batches("build", paths)):
                with self.subTest(source=(path / "lib.kur").read_text()):
                    self.assertEqual(got, want)

    def test_zero_size_instantiations_match_reference(self):
        """A container of an element type with zero-size parts beside bytes, and the containers at a zero-size
        element type, which `Sized` rejects: `kursal build` writes the reference's C, or its error."""
        sources = (
            "fn main() -> i32 { var v = OwnedVec!::new[((), u8)](); &v.push(((), 1u8)); v.destroy(); 0 }\n",
            "fn main() -> i32 { var m = OwnedMap!::new[u32, ((), u8)](std::process::hash_seed()); let old = &m.insert(1u32, ((), 2u8)); m.destroy(); 0 }\n",
            "fn main() -> i32 { let o = Owned!::new(((), 3u8)); o.into_value(); 0 }\n",
            "forall T where Sized[T]. fn boxed(x: T) -> T { Owned!::new(x).into_value() }\nfn main() -> i32 { boxed(((), 4u8)); 0 }\n",
        )
        rejected = (
            "fn main() -> i32 { var v = OwnedVec!::new[()](); &v.push(()); v.destroy(); 0 }\n",
            "fn main() -> i32 { var m = OwnedMap!::new[u32, ()](std::process::hash_seed()); let old = &m.insert(1u32, ()); m.destroy(); 0 }\n",
            "forall T. fn boxed(x: T) -> T { Owned!::new(x).into_value() }\nfn main() -> i32 { boxed(()); 0 }\n",
        )
        imports = "use std::owned::Owned!;\nuse std::collections::{hashmap::OwnedMap!, vec::OwnedVec!};\n"
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index, source in enumerate((*sources, *rejected)):
                root = Path(directory) / f"zero{index}"
                root.mkdir()
                (root / "kur.toml").write_text(manifest("app", entry="main") + f'[dependencies.std]\npath="{LIB / "std"}"\n')
                (root / "lib.kur").write_text(imports + source)
                paths.append(root)
            expected = [in_deep_thread(lambda: reference_entry(path)) for path in paths]
            for want in expected[:len(sources)]:
                self.assertFalse(want.startswith("error:"), want)
            for want in expected[len(sources):]:
                self.assertIn("Sized requires an exact premise", want)
            for path, want, got in zip(paths, expected, self.run_batches("build", paths)):
                with self.subTest(source=(path / "lib.kur").read_text()):
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
        """The compiler the reference built (kc1) builds compiler/ (kc2, split), which builds it again (kc3,
        split, and once more as one file): kc2's split C and kc3's are the same, and kc3's one file is the
        reference's C for compiler/."""
        expected = reference_builds()[built_paths().index(COMPILER)]["c"]
        kc2 = self_built_compiler(self.binary)
        with tempfile.TemporaryDirectory() as directory:
            kc3 = Path(directory) / "kc3"
            started = time.monotonic()
            result = run_kursal(kc2, "build", str(COMPILER), "-o", str(kc3), "--times")
            self.assertEqual(result.returncode, 0, result.stderr)
            print(f"\nkc3 built in {time.monotonic() - started:.1f}s:\n{result.stderr}", file=sys.stderr)
            single = run_kursal(kc2, "build", str(COMPILER), "-o", str(Path(directory) / "single"), "--single-c")
            self.assertEqual(single.returncode, 0, single.stderr)
            kc2_split, kc3_split = ({p.name: p.read_text() for p in Path(f"{stage}.build").glob("*.[ch]")} for stage in (kc2, kc3))
            single_c = (Path(directory) / "single.c").read_text()
        self.assertEqual(kc2_split, kc3_split)
        self.assertEqual(single_c, expected)

    @unittest.skipUnless(os.environ.get("KURSAL_SELFHOST") == "1", "set KURSAL_SELFHOST=1 to compile the compiler with itself")
    def test_self_build_frees_everything(self):
        """kc2 builds compiler/ under valgrind with no error and every byte freed, and writes the reference's C."""
        unusable = valgrind_unusable()
        if unusable is not None:
            self.skipTest(unusable)
        expected = reference_builds()[built_paths().index(COMPILER)]["c"]
        kc2 = self_built_compiler(self.binary)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "kc3"
            started = time.monotonic()
            result = run_valgrind(kc2, "build", str(COMPILER), "-o", str(output), "--single-c", timeout=6 * 3600)
            print(f"\nkc3 built under valgrind in {time.monotonic() - started:.1f}s:\n"
                  + "\n".join(line for line in result.stderr.splitlines() if "in use at exit" in line or "ERROR SUMMARY" in line),
                  file=sys.stderr)
            self.assertEqual(result.returncode, 0, result.stderr[-4000:])
            self.assertIn("All heap blocks were freed -- no leaks are possible", result.stderr)
            self.assertIn("ERROR SUMMARY: 0 errors from 0 contexts", result.stderr)
            self.assertEqual(Path(f"{output}.c").read_text(), expected)

    def test_build_frees_everything(self):
        """The compiler builds a program under valgrind with no error and every byte freed, and the program
        it builds runs."""
        unusable = valgrind_unusable()
        if unusable is not None:
            self.skipTest(unusable)
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

    @unittest.skipIf(os.geteuid() == 0, "permissions do not bind root")
    def test_a_manifest_that_cannot_be_looked_for_stops_resolution(self):
        """A directory under the source root that can be listed but not searched: whether it holds a kur.toml,
        which would make it another package, is unknown, and both resolvers stop with the same error. One
        that cannot be listed is left out, as before."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            sub = root / "p/sub"
            sub.mkdir(parents=True)
            (root / "p/kur.toml").write_text(manifest("p"))
            (root / "p/lib.kur").write_text("")
            (sub / "kur.toml").write_text(manifest("sub"))
            (sub / "lib.kur").write_text("")
            try:
                sub.chmod(0o444)
                expected = reference_resolution(root / "p")
                self.assertEqual(expected, f"error: cannot read {sub / 'kur.toml'} (error 13)")
                self.assertEqual(self.compiler_resolution(root / "p"), expected)
                sub.chmod(0o000)
                expected = reference_resolution(root / "p")
                self.assertFalse(expected.startswith("error:"), expected)
                self.assertEqual(self.compiler_resolution(root / "p"), expected)
            finally:
                sub.chmod(0o755)

    def test_a_failed_output_write_fails_the_command(self):
        with open("/dev/full", "w") as full:
            result = subprocess.run([str(self.binary), "parse", str(ROOT / "reference-compiler/tests/fixtures/sayhi/lib.kur")],
                                    stdout=full, stderr=subprocess.PIPE, text=True, timeout=TIME_LIMIT,
                                    preexec_fn=limit_memory,
                                    env=dict(os.environ, KURSAL_LIB=str(LIB)))
        self.assertEqual((result.returncode, result.stderr), (1, "cannot write the output\n"))

    def test_a_package_without_an_entry_is_not_built(self):
        """`kursal build` and tools/build.py of a library: status 1 and a message, no traceback."""
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "shelf"
            package.mkdir()
            (package / "kur.toml").write_text(manifest("shelf"))
            (package / "lib.kur").write_text("fn half(n: u32) -> u32 { n / 2u32 }\n")
            ours = self.run_compiler("build", str(package), "-o", str(Path(directory) / "ours"))
            reference = subprocess.run([sys.executable, str(ROOT / "reference-compiler/tools/build.py"), str(package),
                                        str(Path(directory) / "reference")],
                                       capture_output=True, text=True, timeout=TIME_LIMIT, env=dict(os.environ, KURSAL_LIB=str(LIB)))
            self.assertEqual((ours.returncode, ours.stdout, ours.stderr),
                             (1, "", "this package has no executable entry; use --emit-c to print its C\n"))
            self.assertEqual((reference.returncode, reference.stdout, reference.stderr),
                             (1, "", "this package has no executable entry\n"))

    def test_a_failed_c_compiler_is_reported_as_the_reference_reports_it(self):
        """`kursal build` and tools/build.py with a C compiler that cannot run, exits with a failure or is
        killed: status 1, the C compiler's own output, then the same message."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            failing, killed, unrunnable = base / "failing-cc", base / "killed-cc", base / "unrunnable-cc"
            failing.write_text("#!/bin/sh\necho 'cc: broken' >&2\nexit 3\n")
            killed.write_text("#!/bin/sh\nkill -KILL $$\n")
            unrunnable.write_text("not a program\n")
            failing.chmod(0o755)
            killed.chmod(0o755)
            cases = ((base / "missing-cc", f"cannot run the C compiler {base / 'missing-cc'} (error 2)\n"),
                     (unrunnable, f"cannot run the C compiler {unrunnable} (error 13)\n"),
                     (failing, f"cc: broken\nthe C compiler {failing} exited with status 3\n"),
                     (killed, f"the C compiler {killed} was killed by signal 9\n"))
            package = ROOT / "examples/core_only"
            for compiler, message in cases:
                with self.subTest(compiler=compiler.name):
                    env = dict(os.environ, KURSAL_LIB=str(LIB), KURSAL_CC=str(compiler))
                    ours = subprocess.run([str(self.binary), "build", str(package), "-o", str(base / "ours")],
                                          capture_output=True, text=True, timeout=TIME_LIMIT, preexec_fn=limit_memory,
                                          env=env)
                    reference = subprocess.run([sys.executable, str(ROOT / "reference-compiler/tools/build.py"),
                                                str(package), str(base / "reference")],
                                               capture_output=True, text=True, timeout=TIME_LIMIT, env=env)
                    self.assertEqual((ours.returncode, ours.stdout, ours.stderr), (1, "", message))
                    self.assertEqual((reference.returncode, reference.stdout, reference.stderr), (1, "", message))

    def test_doc_writes_comments_and_signatures(self):
        """`kursal doc` on a small package: the `//!` overview, a table of each kind of item with its summary
        line, functions, constants and instances in full on the module's page, a page for each type and
        trait, each declaration with its signature highlighted and the types and traits it names linked, a
        `forall` premise of several lines kept whole, and no plain comment or extern outside the source."""
        with tempfile.TemporaryDirectory() as directory:
            package, src = Path(directory) / "gizmo", Path(directory) / "site"
            package.mkdir()
            (package / "kur.toml").write_text(manifest("gizmo"))
            (package / "lib.kur").write_text(DOC_PACKAGE)
            result = self.run_compiler("doc", str(package), "-o", str(src))
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            self.assertEqual(markdown_link_errors(src), [])
            self.assertEqual(sorted(p.name for p in src.iterdir()), ["gizmo", "index.md", "search-index.json", "src"])
            front, _, _ = (src / "gizmo/index.md").read_text().partition("+++\n<div")
            body = page_body(src / "gizmo/index.md")
            point, measure = page_body(src / "gizmo/struct.Point.md"), page_body(src / "gizmo/trait.Measure.md")
            pages = [p.read_text() for p in src.rglob("*.md") if p.relative_to(src).parts[0] != "src"]
            source = (src / "src/gizmo/lib.kur.md").read_text()
            summaries = {r["p"]: r["s"] for r in json.loads((src / "search-index.json").read_text())}
        self.assertIn('layout = "doc.html"', front)
        for name, section, url in (("Point", "Types", "/gizmo/struct.Point.html"), ("Measure", "Traits", "/gizmo/trait.Measure.html"),
                                   ("first", "Functions", "#fn.first"), ("LIMIT", "Constants", "#const.LIMIT"),
                                   ("Measure[Point]", "Instances", "#instance.Measure-5bPoint-5d")):
            self.assertIn(f'section = "{section}"\nname = "{name}"\nurl = "{url}"', front)
        self.assertIn('[[tree]]\nname = "gizmo"\nurl = "/gizmo/"\ndepth = 0\ncurrent = true\n', front)
        self.assertIn("\nGizmos for testing the documentation site.\nA second line of the overview.\n", body)
        rows = re.findall(r'<tr><td><a class="item-link kind-(\w+)" href="([^"]+)"><code>([^<]+)</code></a></td><td>([^<]*)</td></tr>', body)
        self.assertEqual(rows, [
            ("struct", "struct.Point.html", "Point", "A point on a grid."),
            ("trait", "trait.Measure.html", "Measure", "Things with a size."),
            ("fn", "#fn.first", "first", "The first of two values."),
            ("const", "#const.LIMIT", "LIMIT", "The largest coordinate."),
            ("instance", "#instance.Measure-5bPoint-5d", "Measure[Point]", "A point's size is the sum of its coordinates."),
        ])
        expected = [
            (body, "fn.first", "fn", "The first of two values.",
             "forall T, U\n    where Copy[T],\n          Copy[U].\nfn first(a: T, b: U) -> T { ... }"),
            (body, "const.LIMIT", "const", "The largest coordinate.", "const LIMIT: u32 = 100u32;"),
            (body, "instance.Measure-5bPoint-5d", "instance", "A point's size is the sum of its coordinates.",
             "instance Measure[Point] { ... }"),
            (point, "method.origin", "method", "The point at the origin.", "fn origin() -> Point { ... }"),
            (point, "method.sum", "method", "The sum of both coordinates, e.g. 3 for `(1, 2)`, i.e. `x + y`. It stays\nbelow twice `LIMIT`.",
             "fn sum(self) -> u32 { ... }"),
            (point, "instance.Measure-5bPoint-5d", "instance", "A point's size is the sum of its coordinates.",
             "instance Measure[Point] { ... }"),
            (measure, "method.size", "method", "", "fn size(self: Self) -> u32;"),
            (measure, "instance.Measure-5bPoint-5d", "instance", "A point's size is the sum of its coordinates.",
             "instance Measure[Point] { ... }"),
        ]
        for text, anchor, kind, prose, signature in expected:
            with self.subTest(anchor=anchor, kind=kind):
                self.assertIn(f'<section class="item-entry" id="{anchor}">\n<h3 class="item-heading doc-kind-{kind}"><code>{kind}</code>', text)
                self.assertEqual(signature_after(text, anchor), signature)
                entry = text[text.index(f'id="{anchor}"'):]
                if prose:
                    self.assertIn(f"\n{prose}\n\n</section>", entry[:entry.index("</section>") + 10])
        self.assertEqual(summaries["gizmo::Point::sum"], "The sum of both coordinates, e.g. 3 for (1, 2), i.e. x + y.")
        self.assertEqual(first_signature(point), "exposed struct Point {\n    x: u32,\n    y: u32,\n}")
        self.assertIn("\nA point on a grid.\n", point)
        self.assertIn('<span class="tok-keyword">forall</span> T, U', body)
        self.assertIn('<span class="tok-type">u32</span> = <span class="tok-number">100</span><span class="tok-type">u32</span>;', body)
        self.assertIn('<span class="tok-keyword">instance</span> <a class="sig-link sig-trait" href="trait.Measure.html">Measure</a>'
                      '[<a class="sig-link sig-struct" href="struct.Point.html">Point</a>] { ... }', body)
        self.assertIn('<a class="src-link" href="src/gizmo/lib.kur.html#L43">source</a>', body.replace("../", ""))
        self.assertIn('<span class="line" id="L43"><span class="tok-keyword">forall</span> T, U</span>', source)
        for hidden in ("implementation note", "fn abs", "extern"):
            self.assertFalse(any(hidden in page for page in pages), hidden)

    def test_doc_re_exported_modules_instances_and_search(self):
        """A module one package re-exports from another has its pages under both, with a note on the
        re-exporting package's pointing to the other; a type's page lists its methods wherever they are
        defined and the instances that mention it, a trait's page every instance of it; links from a
        package's pages prefer its own paths; and the search index lists each item once, under the
        documented package's path."""
        with tempfile.TemporaryDirectory() as directory:
            base, src = Path(directory), Path(directory) / "site"
            write_files(base, DOC_PACKAGES)
            result = self.run_compiler("doc", str(base / "gizmo"), "-o", str(src))
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            self.assertEqual(markdown_link_errors(src), [])
            facade = (src / "gizmo/shapes/index.md").read_text()
            own = (src / "gadget/shapes/index.md").read_text()
            circle = page_body(src / "gizmo/shapes/struct.Circle.md")
            side = page_body(src / "gizmo/shapes/enum.Side.md")
            area = page_body(src / "gizmo/shapes/trait.Area.md")
            tools = page_body(src / "gizmo/tools/index.md")
            measures = page_body(src / "gadget/measures/index.md")
            index = json.loads((src / "search-index.json").read_text())
        self.assertIn('title = "gizmo::shapes"', facade)
        self.assertIn('<p class="re-export-note">Re-exported from <a href="../../gadget/shapes/"><code>gadget::shapes</code></a>.</p>', facade)
        self.assertNotIn("re-export-note", own)
        self.assertIn('[[tree]]\nname = "shapes"\nurl = "/gizmo/shapes/"\ndepth = 1\ncurrent = true\n', facade)
        self.assertIn('<td><a class="item-link kind-struct" href="struct.Circle.html"><code>Circle</code></a></td><td>A circle.</td>', facade)
        self.assertIn('<p class="re-export-note">Re-exported from <a href="../../gadget/shapes/struct.Circle.html"><code>gadget::shapes::Circle</code></a>.</p>', circle)
        self.assertEqual(signature_after(circle, "method.unit"), "fn unit() -> Circle { ... }")
        self.assertIn('<p class="item-note">Defined in <a href="../../gadget/measures/"><code>gadget::measures</code></a>.</p>', circle)
        self.assertEqual(signature_after(circle, "instance.Area-5bCircle-5d"), "instance Area[Circle] { ... }")
        self.assertEqual(re.findall(r'id="(instance\.[^"]+)"', side), ["instance.Area-5bSide-5d"])
        self.assertEqual(re.findall(r'id="(instance\.[^"]+)"', area), ["instance.Area-5bCircle-5d", "instance.Area-5bSide-5d"])
        self.assertEqual(signature_after(area, "method.area"), "fn area(self: Self) -> u32;")
        self.assertIn("\nThe area, rounded down.\n", area)
        self.assertEqual(signature_after(tools, "fn.radius"), "fn radius(c: Circle) -> u32 { ... }")
        link = re.search(r'<a class="sig-link sig-struct" href="([^"]+)">Circle</a>', tools)
        self.assertIsNotNone(link)
        self.assertEqual(urllib.parse.urljoin("http://site/gizmo/tools/", link.group(1)), "http://site/gizmo/shapes/struct.Circle.html")
        link = re.search(r'<a class="sig-link sig-trait" href="([^"]+)">Area</a>\[<a class="sig-link sig-struct" href="([^"]+)">Circle</a>\]', measures)
        self.assertIsNotNone(link)
        self.assertEqual([urllib.parse.urljoin("http://site/gadget/measures/", url) for url in link.groups()],
                         ["http://site/gadget/shapes/trait.Area.html", "http://site/gadget/shapes/struct.Circle.html"])
        records = {(r["p"], r["k"]): (r["n"], r["u"], r["s"]) for r in index}
        self.assertEqual(len(records), len(index))
        for path, kind, name, url, line in (
            ("gizmo", "module", "gizmo", "/gizmo/", "The gizmo facade."),
            ("gizmo::shapes", "module", "shapes", "/gizmo/shapes/", "Shapes and their areas."),
            ("gizmo::shapes::Circle", "struct", "Circle", "/gizmo/shapes/struct.Circle.html", "A circle."),
            ("gizmo::shapes::Circle::unit", "method", "unit", "/gizmo/shapes/struct.Circle.html#method.unit", "A circle of radius one."),
            ("gizmo::shapes::Side", "enum", "Side", "/gizmo/shapes/enum.Side.html", "A side of a square."),
            ("gizmo::shapes::Area", "trait", "Area", "/gizmo/shapes/trait.Area.html", "Things with an area."),
            ("gizmo::shapes::Area::area", "method", "area", "/gizmo/shapes/trait.Area.html#method.area", "The area, rounded down."),
            ("gizmo::tools::radius", "fn", "radius", "/gizmo/tools/#fn.radius", "The radius of a circle."),
            ("gadget::measures::larger", "fn", "larger", "/gadget/measures/#fn.larger", "The larger of two areas."),
            ("gadget::measures::Area[Circle]", "instance", "Area[Circle]", "/gadget/measures/#instance.Area-5bCircle-5d", "A circle's area, roughly."),
        ):
            with self.subTest(path=path):
                self.assertEqual(records.get((path, kind)), (name, url, line))
        self.assertFalse(any(p.startswith("gadget::shapes") for p, _ in records))

    def test_doc_anchors_survive_an_earlier_declaration(self):
        """Pages and anchors are named for what they document: declarations added before the others leave
        every page and anchor of the site in place."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            write_files(base, DOC_PACKAGES)
            sites = []
            for run in ("first", "second"):
                if run == "second":
                    shapes, measures = base / "gadget/shapes.kur", base / "gadget/measures.kur"
                    shapes.write_text(shapes.read_text().replace("/// A circle.", EARLY_SHAPES + "/// A circle.", 1))
                    measures.write_text(measures.read_text().replace("{Area, Circle, Side}", "{Area, Circle, Early, Side}")
                                        .replace("impl Circle {", EARLY_MEASURES + "impl Circle {", 1))
                src = base / run
                result = self.run_compiler("doc", str(base / "gadget"), "-o", str(src))
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual(markdown_link_errors(src), [])
                sites.append({page_url(src, p): anchors_of(p.read_text()) for p in src.rglob("*.md")})
        first, second = sites
        self.assertGreater(len(first), 8)
        for url, anchors in first.items():
            with self.subTest(page=url):
                self.assertIn(url, second)
                self.assertLessEqual(anchors, second[url])
                self.assertFalse(any(re.fullmatch(r"item-\d+", a) for a in anchors))
        self.assertIn("/gadget/shapes/struct.Early.html", second)
        self.assertLessEqual({"method.unit", "method.zero", "instance.Area-5bCircle-5d"}, second["/gadget/shapes/struct.Circle.html"])
        self.assertIn("fn.early", second["/gadget/shapes/"])
        self.assertIn("instance.Area-5bEarly-5d", second["/gadget/measures/"])

    def test_doc_site_of_the_standard_library(self):
        """`kursal doc lib/std` documents std and its dependencies, the modules std re-exports under std's
        paths too, every link in its Markdown leads to a page and an anchor there, no extern is listed, and
        the search index lists each item once."""
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "site"
            result = self.run_compiler("doc", str(LIB / "std"), "-o", str(src))
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            pages = sorted(src.rglob("*.md"))
            self.assertGreater(len(pages), 250)
            for package in ("std", "core", "alloc", "libc", "std/cmp", "std/collections", "std/thread", "core/cmp", "libc/thread"):
                self.assertTrue((src / package / "index.md").is_file(), package)
            self.assertEqual(markdown_link_errors(src), [])
            text = "".join(page.read_text() for page in pages if page.relative_to(src).parts[0] != "src")
            index = json.loads((src / "search-index.json").read_text())
            cmp = (src / "core/cmp/index.md").read_text()
            landing = (src / "index.md").read_text()
        self.assertIn('<li><a class="package-link" href="core/"><code>core</code></a>'
                      "<p>The core library: the language's own vocabulary types.</p></li>", landing)
        self.assertNotIn("`extern`", text)
        self.assertNotIn("kur_libc_", text)
        self.assertIn('<section class="item-entry" id="instance.Ord-5bu8-2cu8-5d">', cmp)
        self.assertEqual(len({(r["p"], r["k"]) for r in index}), len(index))
        found = {r["p"]: r["u"] for r in index}
        self.assertEqual(found["std::cmp::Ord"], "/std/cmp/trait.Ord.html")
        self.assertEqual(found["std::io::read_line"], "/std/io/#fn.read_line")
        self.assertEqual(found["std::collections::vec::Vec!"], "/std/collections/vec/struct.Vec~.html")
        self.assertNotIn("core::cmp::Ord", found)

    def test_doc_site_builds_with_krayon(self):
        """Krayon builds the standard library's site and the re-exporting packages' site, the pages
        `kursal doc` writes with docs/site's configuration and theme copied over them: every local link
        and anchor of their HTML resolves, no page has an id twice, and the pages link the theme's
        assets by their fingerprinted names."""
        krayon = shutil.which("krayon")
        if krayon is None:
            self.skipTest("krayon is not on the PATH; build Krayon and put it on the PATH to check the site's HTML")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            write_files(base, DOC_PACKAGES)
            for name, path, least in (("std", LIB / "std", 250), ("gizmo", base / "gizmo", 10)):
                with self.subTest(site=name):
                    site = base / f"site-{name}"
                    result = self.run_compiler("doc", str(path), "-o", str(site / "src"))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    shutil.copytree(ROOT / "docs/site", site, dirs_exist_ok=True)
                    built = subprocess.run([krayon, "build", str(site)], capture_output=True, text=True, timeout=TIME_LIMIT,
                                           preexec_fn=limit_memory)
                    self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
                    dist = site / "dist"
                    self.assertGreater(len(list(dist.rglob("*.html"))), least)
                    self.assertIsInstance(json.loads((dist / "search-index.json").read_text()), list)
                    self.assertRegex((dist / "index.html").read_text(),
                                     r'<script defer src="/assets/search\.[0-9a-f]{64}\.js"></script>')
                    self.assertEqual(html_link_errors(dist), [])

    def test_doc_search_ranking(self):
        """The site's search (docs/site/src/assets/search.js) ranks an exact name first, its `!` ignored and
        the query's case before any other, then names the query starts, then the rest; within a tier
        types and traits before other kinds, then shorter paths; a query with `::` matches paths."""
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not on the PATH; install Node.js to check the search ranking")
        records = [("module", "std::buffer"), ("struct", "std::buffer::Buffer!"),
                   ("struct", "std::buffer::BufferRestore!"), ("method", "std::buffer::Buffer!::new"),
                   ("fn", "std::io::write_buffer"), ("fn", "std::io::read_line"), ("fn", "std::io::read_exact"),
                   ("method", "std::io::File!::read_exact"), ("module", "std::io"), ("module", "std::fs"),
                   ("fn", "std::fs::read"), ("trait", "std::cmp::Ord"), ("instance", "std::cmp::Ord[u8, u8]"),
                   ("method", "std::cmp::Ord::cmp"), ("enum", "std::cmp::Order"), ("module", "std::collections::vec"),
                   ("struct", "std::collections::vec::Vec!"), ("struct", "std::collections::vec::VecIter!"),
                   ("method", "std::collections::arena_vec::ArenaVec::len")]
        index = [{"n": p.rsplit("::", 1)[-1], "k": k, "p": p, "u": "/", "s": ""} for k, p in records]
        script = ("const { prepare, rank } = require(process.argv[1]);"
                  "const index = prepare(JSON.parse(process.argv[2]));"
                  "console.log(JSON.stringify(JSON.parse(process.argv[3]).map((q) => rank(index, q, 50).map((r) => r.p))));")
        queries = ["Buffer", "buffer", "Buffer!", "read_line", "Ord", "io::read", "std::fs", "vec", "Vec"]
        result = subprocess.run([node, "-e", script, str(ROOT / "docs/site/src/assets/search.js"), json.dumps(index),
                                 json.dumps(queries)], capture_output=True, text=True, timeout=TIME_LIMIT)
        self.assertEqual(result.returncode, 0, result.stderr)
        ranked = dict(zip(queries, json.loads(result.stdout)))
        buffer = ["std::buffer::Buffer!", "std::buffer", "std::buffer::BufferRestore!",
                  "std::buffer::Buffer!::new", "std::io::write_buffer"]
        self.assertEqual(ranked["Buffer"], buffer)
        self.assertEqual(ranked["Buffer!"], buffer)
        self.assertEqual(ranked["buffer"][:3], ["std::buffer", "std::buffer::Buffer!",
                                                "std::buffer::BufferRestore!"])
        self.assertEqual(ranked["read_line"], ["std::io::read_line"])
        self.assertEqual(ranked["Ord"], ["std::cmp::Ord", "std::cmp::Order", "std::cmp::Ord[u8, u8]", "std::cmp::Ord::cmp"])
        self.assertEqual(ranked["io::read"], ["std::io::read_line", "std::io::read_exact"])
        self.assertEqual(ranked["std::fs"], ["std::fs", "std::fs::read"])
        self.assertEqual(ranked["vec"][:3], ["std::collections::vec", "std::collections::vec::Vec!",
                                             "std::collections::vec::VecIter!"])
        self.assertEqual(ranked["Vec"][:3], ["std::collections::vec::Vec!", "std::collections::vec",
                                             "std::collections::vec::VecIter!"])

    def test_doc_refuses_an_existing_output_and_needs_no_theme(self):
        """`kursal doc` writes no pages over an existing path, says why and leaves it as it was; and it
        reads nothing beside the standard library, so one with no repository around it documents too."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            existing = base / "existing"
            existing.mkdir()
            (existing / "keep").write_text("mine\n")
            result = self.run_compiler("doc", str(LIB / "std"), "-o", str(existing))
            self.assertEqual((result.returncode, result.stderr),
                             (1, "documentation output already exists; choose a new -o path\n"))
            self.assertEqual(sorted(p.name for p in existing.iterdir()), ["keep"])
            (base / "bare").mkdir()
            (base / "bare/lib").symlink_to(LIB)
            bare = subprocess.run([str(self.binary), "doc", str(LIB / "std"), "-o", str(base / "site")],
                                  capture_output=True, text=True, timeout=TIME_LIMIT, preexec_fn=limit_memory,
                                  env=dict(os.environ, KURSAL_LIB=str(base / "bare/lib")))
            self.assertEqual((bare.returncode, bare.stderr), (0, ""))
            self.assertTrue((base / "site/std/index.md").is_file())

    def test_parser_matches_reference(self):
        paths = sorted(p for top in ("lib", "examples", "compiler", "reference-compiler/examples",
                                     "reference-compiler/tests/fixtures")
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
