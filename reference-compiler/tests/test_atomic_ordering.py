"""Source atomic orderings are literal, position-sensitive operation contracts."""
import unittest

from kursal.lower import LowerError
from kursal.infer import InferError
from tests import test_unsafe_authorization


ORDERS = ('Relaxed', 'Acquire', 'Release', 'AcqRel', 'SeqCst')
PRELUDE = 'use core::atomic::Ordering; '


class AtomicOrderingTests(unittest.TestCase):
    run_source = test_unsafe_authorization.UnsafeAuthorizationTests.run_source

    def run_operation(self, expression):
        self.run_source(PRELUDE + '''fn main() -> i32 {
            let p = unsafe core::mem::alloca[u32]();
            unsafe *p = 7u32;
            let result = unsafe ''' + expression + ''';
            7i32
        }''', 7)

    def test_load_store_and_fence_ordering_matrix(self):
        for op, args, allowed in (
            ('p.atomic_load', '', {'Relaxed', 'Acquire', 'SeqCst'}),
            ('p.atomic_store', '7u32, ', {'Relaxed', 'Release', 'SeqCst'}),
            ('core::atomic::fence', '', {'Acquire', 'Release', 'AcqRel', 'SeqCst'}),
        ):
            for order in ORDERS:
                with self.subTest(op=op, order=order):
                    expression = f'{op}({args}Ordering::{order})'
                    if order in allowed:
                        self.run_operation(expression)
                    else:
                        with self.assertRaisesRegex(LowerError, 'ordering'):
                            self.run_operation(expression)

    def test_compare_exchange_ordering_matrix(self):
        allowed = {'Relaxed': {'Relaxed'}, 'Acquire': {'Relaxed', 'Acquire'},
                   'Release': {'Relaxed'}, 'AcqRel': {'Relaxed', 'Acquire'},
                   'SeqCst': {'Relaxed', 'Acquire', 'SeqCst'}}
        for success in ORDERS:
            for failure in ORDERS:
                with self.subTest(success=success, failure=failure):
                    expression = f'p.compare_exchange(7u32, 9u32, Ordering::{success}, Ordering::{failure})'
                    if failure in allowed[success]:
                        self.run_operation(expression)
                    else:
                        with self.assertRaisesRegex(LowerError, 'ordering'):
                            self.run_operation(expression)

    def test_read_modify_write_accepts_all_orderings(self):
        for op in ('atomic_swap', 'fetch_add', 'fetch_sub', 'fetch_and', 'fetch_or', 'fetch_xor'):
            for order in ORDERS:
                with self.subTest(op=op, order=order):
                    self.run_operation(f'p.{op}(1u32, Ordering::{order})')

    def test_ordering_values_must_be_written_as_variants(self):
        for expression in ('p.atomic_load(order)', 'p.atomic_store(7u32, order)',
                           'p.compare_exchange(7u32, 9u32, order, Ordering::Relaxed)',
                           'p.compare_exchange(7u32, 9u32, Ordering::SeqCst, order)',
                           'core::atomic::fence(order)'):
            with self.subTest(expression=expression), self.assertRaisesRegex(LowerError, 'Ordering variant'):
                self.run_source(PRELUDE + 'fn main() -> i32 { '
                                'let p = unsafe core::mem::alloca[u32](); '
                                'let order = Ordering::Acquire; let result = unsafe ' + expression + '; 7i32 }', 7)

    def test_fence_requires_exactly_one_ordering(self):
        for args in ('', 'Ordering::Acquire, Ordering::Release'):
            with self.subTest(args=args), self.assertRaisesRegex(InferError, 'one ordering argument'):
                self.run_operation('core::atomic::fence(' + args + ')')

    def test_variant_named_like_ordering_remains_a_value_operand(self):
        self.run_source('''exposed enum Value { Relaxed, Other }
            fn main() -> i32 {
                let p = unsafe core::mem::alloca[Value]();
                let init = unsafe p.store(Value::Relaxed);
                let result = unsafe p.load();
                match result { Value::Relaxed => 7i32, Value::Other => 0i32 }
            }''', 7)

    def test_atomic_results_and_memory_updates(self):
        self.run_source(PRELUDE + '''fn main() -> i32 {
            let p = unsafe core::mem::alloca[u32]();
            let init = unsafe p.atomic_store(7u32, Ordering::Release);
            let old = unsafe p.fetch_add(2u32, Ordering::AcqRel);
            let (seen, success) = unsafe p.compare_exchange(9u32, 11u32, Ordering::AcqRel, Ordering::Acquire);
            let (failed_seen, failed) = unsafe p.compare_exchange(9u32, 15u32, Ordering::SeqCst, Ordering::SeqCst);
            let now = unsafe p.atomic_load(Ordering::Acquire);
            if old == 7u32 && seen == 9u32 && success && failed_seen == 11u32 && !failed && now == 11u32 {
                7i32
            } else { 0i32 }
        }''', 7)
