"""Tag inspection preserves owned values and observes lent enums without moving them."""
import unittest
import tempfile
from pathlib import Path

from kursal.infer import InferError
from kursal.lower import LowerError, lower
from kursal.resolve import resolve
from kursal.sigs import signatures
from kursal.check import check_program
from kursal.cgen import emit_c
from kursal.placeflow import OwnershipError
from kursal.sexpr import KursError
from tests import test_closure_boundary
from tests.test_lower import write, compile_and_run


class EnumVariantTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def test_non_enum_operands_are_source_errors(self):
        for ty in ('u32', '(u32, u32)', '*u32', "Brand['static]"):
            for lend in ('', '&'):
                with self.subTest(ty=ty, lend=lend), self.assertRaisesRegex(InferError, 'enum'):
                    self.run_source(f'fn bad(x: {ty}) -> () {{ let result = builtin::enum_variant({lend}x); () }} '
                                    'fn main() -> i32 { 0i32 }', 0)

    def test_abstract_type_parameter_is_not_known_to_be_an_enum(self):
        with self.assertRaisesRegex(InferError, 'enum'):
            self.run_source('forall T. fn bad(x: T) -> () { let result = builtin::enum_variant(&x); () } '
                            'fn main() -> i32 { 0i32 }', 0)

    def test_lent_operand_must_be_a_place(self):
        with self.assertRaisesRegex(InferError, 'place'):
            self.run_source('exposed enum E { A } fn main() -> i32 { '
                            'let tag = builtin::enum_variant(&E::A); 0i32 }', 0)

    def test_owned_copy_enum_returns_value_and_tag(self):
        self.run_source('''exposed enum E { A, B(u32) }
            fn main() -> i32 {
                let (value, tag) = builtin::enum_variant(E::B.(6u32));
                match value { E::A => 0i32, E::B(n) => (n + tag) as i32 }
            }''', 7)

    def test_owned_linear_enum_returns_value_and_tag(self):
        self.run_source('''exposed enum E! { A, B(u32) }
            fn main() -> i32 {
                let (value, tag) = builtin::enum_variant(E::B.(6u32));
                match value { E::A => 0i32, E::B(n) => (n + tag) as i32 }
            }''', 7)

    def test_returned_linear_enum_cannot_be_discarded(self):
        with self.assertRaisesRegex(LowerError, 'linear'):
            self.run_source('''exposed enum E! { A }
                fn main() -> i32 { builtin::enum_variant(E::A); 7i32 }''', 7)

    def test_opaque_enum_tag_observation_requires_exposure(self):
        with tempfile.TemporaryDirectory(prefix='kursal-enum-visibility-') as base:
            directory = Path(base)
            write(directory, {
                'hidden.kur': 'enum E! { A, B } fn make() -> E { E::B } fn consume(x: E) -> () { match x { E::A => (), E::B => () } }',
                'lib.kur': '''use package::hidden::{make, consume};
                    fn main() -> i32 {
                        let value = make();
                        let tag = builtin::enum_variant(&value);
                        consume(value);
                        (tag + 6u32) as i32
                    }''',
            })
            resolver = resolve(directory)
            sigs = signatures(resolver)
            with self.assertRaisesRegex(InferError, 'not exposed'):
                lower(resolver, sigs)

    def test_lent_linear_enum_remains_available(self):
        self.run_source('''exposed enum E! { A, B(u32) }
            fn main() -> i32 {
                let value = E::B.(5u32);
                let a = builtin::enum_variant(&value);
                let b = builtin::enum_variant(&value);
                match value { E::A => 0i32, E::B(n) => (n + a + b) as i32 }
            }''', 7)

    def test_tag_of_moved_linear_place_is_rejected(self):
        with self.assertRaises(OwnershipError):
            self.run_source('''exposed enum E! { A }
                fn main() -> i32 {
                    let value = E::A;
                    let moved = value;
                    let tag = builtin::enum_variant(&value);
                    match moved { E::A => tag as i32 }
                }''', 0)

    def test_deferred_closure_operand_inference(self):
        self.run_source('''exposed enum E! { A, B(u32) }
            fn main() -> i32 {
                let f = |x| builtin::enum_variant(x);
                let (value, tag) = f(E::B.(6u32));
                match value { E::A => 0i32, E::B(n) => (n + tag) as i32 }
            }''', 7)

    def test_tag_is_not_readable_while_payload_is_moved(self):
        with self.assertRaisesRegex(KursError, 'readable_tag'):
            self.run_source('''exposed struct Token! { n: u32 }
            exposed enum E! { B(Token) }
            fn main() -> i32 {
                let value = E::B.(Token.{ n: 7u32 });
                let tag = match &value { E::B(payload) => {
                    let moved = payload;
                    let tag = builtin::enum_variant(&value);
                    payload = moved;
                    tag
                } };
                match value { E::B(token) => { let { n } = token; (n + tag) as i32 } }
            }''', 7)
