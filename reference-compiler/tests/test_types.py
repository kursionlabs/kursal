"""Type operations preserve binding scopes and constructor metadata."""

import unittest

from kursal import core as C
from kursal.cgen import erase
from kursal.core import Brand, Exists, Fn, Foreach, Named, Ptr, Skolem, Tuple, U32
from kursal.unify import Meta, zonk
from kursal.types import Types, equal, free_names, subst


class ErasureTests(unittest.TestCase):
    def test_function_representation_preserves_places_and_erases_type_level_metadata(self):
        brand = Skolem("k")
        value = C.Array(Named("Box", (brand, Brand(brand))), 3)
        function = Fn((Ptr(value),), Exists(("k",), Tuple((U32, Brand(brand)))), True, frozenset({0}))
        expected = Fn((Ptr(C.Array(Named("Box", (Skolem("_"), C.UNIT)), 3)),),
                      Tuple((U32, C.UNIT)), places=frozenset({0}))
        self.assertEqual(erase(function, Types(C.Program())), expected)

    def test_transforms_expand_before_erasing_their_components(self):
        source = Tuple((Brand(Skolem("k")), U32))
        transformed = Foreach("T", False, source, Ptr(Named("T")))
        self.assertEqual(erase(transformed, Types(C.Program())), Tuple((Ptr(C.UNIT), Ptr(U32))))


class SubstitutionTests(unittest.TestCase):
    def test_substitution_is_simultaneous(self):
        self.assertEqual(subst(Tuple((Named("T"), Named("U"))), {"T": Named("U"), "U": U32}),
                         Tuple((Named("U"), U32)))

    def test_replacements_are_not_captured_by_binders(self):
        for wrap in (
            lambda body: Exists(("k",), body),
        ):
            with self.subTest(binder=wrap):
                result = subst(wrap(Tuple((Named("T"), Brand(Skolem("k"))))), {"T": Brand(Skolem("k"))})
                self.assertEqual(free_names(result), {"k"})
                self.assertFalse(equal(result, wrap(Tuple((Brand(Skolem("k")), Brand(Skolem("k")))))))

    def test_foreach_binder_scopes_only_its_body(self):
        result = subst(Foreach("T", False, Named("T"), Named("T")), {"T": U32})
        self.assertEqual(result, Foreach("T", False, U32, Named("T")))


class ZonkTests(unittest.TestCase):
    def test_solves_nested_types_without_changing_calling_convention(self):
        variable = Meta()
        original = Fn((Ptr(variable),), Exists(("k",), Tuple((variable, Brand(Skolem("k"))))), True, frozenset({0}))
        variable.ref = U32
        expected = Fn((Ptr(U32),), Exists(("k",), Tuple((U32, Brand(Skolem("k"))))), True, frozenset({0}))
        self.assertEqual(zonk(original), expected)


class EqualityTests(unittest.TestCase):
    def test_permuted_existential_names_do_not_merge_brands(self):
        pair = lambda a, b: Tuple((Brand(Skolem(a)), Brand(Skolem(b))))
        left = Exists(("a", "b"), pair("a", "b"))
        renamed = Exists(("b", "a"), pair("b", "a"))
        collapsed = Exists(("b", "a"), pair("a", "a"))
        self.assertTrue(equal(left, renamed))
        self.assertFalse(equal(left, collapsed))
        self.assertFalse(equal(collapsed, left))

    def test_bound_name_on_one_side_stays_free_on_the_other(self):
        for wrap, var in (
            (lambda name, body: Exists((name,), body), Skolem),
            (lambda name, body: Foreach(name, False, U32, body), Named),
        ):
            with self.subTest(binder=wrap):
                left = wrap("a", var("a"))
                self.assertTrue(equal(left, wrap("b", var("b"))))
                self.assertFalse(equal(left, wrap("b", var("a"))))
                self.assertFalse(equal(wrap("b", var("a")), left))

    def test_nested_binders_shadow_without_overwriting_outer_scope(self):
        left = Exists(("a",), Tuple((Skolem("a"), Exists(("b",), Skolem("a")))))
        renamed = Exists(("b",), Tuple((Skolem("b"), Exists(("a",), Skolem("b")))))
        captured = Exists(("b",), Tuple((Skolem("b"), Exists(("a",), Skolem("a")))))
        self.assertTrue(equal(left, renamed))
        self.assertFalse(equal(left, captured))
