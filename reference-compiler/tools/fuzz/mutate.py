"""Mutation of existing sources: programs near ones the front end accepts.

A seed picks a source module (a generated program, a module of `examples/`, or a module of a package
the reference's own tests lower, from a corpus `python -m tools.fuzz corpus` writes) and applies one to three mutations to its function bodies, statement by statement:
drop, duplicate or swap statements; move a statement into or out of a branch or loop; insert an early
`return` (or a trap) inside a loop or branch holding linear values; replace a moved name with another of
the same spelling class; turn a `let` into a `var`; lend instead of moving (`f(x)` to `f(&x)`) and the
reverse; and swap the arms of an `if`. Mutations that keep the front end accepting are the interesting
ones: there the checker must agree."""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Optional

from tools.fuzz.gen import Gen
from tools.fuzz.oracle import ROOT, TARGET, manifest
from tools.fuzz.reduce import split_statements


def sources() -> list[tuple[str, bool]]:
    """Single-module sources to mutate, each with whether it needs the standard library: examples whose
    package is one module depending on at most `core`."""
    found: list[tuple[str, bool]] = []
    for toml in sorted((ROOT / "examples").glob("*/kur.toml")):
        mods = sorted(toml.parent.rglob("*.kur"))
        text = toml.read_text()
        deps = re.findall(r"\[dependencies\.(\w+)\]", text)
        if len(mods) == 1 and set(deps) <= {"core"}:
            found.append((mods[0].read_text(), False))
    return found


_SOURCES: Optional[list[tuple[str, bool]]] = None
# The corpus of test packages, as `tests.test_compiler.lowering_cases` lists them: {files, target, lower}.
CORPUS: list[dict[str, object]] = []


def load_corpus(path: Path) -> None:
    import json
    CORPUS[:] = json.loads(path.read_text())


class Mutator:
    def __init__(self, seed: int) -> None:
        self.r = random.Random(seed)
        self.seed = seed

    def base(self) -> str:
        global _SOURCES
        if self.r.random() < 0.6:
            return Gen(self.seed, self.r.randrange(2, 5)).program()
        if _SOURCES is None:
            _SOURCES = sources()
        text, _ = self.r.choice(_SOURCES)
        return text

    def package(self) -> dict[str, str]:
        if CORPUS and self.r.random() < 0.5:
            return self.corpus_package()
        text = self.base()
        lines = split_statements(text)
        for _ in range(self.r.randrange(1, 4)):
            lines = self.mutate(lines)
        text = "\n".join(lines) + "\n"
        entry = "main" if re.search(r"^fn main\(\) -> i32", text, re.M) else None
        return {"kur.toml": manifest(entry), "lib.kur": text}

    def corpus_package(self) -> dict[str, str]:
        """A package of the corpus with one of its modules mutated."""
        case = self.r.choice(CORPUS)
        files = dict(case["files"])  # type: ignore[call-overload]
        modules = [rel for rel in files if rel.endswith(".kur")]
        rel = self.r.choice(modules)
        lines = split_statements(files[rel])
        for _ in range(self.r.randrange(1, 4)):
            lines = self.mutate(lines)
        files[rel] = "\n".join(lines) + "\n"
        files[TARGET] = str(case["target"])
        return files

    def body_lines(self, lines: list[str]) -> list[int]:
        """Indices of statements inside function bodies (lines ending in `;` that are not declarations)."""
        return [i for i, line in enumerate(lines)
                if line.rstrip().endswith(";") and not line.lstrip().startswith(("use ", "export ", "fn ", "//"))]

    def mutate(self, lines: list[str]) -> list[str]:
        r = self.r
        body = self.body_lines(lines)
        if not body:
            return lines
        i = r.choice(body)
        kind = r.choice(["drop", "dup", "swap", "move_down", "move_up", "return", "return", "letvar", "lend",
                         "unlend", "rename", "trap", "if_swap", "wrap_if", "wrap_loop"])
        lines = list(lines)
        line = lines[i]
        if kind == "drop":
            del lines[i]
        elif kind == "dup":
            lines.insert(i, line)
        elif kind == "swap":
            j = r.choice(body)
            lines[i], lines[j] = lines[j], lines[i]
        elif kind == "move_down":
            j = min(len(lines) - 1, i + r.randrange(1, 6))
            lines.insert(j, lines.pop(i))
        elif kind == "move_up":
            j = max(0, i - r.randrange(1, 6))
            lines.insert(j, lines.pop(i))
        elif kind == "return":
            value = self.return_value(lines, i)
            lines.insert(i, f"return {value};")
        elif kind == "trap":
            lines.insert(i, "core::process::trap();")
        elif kind == "letvar":
            lines[i] = re.sub(r"^(\s*)let ", r"\1var ", line, count=1) if "let " in line else \
                re.sub(r"^(\s*)var ", r"\1let ", line, count=1)
        elif kind == "lend":
            lines[i] = re.sub(r"\((\w+)\)", lambda m: f"(&{m.group(1)})", line, count=1)
        elif kind == "unlend":
            lines[i] = line.replace("(&", "(", 1)
        elif kind == "rename":
            names = re.findall(r"\b([a-z_][a-z0-9_]*)\b", line)
            pool = re.findall(r"\b([a-z][a-z0-9]*\d)\b", "\n".join(lines[max(0, i - 30):i]))
            if names and pool:
                old = r.choice(names)
                lines[i] = re.sub(rf"\b{re.escape(old)}\b", r.choice(pool), line, count=1)
        elif kind == "if_swap":
            lines[i] = re.sub(r"if (.*?) \{ (.*?) \} else \{ (.*?) \}", r"if \1 { \3 } else { \2 }", line, count=1)
        elif kind == "wrap_if":
            lines[i] = f"if {r.choice(['true', 'false', '1u32 < 2u32'])} {{ {line} }}"
        elif kind == "wrap_loop":
            lines[i] = f"var wl{i} = 0u32; while wl{i} < 2u32 {{ {line} wl{i} = wl{i} + 1u32; }}"
        return lines

    def return_value(self, lines: list[str], i: int) -> str:
        """A guess at the enclosing function's return value: a literal of its return type."""
        for j in range(i, -1, -1):
            m = re.search(r"\) -> ([^{]+?)\s*\{?$", lines[j])
            if m and re.match(r"\s*(forall .*)?(unsafe )?fn ", lines[j]):
                ty = m.group(1).strip()
                if re.fullmatch(r"[ui](8|16|32|64|size)", ty):
                    return f"0{ty}"
                if ty == "bool":
                    return "false"
                if ty == "()":
                    return "()"
                if ty == "T!":
                    return "mk_t(0u32)"
                return "core::process::trap()"
        return "0u32"
