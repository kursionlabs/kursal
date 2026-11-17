"""Delta debugging: shrink a program while it shows the same bug.

The module is split into lines with one statement or brace each, then ddmin (Zeller and Hildebrandt)
removes chunks of lines, halving the chunk size when no chunk can go, until no single line can be
removed; then the same over the words of what is left. A candidate is kept when its outcome has the
same signature (bug class and normalized message) as the original's."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

from tools.fuzz.oracle import Outcome, run_case


def split_statements(text: str) -> list[str]:
    """One statement per line: a line is broken after each `;` and around the braces of blocks (not of
    struct literals `.{ }` or patterns `let { } =`), outside lines with string literals or comments."""
    out: list[str] = []
    for line in text.splitlines():
        if '"' in line or "b'" in line or "//" in line:
            out.append(line)
            continue
        pieces, current, stack = [], "", []
        for i, c in enumerate(line):
            if c == "{":
                before = current.rstrip()
                block = not before.endswith(".") and not re.search(r"\b(let|var)( exists '\w+\.)?$", before)
                stack.append(block)
                current += c
                if block:
                    pieces.append(current)
                    current = ""
            elif c == "}":
                block = stack.pop() if stack else False
                if block:
                    pieces.append(current)
                    current = c
                else:
                    current += c
            elif c == ";" and not stack or c == ";" and stack[-1]:
                pieces.append(current + c)
                current = ""
            else:
                current += c
        pieces.append(current)
        out.extend(piece.strip() for piece in pieces if piece.strip())
    return out


def ddmin(items: list[str], keeps: Callable[[list[str]], bool]) -> list[str]:
    n = 2
    while len(items) >= 2:
        chunk = max(1, len(items) // n)
        removed = False
        start = 0
        while start < len(items):
            candidate = items[:start] + items[start + chunk:]
            if candidate and keeps(candidate):
                items = candidate
                removed = True
                n = max(n - 1, 2)
            else:
                start += chunk
        if not removed:
            if chunk == 1:
                break
            n = min(n * 2, len(items))
    return items


def minimize(files: dict[str, str], work: Path, selfhost: Optional[Path] = None,
             execute: bool = False) -> tuple[dict[str, str], Outcome]:
    """The smallest program found that shows the bug `files` shows, and its outcome."""
    first = run_case(files, work, selfhost, execute)
    target = first.signature()
    if not target:
        return files, first
    seen: dict[str, bool] = {}
    best = [first]
    current = dict(files)

    def keeps(candidate: dict[str, str]) -> bool:
        key = repr(sorted(candidate.items()))
        if key in seen:
            return seen[key]
        out = run_case(candidate, work, selfhost, execute)
        ok = out.signature() == target
        seen[key] = ok
        if ok:
            best[0] = out
        return ok

    for rel in sorted(rel for rel in files if rel.endswith(".kur")):
        def with_text(text: str) -> dict[str, str]:
            return {**current, rel: text}
        lines = ddmin(split_statements(current[rel]), lambda ls: keeps(with_text("\n".join(ls) + "\n")))
        current[rel] = "\n".join(lines) + "\n"
        words = " \n".join(lines).split(" ")
        words = ddmin(words, lambda ws: keeps(with_text(" ".join(ws).replace(" \n", "\n") + "\n")))
        text = " ".join(words).replace(" \n", "\n") + "\n"
        if keeps(with_text(text)):
            current[rel] = text
    return current, best[0]
