"""The self-hosted compiler in compiler/ agrees with this one: built by the reference compiler, its parser
produces the same syntax tree for every source file in the repository, and rejects bad syntax with the
same message; its type modules give the same results as these on small cases."""

import dataclasses
import hashlib
import itertools
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
from kursal.unify import Meta, match_head, unify, zonk

ROOT = Path(__file__).resolve().parents[2]
COMPILER = ROOT / "compiler"
CACHE = Path(__file__).resolve().parents[1] / ".build"


def build_compiler() -> Path:
    """The compiler binary, rebuilt when its sources or the library's change."""
    sources = sorted([*COMPILER.rglob("*.kur"), *COMPILER.rglob("*.toml"), *(ROOT / "lib").rglob("*.kur"),
                      *(ROOT / "reference-compiler/kursal").glob("*.py")])
    digest = hashlib.sha256()
    for path in sources:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    binary = CACHE / f"kursalc-{digest.hexdigest()[:16]}"
    if not binary.exists():
        CACHE.mkdir(exist_ok=True)
        for old in CACHE.glob("kursalc-*"):
            old.unlink()
        built = subprocess.run([sys.executable, str(ROOT / "reference-compiler/tools/build.py"), str(COMPILER), str(binary)],
                               capture_output=True, text=True)
        if built.returncode != 0:
            raise RuntimeError(built.stdout + built.stderr)
    return binary


def dump(node: object) -> str:
    """The canonical text of a syntax tree, as compiler/dump.kur writes it."""
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
)


