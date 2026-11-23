"""Existential witnesses keep lexical identity across lowering and CFG joins."""
import unittest

from kursal.infer import InferError
from tests import test_closure_boundary


class ExistentialLoweringTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def test_repacked_branches_join(self):
        self.run_source('''
        fn choose(flag: bool) -> exists 'r. (core::cap::Brand['r], u32) {
            if flag {
                let exists 'a. w = core::cap::fresh();
                pack 'a. (w, 7u32)
            } else {
                let exists 'a. w = core::cap::fresh();
                pack 'a. (w, 9u32)
            }
        }
        fn main() -> i32 {
            let exists 'x. (wx, x) = choose(true);
            let exists 'y. (wy, y) = choose(false);
            ((x + y) as i32)
        }''', 16)

    def test_inner_opening_cannot_shadow_an_outer_one(self):
        with self.assertRaisesRegex(InferError, "6:17: let exists 'a reuses a brand already in scope"):
            self.run_source('''
        forall 'a. fn same(x: core::cap::Brand['a], y: core::cap::Brand['a]) -> u32 { 7u32 }
        fn main() -> i32 {
            let exists 'a. outer = core::cap::fresh();
            let n = {
                let exists 'a. inner = core::cap::fresh();
                same(outer, outer)
            };
            ((n + same(outer, outer)) as i32)
        }''', 14)

    def test_nested_openings_cannot_unify(self):
        with self.assertRaisesRegex(InferError, r"brand mismatch: Brand\['b\] and Brand\['a\]; 'b was opened at 6:21, 'a at 4:17"):
            self.run_source('''
            forall 'a. fn same(x: core::cap::Brand['a], y: core::cap::Brand['a]) -> u32 { 7u32 }
            fn main() -> i32 {
                let exists 'a. outer = core::cap::fresh();
                let n = {
                    let exists 'b. inner = core::cap::fresh();
                    same(outer, inner)
                };
                (n as i32)
            }''', 7)

    def test_unpacked_value_cannot_escape_block(self):
        with self.assertRaisesRegex(InferError, 'brand.*escape'):
            self.run_source('''fn main() -> i32 {
                let escaped = { let exists 'a. w = core::cap::fresh(); w };
                0i32
            }''', 0)

    def test_pack_requires_an_in_scope_witness(self):
        with self.assertRaisesRegex(InferError, 'brand.*scope'):
            self.run_source('''fn main() -> i32 {
                let p = pack 'missing. 7u32;
                0i32
            }''', 0)

    def test_opening_cannot_repeat_a_name_in_the_same_scope(self):
        with self.assertRaisesRegex(InferError, "5:13: let exists 'a reuses a brand already in scope"):
            self.run_source('''
        forall 'a. fn same(x: core::cap::Brand['a], y: core::cap::Brand['a]) -> u32 { 7u32 }
        fn main() -> i32 {
            let exists 'a. outer = core::cap::fresh();
            let exists 'a. inner = core::cap::fresh();
            ((same(outer, outer) + same(inner, inner)) as i32)
        }''', 14)

    def test_loop_reseals_its_witness(self):
        self.run_source('''fn main() -> i32 {
            var package = core::cap::fresh();
            var n = 0u32;
            while n < 3 {
                let exists 'a. witness = package;
                package = pack 'a. witness;
                n = n + 1;
            }
            (n as i32)
        }''', 3)

    def test_closure_opening_cannot_shadow_a_generic_brand(self):
        with self.assertRaisesRegex(InferError, "5:17: let exists 'a reuses a brand already in scope"):
            self.run_source('''
        forall 'a. fn same(x: core::cap::Brand['a], y: core::cap::Brand['a]) -> u32 { 7u32 }
        forall 'a. fn touch(w: core::cap::Brand['a]) -> u32 {
            let f = || {
                let exists 'a. inner = core::cap::fresh();
                same(w, w)
            };
            f()
        }
        fn main() -> i32 {
            let exists 'a. w = core::cap::fresh();
            (touch(w) as i32)
        }''', 7)

    def test_pack_in_an_inner_block_selects_its_own_witness(self):
        self.run_source('''fn main() -> i32 {
            let exists 'a. outer = core::cap::fresh();
            let p = {
                let exists 'c. inner = core::cap::fresh();
                pack 'c. inner
            };
            let exists 'b. reopened = p;
            let p = pack 'a. outer;
            7i32
        }''', 7)

    def test_opening_cannot_repeat_a_binder(self):
        with self.assertRaisesRegex(InferError, 'repeats a brand'):
            self.run_source('''
            fn bad(p: exists 'x, 'y. (core::cap::Brand['x], core::cap::Brand['y])) -> u32 {
                let exists 'a, 'a. (x, y) = p;
                0u32
            }
            fn main() -> i32 { 0i32 }
            ''', 0)

    def test_local_brand_cannot_solve_an_outer_inference_variable(self):
        with self.assertRaisesRegex(InferError, 'brand.*escape'):
            self.run_source('''forall T. exposed enum Option { None, Some(T) }
            fn main() -> i32 {
                var slot = Option::None;
                {
                    let exists 'a. w = core::cap::fresh();
                    slot = Option::Some.(w);
                };
                0i32
            }''', 0)

    def test_outer_phantom_pack_witness_in_closure_is_rejected(self):
        from kursal.lower import LowerError
        with self.assertRaisesRegex(LowerError, 'block-local brand'):
            self.run_source('''fn main() -> i32 {
                let exists 'a. w = core::cap::fresh();
                let f = || pack 'a. 7u32;
                let p = f();
                0i32
            }''', 0)
