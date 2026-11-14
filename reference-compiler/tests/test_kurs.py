"""The typed KURS reader validates container shapes and retains source positions; the writer is its
inverse, and every lowered program survives the round trip with the same checker verdict and C."""

import os
import sys
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Optional

from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.kurs import parse_program, write_program
from kursal.lower import lower
from kursal.resolve import LIB, resolve
from kursal.sexpr import KursError
from kursal.sigs import signatures

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ("core", "alloc", "std", "libc", "thread", "toml")

# Every form of the serialization, written the way `write_program` writes it.
CANONICAL = r'''(type (in (module 0 a) abstract) Secret (struct (k u32)))
(type (in (module 0 a b) (exposed (module 0 a))) Handle! (forall (T $k) ((Copy T) (Eq T)) (struct (at (ptr T)) (brand (brand $k)))))
(type Choice (enum (None (tuple)) (Some (array u8 4))))
(type Any (exists ($a $b) (foreach ((ptr F) (Handle u32 $a)) (foreach (G F) (tuple F G)) (retain F))))
(type Callback (struct (plain (fn ((& u32) u64) !)) (risky (fn (unsafe) (forall ($r U) ((Copy U)) _ (U (& (ptr U))))))))
(fun (in (module 0 a)) main i32 ()
  (locals (&x u32) ("&odd name" (tuple)))
  (entry ()
    (= %n (const i32 0))
    (unsafe (= %p (alloca u32)))
    (= (%a %b) (destruct %t (tuple u32 u32)))
    (= %s (sizeof Secret))
    (= %v (construct_variant Choice Some %bytes))
    (= %h (pack (exists ($a) (Handle u32 $a)) ((brand $k)) %raw))
    (= %o (open %h ($a)))
    (= %c (call (f u32 $a) %x %y))
    (= %g (fn g))
    (= %m (foreach Any %src (mapper u32) %env))
    (= %t (trait_call Eq eq (u32) %x %y))
    (= %l (ptr_atomic_load seq_cst %p))
    (= %q (store &x %n))
    (jnz %c (next u32 %n) (done)))
  (next ($k) ((%m u32))
    (empty &x &y)
    (known &v Some)
    (opened &h ($a $b))
    (switch %v Choice (None done) (Some (more u32) %m)))
  (more ()
    (jmp done %n))
  (done ()
    (unreachable)))
(fun (unsafe) raw (forall (T) () T ((ptr T))
  (entry ((%p (ptr T)))
    (= %v (load %p))
    (ret %v))))
(fun (in (module 1) unsafe) helper (tuple) ()
  (entry ()
    (= %u (construct (tuple)))
    (ret %u)))
(extern fun puts i32 ((ptr u8)))
(extern value errno i32)
(data (in (module 0)) greeting (array u8 13) ("hello,\t\"world\"\\\n" 7 (nested "café\x01" "")))
(trait (in (module 0)) Eq (T)
  (eq bool (T T))
  ((unsafe) reset (tuple) ((& T))))
(trait Within (T $k))
(instance (in (module 0)) Eq (forall (T) ((Copy T)) ((Handle T $k)))
  (fun eq bool ((Handle T $k) (Handle T $k))
    (entry ((%a (Handle T $k)) (%b (Handle T $k)))
      (= %r (const bool true))
      (ret %r)))
  (fun (unsafe) reset (tuple) ((Handle T $k))
    (entry ((&a (Handle T $k)))
      (= %u (construct (tuple)))
      (ret %u))))
(instance Eq (u32))
'''

# The same program in other spellings the reader accepts.
VARIANTS = (
    ("(= %n (const i32 0))", "(= %n (const i32 0) (evidence))"),
    ("(type Choice (enum", "(type (unsafe) Choice (enum"),
    ("(forall (T $k) ((Copy T) (Eq T)) (struct (at (ptr T)) (brand (brand $k))))",
     "(forall (T) ((Copy T)) (forall ($k) ((Eq T)) (struct (at (ptr T)) (brand (brand $k)))))"),
    ("(empty &x &y)", "(empty &x) (empty) (empty &y)"),
    ("(jmp done %n)", "(jmp (done) %n)"),
    ("(fun eq bool", "(method eq bool"),
    ("(instance Eq (u32))", "(instance Eq (forall () () (u32)))"),
)


def packages() -> list[Path]:
    """The library packages and every example package."""
    return [LIB / name for name in LIBRARY] + sorted(p.parent for p in (ROOT / "examples").glob("*/kur.toml"))


def round_trip(package: Path) -> tuple[str, Optional[str]]:
    """(package, None if it does not build, else what the round trip changed, or "")."""
    sys.setrecursionlimit(20000)
    try:
        resolver = resolve(package)
        program = lower(resolver, signatures(resolver))
    except KursError:
        return package.name, None
    text = write_program(program)
    copy = parse_program(text)
    if write_program(copy) != text:
        return package.name, "the text changed"
    if emit_c(copy, check_program(copy)) != emit_c(program, check_program(program)):
        return package.name, "the C changed"
    return package.name, ""


class KursReaderTests(unittest.TestCase):
    def test_optional_evidence_does_not_discard_instruction_position(self):
        program = parse_program("""(fun main i32 ()
  (entry () (= %n (const i32 0) (evidence)) (ret %n)))""")
        instruction = program.funs["main"].blocks[0].instrs[0]
        self.assertEqual(instruction.pos, (2, 13))
        check_program(program)

    def test_atoms_are_rejected_where_structural_forms_are_required(self):
        for source in ("garbage", "(fun main i32 () garbage)", "(type T (struct u32))"):
            with self.subTest(source=source), self.assertRaisesRegex(KursError, "expected a list"):
                parse_program(source)

    def test_copy_kind_requires_a_parameter_name(self):
        with self.assertRaisesRegex(KursError, "Copy requires a type parameter name"):
            parse_program("(type T (forall (A) ((Copy (tuple u32))) A))")

    def test_place_parameters_of_function_types_and_trait_methods(self):
        program = parse_program(CANONICAL)
        plain = program.types["Callback"].body.fields[0][1]
        self.assertEqual(plain.places, frozenset({0}))
        self.assertEqual(program.traits["Eq"].methods[1].places, frozenset({0}))
        self.assertEqual(program.traits["Eq"].methods[0].places, frozenset())


class KursWriterTests(unittest.TestCase):
    def test_canonical_text_is_written_back_unchanged(self):
        self.assertEqual(write_program(parse_program(CANONICAL)), CANONICAL)

    def test_other_spellings_are_written_canonically(self):
        for canonical, variant in VARIANTS:
            with self.subTest(variant=variant):
                self.assertIn(canonical, CANONICAL)
                self.assertEqual(write_program(parse_program(CANONICAL.replace(canonical, variant))), CANONICAL)

    def test_names_that_are_not_words_are_string_literals(self):
        program = parse_program('(extern fun "two words" i32 ("(" ""))')
        self.assertEqual(write_program(program), '(extern fun "two words" i32 ("(" ""))\n')

    def test_every_lowered_program_round_trips(self):
        with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            results = list(pool.map(round_trip, packages()))
        self.assertGreaterEqual(sum(problem is not None for _, problem in results), 50)
        for name, problem in results:
            with self.subTest(package=name):
                self.assertIn(problem, ("", None))


if __name__ == "__main__":
    unittest.main()
