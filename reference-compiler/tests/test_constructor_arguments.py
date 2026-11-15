"""Constructor arguments constrain inference; none may be silently discarded."""
import unittest

from kursal.infer import InferError
from tests import test_closure_boundary


DECLS = '''forall T, U. exposed struct S { first: T, second: U }
    forall T, U. exposed tuple Pair(T, U);
    forall T, U. exposed enum E { Item(T, U), Empty }
    exposed struct Plain { n: u32 }
    exposed tuple Single(u32);
    exposed enum Flag { On }
'''


class ConstructorArgumentTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def reject(self, body):
        with self.assertRaisesRegex(InferError, 'type arguments'):
            self.run_source(DECLS + 'fn main() -> i32 { ' + body + ' }', 7)

    def test_excess_arguments_on_generic_constructors(self):
        for expression in ('S[u32, u32, bool].{ first: 3u32, second: 4u32 }',
                           'Pair[u32, u32, bool].(3u32, 4u32)',
                           'E[u32, u32, bool]::Item.(3u32, 4u32)',
                           'E[u32, u32, bool]::Empty'):
            with self.subTest(expression=expression):
                self.reject('let x = ' + expression + '; 7i32')

    def test_nongeneric_constructors_reject_arguments(self):
        for expression in ('Plain[u32].{ n: 7u32 }', 'Single[u32].(7u32)', 'Flag[u32]::On'):
            with self.subTest(expression=expression):
                self.reject('let x = ' + expression + '; 7i32')

    def test_match_patterns_reject_excess_arguments(self):
        self.reject('let value = E::Item.(3u32, 4u32); match value { '
                    'E[u32, u32, bool]::Item(a, b) => (a + b) as i32, E::Empty => 0i32 }')

    def test_arguments_on_variant_segment_are_not_ignored(self):
        for expression in ('E[u32, u32]::Empty[bool]', 'E[u32, u32]::Item[bool].(3u32, 4u32)'):
            with self.subTest(expression=expression):
                self.reject('let x = ' + expression + '; 7i32')
        self.reject('let value = E::Item.(3u32, 4u32); match value { '
                    'E::Item[bool](a, b) => (a + b) as i32, E::Empty => 0i32 }')

    def test_namespace_arguments_are_not_ignored(self):
        for expression in ('package[bool]::Plain.{ n: 7u32 }',
                           'package[bool]::Single.(7u32)', 'package[bool]::Flag::On'):
            with self.subTest(expression=expression):
                self.reject('let x = ' + expression + '; 7i32')
        with self.assertRaisesRegex(InferError, 'type arguments'):
            self.run_source(DECLS + 'export package::Flag::On; '
                            'fn main() -> i32 { let x = package[bool]::On; 7i32 }', 7)

    def test_omitted_partial_and_full_arguments_infer_remaining_types(self):
        for args in ('', '[u32]', '[u32, u32]'):
            with self.subTest(args=args):
                self.run_source(DECLS + '''fn main() -> i32 {
                    let s = S''' + args + '''.{ first: 3u32, second: 4u32 };
                    let (a, b) = Pair''' + args + '''.(s.first, s.second);
                    match E''' + args + '''::Item.(a, b) {
                        E::Item(x, y) => (x + y) as i32, E::Empty => 0i32
                    }
                }''', 7)

    def test_payload_free_variant_uses_expected_type(self):
        self.run_source(DECLS + '''fn main() -> i32 {
            let value: E[u32, bool] = E[u32]::Empty;
            match value { E::Item(a, b) => 0i32, E::Empty => 7i32 }
        }''', 7)

    def test_brand_arguments_remain_inferred_or_explicit(self):
        for args in ('', "['a]", "['a, u32]"):
            with self.subTest(args=args):
                self.run_source('''forall 'k, T. exposed struct Tagged { witness: core::cap::Brand['k], value: T }
                    fn main() -> i32 {
                        let exists 'a. witness = core::cap::fresh();
                        let tagged = Tagged''' + args + '''.{ witness, value: 7u32 };
                        tagged.value as i32
                    }''', 7)
