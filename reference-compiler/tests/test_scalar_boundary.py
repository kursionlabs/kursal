"""Surface and core literals use strict ranges without losing signed minima."""
import unittest

from kursal.check import CheckError, check_program
from kursal.infer import InferError
from kursal.lower import LowerError
from kursal.sigs import SigError
from kursal.kurs import parse_program
from tests import test_closure_boundary


class ScalarBoundaryTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def test_surface_integer_ranges_are_diagnosed_upstream(self):
        for ty, text in (('i8', '128'), ('i8', '-129'), ('u8', '256'), ('u8', '-1'),
                         ('i64', '9223372036854775808'), ('u64', '18446744073709551616')):
            with self.subTest(ty=ty, text=text), self.assertRaisesRegex(LowerError, 'literal.*fit'):
                self.run_source('fn f() -> ' + ty + ' { ' + text + ' } fn main() -> i32 { 0i32 }', 0)

    def test_surface_float_overflow_is_diagnosed_upstream(self):
        for ty, text in (('f32', '1' + '0' * 39 + '.0'), ('f32', '-1' + '0' * 39 + '.0'), ('f64', '1' + '0' * 309 + '.0')):
            with self.subTest(ty=ty), self.assertRaisesRegex(LowerError, 'literal.*fit'):
                self.run_source('fn f() -> ' + ty + ' { ' + text + ' } fn main() -> i32 { 0i32 }', 0)

    def test_folded_constants_use_the_same_literal_ranges(self):
        for ty, text in (('i8', '128'), ('i8', '-129'), ('u8', '-1'), ('f32', '1' + '0' * 39 + '.0'), ('f64', '1' + '0' * 309 + '.0')):
            with self.subTest(ty=ty, text=text), self.assertRaises(SigError):
                self.run_source('const BAD: ' + ty + ' = ' + text + '; fn main() -> i32 { 0i32 }', 0)

    def test_signed_minima_and_unsigned_maxima_execute(self):
        self.run_source('''const MIN: i64 = -9223372036854775808;
            fn main() -> i32 {
                let a = -128i8; let b = -32768i16; let c = -2147483648i32;
                let d = -9223372036854775808i64; let u = 18446744073709551615u64;
                if a == -128 && b == -32768 && c == -2147483648 && d == MIN && u == 18446744073709551615 {
                    7i32
                } else { 1i32 }
            }''', 7)

    def test_arithmetic_still_wraps(self):
        self.run_source('''const WRAP: u8 = 250 + 10;
            const NEG: i8 = -128; const AGAIN: i8 = -NEG;
            fn main() -> i32 {
                let x = 250u8; let y = x + 10;
                if WRAP == 4 && y == 4 && AGAIN == -128 { 7i32 } else { 1i32 }
            }''', 7)

    def test_core_rejects_positive_signed_minimum_magnitude(self):
        for ty, literal in (('i8', '128'), ('i64', '9223372036854775808')):
            for source in ('(data d ' + ty + ' ' + literal + ')',
                           '(fun f ' + ty + ' () (entry () (= %n (const ' + ty + ' ' + literal + ')) (ret %n)))'):
                with self.subTest(source=source), self.assertRaises(CheckError):
                    check_program(parse_program(source))

    def test_undetermined_float_gets_inference_diagnostic(self):
        with self.assertRaisesRegex(InferError, 'floating literal.*not determined'):
            self.run_source('fn main() -> i32 { let x = 1.5; 0i32 }', 0)

    def test_core_integer_spelling_and_bounds(self):
        for ty, text in (('i8', '+1'), ('i8', '01'), ('i8', '1_0'), ('i8', '-00'), ('i8', '-129'), ('i8', '128'),
                         ('i8', '-0'), ('u8', '-0'), ('bool', '1'), ('bool', 'True')):
            with self.subTest(ty=ty, text=text), self.assertRaises(CheckError):
                check_program(parse_program('(data d ' + ty + ' ' + text + ')'))
        for ty, text in (('i8', '-128'), ('i8', '127'), ('u8', '255'), ('u8', '0'), ('bool', 'true')):
            check_program(parse_program('(data d ' + ty + ' ' + text + ')'))

    def test_overflow_diagnostic_points_at_literal(self):
        source = 'fn f() -> u8 {\n    256\n}\nfn main() -> i32 { 0i32 }'
        with self.assertRaises(LowerError) as error:
            self.run_source(source, 0)
        self.assertEqual(error.exception.pos, (2, 5))

    def test_finite_inferred_and_folded_floats_execute(self):
        self.run_source('''const V: f32 = -1.5;
            fn value() -> f32 { -1.5 }
            fn main() -> i32 {
                let x: f32 = 1.5;
                if value() == V && x == 1.5 { 7i32 } else { 1i32 }
            }''', 7)