# Cases for `kursalc selftest-types`; compiler/selftest.kur describes their forms.
TYPE_CASES = (
    # show
    "(show (tuple))",
    "(show (tuple (struct) (enum (A u32)) (exists ($a $b) (builtin::Brand $a)) (array u8 3)))",
    "(show (foreach (F (foreach (G (tuple u8 u16)) (builtin::Pointer G))) (tuple F G)))",
    "(show (foreach (F (foreach (G (tuple u8)) G)) F (retain)))",
    "(show (foreach ((builtin::Pointer F) (builtin::Pointer (tuple u8))) F (retain X Y)))",
    "(show (builtin::Fn (unsafe) (forall ($a) () ! ((Pair u32 $a) _))))",
    # subst: capture avoidance, numbering, foreach retained binders
    "(subst (exists ($a) (tuple $a $b)) ((b $a)))",
    "(subst (builtin::Fn (forall ($a) () $b ($a))) ((b $a)))",
    "(subst (tuple (exists ($a) (tuple $a $b)) (exists ($a#7) (tuple $a#7 $b))) ((b (tuple $a $a#7))))",
    "(subst (exists ($a) $a) ((a $z)))",
    "(subst (T T) ((T u8)))",
    "(subst (foreach (F (foreach (G (tuple T T)) (tuple G U))) (tuple F G U)) ((T u8) (U G)))",
    "(subst (foreach (F (foreach (G (tuple T T)) (tuple G U))) (tuple F G U)) ((T u8) (U F)))",
    "(subst (foreach (F (tuple T)) (tuple F T)) ((T (tuple F)) (F u8)))",
    # free names
    "(free (foreach (F (tuple T $k)) (tuple F G)))",
    "(free (builtin::Fn (forall ($a) () $a ($b T))))",
    "(brands (tuple $a (exists ($b) (tuple $b $c)) (P $d)))",
    "(extractable (tuple T (exists ($a) U) (builtin::Fn (forall ($b) () V ())) (builtin::Fn (W) X) (foreach (F Y) Z) $k))",
    # equality modulo bound names
    "(equal (exists ($a) (builtin::Brand $a)) (exists ($b) (builtin::Brand $b)))",
    "(equal (exists ($a $b) (tuple $a $b)) (exists ($b $a) (tuple $a $b)))",
    "(equal (exists ($a) (tuple $a $b)) (exists ($b) (tuple $b $b)))",
    "(equal (foreach (F (tuple u8)) F) (foreach (G (tuple u8)) G))",
    "(equal (foreach (F (tuple u8)) (tuple F G)) (foreach (G (tuple u8)) (tuple G G)))",
    "(equal (builtin::Fn (forall ($p) () u8 ((builtin::Brand $p)))) (builtin::Fn (forall ($q) () u8 ((builtin::Brand $q)))))",
    "(equal (builtin::Fn (u8) u8) (builtin::Fn (unsafe) (u8) u8))",
    "(equal (P T) (P T U))",
    "(fits ! u32)",
    "(fits u32 u8)",
    # unification
    "(unify ((a type) (i int)) (tuple ?a ?i) (tuple u32 u8))",
    "(unify ((i int)) ?i bool)",
    "(unify ((a type) (i int)) (tuple ?a ?a) (tuple ?i u8))",
    "(unify ((i int) (f float)) ?i ?f)",
    "(unify ((f float) (a type)) (tuple ?f ?a) (tuple ?a f32))",
    "(unify ((k brand)) (builtin::Brand $?k) (builtin::Brand $x))",
    "(unify ((k brand)) (tuple $?k) (tuple u32))",
    "(unify ((a type)) ?a $x)",
    "(unify ((a type)) ?a (builtin::Pointer ?a))",
    "(unify ((a type)) (array ?a 2) (array u8 3))",
    "(unify ((a type) (b type)) (struct (x ?a) (y ?b)) (struct (x ?b) (y u8)))",
    "(unify ((a type)) (exists ($x) (tuple $x ?a)) (exists ($y) (tuple $y u8)))",
    "(unify ((k brand)) (exists ($x) (tuple $x $?k)) (exists ($y) (tuple $y $y)))",
    "(unify-closed ((k brand)) (exists ($x) (tuple $x $?k)) (exists ($y) (tuple $y $y)))",
    "(unify-closed ((a type)) (builtin::Fn (forall ($p) () ?a ((builtin::Brand $p)))) "
    "(builtin::Fn (forall ($q) () u8 ((builtin::Brand $q)))))",
    "(unify-closed ((k brand)) (tuple (exists ($x) (tuple $x $rigid#0)) (exists ($x) (tuple $x $?k))) "
    "(tuple (exists ($y) (tuple $y $rigid#0)) (exists ($y) (tuple $y $y))))",
    "(unify ((a type)) (foreach (F (tuple ?a)) (builtin::Pointer F)) (foreach (G (tuple u8)) (builtin::Pointer G)))",
    "(unify () (builtin::Fn (u8) u8) (builtin::Fn (unsafe) (u8) u8))",
    "(match-head (T $k) ((Pair T (builtin::Brand $k))) ((Pair u8 (builtin::Brand $z))) true)",
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
    "(copy ((type Node (T) (struct (v T) (next (builtin::Pointer (Node T)))))) ((T copy)) (Node T))",
    "(copy ((type Node (T) (struct (v T) (next (builtin::Pointer (Node T)))))) ((T any)) (Node T))",
    "(copy ((type L (T) (enum (Nil (tuple)) (Cons (tuple T (L T)))))) () (L u8))",
    "(copy ((linear R () u8)) () (tuple R))",
    "(copy () () (foreach (F (tuple u8)) (builtin::Pointer F)))",
    "(copy () () (foreach (F (tuple u8)) u16))",
    "(zero ((type E () (struct (a (tuple)) (b (array u8 0))))) E)",
    "(zero () T)",
    "(zero () (exists ($a) (builtin::Brand $a)))",
    "(expand ((type P (T) (struct (a T) (b u8)))) (foreach (F (P u16)) (builtin::Pointer F)))",
    "(expand () (foreach ((builtin::Pointer F) (builtin::Pointer (tuple u8 u16))) (array F 2)))",
    "(expand () (foreach (G (foreach (F (struct (x u8) (y u16))) (tuple F F))) (tuple G F)))",
    "(expand () (foreach (F (array u32 0)) (builtin::Pointer F)))",
    "(expand () (foreach ((builtin::Pointer F) (builtin::Pointer (enum (A u8)))) F))",
    "(expand () (foreach ((builtin::Pointer F) (tuple u8)) F))",
    "(expand () (Q u8))",
    "(expand ((type P (T) T)) (P))",
    "(expand () (struct (a u8)))",
    "(fields ((type P (T) (struct (a T) (b u8)))) (P u16))",
    "(fields () (array u8 3))",
    "(fields () (enum (A u8)))",
    "(variants ((type O (T) (enum (Some T) (None (tuple))))) (O u8))",
    "(variants () (tuple u8))",
    "(exposure ((opaque H () (struct (a u8)))) () H)",
    "(exposure ((type P () (builtin::Pointer (struct (a u8))))) () P)",
    "(exposure () () (builtin::Pointer (enum (A u8))))",
    "(exposure () () (builtin::Pointer (struct (a u8))))",
    "(exposure () ((T any)) T)",
    "(exposure () () (foreach (F (tuple u8 u16)) (builtin::Pointer F)))",
    "(pattern () () (foreach ((builtin::Pointer F) (tuple (builtin::Pointer u8) (builtin::Pointer u16))) F))",
    "(pattern () () (foreach ((builtin::Pointer F) (tuple (builtin::Pointer u8) u16)) F))",
    "(pattern ((opaque H () (tuple (builtin::Pointer u8)))) () (foreach ((builtin::Pointer F) H) F))",
    "(pattern () () (foreach ((builtin::Pointer F) (foreach (G (tuple u8)) (builtin::Pointer G))) F))",
    # layout
    "(layout (type L (T) (enum (Nil (tuple)) (Cons (builtin::Pointer (L T))))))",
    "(layout (type L () (struct (next L))))",
    "(layout (type A () (builtin::Pointer A)))",
    "(layout (type G (T) (enum (Leaf T) (Node (builtin::Pointer (G (tuple T T)))))))",
    "(layout (type H (T) (enum (Leaf T) (Node (foreach (F T) (builtin::Pointer (H F)))))))",
    "(layout (type S () (foreach (F S) F)))",
    "(layout (type A () (struct (b (builtin::Pointer B)))) (type B () (enum (x A) (y (array B 0)))))",
    "(layout (type A ($k) (struct (b (builtin::Brand $k)) (n (builtin::Pointer (A $k))))))",
    # formation
    "(formation (type P (T $k) (struct (a T) (b (builtin::Brand $k)))))",
    "(formation (type E () (exists ($a) u8)))",
    "(formation (type E () (exists ($a) (exists ($b) (tuple $a $b)))))",
    "(formation (type E () (exists () u8)))",
    "(formation (type B () (builtin::Brand $z)))",
    "(formation (type R () (foreach (G (foreach (F (tuple u8)) F)) G (retain X))))",
    "(formation (type R () (foreach (G (foreach (F (tuple u8)) F)) (tuple G F))))",
    "(formation (type F () (builtin::Fn (forall (T) () u8 ()))))",
    "(formation (type D () (struct (a u8) (a u16))))",
    "(formation (type I () (tuple _)))",
    "(formation (type V (T) (T u8)))",
    "(formation (type U () Nope))",
    "(formation (type W (T) u8) (type X () (W)))",
    "(formation (type K ($k) u8) (type X () (K u8)))",
    "(formation (type K (T) u8) (type X () (K $static)))",
    "(formation (type C (+T) u8) (linear L () u8) (type X () (C L)))",
    "(formation (type C (+T) u8) (type X () (C (builtin::Pointer u8))))",
    "(formation (type S ($static) u8))",
    "(formation (type D (T T) u8))",
    "(kind () ((k brand)) $k)",
    "(kind () () (tuple u8 (builtin::Fn (forall ($a) () u8 ((builtin::Brand $a))))))",
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
    "(mono (f (T) (call g T)) (g (T) (fn f (builtin::Pointer T))))",
    "(mono (f ($k) (call f (builtin::Brand $k))))",
    "(mono (f (T) (foreach f (foreach (F T) F) T)))",
    "(mono (f (T) (foreach f (foreach (F (tuple u8)) F) T)))",
    "(mono (f (T) (call g (List T))) (g (T)))",
    "(mono (f (T) (trait_call Show show (List T))))",
    # coherence
    "(coherence ((T) (Show (List T))) (() (Show (List u8))))",
    "(coherence (() (Show u8)) (() (Show u16)))",
    "(coherence (() (Show (exists ($a) (P $a u8)))) (() (Show (exists ($a) (P $a u16)))))",
    "(coherence ((T) (Show u8)))",
    "(coherence (($k) (Show (exists ($a) (builtin::Brand $k)))))",
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
    """What `kursalc selftest-types CASE` prints for one case, computed with these modules."""
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
        return subprocess.run([str(self.binary), *args], capture_output=True, text=True, timeout=600)

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
