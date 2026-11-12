"""Component typing, retained scope, and emitted operands must describe one map."""

import unittest

from kursal import core as C
from kursal.check import check_program
from kursal.cgen import emit_c
from kursal.formation import Formation
from kursal.kurs import parse_type
from kursal.sexpr import read
from kursal.sexpr import KursError
from kursal.types import Types, equal, foreach_binders, foreach_item, free_names, show, subst
from kursal.unify import unify


class ComponentTests(unittest.TestCase):
    def setUp(self):
        self.types = Types(C.Program())

    def test_pointer_leaf_is_not_addressed_again(self):
        mapped = C.Foreach("F", True, C.Ptr(C.U32), C.Named("F"))
        self.assertEqual(self.types.expand(mapped), C.Tuple((C.U32,)))

    def test_pointer_field_keeps_both_pointer_layers(self):
        mapped = C.Foreach("F", True, C.Ptr(C.Tuple((C.Ptr(C.U32),))), C.Named("F"))
        self.assertEqual(self.types.expand(mapped), C.Tuple((C.Ptr(C.U32),)))

    def test_nested_indirect_source_retains_stripped_type(self):
        inner = C.Foreach("G", True, C.Ptr(C.U32), C.Ptr(C.Named("G")))
        outer = C.Foreach("F", True, inner, C.Tuple((C.Named("G"), C.Named("F"))))
        self.assertEqual(foreach_item(outer), C.Ptr(C.Named("G")))
        self.assertEqual(self.types.expand(outer), C.Tuple((C.Tuple((C.U32, C.U32)),)))

    def test_empty_array_has_schema_but_no_runtime_components(self):
        inner = C.Foreach("G", True, C.Ptr(C.Array(C.U32, 0)), C.Ptr(C.Named("G")))
        outer = C.Foreach("F", False, inner, C.Tuple((C.Named("F"), C.Named("G"))))
        self.assertEqual(self.types.rows(outer.source)[1], [])
        self.assertEqual(self.types.expand(outer), C.Array(C.Tuple((C.Ptr(C.U32), C.U32)), 0))

    def test_pointer_pattern_needs_more_than_an_unknown_exposed_shape(self):
        for source in (C.Named("T"), C.Tuple((C.U32,)), C.Array(C.U32, 0)):
            with self.subTest(source=source):
                mapped = C.Foreach("F", True, source, C.Named("F"))
                self.assertFalse(self.types.pattern_available(mapped, None, {"T": C.ANY}))
        mapped = C.Foreach("F", True, C.Ptr(C.Named("T")), C.Named("F"))
        self.assertTrue(self.types.pattern_available(mapped, None, {"T": C.ANY}))

    def test_checker_rejects_pointer_pattern_before_expansion(self):
        f = C.Named("F")
        source = C.Tuple((C.U32,))
        mapped = C.Foreach("F", True, source, C.Ptr(f))
        mapper = C.Fun("identity", (C.Param("F"),), (), C.Ptr(f), (C.Ptr(f),),
                       (C.Block("entry", (("%x", C.Ptr(f)),), (), C.Ret("%x")),))
        caller = C.Fun("walk", (), (), mapped, (source,),
                       (C.Block("entry", (("%x", source),),
                                (C.Instr(("%out",), "foreach", ("%x", "identity"), ty=mapped),),
                                C.Ret("%out")),))
        with self.assertRaisesRegex(KursError, "foreach_pointer_pattern"):
            check_program(C.Program(funs={"identity": mapper, "walk": caller}))

    def test_nested_leaf_c_passes_the_singleton_member(self):
        source = C.Foreach("G", False, C.U32, C.Named("G"))
        mapped = C.Foreach("F", False, source, C.Named("G"))
        g = C.Named("G")
        mapper = C.Fun("identity", (C.Param("F"), C.Param("G")), (), g, (g,),
                       (C.Block("entry", (("%x", g),), (), C.Ret("%x")),))
        caller = C.Fun("walk", (), (), mapped, (source,),
                       (C.Block("entry", (("%x", source),),
                                (C.Instr(("%out",), "foreach", ("%x", "identity"), ty=mapped),),
                                C.Ret("%out")),))
        program = C.Program(funs={"identity": mapper, "walk": caller})
        output = emit_c(program, check_program(program))
        self.assertRegex(output, r"\.e0 = [A-Za-z_][A-Za-z_0-9]*\([A-Za-z_][A-Za-z_0-9]*\.e0\);")


class RetainedScopeTests(unittest.TestCase):
    def nested(self, inner, outer, free=C.U32):
        source = C.Foreach(inner, False, C.Tuple((C.U32,)), C.Named(inner))
        return C.Foreach(outer, False, source, C.Tuple((C.Named(inner), C.Named(outer), free)))

    def test_retained_names_are_bound_and_alpha_equivalent(self):
        left, right = self.nested("G", "F"), self.nested("Y", "X")
        self.assertEqual(free_names(left), set())
        Formation(C.Program()).kind(left, {})
        self.assertTrue(equal(left, right))
        self.assertTrue(unify(left, right, closed=True))
        wrong = C.Foreach("X", False, right.source, C.Tuple((C.Named("X"), C.Named("X"), C.U32)))
        self.assertFalse(equal(left, wrong))
        self.assertFalse(unify(left, wrong, closed=True))

    def test_substitution_transports_retained_renaming_without_capture(self):
        original = self.nested("G", "F", C.Named("T"))
        result = subst(original, {"T": C.Named("G"), "G": C.U32})
        expected = self.nested("Y", "X", C.Named("G"))
        self.assertEqual(free_names(result), {"G"})
        self.assertTrue(equal(result, expected))
        self.assertEqual(subst(original, {"G": C.U32}), original)

    def test_specialization_does_not_add_source_binders(self):
        inner = C.Foreach("G", False, C.Tuple((C.U32,)), C.Named("G"))
        generic = C.Foreach("F", False, C.Named("T"), C.Named("F"))
        specialized = subst(generic, {"T": inner})
        nested = C.Foreach("F", False, inner, C.Named("F"))
        self.assertEqual(foreach_binders(specialized), ("F",))
        self.assertEqual(foreach_item(specialized), C.Named("F"))
        self.assertEqual(Types(C.Program()).expand(specialized), C.Tuple((C.U32,)))
        self.assertEqual(foreach_binders(nested), ("F", "G"))
        self.assertFalse(equal(specialized, nested))
        self.assertEqual(parse_type(read(show(specialized))[0]), specialized)
        with self.assertRaisesRegex(KursError, "source prefix"):
            Formation(C.Program()).kind(C.Foreach("F", False, C.U32, C.Named("F"), ("G",)), {})
