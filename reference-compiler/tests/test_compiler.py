"""The self-hosted compiler in compiler/ agrees with this one: built by the reference compiler, its parser
produces the same syntax tree for every source file in the repository, and rejects bad syntax with the
same message."""

import dataclasses
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kursal.parser import parse
from kursal.sexpr import KursError

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


if __name__ == "__main__":
    unittest.main()
