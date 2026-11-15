"""Brands have no representation: code and C types are made once for every choice of brands."""

import re
import tempfile
import unittest
from pathlib import Path

from kursal.cgen import Backend
from kursal.check import check_program
from kursal.lower import lower
from kursal.resolve import resolve
from kursal.sigs import signatures
from tests.test_lower import compile_and_run, write

SOURCE = """
forall 'k.
struct Tagged { value: i32, brand: core::cap::Brand['k] }

forall 'k.
fn read(t: Tagged['k]) -> i32 {
    let { value, brand } = t;
    value + 1i32
}

forall 'j, 'k.
fn both(s: Tagged['j], t: Tagged['k]) -> i32 {
    read(s) + read(t)
}

fn main() -> i32 {
    let exists 'a. x = core::cap::fresh();
    let exists 'b. y = core::cap::fresh();
    let p = Tagged.{ value: 2i32, brand: x };
    let q = Tagged.{ value: 3i32, brand: y };
    read(p) + read(q) + both(p, q) + both(q, q) - 22i32
}
"""


class BrandSpecializationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory(prefix="kursal-brands-") as base:
            directory = Path(base)
            write(directory, {"lib.kur": SOURCE})
            resolver = resolve(directory)
            sigs = signatures(resolver)
            cls.program = lower(resolver, sigs)
            cls.backend = Backend(cls.program, check_program(cls.program))
            cls.c = cls.backend.emit_program(sigs.name(resolver.packages[0].entry_decl))
            c_file = directory / "main.c"
            c_file.write_text(cls.c)
            cls.status = compile_and_run(c_file, directory / "main")

    def fun(self, name):
        return next(f for n, f in self.program.funs.items() if n.endswith("::" + name))

    def bodies(self, symbol):
        return re.findall(rf"^static \S+ {symbol}\(.*\) {{$", self.c, re.M)

    def test_a_function_used_at_two_brands_has_one_body(self):
        self.assertEqual(self.status, 0)
        for name in ("read", "both"):
            # `read` at 'a and at 'b; `both` at ('a, 'b) and at ('b, 'b).
            ((_, symbol),) = self.backend.symbols[id(self.fun(name))]
            self.assertEqual(len(self.bodies(symbol)), 1)

    def test_aggregates_differing_only_in_brands_share_a_c_type(self):
        self.assertEqual(len(re.findall(r"^struct K_\w*Tagged\w* \{", self.c, re.M)), 1)
