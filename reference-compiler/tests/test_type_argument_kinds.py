"""Constant paths and nominal argument kinds are frontend contracts."""
import unittest

from kursal.infer import InferError
from kursal.sigs import SigError
from tests import test_closure_boundary


class TypeArgumentKindTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def test_constant_value_paths_reject_arguments(self):
        for path in ('N[bool]', 'package[bool]::N'):
            with self.subTest(path=path), self.assertRaisesRegex(InferError, 'type arguments'):
                self.run_source('const N: i32 = 7i32; fn main() -> i32 { ' + path + ' }', 7)

    def test_folded_constant_paths_reject_arguments(self):
        for path in ('N[bool]', 'package[bool]::N'):
            with self.subTest(path=path), self.assertRaisesRegex(SigError, 'type arguments'):
                self.run_source('const N: i32 = 7i32; const M: i32 = ' + path + '; '
                                'fn main() -> i32 { M }', 7)

    def test_primitive_and_parameter_types_reject_arguments(self):
        for source in ('fn bad(x: u32[bool]) -> u32 { x }',
                       'forall T. fn bad(x: T[u32]) -> T { x }'):
            with self.subTest(source=source), self.assertRaisesRegex(SigError, 'type arguments'):
                self.run_source(source + ' fn main() -> i32 { 7i32 }', 7)

    def test_brand_constructor_requires_one_brand_argument(self):
        for ty in ('core::cap::Brand', 'core::cap::Brand[u32]', "core::cap::Brand['static, 'static]"):
            with self.subTest(ty=ty), self.assertRaisesRegex(SigError, 'brand argument'):
                self.run_source('fn bad(x: ' + ty + ') -> () { () } fn main() -> i32 { 7i32 }', 7)

    def test_nominal_type_and_brand_arguments_cannot_be_interchanged(self):
        for parameter, arg in (("'a", 'u32'), ('T', "'static")):
            with self.subTest(parameter=parameter), self.assertRaisesRegex(SigError, 'argument.*kind'):
                self.run_source('forall ' + parameter + '. exposed struct Box {} '
                                'fn bad(x: Box[' + arg + ']) -> () { () } fn main() -> i32 { 7i32 }', 7)

    def test_valid_nominal_brand_and_type_arguments(self):
        self.run_source('''forall 'a, T. exposed struct Tagged { witness: core::cap::Brand['a], value: T }
            fn main() -> i32 {
                let exists 'a. witness = core::cap::fresh();
                let value: Tagged['a, i32] = Tagged.{ witness, value: 7i32 };
                value.value
            }''', 7)

    def test_trait_brand_arguments_infer_with_brand_kind(self):
        for args in ("['a]", '', '[_]'):
            with self.subTest(args=args):
                self.run_source('''forall 'a. trait Read { fn read(x: core::cap::Brand['a]) -> i32; }
            forall 'a. instance Read['a] { fn read(x: core::cap::Brand['a]) -> i32 { 7i32 } }
            fn main() -> i32 {
                let exists 'a. witness = core::cap::fresh();
                Read''' + args + '''::read(witness)
            }''', 7)

    def test_trait_premise_and_instance_argument_kinds(self):
        for declaration in ('forall T where Tag[u32]. fn bad(x: T) -> T { x }',
                            'instance Tag[u32] {}'):
            with self.subTest(declaration=declaration), self.assertRaisesRegex(SigError, 'argument.*kind'):
                self.run_source("forall 'a. trait Tag {} " + declaration + ' fn main() -> i32 { 7i32 }', 7)

    def test_trait_premise_and_instance_argument_counts(self):
        for declaration in ('forall T where Tag[T, T]. fn bad(x: T) -> T { x }',
                            'instance Tag[u32, u32] {}'):
            with self.subTest(declaration=declaration), self.assertRaisesRegex(SigError, 'type arguments'):
                self.run_source('forall T. trait Tag {} ' + declaration + ' fn main() -> i32 { 7i32 }', 7)
