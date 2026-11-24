"""Strict core local scope, with frontend SSA transport of local witnesses."""
import unittest

from kursal.check import check_program
from kursal.kurs import parse_program
from kursal.sexpr import KursError
from tests import test_closure_boundary


class LocalScopeTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def test_unused_local_cannot_reference_open_block_or_static_brand(self):
        for ty in ('(brand $k)', '(ptr (brand $k))',
                   '(brand $static)'):
            with self.subTest(ty=ty), self.assertRaisesRegex(KursError, 'function-generic free brands'):
                check_program(parse_program(f'''(fun f u32 () (locals (&slot {ty}))
                    (entry () (= %r (const u32 0)) (ret %r))
                    (dead ($k) () (= %r (const u32 0)) (ret %r)))'''))

    def test_generic_and_existentially_bound_local_brands(self):
        check_program(parse_program('''(fun f (forall ($k) () u32 ()
            (locals (&x (brand $k)) (&p (exists ($a) (brand $a))))
            (entry () (= %r (const u32 0)) (ret %r))))'''))

    def test_local_witness_is_threaded_through_branches_and_loops(self):
        self.run_source('''forall 'a. fn read(w: core::cap::Brand['a]) -> u32 { 7u32 }
            fn main() -> i32 {
                let exists 'a. witness = core::cap::fresh();
                var w = witness;
                var n = 0u32;
                while n < 2 { if n == 0 { w = witness; } else { w = w; }; n = n + 1; }
                read(w) as i32
            }''', 7)

    def test_local_witness_lending_uses_generic_adapter(self):
        self.run_source('''forall 'a. fn touch(&x: core::cap::Brand['a]) -> u32 { 7u32 }
            fn main() -> i32 { var exists 'a. w = core::cap::fresh(); touch(&w) as i32 }''', 7)

    def test_indirect_lending_adapter_retains_brand_arguments(self):
        self.run_source('''forall 'a. fn touch(&x: core::cap::Brand['a]) -> u32 { 7u32 }
            fn main() -> i32 {
                var exists 'a. w = core::cap::fresh();
                let action = touch;
                action(&w) as i32
            }''', 7)

    def test_partial_linear_product_reconstructs_after_field_store(self):
        self.run_source('''exposed struct H! { value: u32 }
            forall 'a. exposed struct Tagged! { witness: core::cap::Brand['a], h: H! }
            fn main() -> i32 {
                let exists 'a. w = core::cap::fresh();
                var tagged = Tagged!.{ witness: w, h: H!.{ value: 7u32 } };
                let moved = tagged.h;
                if true { let snapshot = tagged.witness; } else { () };
                tagged.h = moved;
                let { witness, h } = tagged;
                let { value } = h;
                value as i32
            }''', 7)

    def test_lending_projected_field_restores_product(self):
        self.run_source('''forall 'a. exposed struct Tagged { witness: core::cap::Brand['a], n: u32 }
            fn bump(&n: u32) -> () { n = n + 1u32; }
            fn main() -> i32 {
                let exists 'a. w = core::cap::fresh();
                var tagged = Tagged.{ witness: w, n: 6u32 };
                bump(&tagged.n);
                tagged.n as i32
            }''', 7)

    def test_trait_lending_adapter(self):
        self.run_source('''forall 'a. trait Touch { fn touch(&x: core::cap::Brand['a]) -> u32; }
            forall 'a. instance Touch['a] {
                fn touch(&x: core::cap::Brand['a]) -> u32 { 7u32 }
            }
            fn main() -> i32 {
                var exists 'a. w = core::cap::fresh();
                Touch['a]::touch(&w) as i32
            }''', 7)

    def test_promoted_lends_cannot_hide_overlap(self):
        with self.assertRaisesRegex(KursError, 'overlap'):
            self.run_source('''forall 'a. fn touch(&x: core::cap::Brand['a], &y: core::cap::Brand['a]) -> u32 { 7u32 }
                fn main() -> i32 { var exists 'a. w = core::cap::fresh(); touch(&w, &w) as i32 }''', 7)

    def test_match_on_a_locally_branded_sum_in_place(self):
        # The switch is on the enum's value; each arm holds the payload and rebuilds the enum.
        self.run_source('''forall 'a. exposed enum E { A(core::cap::Brand['a], u32), B }
            forall 'a. exposed struct M { e: E['a], n: u32 }
            fn main() -> i32 {
                let exists 'a. w = core::cap::fresh();
                var value = M.{ e: E::A.(w, 5u32), n: 2u32 };
                let k = match &value.e { E::A(_, k) => k, E::B => 0u32 };
                let m = match &value.e { E::A(_, k) => k + value.n, E::B => 9u32 };
                let { e, n } = value;
                match e { E::A(_, x) => (k + m + x + n) as i32, E::B => 0i32 }
            }''', 5 + 7 + 5 + 2)

    def test_locally_branded_sum_tag_has_frontend_diagnostic(self):
        from kursal.lower import LowerError
        with self.assertRaisesRegex(LowerError, 'structural SSA lowering'):
            self.run_source('''forall 'a. exposed enum E { A(core::cap::Brand['a]) }
                fn main() -> i32 {
                    let exists 'a. w = core::cap::fresh();
                    var value = E::A.(w);
                    let tag = core::mem::enum_variant(&value);
                    match value { E::A(payload) => tag as i32 }
                }''', 0)
