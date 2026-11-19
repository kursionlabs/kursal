"""Coherence rejects overlapping heads before declaration order can select one."""

import unittest

from kursal import core as C
from kursal.check import check_program
from kursal.coherence import check_coherence
from kursal.infer import infer
from kursal.kurs import parse_program
from kursal.resolve import resolve
from kursal.sexpr import KursError
from kursal.sigs import signatures
from kursal.unify import Meta, unify
from tests.test_resolve import manifest, tree


T, U = C.Named("T"), C.Named("U")


def head(*args, params=()):
    return C.Premise("P", args), tuple(C.Param(p) for p in params), (4, 2)


class CoherenceTests(unittest.TestCase):
    def overlaps(self, left, right):
        for pair in ((left, right), (right, left)):
            with self.subTest(pair=pair), self.assertRaisesRegex(KursError, "4:2: overlapping instances of P"):
                check_coherence(pair)

    def test_duplicates_and_generic_specializations_overlap(self):
        self.overlaps(head(C.U32), head(C.U32))
        self.overlaps(head(T, params=("T",)), head(C.U32))
        self.overlaps(head(C.Named("Box", (T,)), params=("T",)), head(C.Named("Box", (C.U32,))))

    def test_two_generic_scopes_are_independent(self):
        self.overlaps(head(T, C.U32, params=("T",)), head(C.BOOL, T, params=("T",)))

    def test_repeated_variables_and_occurs_check_prove_disjointness(self):
        check_coherence([head(T, T, params=("T",)), head(C.U32, C.BOOL)])
        check_coherence([head(T, C.Ptr(T), params=("T",)), head(C.Ptr(U), U, params=("U",))])

    def test_failed_pairs_do_not_poison_later_comparisons(self):
        with self.assertRaisesRegex(KursError, "overlapping"):
            check_coherence([head(C.U32, C.BOOL), head(C.BOOL, T, params=("T",)),
                             head(T, T, params=("T",))])

    def test_full_tuple_includes_outputs(self):
        check_coherence([head(C.U32, C.BOOL), head(C.U32, C.U32)])
        self.overlaps(head(T, U, params=("T", "U")), head(C.U32, C.BOOL))

    def test_nominal_names_array_lengths_and_function_contracts_distinguish_heads(self):
        check_coherence([head(C.Named("A")), head(C.Named("B"))])
        check_coherence([head(C.Array(C.U32, 2)), head(C.Array(C.U32, 3))])
        check_coherence([head(C.Fn((C.U32,), C.U32)), head(C.Fn((C.U32,), C.U32, unsafe=True)),
                         head(C.Fn((C.U32,), C.U32, places=frozenset({0})))])

    def test_heads_that_differ_only_under_binders_overlap(self):
        brand = (C.Param("b", C.BRAND),)
        witness = (C.Brand(C.Skolem("b")),)
        self.overlaps(head(C.Fn((witness[0],), C.U32, forall=brand)), head(C.Fn((witness[0],), C.BOOL, forall=brand)))
        self.overlaps(head(C.Exists(("a",), C.Tuple((C.U32, C.Brand(C.Skolem("a")))))),
                      head(C.Exists(("a",), C.Tuple((C.BOOL, C.Brand(C.Skolem("a")))))))

    def test_alpha_renamed_binders_overlap(self):
        self.overlaps(head(C.Exists(("a",), C.Brand(C.Skolem("a")))),
                      head(C.Exists(("b",), C.Brand(C.Skolem("b")))))
        self.overlaps(head(C.Foreach("A", False, C.Tuple((C.U32,)), C.Named("A"))),
                      head(C.Foreach("B", False, C.Tuple((C.U32,)), C.Named("B"))))
        self.overlaps(head(C.Fn((), C.Brand(C.Skolem("a")), (C.Param("a", C.BRAND),))),
                      head(C.Fn((), C.Brand(C.Skolem("b")), (C.Param("b", C.BRAND),))))

    def test_brand_parameters_are_fresh_variables(self):
        check = (C.Premise("P", (C.Skolem("k"),)), (C.Param("k", C.BRAND),), (4, 2))
        self.overlaps(check, head(C.Skolem("static")))

    def test_copy_constraints_do_not_establish_disjointness(self):
        constrained = (C.Premise("P", (T,)), (C.Param("T", C.COPY),), (4, 2))
        self.overlaps(constrained, head(C.Named("Linear")))

    def test_kurs_rejects_unused_overlaps(self):
        source = """(trait P (T)) (instance P (u32))
          (instance P (forall (T) () (T)))"""
        with self.assertRaisesRegex(KursError, "overlapping instances of P"):
            check_program(parse_program(source))

    def test_surface_ignores_conditional_premises_for_disjointness(self):
        source = """
forall T. trait P {}
forall T. trait Q {}
forall T where Q[T]. instance P[T] {}
instance P[u32] {}
"""
        root = tree({"p/kur.toml": manifest("p"), "p/lib.kur": source})
        resolver = resolve(root / "p")
        with self.assertRaisesRegex(KursError, "overlapping instances of P: "):
            infer(resolver, signatures(resolver))
