"""Copy premises denote in-scope type parameters, not arbitrary type expressions."""
import unittest

from kursal.sigs import SigError
from kursal.resolve import ResolveError
from tests import test_closure_boundary


class CopyPremiseTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def test_copy_requires_a_bare_parameter(self):
        for argument in ('u32', 'Token', 'package::Token', 'T[u32]', '(T, T)', '*T', "'a"):
            with self.subTest(argument=argument), self.assertRaisesRegex(SigError, 'Copy'):
                self.run_source("exposed struct Token {} forall T, 'a where Copy[" + argument + ']. '
                                'fn bad(x: T) -> T { x } fn main() -> i32 { 7i32 }', 7)

    def test_copy_requires_one_argument(self):
        with self.assertRaisesRegex(SigError, 'Copy'):
            self.run_source('forall T, U where Copy[T, U]. fn bad(x: T) -> T { x } '
                            'fn main() -> i32 { 7i32 }', 7)

    def test_unknown_parameter_is_rejected_by_resolution(self):
        with self.assertRaises(ResolveError):
            self.run_source('forall T where Copy[Missing]. fn bad(x: T) -> T { x } '
                            'fn main() -> i32 { 7i32 }', 7)

    def test_repeated_copy_premises_are_idempotent(self):
        self.run_source('''forall T where Copy[T], Copy[T]. fn twice(x: T) -> (T, T) { (x, x) }
            fn main() -> i32 { let (a, b) = twice(3i32); a + b + 1i32 }''', 7)

    def test_determined_parameter_can_require_copy(self):
        self.run_source('''forall T -> U where Copy[U]. trait Get { fn get(x: T) -> U; }
            instance Get[u32 -> i32] { fn get(x: u32) -> i32 { x as i32 } }
            fn main() -> i32 { Get::get(7u32) }''', 7)

    def test_method_can_strengthen_outer_type_before_checking_other_premises(self):
        for premises in ('Copy[U], Copy[T], Ready[T]', 'Ready[T], Copy[T], Copy[U]'):
            with self.subTest(premises=premises):
                self.run_source('''forall T where Copy[T]. trait Ready { fn read(x: T) -> i32; }
                    instance Ready[i32] { fn read(x: i32) -> i32 { x } }
                    forall T. exposed struct Box { value: T }
                    forall T. impl Box[T] {
                        forall U where ''' + premises + '''.
                        fn read(self: Box[T], unused: U) -> i32 { Ready[T]::read(self.value) }
                    }
                    fn main() -> i32 { Box::read(Box.{ value: 7i32 }, ()) }''', 7)

    def test_method_copy_constraint_remains_on_its_signature(self):
        from kursal.infer import InferError
        with self.assertRaisesRegex(InferError, 'Copy'):
            self.run_source('''exposed struct Token! {}
                forall T. exposed struct Box { value: T }
                forall T. impl Box[T] {
                    forall U where Copy[T], Copy[U]. fn drop(self: Box[T], unused: U) -> () { () }
                }
                fn main() -> i32 { Box::drop(Box.{ value: Token!.{} }, ()); 7i32 }''', 7)
