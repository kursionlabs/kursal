"""Cast authorization and package boundaries are diagnosed before core checking."""
import unittest

from kursal.lower import LowerError
from kursal.sexpr import KursError
from tests import test_closure_boundary


class CastTypingTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def test_noninteger_casts_require_authorization(self):
        for source, target in (('bool', 'u32'), ('f32', 'bool'), ('u32', '*u32'),
                               ('*u32', 'usize'), ('(u32,)', 'u32')):
            with self.subTest(source=source, target=target), self.assertRaisesRegex(LowerError, 'unsafe'):
                self.run_source(f'fn bad(x: {source}) -> {target} {{ x as {target} }} '
                                'fn main() -> i32 { 0i32 }', 0)

    def test_cast_cannot_create_an_existential_package(self):
        with self.assertRaisesRegex(LowerError, 'existential'):
            self.run_source("fn bad(x: u32) -> exists 'a. core::cap::Brand['a] { "
                            "unsafe (x as exists 'a. core::cap::Brand['a]) } "
                            'fn main() -> i32 { 0i32 }', 0)

    def test_cast_cannot_erase_an_existential_package(self):
        with self.assertRaisesRegex(LowerError, 'existential'):
            self.run_source("fn bad(x: exists 'a. core::cap::Brand['a]) -> u32 { unsafe (x as u32) } "
                            'fn main() -> i32 { 0i32 }', 0)

    def test_safe_integer_casts_keep_modular_semantics(self):
        self.run_source('''fn main() -> i32 {
            let x = -1i8;
            if (x as u32) == 4294967295u32 && (255u32 as i8) == -1i8 { 7i32 } else { 0i32 }
        }''', 7)

    def test_number_casts_convert_by_value_without_authorization(self):
        self.run_source('''fn main() -> i32 {
            let half = 2.5f64;
            let nan = 0.0f64 / 0.0f64;
            let big = 1.0f64 / 0.0f64;
            let small = (half as i64) == 2i64 && ((-half) as i64) == -2i64 && (7i64 as f64) == 7.0
                && ((-1.5f32) as u8) == 0u8 && (300.0f32 as u8) == 255u8;
            let saturated = (nan as i32) == 0i32 && (big as i64) == 9223372036854775807i64
                && ((-big) as i64) == -9223372036854775808i64 && (big as usize) == !0usize
                && ((-big) as isize) < 0isize && (3.9f32 as usize) == 3usize;
            let widths = (16777217u32 as f32) == 16777216.0f32 && ((0.1f64 as f32) as f64) != 0.1f64
                && (18446744073709551615u64 as f64) == 18446744073709551616.0f64;
            if small && saturated && widths { 7i32 } else { 0i32 }
        }''', 7)

    def test_authorized_scalar_and_aggregate_casts(self):
        self.run_source('''fn main() -> i32 {
            let pair = (7u32, 9u32);
            let rebuilt = unsafe (pair as (i32, i32));
            if unsafe (true as u32) == 1u32 { rebuilt.0 } else { 0i32 }
        }''', 7)

    def test_a_type_holding_itself_in_an_empty_array_is_cast(self):
        # An array of length 0 holds no value: the walks for what a cast may not take or make, and for a
        # layout the compiler chooses, stop there instead of unfolding the type again.
        self.run_source('''struct R { a: [R; 0], b: u32 }
            fn same(x: (u64, R)) -> (u64, R) { unsafe (x as (u64, R)) }
            fn main() -> i32 { 7i32 }''', 7)

    def test_authorization_does_not_extend_to_nested_casts(self):
        with self.assertRaisesRegex(LowerError, 'unsafe'):
            self.run_source('fn bad(x: bool) -> bool { unsafe ((x as u32) as bool) } '
                            'fn main() -> i32 { 0i32 }', 0)

    def test_unsafe_function_and_lifted_closure_authorize_casts(self):
        self.run_source('''unsafe fn convert(x: bool) -> i32 {
            let f = |y: bool| y as i32;
            f(x) + (x as i32) + 5
        }
        fn main() -> i32 { unsafe convert(true) }''', 7)

    def test_values_of_abstract_types_are_not_cast(self):
        with self.assertRaisesRegex(KursError, 'an unsafe cast cannot produce or take apart'):
            self.run_source('''forall T, U. unsafe fn convert(x: T) -> U { x as U }
                fn main() -> i32 { (unsafe convert[(u32, u32), (i32, i32)]((7u32, 9u32))).0 }
            ''', 7)

    def test_nominal_aggregate_cast_preserves_representation(self):
        self.run_source('''exposed struct Pair { x: u32, y: u32 }
            fn main() -> i32 {
                let p = Pair.{ x: 7u32, y: 9u32 };
                let q = unsafe (p as (i32, i32));
                q.0
            }''', 7)

    def test_pointer_casts_are_supported(self):
        self.run_source('''fn main() -> i32 {
            let p = unsafe (0usize as *u32);
            let q = unsafe (p as *u8);
            if unsafe (q as usize) == 0usize { 7i32 } else { 0i32 }
        }''', 7)

    def test_target_restrictions_are_backend_errors(self):
        with self.assertRaisesRegex(KursError, 'raw pointers only with pointers or integers') as error:
            self.run_source('fn main() -> i32 { let x = unsafe ((unsafe (0usize as *u32)) as f32); 0i32 }', 0)
        self.assertNotIsInstance(error.exception, LowerError)

    def test_diverging_operand_needs_no_cast(self):
        self.run_source('''fn main() -> i32 {
            let x = ((return 7i32)) as *u32;
            0i32
        }''', 7)
