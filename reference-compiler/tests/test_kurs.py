"""The typed KURS reader validates container shapes and retains source positions."""

import unittest

from kursal.check import check_program
from kursal.kurs import parse_program
from kursal.sexpr import KursError


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
