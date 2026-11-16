"""`core::process::trap`, the builtin that stops the program, and the bounds on usize-valued text the
compilers read (tuple indices, KURS integers)."""
import tempfile
import unittest
from pathlib import Path

from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.kurs import parse_program, parse_type, write_program
from kursal.lower import lower
from kursal.parser import parse
from kursal.resolve import ResolveError, resolve
from kursal.sexpr import KursError, read
from kursal.sigs import signatures
from tests.test_lower import compile_and_run, write

DIVERGES = '''
fn pick(n: u32) -> u32 {
    let x = if n > 3u32 { n } else { core::process::trap() };
    x + 1u32
}
fn unit_arm(n: u32) -> u32 {
    match n { 0u32 => core::process::trap(), _ => () };
    n
}
'''


class TrapTests(unittest.TestCase):
    def build(self, source: str, directory: Path):
        write(directory, {"lib.kur": source}, core=True)
        resolver = resolve(directory)
        sigs = signatures(resolver)
        program = lower(resolver, sigs)
        return program, sigs.name(resolver.packages[0].entry_decl)

    def run_source(self, source: str) -> int | str:
        with tempfile.TemporaryDirectory(prefix="kursal-trap-") as base:
            directory = Path(base)
            program, entry = self.build(source, directory)
            path = directory / "program.c"
            path.write_text(emit_c(program, check_program(program), entry))
            return compile_and_run(path, directory / "program")

    def test_trap_is_never_in_any_expression_position(self):
        self.assertEqual(self.run_source(DIVERGES + 'fn main() -> i32 { (pick(5u32) + unit_arm(2u32)) as i32 }'), 8)

    def test_a_trap_stops_the_program_by_a_signal(self):
        status = self.run_source(DIVERGES + 'fn main() -> i32 { (pick(1u32) as i32) }')
        assert isinstance(status, int)
        self.assertLess(status, 0)

    def test_the_kurs_of_a_trap_round_trips_to_the_same_c(self):
        with tempfile.TemporaryDirectory(prefix="kursal-trap-") as base:
            program, entry = self.build(DIVERGES + 'fn main() -> i32 { (pick(5u32) as i32) }', Path(base))
            text = write_program(program)
            self.assertIn("(trap)", text)
            again = parse_program(text)
            self.assertEqual(write_program(again), text)
            c = emit_c(program, check_program(program), entry)
            self.assertEqual(emit_c(again, check_program(again), entry), c)
            lines = [line.strip() for line in c.splitlines()]
            self.assertIn("KUR_TRAP();", lines)
            # the `unreachable` after a trap writes no second one
            self.assertFalse(any(a == b == "KUR_TRAP();" for a, b in zip(lines, lines[1:])))

    def test_only_core_names_the_builtin(self):
        with tempfile.TemporaryDirectory(prefix="kursal-trap-") as base, \
                self.assertRaisesRegex(ResolveError, "only package core names builtins"):
            self.build('fn main() -> i32 { builtin::trap() }', Path(base))

    def test_a_tuple_index_past_usize_max_is_a_syntax_error(self):
        parse('fn f(p: (u32, u32)) -> u32 { p.0 + p.18446744073709551615 }', "t.kur")
        with self.assertRaisesRegex(KursError, "1:37: syntax error"):
            parse('fn f(p: (u32, u32)) -> u32 { p.0 + p.18446744073709551616 }', "t.kur")

    def test_a_kurs_integer_past_usize_max_is_rejected(self):
        for value in ("18446744073709551616", "18446744073709551617"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "invalid literal for int"):
                parse_type(read(f"(array u8 {value})")[0])


if __name__ == "__main__":
    unittest.main()
