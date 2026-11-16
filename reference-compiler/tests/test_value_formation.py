import unittest
from kursal.sigs import SigError
from kursal.sexpr import KursError
from kursal.infer import InferError
from tests import test_closure_boundary


class ValueFormationTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def test_brands_are_not_value_types(self):
        for ty in ("'a", "*'a", "('a, u32)", "['a; 2]", "fn('a) -> u32", "fn(u32) -> 'a", "exists 'b. 'b"):
            with self.subTest(ty=ty), self.assertRaisesRegex(KursError, 'syntax error'):
                self.run_source("forall 'a. fn bad(x: " + ty + ') -> () { () } fn main() -> i32 { 7i32 }', 7)

    def test_nominal_fields_require_value_types(self):
        for decl in ("forall 'a. exposed struct Bad { field: 'a }", "forall 'a. exposed tuple Bad('a);", "forall 'a. exposed enum Bad { Item('a) }"):
            with self.subTest(decl=decl), self.assertRaisesRegex(KursError, 'syntax error'):
                self.run_source(decl + ' fn main() -> i32 { 7i32 }', 7)

    def test_copy_constrained_nominal_application(self):
        with self.assertRaisesRegex(SigError, 'Copy'):
            self.run_source('''exposed struct Token! {}
                forall T where Copy[T]. exposed struct Box { value: T }
                fn bad(x: Box[Token!]) -> () { () }
                fn main() -> i32 { 7i32 }''', 7)

    def test_inferred_copy_constraints(self):
        for expression in ('consume(token)', 'let f = consume[Token!]; f(token)', 'let b = Box.{ value: token }; ()'):
            with self.subTest(expression=expression), self.assertRaisesRegex(InferError, 'Copy'):
                self.run_source('''exposed struct Token! {}
                    forall T where Copy[T]. fn consume(x: T) -> () { () }
                    forall T where Copy[T]. exposed struct Box { value: T }
                    fn main() -> i32 { let token = Token!.{}; ''' + expression + '; 7i32 }', 7)

    def test_copy_constraints_wait_for_later_inference(self):
        self.run_source('''forall T where Copy[T]. fn twice(x: T) -> (T, T) { (x, x) }
            fn main() -> i32 { let f = |x| twice(x); let (a, b) = f(3i32); a + b + 1i32 }''', 7)

    def test_generic_caller_must_supply_copy(self):
        with self.assertRaisesRegex(InferError, 'Copy'):
            self.run_source('''forall T where Copy[T]. fn consume(x: T) -> () { () }
                forall U. fn bad(x: U) -> () { consume(x) }
                fn main() -> i32 { 7i32 }''', 7)

    def test_nested_nominal_copy_checks_wait_for_declarations(self):
        self.run_source('''exposed struct First { value: Box[Later] }
            forall T where Copy[T]. exposed struct Box { value: T }
            exposed struct Later { n: i32 }
            fn main() -> i32 {
                let x = First.{ value: Box.{ value: Later.{ n: 7i32 } } };
                x.value.value.n
            }''', 7)
        with self.assertRaisesRegex(SigError, 'Copy'):
            self.run_source('''exposed struct First { value: Box[Later!] }
                forall T where Copy[T]. exposed struct Box { value: T }
                exposed struct Later! {}
                fn main() -> i32 { 7i32 }''', 7)

    def test_trait_parameter_copy_constraint(self):
        with self.assertRaisesRegex(InferError, 'Copy'):
            self.run_source('''exposed struct Token! {}
                forall T where Copy[T]. trait Take { fn take(x: T) -> (); }
                fn main() -> i32 { Take::take(Token!.{}); 7i32 }''', 7)

    def test_copy_nominal_annotation_in_function_body(self):
        with self.assertRaisesRegex(SigError, 'Copy'):
            self.run_source('''exposed struct Token! {}
                forall T where Copy[T]. exposed struct Box { value: T }
                fn main() -> i32 { let x: Box[Token!] = Box.{ value: Token!.{} }; 7i32 }''', 7)
