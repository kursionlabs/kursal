"""Constant evaluation agrees with emitted arithmetic and short-circuiting."""
import unittest

from kursal.sigs import SigError
from tests import test_closure_boundary


class ConstantRuntimeTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def test_short_circuit_skips_unexecuted_division(self):
        self.run_source('''const A: bool = false && (1u32 / 0u32 == 0u32);
            const B: bool = true || (1u32 % 0u32 == 0u32);
            fn zero() -> u32 { 0u32 }
            fn main() -> i32 {
                let a = false && (1u32 / zero() == 0u32);
                let b = true || (1u32 % zero() == 0u32);
                if a == A && b == B { 7i32 } else { 1i32 }
            }''', 7)

    def test_nested_short_circuit_skips_traps(self):
        self.run_source('''const A: bool = true || ((1u32 / 0u32 + 3u32) << 2u32 == 1u32);
            const B: bool = false && (true || (1u32 / 0u32 == 0u32));
            fn main() -> i32 { if A && !B { 7i32 } else { 1i32 } }
            ''', 7)

    def test_selected_division_by_zero_still_fails(self):
        for expression in ('true && (1u32 / 0u32 == 0u32)', 'false || (1u32 % 0u32 == 0u32)'):
            with self.subTest(expression=expression), self.assertRaisesRegex(SigError, 'division by zero'):
                self.run_source('const X: bool = ' + expression + '; fn main() -> i32 { 0i32 }', 0)

    def test_dead_operands_are_still_type_checked(self):
        for expression in ('true || 1u32', 'false && (true + 1u32)',
                           'true || (true == 1u8)', 'false && (256u8 == 0u8)'):
            with self.subTest(expression=expression), self.assertRaises(SigError):
                self.run_source('const X: bool = ' + expression + '; fn main() -> i32 { 0i32 }', 0)

    def test_bool_integer_comparison_is_rejected(self):
        for expression in ('true == 1u8', 'false != 0u8', '1u8 == true'):
            with self.subTest(expression=expression), self.assertRaises(SigError):
                self.run_source('const X: bool = ' + expression + '; fn main() -> i32 { 0i32 }', 0)

    def test_shift_count_infers_left_operand_type(self):
        self.run_source('''const X: bool = (1u8 << 9) == 2u8;
            const Y: bool = (-8i16 >> 1) == -4i16;
            const Z: bool = (1u8 << 9) == (2u8 << 0);
            fn main() -> i32 { if X && Y && Z { 7i32 } else { 1i32 } }
            ''', 7)

    def test_integer_operator_matrix_matches_runtime(self):
        for ty in ('i8', 'u8', 'i16', 'u16', 'i32', 'u32', 'i64', 'u64', 'isize', 'usize'):
            bits = 64 if ty.endswith('size') else int(ty[1:])
            signed = ty.startswith('i')
            lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
            pairs = [(op, hi, 2) for op in ('+', '-', '*', '/', '%', '&', '|', '^')]
            if signed:
                pairs += [(op, lo, -1) for op in ('/', '%')]
                pairs += [(op, -7, 3) for op in ('/', '%')]
            pairs += [(op, lo if signed else hi, count)
                      for op in ('<<', '>>') for count in ((-1, 0, bits, bits + 1) if signed else (0, bits, bits + 1))]
            declarations, checks = [], []
            for k, (op, a, b) in enumerate(pairs):
                declarations.append(f'const C{k}: {ty} = ({a}{ty}) {op} ({b}{ty});')
                declarations.append(f'fn f{k}(a: {ty}, b: {ty}) -> {ty} {{ a {op} b }}')
                checks.append(f'if f{k}({a}{ty}, {b}{ty}) != C{k} {{ return 1i32; }}')
            with self.subTest(ty=ty):
                self.run_source('\n'.join(declarations) + '\nfn main() -> i32 { ' + ' '.join(checks) + ' 0i32 }', 0)

    def test_failed_probe_does_not_turn_range_error_into_cycle(self):
        with self.assertRaisesRegex(SigError, '256 does not fit u8'):
            self.run_source('''const X: u8 = BAD + 1;
                const BAD: u8 = 256;
                fn main() -> i32 { 0i32 }
                ''', 0)

    def test_shift_operands_require_the_same_type(self):
        with self.assertRaisesRegex(SigError, 'one type'):
            self.run_source('const X: u8 = 1u8 << 1u32; fn main() -> i32 { 0i32 }', 0)

    def test_integer_casts_match_runtime(self):
        self.run_source('''const A: u8 = (-1i32 as u8);
            const B: i8 = (255u32 as i8);
            const C: u64 = (-1i8 as u64);
            fn main() -> i32 {
                let a = -1i32; let b = 255u32; let c = -1i8;
                if (a as u8) == A && (b as i8) == B && (c as u64) == C { 7i32 } else { 1i32 }
            }''', 7)
