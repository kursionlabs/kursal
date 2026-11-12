"""Explicit trait arguments constrain inference without silently ignoring input."""
import unittest

from kursal.infer import InferError
from tests import test_closure_boundary


DECLS = '''forall A, B. trait Pairing { fn sum(a: A, b: B) -> u32; }
    instance Pairing[u32, u32] { fn sum(a: u32, b: u32) -> u32 { a + b } }
'''


class TraitArgumentTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def reject(self, expression, prefix=''):
        with self.assertRaisesRegex(InferError, 'type arguments'):
            self.run_source(DECLS + prefix + 'fn main() -> i32 { (' + expression + ') as i32 }', 7)

    def test_excess_trait_arguments(self):
        self.reject('Pairing[u32, u32, bool]::sum(3u32, 4u32)')

    def test_method_segment_arguments_are_rejected(self):
        self.reject('Pairing[u32, u32]::sum[bool](3u32, 4u32)')

    def test_namespace_arguments_are_rejected(self):
        self.reject('package[bool]::Pairing[u32, u32]::sum(3u32, 4u32)')
        self.reject('package[bool]::sum(3u32, 4u32)', 'export package::Pairing::sum; ')

    def test_imported_method_arguments_are_rejected(self):
        self.reject('sum[bool](3u32, 4u32)', 'use package::Pairing::sum; ')

    def test_associated_parameter_calls_reject_arguments(self):
        for call in ('T[u32]::sum(a, b)', 'T::sum[u32](a, b)'):
            with self.subTest(call=call), self.assertRaisesRegex(InferError, 'type arguments'):
                self.run_source(DECLS + 'forall T where Pairing[T, T]. '
                                'fn add(a: T, b: T) -> u32 { ' + call + ' } '
                                'fn main() -> i32 { add(3u32, 4u32) as i32 }', 7)

    def test_omitted_partial_full_and_placeholder_arguments(self):
        for args in ('', '[u32]', '[u32, u32]', '[_, u32]', '[u32, _]'):
            with self.subTest(args=args):
                self.run_source(DECLS + 'fn main() -> i32 { Pairing' + args + '::sum(3u32, 4u32) as i32 }', 7)

    def test_aliases_and_imported_methods_infer_arguments(self):
        self.run_source(DECLS + '''use package::Pairing as P;
            use package::Pairing::sum;
            fn main() -> i32 { (P[u32]::sum(1u32, 2u32) + sum(2u32, 2u32)) as i32 }''', 7)

    def test_determined_arguments_and_premise_dispatch(self):
        self.run_source('''forall T -> U. trait Convert { fn convert(x: T) -> U; }
            instance Convert[u32 -> i32] { fn convert(x: u32) -> i32 { x as i32 } }
            forall T, U where Convert[T -> U]. fn run(x: T) -> U { T::convert(x) }
            fn main() -> i32 {
                let a = Convert[u32]::convert(3u32);
                let b = run(4u32);
                a + b
            }''', 7)
