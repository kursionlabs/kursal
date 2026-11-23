"""Component typing, the binder's scope, and emitted operands must describe one map."""

import unittest

from kursal import core as C
from kursal.check import check_program
from kursal.cgen import emit_c
from kursal.formation import Formation
from kursal.kurs import parse_program, parse_type
from kursal.sexpr import read
from kursal.sexpr import KursError
from kursal.types import Types, equal, free_names, show, subst
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

    def test_a_sum_has_rows_by_value_only(self):
        opt = C.Enum((("none", C.Tuple(())), ("some", C.Prim("u8"))))
        self.assertEqual(self.types.expand(C.Foreach("F", False, opt, C.Ptr(C.Named("F")))),
                         C.Enum((("none", C.Ptr(C.Tuple(()))), ("some", C.Ptr(C.Prim("u8"))))))
        # A pointer to a sum has no rows (TY-FOREACH-ROWS-PTR), with either pattern: formation rejects one
        # written, and `Exposed` of a pointer source, which a traversal and `sizeof` need where a
        # parameter is in the pointee, never holds of one, so none is reached at an instantiation.
        for indirect in (True, False):
            with self.subTest(indirect=indirect):
                self.assertEqual(self.types.rows(C.Ptr(opt))[1], [])
                self.assertEqual(self.types.expand(C.Foreach("F", indirect, C.Ptr(opt), C.Named("F"))), C.Tuple(()))

    def test_empty_array_has_schema_but_no_runtime_components(self):
        mapped = C.Foreach("F", True, C.Ptr(C.Array(C.U32, 0)), C.Tuple((C.Named("F"),)))
        self.assertEqual(self.types.rows(mapped.source)[1], [])
        self.assertEqual(self.types.expand(mapped), C.Array(C.Tuple((C.U32,)), 0))

    def test_pointer_pattern_needs_more_than_an_unknown_exposed_shape(self):
        for source in (C.Named("T"), C.Tuple((C.U32,)), C.Array(C.U32, 0)):
            with self.subTest(source=source):
                mapped = C.Foreach("F", True, source, C.Named("F"))
                self.assertFalse(self.types.pattern_available(mapped))
        mapped = C.Foreach("F", True, C.Ptr(C.Named("T")), C.Named("F"))
        self.assertTrue(self.types.pattern_available(mapped))

    def test_formation_rejects_pointer_pattern_without_pointer_components(self):
        f = C.Named("F")
        source = C.Tuple((C.U32,))
        mapped = C.Foreach("F", True, source, C.Ptr(f))
        mapper = C.Fun("identity", (C.Param("F"),), (), C.Ptr(f), (C.Ptr(f),),
                       (C.Block("entry", (("%x", C.Ptr(f)),), (), C.Ret("%x")),))
        caller = C.Fun("walk", (), (), mapped, (source,),
                       (C.Block("entry", (("%x", source),),
                                (C.Instr(("%out",), "foreach", ("%x", "identity"), ty=mapped),),
                                C.Ret("%out")),))
        with self.assertRaisesRegex(KursError, r"foreach \*F needs a source written with pointer components"):
            check_program(C.Program(funs={"identity": mapper, "walk": caller}))


    def test_a_map_of_a_leaf_c_passes_the_singleton_member(self):
        operand = C.Foreach("G", False, C.U32, C.Named("G"))
        mapped = C.Foreach("F", False, C.U32, C.Named("F"))
        f = C.Named("F")
        mapper = C.Fun("identity", (C.Param("F"),), (), f, (f,),
                       (C.Block("entry", (("%x", f),), (), C.Ret("%x")),))
        caller = C.Fun("walk", (), (), mapped, (operand,),
                       (C.Block("entry", (("%x", operand),),
                                (C.Instr(("%out",), "foreach", ("%x", "identity"), ty=mapped),),
                                C.Ret("%out")),))
        program = C.Program(funs={"identity": mapper, "walk": caller})
        output = emit_c(program, check_program(program))
        self.assertRegex(output, r"\.e0 = [A-Za-z_][A-Za-z_0-9]*\([A-Za-z_][A-Za-z_0-9]*\.e0\);")


MAPPERS = """
    (type L! (struct (n u32)))
    (type Opt (enum (none (tuple)) (some u8)))
    (fun wrap (forall (F) () (tuple F u8) (F)
      (entry ((%x F)) (= %z (const u8 0)) (= %t (construct (tuple F u8) %x %z)) (ret %t))))
    (fun unwrap (forall (F) () F ((tuple F u8))
      (entry ((%t (tuple F u8))) (= (%x %z) (destruct %t (tuple F u8))) (ret %x))))
    (fun keep (forall (F) () (tuple (ptr F)) ((ptr F))
      (entry ((%p (ptr F))) (= %t (construct (tuple (ptr F)) %p)) (ret %t))))
    (fun pair (forall (F G) () (tuple F u8) (F)
      (entry ((%x F)) (= %z (const u8 0)) (= %t (construct (tuple F u8) %x %z)) (ret %t))))
"""


