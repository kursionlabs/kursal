"""Abstract values pass through unchanged; shape-dependent operations need a known shape."""
import unittest

from kursal.infer import InferError
from kursal.lower import LowerError
from tests import test_closure_boundary


class AbstractShapeTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def test_generic_wildcard_match_is_not_a_shape_proof(self):
        for header in ('forall T.', 'forall T where Copy[T].'):
            for scrutinee in ('x', '&x'):
                with self.subTest(header=header, scrutinee=scrutinee), self.assertRaisesRegex(LowerError, 'match'):
                    self.run_source(header + ' fn bad(x: T) -> () { match ' + scrutinee + ' { _ => () } } '
                                    'fn main() -> i32 { 7i32 }', 7)

    def test_projection_and_destructuring_require_known_shapes(self):
        for body in ('let y = x.field; ()', 'let y = x.0; ()', 'let (a, b) = x; ()',
                     'let { field } = x; ()', 'let [a, b] = x; ()'):
            with self.subTest(body=body), self.assertRaises(InferError):
                self.run_source('forall T. fn bad(x: T) -> () { ' + body + ' } '
                                'fn main() -> i32 { 7i32 }', 7)

    def test_existential_opening_requires_a_package(self):
        with self.assertRaisesRegex(InferError, 'opens'):
            self.run_source("forall T. fn bad(x: T) -> () { let exists 'a. y = x; () } "
                            'fn main() -> i32 { 7i32 }', 7)

    def test_unary_operators_require_primitive_types(self):
        for op in ('-', '!'):
            with self.subTest(op=op), self.assertRaisesRegex(InferError, 'operator'):
                self.run_source('forall T. fn bad(x: T) -> T { ' + op + 'x } '
                                'fn main() -> i32 { 7i32 }', 7)

    def test_generic_values_survive_aggregate_temporaries_and_branches(self):
        self.run_source('''exposed struct Token! { n: i32 }
            forall T. fn pass(x: T, flag: bool) -> T {
                let pair = (x, if flag { 1u32 } else { 2u32 });
                let (value, count) = pair;
                if count == 1u32 { value } else { value }
            }
            fn main() -> i32 {
                let { n } = pass(Token!.{ n: 7i32 }, true);
                n
            }''', 7)

    def test_known_enum_can_carry_abstract_payload(self):
        self.run_source('''forall T. exposed enum E { Item(T) }
            forall T. fn unwrap(x: E[T]) -> T { match x { E::Item(value) => value } }
            exposed struct Token! { n: i32 }
            fn main() -> i32 { let { n } = unwrap(E::Item.(Token!.{ n: 7i32 })); n }''', 7)

    def test_generic_existential_payload_passes_through(self):
        self.run_source("""forall T. fn pass(x: exists 'a. (core::cap::Brand['a], T)) -> T {
                let exists 'a. (witness, value) = x;
                value
            }
            fn main() -> i32 {
                let exists 'a. witness = core::cap::fresh();
                pass(pack 'a. (witness, 7i32))
            }""", 7)

    def test_generic_pointer_load_and_store_do_not_unfold_pointee(self):
        self.run_source('''forall T. unsafe fn pass(x: T) -> T {
                let p = core::mem::alloca[T]();
                unsafe *p = x;
                *p
            }
            exposed struct Token! { n: i32 }
            fn main() -> i32 { let { n } = unsafe pass(Token!.{ n: 7i32 }); n }''', 7)
