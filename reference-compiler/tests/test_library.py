"""The standard library: every package lowers, passes the core checker, and becomes C that compiles."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.lower import lower
from kursal.resolve import LIB, resolve
from kursal.sigs import signatures

PACKAGES = ("core", "alloc", "std", "libc", "thread", "toml")


class LibraryTests(unittest.TestCase):
    def test_every_package_compiles_to_c(self):
        sys.setrecursionlimit(20000)
        with tempfile.TemporaryDirectory(prefix="kursal-library-") as base:
            for name in PACKAGES:
                with self.subTest(package=name):
                    resolver = resolve(LIB / name)
                    program = lower(resolver, signatures(resolver))
                    c_file = Path(base) / f"{name}.c"
                    c_file.write_text(emit_c(program, check_program(program)))
                    built = subprocess.run(["cc", "-w", "-fsyntax-only", str(c_file)], capture_output=True, text=True)
                    self.assertEqual(built.returncode, 0, built.stderr[:500])


if __name__ == "__main__":
    unittest.main()
