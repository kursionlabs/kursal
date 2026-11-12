"""Unsupported primitive operands are source errors before core lowering."""
import unittest

from kursal.infer import InferError
from tests import test_closure_boundary


class PrimitiveTypingTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def test_boolean_arithmetic_and_ordering_are_rejected(self):
        for op in ('+', '-', '*', '/', '%', '&', '|', '^', '<<', '>>', '<', '<=', '>', '>='):
            with self.subTest(op=op), self.assertRaisesRegex(InferError, 'operator'):
                self.run_source('fn bad(a: bool, b: bool) -> bool { a ' + op + ' b } '
                                'fn main() -> i32 { 0i32 }', 0)

    def test_float_bitwise_and_remainder_are_rejected(self):
        for op in ('%', '&', '|', '^', '<<', '>>'):
            with self.subTest(op=op), self.assertRaisesRegex(InferError, 'operator'):
                self.run_source('fn bad(a: f32, b: f32) -> f32 { a ' + op + ' b } '
                                'fn main() -> i32 { 0i32 }', 0)

    def test_invalid_unary_operands_are_rejected(self):
        for ty, op in (('bool', '-'), ('f32', '!'), ('(u32, u32)', '-'), ('(u32, u32)', '!')):
            with self.subTest(ty=ty, op=op), self.assertRaisesRegex(InferError, 'operator'):
                self.run_source('fn bad(x: ' + ty + ') -> ' + ty + ' { ' + op + 'x } '
                                'fn main() -> i32 { 0i32 }', 0)

    def test_product_and_brand_equality_are_rejected(self):
        for declaration in ('fn bad(x: (u32, u32), y: (u32, u32)) -> bool { x == y }',
                            "forall 'a. fn bad(x: Brand['a], y: Brand['a]) -> bool { x == y }"):
            with self.subTest(declaration=declaration), self.assertRaisesRegex(InferError, 'operator'):
                self.run_source(declaration + ' fn main() -> i32 { 0i32 }', 0)

    def test_arbitrary_type_parameter_is_not_numeric(self):
        with self.assertRaisesRegex(InferError, 'operator'):
            self.run_source('forall T. fn bad(x: T, y: T) -> T { x + y } '
                            'fn main() -> i32 { 0i32 }', 0)

    def test_operator_constraints_wait_for_closure_argument_inference(self):
        self.run_source('''fn main() -> i32 {
            let f = |x| x + x;
            f(3i32) + 1
        }''', 7)
        with self.assertRaisesRegex(InferError, 'operator'):
            self.run_source('''fn main() -> i32 {
                let f = |x| x + x;
                if f(true) { 7i32 } else { 0i32 }
            }''', 0)

    def test_pointer_equality_is_valid_but_ordering_is_not(self):
        self.run_source('''fn same(a: *u32, b: *u32) -> bool { a == b }
            fn main() -> i32 { 7i32 }''', 7)
        with self.assertRaisesRegex(InferError, 'operator'):
            self.run_source('''fn bad(a: *u32, b: *u32) -> bool { a < b }
                fn main() -> i32 { 0i32 }''', 0)

    def test_valid_boolean_float_and_integer_operations(self):
        self.run_source('''fn negate(x: i8) -> i8 { -x }
            fn invert(x: u8) -> u8 { !x }
            fn float_ops(a: f32, b: f32) -> bool {
                (a + b) == 5.0 && (a - b) == 1.0 && (a * b) == 6.0 &&
                (a / b) == 1.5 && -a < b && a >= b && a != b
            }
            fn bool_ops(a: bool, b: bool) -> bool { !a == b && (a || b) && a != b }
            fn main() -> i32 {
                if negate(-7i8) == 7 && invert(0u8) == 255 && float_ops(3.0, 2.0) && bool_ops(false, true) {
                    7i32
                } else { 1i32 }
            }''', 7)

    def test_diverging_operand_does_not_require_a_primitive_value(self):
        self.run_source('''fn f() -> bool { ((return true)) == ((return false)) }
            fn main() -> i32 { if f() { 7i32 } else { 1i32 } }''', 7)
