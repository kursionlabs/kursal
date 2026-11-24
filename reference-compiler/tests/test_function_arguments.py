"""Function-path arguments are checked wherever paths denote callable values."""
import unittest

from kursal.infer import InferError
from tests import test_closure_boundary


DECLS = '''forall T. fn id(x: T) -> T { x }
    forall T. exposed struct Box { value: T }
    forall T. impl Box[T] { fn get(self: Box[T]) -> T { self.value } }
'''


class FunctionArgumentTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def reject(self, body, prefix=''):
        with self.assertRaisesRegex(InferError, 'type argument'):
            self.run_source(DECLS + prefix + 'fn main() -> i32 { ' + body + ' }', 7)

    def test_namespace_arguments_on_calls_and_values(self):
        self.reject('package[bool]::id(7i32)')
        self.reject('let f = package[bool]::id[i32]; f(7i32)')
        self.reject('package[bool]::Box[i32]::get(Box.{ value: 7i32 })')

    def test_reexported_method_namespace_is_not_an_impl_target(self):
        self.reject('package[i32]::get(Box.{ value: 7i32 })', 'export package::Box::get; ')

    def test_local_callable_arguments_are_not_ignored(self):
        self.reject('let f = id[i32]; f[bool](7i32)')
        self.reject('let f = id[i32]; let g = f[bool]; g(7i32)')
        self.reject('let f = |x: i32| x; f[bool](7i32)')

    def test_impl_qualifier_argument_count(self):
        self.reject('Box[i32, bool]::get(Box.{ value: 7i32 })')

    def test_named_function_argument_counts(self):
        self.reject('id[i32, bool](7i32)')
        self.reject('let f = id[i32, bool]; f(7i32)')

    def test_valid_function_paths_and_values(self):
        self.run_source(DECLS + '''use package::id as identity;
            fn main() -> i32 {
                let f = package::id[i32];
                identity[i32](f(7i32))
            }''', 7)

    def test_impl_qualifiers_and_method_values(self):
        for path in ('Box[i32]::get', 'Box::get[i32]', 'package::Box[i32]::get'):
            with self.subTest(path=path):
                self.run_source(DECLS + 'fn main() -> i32 { let f = ' + path + '; f(Box.{ value: 7i32 }) }', 7)

    def test_alias_and_imported_method_inference(self):
        self.run_source(DECLS + '''use package::Box as B;
            use package::Box::get;
            fn main() -> i32 { B[i32]::get(Box.{ value: get(Box.{ value: 7i32 }) }) }''', 7)

    def test_brand_qualified_method_value_is_specialized(self):
        self.run_source('''forall 'a. exposed struct Tagged { witness: core::cap::Brand['a], value: i32 }
            forall 'a. impl Tagged['a] { fn get(self: Tagged['a]) -> i32 { self.value } }
            fn main() -> i32 {
                let exists 'a. witness = core::cap::fresh();
                let f = Tagged['a]::get;
                f(Tagged.{ witness, value: 7i32 })
            }''', 7)
