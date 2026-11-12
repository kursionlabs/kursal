"""Builtin calls reject operands which would otherwise be ignored or indexed blindly."""
import unittest

from kursal.infer import InferError
from tests import test_unsafe_authorization


class BuiltinArityTests(unittest.TestCase):
    run_source = test_unsafe_authorization.UnsafeAuthorizationTests.run_source

    def reject(self, expression, message='argument'):
        with self.assertRaisesRegex(InferError, message):
            self.run_source('fn main() -> i32 { let x = unsafe ' + expression + '; 7i32 }', 7)

    def test_zero_operand_builtins_reject_extras(self):
        for name in ('fresh', 'static_brand', 'alloca[u32]'):
            for args in ('7u32', '7u32, 9u32'):
                with self.subTest(name=name, args=args):
                    self.reject(f'builtin::{name}({args})')

    def test_enum_variant_requires_one_operand(self):
        for args in ('', '7u32, 9u32'):
            with self.subTest(args=args):
                self.reject(f'builtin::enum_variant({args})')

    def test_volatile_operand_counts(self):
        for name, counts in (('load_volatile', (0, 2, 3)), ('store_volatile', (0, 1, 3))):
            for count in counts:
                with self.subTest(name=name, count=count):
                    self.reject(f'builtin::{name}(' + ', '.join(['7u32'] * count) + ')')

    def test_alloca_requires_exactly_one_type_argument(self):
        for name in ('alloca', 'alloca[u32, u64]'):
            with self.subTest(name=name):
                self.reject(f'builtin::{name}()', 'type argument')

    def test_other_builtins_reject_type_arguments(self):
        for name, args in (('fresh', ''), ('static_brand', ''), ('enum_variant', '7u32'),
                           ('fence', '7u32'), ('load_volatile', '7u32'), ('store_volatile', '7u32, 9u32')):
            with self.subTest(name=name):
                self.reject(f'builtin::{name}[u32]({args})', 'type argument')

    def test_namespace_type_arguments_are_not_ignored(self):
        self.reject('builtin[u32]::fresh()', 'type argument')

    def test_unknown_builtin_is_a_source_error(self):
        self.reject('builtin::unknown()', 'unknown builtin')

    def test_valid_builtin_calls(self):
        self.run_source('''exposed enum Choice { A, B }
            fn main() -> i32 {
                let exists 'a. witness = builtin::fresh();
                let static_witness = unsafe builtin::static_brand();
                let p = unsafe builtin::alloca[u32]();
                let init = unsafe builtin::store_volatile(p, 7u32);
                let value = unsafe builtin::load_volatile(p);
                let (choice, tag) = builtin::enum_variant(Choice::B);
                if tag == 1u32 { value as i32 } else { 0i32 }
            }''', 7)
