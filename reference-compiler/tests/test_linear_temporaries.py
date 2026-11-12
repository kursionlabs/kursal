"""Discarded results, projected temporaries, and ownership source locations."""
import unittest

from kursal.lower import LowerError
from kursal.placeflow import OwnershipError
from tests import test_closure_boundary

PREFIX = '''exposed struct Token! { n: u32 }
fn make(n: u32) -> Token { Token.{ n } }
fn take(t: Token) -> u32 { let { n } = t; n }
'''


class LinearTemporaryTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def test_discarded_expression_results_are_upstream(self):
        for expression in ('make(7u32)', 'Token.{ n: 7u32 }',
                           '(make(7u32), 1u32)', 'if true { make(7u32) } else { make(9u32) }'):
            with self.subTest(expression=expression), self.assertRaisesRegex(LowerError, 'discard.*linear'):
                self.run_source(PREFIX + 'fn main() -> i32 { ' + expression + '; 0i32 }', 0)

    def test_temporary_projection_cannot_drop_linear_sibling(self):
        with self.assertRaisesRegex(LowerError, 'projection.*linear'):
            self.run_source(PREFIX + '''fn pair() -> (Token, u32) { (make(7u32), 3u32) }
                fn main() -> i32 { (pair().1 as i32) }''', 3)

    def test_temporary_projection_may_drop_copy_sibling(self):
        self.run_source(PREFIX + '''fn pair() -> (Token, u32) { (make(7u32), 3u32) }
            fn main() -> i32 { (take(pair().0) as i32) }''', 7)

    def test_abandoned_operand_reports_its_origin(self):
        source = PREFIX + '''fn combine(t: Token, n: u32) -> u32 { take(t) + n }
fn main() -> i32 {
    combine(
        make(7u32),
        { return 0i32; 2u32 }
    );
    0i32
}'''
        with self.assertRaisesRegex(OwnershipError, 'temporary expression result') as error:
            self.run_source(source, 0)
        self.assertEqual(error.exception.pos, (7, 9))

    def test_assignment_diagnostic_points_to_assignment(self):
        source = PREFIX + '''fn main() -> i32 {
    var t = make(7u32);
    t = make(
        9u32
    );
    (take(t) as i32)
}'''
        with self.assertRaisesRegex(OwnershipError, 'overwrite') as error:
            self.run_source(source, 0)
        self.assertEqual(error.exception.pos, (6, 5))

    def test_unused_value_diagnostic_points_to_binding(self):
        source = PREFIX + '''fn main() -> i32 {
    let t = make(7u32);
    0i32
}'''
        with self.assertRaisesRegex(OwnershipError, 'not consumed') as error:
            self.run_source(source, 0)
        self.assertEqual(error.exception.pos, (5, 5))

    def test_temporary_root_does_not_alias_a_parameter(self):
        self.run_source('''fn add(a: u32, b: u32) -> u32 { a + b }
            fn f(&t0: u32) -> u32 { add(1u32, 2u32) + t0 }
            fn main() -> i32 { var n = 7u32; (f(&n) as i32) }
            ''', 10)

    def test_copy_expression_results_may_be_discarded(self):
        self.run_source('fn main() -> i32 { 7u32; (1u32, 2u32); 3i32 }', 3)

    def test_duplicate_place_roots_are_core_errors(self):
        from kursal.check import CheckError, check_program
        from kursal.kurs import parse_program
        for signature, locals_, params in (
            ('()', '(locals (&x u32) (&x u32))', '()'),
            ('(u32)', '(locals (&x u32))', '((&x u32))'),
            ('(u32 u32)', '', '((&x u32) (&x u32))'),
        ):
            with self.subTest(params=params, locals=locals_), self.assertRaisesRegex(CheckError, 'duplicate_place_root'):
                check_program(parse_program('(fun f u32 ' + signature + ' ' + locals_ +
                    ' (entry ' + params + ' (= %n (const u32 1))'
                    ' (= %u (store &x %n)) (= %r (load &x)) (ret %r)))'))

    def test_call_position_is_not_its_last_argument(self):
        program = self.run_source(PREFIX + '''fn main() -> i32 {
    take(
        make(7u32)
    );
    0i32
}''', 0)
        calls = [i for f in program.funs.values() for b in f.blocks for i in b.instrs
                 if i.op == 'call' and i.args[0].endswith('::take')]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].pos, (5, 5))

    def test_operand_survives_branch_in_later_argument(self):
        self.run_source(PREFIX + '''fn combine(t: Token, n: u32) -> u32 { take(t) + n }
            fn f(c: bool) -> u32 { combine(make(7u32), if c { 2u32 } else { 3u32 }) }
            fn main() -> i32 { ((f(true) + f(false)) as i32) }
            ''', 19)

    def test_nested_projection_checks_each_discarded_sibling(self):
        with self.assertRaisesRegex(LowerError, 'projection.*linear'):
            self.run_source(PREFIX + '''fn pair() -> ((Token, u32), u32) { ((make(7u32), 3u32), 4u32) }
                fn main() -> i32 { ((pair().0).1 as i32) }''', 3)