class MapFormTests(unittest.TestCase):
    """A traversal of a value of a foreach type over the same source and pattern maps its components."""

    def test_a_map_takes_each_component_of_the_value(self):
        check_program(parse_program(MAPPERS + """
            (fun f (foreach (G (tuple L! u32)) G) ((tuple L! u32))
              (entry ((%s (tuple L! u32)))
                (= %w (foreach (foreach (F (tuple L! u32)) (tuple F u8)) %s wrap))
                (= %u (foreach (foreach (G (tuple L! u32)) G) %w unwrap))
                (ret %u)))"""))

    def test_a_map_of_a_sum_takes_the_held_payload(self):
        check_program(parse_program(MAPPERS + """
            (fun f (foreach (F Opt) F) ((foreach (F Opt) (tuple F u8)))
              (entry ((%v (foreach (F Opt) (tuple F u8))))
                (= %out (foreach (foreach (F Opt) F) %v unwrap))
                (ret %out)))"""))

    def test_no_map_has_a_pointer_to_a_sum_as_its_source(self):
        with self.assertRaisesRegex(KursError, "a foreach source cannot be a pointer to a sum"):
            check_program(parse_program(MAPPERS + """
                (fun f (foreach ((ptr F) (ptr Opt)) (tuple (ptr F))) ((foreach ((ptr F) (ptr Opt)) (ptr F)))
                  (entry ((%v (foreach ((ptr F) (ptr Opt)) (ptr F))))
                    (= %out (foreach (foreach ((ptr F) (ptr Opt)) (tuple (ptr F))) %v keep))
                    (ret %out)))"""))

    def test_a_map_keeps_the_source_and_pattern(self):
        for name, (error, operand, written, mapper) in {
            "another source": ("foreach_source", "(foreach (F (tuple u32 u8)) (tuple F u8))",
                               "(foreach (F (tuple u32 u16)) F)", "unwrap"),
            "another pattern": ("foreach_source", "(foreach (F (ptr (tuple u32))) (tuple F u8))",
                                "(foreach ((ptr F) (ptr (tuple u32))) F)", "unwrap"),
            "the row item for the component": ("mapper_does_map", "(foreach (F (tuple u32 u8)) (tuple F u8))",
                                               "(foreach (F (tuple u32 u8)) (tuple F u8))", "wrap"),
            "a second binder": ("wrong number of type arguments", "(foreach (F (tuple u32 u8)) F)",
                                "(foreach (F (tuple u32 u8)) (tuple F u8))", "pair"),
        }.items():
            with self.subTest(case=name), self.assertRaisesRegex(KursError, error):
                check_program(parse_program(MAPPERS + f"""
                    (fun f {written} ({operand})
                      (entry ((%v {operand})) (= %out (foreach {written} %v {mapper})) (ret %out)))"""))



class BinderScopeTests(unittest.TestCase):
    """A foreach type binds one name, its binder, in its body."""

    def test_the_binder_is_bound_and_alpha_equivalent(self):
        left = C.Foreach("F", False, C.Tuple((C.U32,)), C.Tuple((C.Named("F"), C.U32)))
        right = C.Foreach("G", False, C.Tuple((C.U32,)), C.Tuple((C.Named("G"), C.U32)))
        self.assertEqual(free_names(left), set())
        Formation(C.Program()).kind(left, {})
        self.assertTrue(equal(left, right))
        self.assertTrue(unify(left, right, closed=True))
        wrong = C.Foreach("G", False, right.source, C.Tuple((C.U32, C.Named("G"))))
        self.assertFalse(equal(left, wrong))
        self.assertFalse(unify(left, wrong, closed=True))

    def test_substitution_renames_a_binder_it_would_capture(self):
        original = C.Foreach("F", False, C.Tuple((C.Named("T"),)), C.Tuple((C.Named("F"), C.Named("T"))))
        result = subst(original, {"T": C.Named("F")})
        self.assertEqual(free_names(result), {"F"})
        self.assertTrue(equal(result, C.Foreach("G", False, C.Tuple((C.Named("F"),)),
                                                C.Tuple((C.Named("G"), C.Named("F"))))))
        self.assertEqual(subst(original, {"F": C.U32}), original)

    def test_kurs_writes_and_reads_the_one_binder(self):
        mapped = C.Foreach("F", True, C.Ptr(C.Tuple((C.U32,))), C.Named("F"))
        self.assertEqual(parse_type(read(show(mapped))[0]), mapped)
        with self.assertRaisesRegex(KursError, "foreach requires source and body"):
            parse_type(read("(foreach (F (tuple u8)) F (retain))")[0])
