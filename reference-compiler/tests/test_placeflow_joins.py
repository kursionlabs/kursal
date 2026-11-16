"""Candidate joins forget only content that core edge entailment can discard."""

import unittest

from kursal import core as C
from kursal.check import check_program
from kursal.kurs import parse_program
from kursal.lower import lower
from kursal.placeflow import PlaceFlow
from kursal.resolve import resolve
from kursal.sexpr import KursError
from kursal.sigs import signatures
from tests.test_resolve import manifest, tree


def lowered(source):
    root = tree({"p/kur.toml": manifest("p", ("core",)), "p/lib.kur": source})
    resolver = resolve(root / "p")
    program = lower(resolver, signatures(resolver))
    check_program(program)
    return program


ENUM_JOIN = """
(type L! (tuple u32))
(type E! (enum (A L!) (B L!)))
(fun consume (tuple) (L!)
  (entry ((%l L!)) (= %n (destruct %l L!)) (= %u (construct (tuple))) (ret %u)))
(fun walk (tuple) (E!) (locals (&e E!))
  (entry ((%input E!)) (= %u (store &e %input))
    (switch &e E! (A (left)) (B (right))))
  (left () (= %a (load &e.A)) (= %u (call consume %a)) (jmp join))
  (right () (= %b (load &e.B)) (= %u (call consume %b)) (jmp join))
  (join () (= %u (construct (tuple))) (ret %u)))
"""

EMPTY_KNOWN_JOIN = """
(type E (enum (A u32) (B u32)))
(fun f u32 () (locals (&e E))
  (entry () (= %n (const u32 1)) (= %e (construct_variant E A %n))
    (= %u (store &e %e)) (switch &e E (A a) (B b)))
  (a () (known &e A) (jmp join_a))
  (b () (known &e B) (= %z (const u32 0)) (ret %z))
  (join_a () (empty &e) (known &e A)
    (= %z (const u32 0)) (ret %z)))
"""


class JoinTests(unittest.TestCase):
    def test_empty_root_can_retain_known_fact_but_not_reveal_tag(self):
        check_program(parse_program(EMPTY_KNOWN_JOIN))
        unreadable = EMPTY_KNOWN_JOIN.replace(
            "(= %z (const u32 0)) (ret %z)))",
            "(= %t (variant_tag &e E)) (ret %t)))",
        )
        with self.assertRaisesRegex(KursError, "readable_tag"):
            check_program(parse_program(unreadable))

    def test_consumed_variant_payloads_join_as_empty_root(self):
        program = parse_program(ENUM_JOIN)
        fun = program.funs["walk"]
        PlaceFlow(program).plan(fun)
        self.assertEqual(fun.blocks[-1].empty, ("&e",))
        self.assertEqual(fun.blocks[-1].known, ())
        check_program(program)

    def test_live_payload_cannot_be_forgotten(self):
        source = ENUM_JOIN.replace('(= %b (load &e.B)) (= %u (call consume %b))', '')
        program = parse_program(source)
        PlaceFlow(program).plan(program.funs["walk"])
        self.assertNotIn("&e", program.funs["walk"].blocks[-1].empty)
        with self.assertRaises(KursError):
            check_program(program)

    def test_place_destruct_updates_the_next_header(self):
        program = parse_program("""
(type L! (tuple u32))
(fun split u32 (L!) (locals (&x L!))
  (entry ((%x L!)) (= %u (store &x %x)) (= %n (destruct &x L!)) (jmp next %n))
  (next ((%n u32)) (ret %n)))
""")
        PlaceFlow(program).plan(program.funs["split"])
        self.assertEqual(program.funs["split"].blocks[-1].empty, ("&x",))
        check_program(program)

    def test_dead_copy_brand_in_branch_and_loop(self):
        lowered("""
fn branch(c: bool) -> u32 {
    if c {
        let exists 'a. witness = core::cap::fresh();
        1u32
    } else { 2u32 }
}
fn looping(c: bool) -> u32 {
    var again = c;
    while again {
        let exists 'a. witness = core::cap::fresh();
        again = false;
    }
    0u32
}
""")

    def test_live_linear_brand_payload_still_rejected(self):
        with self.assertRaises(KursError):
            lowered("""
forall 'a. exposed struct Token! { witness: core::cap::Brand['a] }
fn branch(c: bool) -> u32 {
    if c {
        let exists 'a. witness = core::cap::fresh();
        let token = Token!['a].{ witness: witness };
        1u32
    } else { 2u32 }
}
""")


class OperandPolicyTests(unittest.TestCase):
    prefix = """
fn bump(&x: u32) -> u32 { x = x + 1u32; x }
fn combine(a: u32, b: u32) -> u32 { a + b }
"""

    def test_a_read_and_a_nested_lend_need_an_explicit_binding(self):
        for expression in ('combine(n, bump(&n))', 'combine(bump(&n), n)'):
            with self.subTest(expression=expression):
                with self.assertRaisesRegex(KursError, 'plain read overlaps a lend'):
                    lowered(self.prefix + 'fn main() -> u32 { var n = 1u32; ' + expression + ' }')
        lowered(self.prefix + """
fn main() -> u32 { var n = 1u32; let before = n; combine(before, bump(&n)) }
""")

    def test_field_overlap_is_path_based(self):
        prefix = self.prefix + 'exposed struct Pair { a: u32, b: u32 }\n'
        with self.assertRaisesRegex(KursError, 'plain read overlaps a lend'):
            lowered(prefix + 'fn bad(&p: Pair) -> u32 { combine(p.a, bump(&p.a)) }')
        lowered(prefix + 'fn good(&p: Pair) -> u32 { combine(p.a, bump(&p.b)) }')

    def test_a_linear_read_and_a_lend_need_an_explicit_binding(self):
        prefix = """
exposed struct Token! { n: u32 }
fn bump(&t: Token!) -> u32 { t.n = t.n + 1u32; t.n }
fn consume(t: Token!, n: u32) -> u32 { let { n: old } = t; old + n }
"""
        with self.assertRaisesRegex(KursError, 'plain read overlaps a lend'):
            lowered(prefix + "fn use_token(t: Token!) -> u32 { consume(t, bump(&t)) }")
        lowered(prefix + "fn use_token(t: Token!) -> u32 { var t = t; let n = bump(&t); consume(t, n) }")
