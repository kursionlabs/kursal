"""The editor grammars in editors/: each is generated from its grammar.js, parses every Kursal source or
readable KURS file it is given, and agrees with the reference parser on what is syntax."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kursal.lower import lower
from kursal.parser import parse
from kursal.readable import emit_kurs
from kursal.resolve import resolve
from kursal.sexpr import KursError
from kursal.sigs import signatures

ROOT = Path(__file__).resolve().parents[2]
KUR = ROOT / "editors/tree-sitter-kur"
KURS = ROOT / "editors/tree-sitter-kurs"

ACCEPTED = (
    "use package::{mem::{Region as R, raw::*}, raw}; export raw;",
    "module a::b; forall T. exposed(package::mem) struct Node! { value: T }",
    "exposed enum Choice { Some(u32), None } forall T. tuple Pair(T, T,);",
    'extern "C" fn abort() -> !; const LIMIT: u32 = 9u32;',
    "forall Self -> Item. trait Iterator { fn next(&self: Self) -> Option[Item]; }",
    "unsafe instance Send[Cell[u32]] { } impl i8 { const MIN: i8 = -128; forall T. fn f(self) {} }",
    "fn f(&x: u32, g: fn(&_: u32, u8) -> u32, h: closure(&it: u32) -> ()) -> exists 'a, 'b. Pair['a, 'b] {"
    " pack 'a, 'b. Pair.(x, x) }",
    "fn f(t: foreach *F of (u32, u8) => Box[F], g: forall T. fn(T) -> T, a: [u8; 4usize]) -> Map[K, V -> _] {"
    " sizeof[u32]() }",
    "fn f() -> u32 { let exists 'k. { a, b: (c, _), ..rest } = g(); let [x, y,] = h(); var n: u32 = 0u32;"
    " n = n + 1u32; s.a.0 = 3u32; unsafe *p = v; unsafe f(&x); n }",
    "fn f(b: bool, o: Option[u32]) -> u32 { if b { 1u32 } else if !b { 2u32 } else { 3u32 }"
    " match o { Option::Some(v) | Option::Other(v, _) => v, 0 | 1..=9 => { 1u32 } -5..=-1 => 3u32, _ => 0u32 } }",
    "fn f() -> u32 { while x < 3u32 { x = x + 1u32; } for i in it { g(i); } foreach c: C of p { (c, 1u32) } }",
    "fn f() -> u32 { let g = |&x, y: u32| x + y; let h = || -> u32 { 1u32 }; let k = |x| return x; 0u32 }",
    "fn f() -> u32 { let p = Point.{ x: 1u32, y }; let q = Point.{ ..all }; let r = Option[u32]::Some.(3u32);"
    " let s = Map!['k]::new(); let t = [1u32, 2u32, ..]; let u = (1u32,); let v = -x.m(&y)? as u64 * 3; 0u32 }",
    "fn f(c: u8) -> bool { let s = f\"{{x}} = {x + 1u32} {f\"{y}\"}\\n\"; c == b'\\n' || c == b'\\x41' && c != b' ' }",
    "fn f() -> f64 { 1_000.5f64 + 0x_ff as f64 + 0b1010u8 as f64 + 2f32 as f64 }",
)

# Forms of the surface syntax that Kursal does not have: a struct literal without `.`, a place written on
# a parameter's type, an unnamed place slot, and a `forall` ended by `=>`.
REJECTED = (
    "fn f() -> Point { Point { x: 1u32 } }",
    "fn f(x: &u32) {}",
    "fn f(g: fn(&u32) -> u32) {}",
    "forall T => fn f(x: T) {}",
    "fn f() { let (x) = g(); }",
    "fn f() -> u32 { match x { _ => 1u32, A => 2u32 } }",
)

READABLE_PACKAGES = ("shapes", "option_result", "skolems", "mint_join", "generic_capabilities", "iter_combinators")


def tree_sitter(grammar: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tree-sitter", *args], cwd=grammar, capture_output=True, text=True, timeout=300)


@unittest.skipUnless(shutil.which("tree-sitter") and shutil.which("cc"), "tree-sitter or cc not installed")
class EditorGrammarTests(unittest.TestCase):
    def test_generated_parsers_are_current(self):
        for grammar in (KUR, KURS):
            with self.subTest(grammar=grammar.name), tempfile.TemporaryDirectory() as directory:
                copy = Path(directory)
                for name in ("grammar.js", "tree-sitter.json"):
                    shutil.copy(grammar / name, copy / name)
                generated = tree_sitter(copy, "generate")
                self.assertEqual(generated.returncode, 0, generated.stderr)
                for name in ("grammar.json", "node-types.json", "parser.c"):
                    self.assertEqual((copy / "src" / name).read_text(), (grammar / "src" / name).read_text(),
                                     f"{grammar.name}/src/{name} is stale: run `tree-sitter generate`")

    def test_every_source_parses(self):
        paths = [str(path) for top in ("lib", "examples", "reference-compiler/examples") for path in (ROOT / top).rglob("*.kur")
                 if "bad" not in path.relative_to(ROOT).parts]
        self.assertGreater(len(paths), 100)
        result = tree_sitter(KUR, "parse", "-q", *paths)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = tree_sitter(KUR, "query", "queries/highlights.scm", *paths)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_agrees_with_the_reference_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            for accepted, sources in ((True, ACCEPTED), (False, REJECTED)):
                for index, source in enumerate(sources):
                    with self.subTest(source=source):
                        try:
                            parse(source)
                            parsed = True
                        except KursError:
                            parsed = False
                        self.assertEqual(parsed, accepted, "reference parser")
                        path = Path(directory) / f"case{int(accepted)}_{index}.kur"
                        path.write_text(source)
                        result = tree_sitter(KUR, "parse", "-q", str(path))
                        self.assertEqual(result.returncode == 0, accepted, result.stdout + result.stderr)

    def test_readable_kurs_parses(self):
        sys.setrecursionlimit(20000)
        with tempfile.TemporaryDirectory() as directory:
            paths = [str(ROOT / "reference-compiler/examples/counter/generated/program.kurs")]
            for name in READABLE_PACKAGES:
                resolver = resolve(ROOT / "examples" / name)
                path = Path(directory) / f"{name}.kurs"
                path.write_text(emit_kurs(lower(resolver, signatures(resolver))))
                paths.append(str(path))
            result = tree_sitter(KURS, "parse", "-q", *paths)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = tree_sitter(KURS, "query", "queries/highlights.scm", *paths)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
