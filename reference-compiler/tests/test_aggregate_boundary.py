"""Aggregate/data counts and representations agree across the core and C."""
import unittest

from kursal.check import CheckError, check_program
from kursal.kurs import parse_program
from tests import test_c_names


class AggregateBoundaryTests(unittest.TestCase):
    run_core = test_c_names.CNameTests.run_core

    def test_data_initializers_are_checked_recursively(self):
        cases = (
            '(data x (tuple u8) (256))',
            '(data x (tuple (tuple u8)) ((256)))',
            '(data x (array u8 2) (1))',
            '(data x (array u8 1) (1 2))',
            '(data x (array u8 0) (1))',
            '(data x (array u8 3) ("ab"))',
            '(data x (array u8 1) ("é"))',
            '(data x u8 (1 2))',
            '(data x bool nonsense)',
        )
        for source in cases:
            with self.subTest(source=source), self.assertRaises(CheckError):
                check_program(parse_program(source))

    def test_unsupported_data_types_are_rejected(self):
        for ty in ('(builtin::Pointer u8)', '(exists ($a) (builtin::Brand $a))',
                   '(enum (A u8))', '(builtin::Brand $static)'):
            with self.subTest(ty=ty), self.assertRaises(CheckError):
                check_program(parse_program('(data x ' + ty + ' ())'))

    def test_nested_data_executes(self):
        self.run_core('''(data values (tuple (array i32 2) i32) ((7 9) 3))
            (fun main i32 ()
              (entry () (= %p (data values)) (unsafe (= %v (builtin::unsafe::deref %p)))
                (= (%array %c) (destruct %v (tuple (array i32 2) i32)))
                (= (%a %b) (destruct %array (array i32 2)))
                (= %n (add %a %b)) (= %r (add %n %c)) (ret %r)))''', 19)

    def test_array_fill_repeats_last_copy_element(self):
        self.run_core('''(fun main i32 () (entry ()
            (= %a (const i32 7)) (= %b (const i32 9))
            (= %v (construct (array i32 4) %a %b))
            (= (%w %x %y %z) (destruct %v (array i32 4)))
            (= %p (add %w %x)) (= %q (add %y %z)) (= %r (add %p %q)) (ret %r)))''', 34)

    def test_aggregate_instruction_counts(self):
        cases = (
            '(= %x (construct (tuple i32)))',
            '(= %x (construct (array i32 1)))',
            '(= %x (const i32 1)) (= %a (construct (array i32 0) %x))',
            '(= %a (construct (tuple))) (= %x (destruct %a (tuple)))',
        )
        for instructions in cases:
            with self.subTest(instructions=instructions), self.assertRaises(CheckError):
                check_program(parse_program('(fun f i32 () (entry () ' + instructions +
                    ' (= %r (const i32 0)) (ret %r)))'))

    def test_mixed_byte_strings_use_decoded_utf8_counts(self):
        self.run_core('''(data bytes (array u8 4) ("é" 7 "!"))
            (fun main i32 () (entry () (= %p (data bytes))
              (unsafe (= %v (builtin::unsafe::deref %p)))
              (= (%a %b %c %d) (destruct %v (array u8 4)))
              (= %x (builtin::int_cast %c i32)) (= %y (builtin::int_cast %d i32))
              (= %r (add %x %y)) (ret %r)))''', 40)

    def test_empty_array_representation_is_initialized(self):
        output = self.run_core('''(fun keep i32 ((array u8 0)) (entry ((%a (array u8 0)))
              (= () (destruct %a (array u8 0))) (= %r (const i32 7)) (ret %r)))
            (fun main i32 () (entry ()
            (= %empty (construct (array u8 0)))
            (= %r (call keep %empty)) (ret %r)))''', 7)
        self.assertNotIn('entry_empty', output)

    def test_nested_empty_data(self):
        self.run_core('''(data values (tuple (array u8 0) (tuple) i32) (() () 7))
            (fun main i32 () (entry () (= %p (data values))
              (unsafe (= %v (builtin::unsafe::deref %p)))
              (= (%a %b %n) (destruct %v (tuple (array u8 0) (tuple) i32)))
              (ret %n)))''', 7)

    def test_linear_array_elements_cannot_be_repeated(self):
        with self.assertRaisesRegex(CheckError, 'array_construct_element_copy'):
            check_program(parse_program('''(type L! (tuple i32))
                (fun f (array L 2) () (entry () (= %n (const i32 7))
                  (= %l (construct L %n)) (= %array (construct (array L 2) %l)) (ret %array)))'''))

    def test_float_literals_reject_nonfinite_or_unrepresentable_values(self):
        for ty, literal in (('f32', '1e39'), ('f64', '1e309'), ('f64', 'nan'),
                            ('f64', 'inf'), ('f64', '1_2.0')):
            for source in ('(data x ' + ty + ' ' + literal + ')',
                           '(fun f ' + ty + ' () (entry () (= %n (const ' + ty + ' ' + literal + ')) (ret %n)))'):
                with self.subTest(source=source), self.assertRaises(CheckError):
                    check_program(parse_program(source))

    def test_finite_float_data(self):
        self.run_core('''(data value f32 1.5)
            (fun main i32 () (entry () (= %p (data value))
              (unsafe (= %x (builtin::unsafe::deref %p)))
              (= %expected (const f32 1.5)) (= %same (eq %x %expected))
              (jnz %same (yes) (no)))
              (yes () (= %r (const i32 7)) (ret %r))
              (no () (= %r (const i32 1)) (ret %r)))''', 7)
