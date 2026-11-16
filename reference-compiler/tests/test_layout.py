"""Recursive types must have finite storage and a finite family of C names."""

import subprocess
import unittest

from kursal import core as C
from kursal.cc import FLAGS, c_compiler
from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.kurs import parse_program
from kursal.sexpr import KursError
from kursal.resolve import resolve
from kursal.sigs import signatures
from kursal.types import Types
from tests.test_resolve import manifest, tree


class LayoutTests(unittest.TestCase):
    def test_copy_query_does_not_accept_a_recursive_obligation(self):
        alias = C.Program(types={"A": C.TypeDecl("A", (), (), C.Named("A"))})
        wrapped = C.Program(types={"A": C.TypeDecl(
            "A", (), (), C.Struct((("self", C.Named("A")),)))})
        for program in (alias, wrapped):
            with self.subTest(body=program.types["A"].body):
                self.assertFalse(Types(program).is_copy(C.Named("A")))

        pointer = C.Program(types={"A": C.TypeDecl(
            "A", (), (), C.Struct((("next", C.Ptr(C.Named("A"))),)))})
        self.assertTrue(Types(pointer).is_copy(C.Named("A")))

    def rejects(self, source, message):
        with self.assertRaisesRegex(KursError, message):
            check_program(parse_program(source))

    def compiles(self, source, ty):
        program = parse_program(source)
        program.externs["probe"] = C.Extern("probe", ty)
        text = emit_c(program, check_program(program))
        result = subprocess.run([c_compiler(), *FLAGS, "-x", "c", "-fsyntax-only", "-"],
                                input=text, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + text)

    def test_direct_mutual_and_wrapped_value_cycles(self):
        for source in ("(type A (struct (self A)))",
                       "(type A (tuple B)) (type B (enum (Back A)))",
                       "(type Value (forall (T) () (struct (value T)))) (type A (Value A))",
                       "(type A (exists ($k) (tuple A (brand $k))))"):
            with self.subTest(source=source):
                self.rejects(source, "infinite by-value layout")

    def test_pointers_and_pointer_wrappers_break_storage_cycles(self):
        self.compiles("(type A (struct (next (ptr A))))", C.Named("A"))
        self.compiles("""
(type Ref (forall (T) () (struct (ptr (ptr T)))))
(type Node (forall (T) () (struct (value T) (next (Ref (Node T))))))
""", C.Named("Node", (C.U32,)))

    def test_function_fields_break_storage_cycles(self):
        self.compiles("(type A (struct (callback (fn (A) A))))", C.Named("A"))

    def test_generic_pointer_recursion_permits_permutations_and_fixed_resets(self):
        self.compiles("""(type Pair (forall (T U) ()
          (struct (next (ptr (Pair U T))))))""", C.Named("Pair", (C.U32, C.BOOL)))
        self.compiles("""(type Node (forall (T) ()
          (struct (next (ptr (Node u32))))))""", C.Named("Node", (C.BOOL,)))

    def test_growing_arguments_are_rejected_even_behind_pointers(self):
        self.rejects("""(type Node (forall (T) ()
          (struct (next (ptr (Node (tuple T T)))))))""", "recursive type expansion")
        self.rejects("""
(type A (forall (T) () (struct (p (ptr (B T))))))
(type B (forall (U) () (struct (p (ptr (A (tuple U U)))))))
""", "recursive type expansion")

    def test_foreach_cannot_hide_growth_in_a_fresh_binder(self):
        self.rejects("""(type A (forall (T) ()
          (struct (p (ptr (foreach (F (tuple (tuple T T))) (A F)))))))""",
                     "foreach components.*may grow")

    def test_foreach_sources_and_bodies_must_terminate(self):
        self.rejects("(type A (foreach (F A) u32))", "recursive foreach source")
        self.rejects("(type A (foreach (F (ptr A)) u32))", "recursive foreach source")
        self.rejects("(type A (foreach (F (tuple u32)) (tuple A F)))", "infinite by-value layout")
        self.compiles("(type A (foreach (F (tuple u32)) (ptr A)))", C.Named("A"))
        self.compiles("(type A (foreach (A (tuple u32)) A))", C.Named("A"))

    def test_erased_arguments_and_empty_arrays_do_not_store_their_types(self):
        self.compiles("(type Phantom (forall (T) () u32)) (type A (Phantom A))", C.Named("A"))
        self.compiles("(type A (array A 0)) (type B (struct (a A) (n u32)))", C.Named("B"))

    def test_recursive_aliases_require_an_aggregate_name(self):
        for body in ("A", "(ptr A)", "(fn () A)"):
            with self.subTest(body=body):
                self.rejects(f"(type A {body})", "infinite by-value layout|aggregate boundary")

    def test_function_alias_can_recur_through_a_named_aggregate(self):
        self.compiles("(type Callback (fn () Node)) (type Node (struct (run Callback)))",
                      C.Named("Callback"))

    def test_repeated_names_can_be_finite_nested_applications(self):
        self.compiles("""(type Option (forall (T) () (enum (Some T) (None (tuple)))))
          (type A (Option (Option u32)))""", C.Named("A"))

    def test_fresh_existential_brands_cannot_hide_a_storage_cycle(self):
        self.rejects("""(type A (forall ($k) ()
          (exists ($q) (struct (next (A $q))))))""", "infinite by-value layout")

    def test_surface_declarations_are_checked_before_inference(self):
        for source, error in (
            ("exposed struct Bad { inner: Bad }", "infinite by-value"),
            ("forall T. exposed struct Bad { next: *Bad[(T, T)] }", "recursive type expansion"),
        ):
            with self.subTest(source=source):
                root = tree({"p/kur.toml": manifest("p"), "p/lib.kur": source})
                with self.assertRaisesRegex(KursError, error):
                    signatures(resolve(root / "p"))
