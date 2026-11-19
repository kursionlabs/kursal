"""No name the compiler made reaches a message about source: not an opened brand's label (`b#open9`), a
brand spelled as KURS spells it (`$k`), a closure's environment (`Env#1`), a binding's identity (`o#2`), a
lifted body (`f#closure0`) or a foreach binder (`#F0`). The sweep runs the front end over the rejected
examples, the fuzzer's regressions and the cases the self-hosted compiler is compared on."""

import re
import sys
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from kursal.lower import lower
from kursal.resolve import LIB, resolve
from kursal.sexpr import KursError
from kursal.sigs import signatures
from tests.infer_cases import INFER_ERRORS, VISIBILITY_ERRORS
from tests.lower_cases import LOWER_ERRORS
from tests.test_compiler import SIGNATURE_ERRORS
from tests.test_fuzz_regressions import REJECTED as FUZZ_REJECTED

ROOT = Path(__file__).resolve().parents[2]

# A name with a `#` part, or a brand as `$label`.
MADE = re.compile(r"\w#\w|\$[A-Za-z_]")


def place(directory: Path, case: str | dict[str, str]) -> Path:
    """`case` as files under `directory`, as tests/test_compiler.py places it: a dict of files, of which the
    package in `a/` is the one; a source naming `std::`, a single file; else the module of a package `p`."""
    if isinstance(case, dict):
        for rel, text in case.items():
            (directory / rel).parent.mkdir(parents=True, exist_ok=True)
            (directory / rel).write_text(text)
        return directory / "a"
    directory.mkdir(parents=True, exist_ok=True)
    if "std::" in case:
        (directory / "case.kur").write_text(case)
        return directory / "case.kur"
    core = f'[dependencies.core]\npath="{LIB / "core"}"\n' if "core::" in case else ""
    (directory / "kur.toml").write_text('[package]\nname="p"\nsource_root="."\n' + core)
    (directory / "lib.kur").write_text(case)
    return directory


def message(path: Path) -> str:
    """The front end's message about the package at `path`, or "" when it accepts it."""
    sys.setrecursionlimit(20000)
    try:
        resolver = resolve(path)
        lower(resolver, signatures(resolver))
    except KursError as error:
        return str(error)
    return ""


class DiagnosticNameTests(unittest.TestCase):
    def test_no_made_name_reaches_a_message(self):
        sources: list[str | dict[str, str]] = [
            *(case for case, _ in INFER_ERRORS), *(case for case, _ in VISIBILITY_ERRORS),
            *(case for case, _ in LOWER_ERRORS), *SIGNATURE_ERRORS,
            *(case if isinstance(case, str) else case[0] for case in FUZZ_REJECTED),
        ]
        with tempfile.TemporaryDirectory(prefix="kursal-names-") as base:
            paths = [place(Path(base) / f"case{k}", case) for k, case in enumerate(sources)]
            paths.extend(sorted((ROOT / "examples/bad").glob("*.kur")))
            with ProcessPoolExecutor(max_workers=4) as pool:
                messages = list(pool.map(message, paths))
        self.assertGreater(sum(bool(m) for m in messages), 150)
        for path, text in zip(paths, messages):
            with self.subTest(path=path.name):
                self.assertIsNone(MADE.search(text.removeprefix(str(path))), text)


if __name__ == "__main__":
    unittest.main()
