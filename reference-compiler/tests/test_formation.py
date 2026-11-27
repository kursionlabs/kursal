"""Reject malformed written types even in unused declarations and dead blocks."""

import unittest

from kursal import core as C
from kursal.check import check_program
from kursal.kurs import parse_program
from kursal.sexpr import KursError


class FormationTests(unittest.TestCase):
    def rejects(self, source, message):
        with self.assertRaisesRegex(KursError, "type formation: " + message):
            check_program(parse_program(source))

    def test_unknown_names_and_nominal_arity_in_unused_declarations(self):
        self.rejects("(type Bad Unknown)", "unknown type Unknown")
        self.rejects("(type Box (forall (T) () (tuple T))) (type Bad Box)", "wrong number")
        self.rejects("(type Box (forall (T) () (tuple T))) (type Bad (Box u32 u32))", "wrong number")
        self.rejects("(extern fun f Typo ())", "unknown type Typo")
        self.rejects("(data d Typo ())", "unknown type Typo")

    def test_type_and_brand_arguments_have_distinct_kinds(self):
        self.rejects("(type Box (forall (T) () (tuple T))) "
                     "(type Bad (forall ($k) () (Box $k)))", "expected any")
        self.rejects("(type Tag (forall ($k) () (brand $k))) (type Bad (Tag u32))",
                     "expected brand")
        self.rejects("(type Bad (ptr $missing))", "brand missing is not in scope")
        self.rejects("(type Bad (forall ($k) () (ptr $k)))", "expected any")
        check_program(parse_program("(type Tag (forall ($k) () (brand $k))) "
                                    "(type Good (exists ($k) (Tag $k)))"))

    def test_copy_arguments_are_checked(self):
        self.rejects("(type Linear! (tuple)) (type Box (forall (T) ((Copy T)) (tuple T))) "
                     "(type Bad (Box Linear!))", "expected Copy")
        self.rejects("(type Box (forall (T) ((Copy T)) (tuple T))) "
                     "(type Bad (forall (U) () (Box U)))", "expected Copy")
        check_program(parse_program("(type Box (forall (T) ((Copy T)) (tuple T))) "
                                    "(type Good (forall (U) ((Copy U)) (Box U)))"))

    def test_a_type_linear_for_every_instantiation_carries_the_bang(self):
        lin = "(type L! (struct (x u32))) (type Opt (forall (T) () (enum (None (tuple)) (Some T)))) "
        self.rejects(lin + "(type S (struct (l L!)))", "type S holds a linear type; declare it S!$")
        self.rejects(lin + "(type S (Opt L!))", "type S holds a linear type; declare it S!$")
        self.rejects(lin + "(type G (forall (T) () (tuple T (tuple u8 L!))))",
                     "type G holds a linear type whatever its type arguments; declare it G!$")
        self.rejects(lin + "(type E (forall ($k) () (enum (A (brand $k)) (B L!))))",
                     "type E holds a linear type; declare it E!$")
        self.rejects(lin + "(type F (foreach (X (tuple u32 L!)) X))", "type F holds a linear type; declare it F!$")
        self.rejects(lin + "(type G (forall (T) () (foreach (X T) (ptr X))))",
                     "type G holds a linear type whatever its type arguments; declare it G!$")
        self.rejects(lin + "(type P (forall (T) () (foreach (X (tuple T (array L! 1))) X)))",
                     "type P holds a linear type whatever its type arguments; declare it P!$")
        check_program(parse_program(
            lin + "(type H! (struct (o (Opt L!)) (p (ptr L!)))) (type W (forall (T) () (Opt T))) "
            "(type Cap! (forall ($k) () (tuple))) (type F (foreach (X (tuple u32 L!)) (ptr X))) "
            "(type G! (forall (T) () (foreach (X T) (ptr X))))"))

    def test_trait_premises_have_valid_names_arity_and_kinds(self):
        self.rejects("(type Bad (forall (T) ((Missing T)) T))", "unknown trait Missing")
        self.rejects("(trait P (T)) (type Bad (forall (T) ((P T T)) T))", "wrong number")
        self.rejects("(trait P ($k)) (type Bad (forall (T) ((P T)) T))", "expected brand")
        self.rejects("(trait P (T) (run Missing (T)))", "unknown type Missing")
        check_program(parse_program("(trait P ($k) (run (brand $k) ())) "
                                    "(type Good (forall ($k) ((P $k)) (tuple)))"))

    def test_an_array_has_at_least_one_element(self):
        self.rejects("(type Bad (array u8 0))", "an array has at least one element")
        self.rejects("(type Bad (struct (a (array (array u8 0) 2))))", "an array has at least one element")
        self.rejects("(data d (array u8 0) ())", "an array has at least one element")
        check_program(parse_program("(type Good (array (tuple) 1))"))

    def test_binders_are_scoped_and_names_unique(self):
        self.rejects("(type Bad (forall (T T) () T))", "duplicate parameter")
        self.rejects("(type Bad (exists ($k $k) (tuple)))", "duplicate parameter")
        self.rejects("(type Bad (struct (x u32) (x u32)))", "duplicate component")
        self.rejects("(type Bad (foreach (F F) F))", "unknown type F")
        check_program(parse_program("(type Good (foreach (F (tuple u32)) (tuple F F)))"))

    def test_existential_binders_are_used_and_flattened(self):
        self.rejects("(type Bad (exists () u32))", "existential requires")
        self.rejects("(type Bad (exists ($k) u32))", "existential brand binder is unused")
        self.rejects("(type Bad (exists ($a) (exists ($b) "
                     "(tuple (brand $a) (brand $b)))))",
                     "nested existential binders must be flattened")
        check_program(parse_program("(type Good (exists ($a $b) "
                                    "(tuple (brand $a) (brand $b))))"))

    def test_arrays_and_place_indices_are_valid(self):
        self.rejects("(type Bad (array u32 -1))", "negative array")
        p = C.Program(types={"Bad": C.TypeDecl("Bad", (), (), C.Fn((), C.UNIT, places=frozenset({0})))})
        with self.assertRaisesRegex(KursError, "place index"):
            check_program(p)

    def test_unknown_types_are_rejected_in_dead_blocks(self):
        self.rejects("""(fun main u32 ()
          (entry () (= %n (const u32 0)) (ret %n))
          (dead ((%x Missing)) (ret %x)))""", "unknown type Missing")

    def test_call_type_arguments_are_formed_even_when_unused(self):
        self.rejects("""(fun f (forall (T) () u32 ()
          (entry () (= %n (const u32 0)) (ret %n))))
          (fun main u32 () (entry () (= %r (call (f Missing))) (ret %r)))""", "unknown type Missing")
        self.rejects("""(extern fun foreign u32 ())
          (fun main u32 () (entry () (unsafe (= %r (call (foreign u32)))) (ret %r)))""", "wrong number")
        self.rejects("""(fun f (forall ($k) () u32 ()
          (entry () (= %n (const u32 0)) (ret %n))))
          (fun main u32 () (entry () (= %p (fn (f $q))) (= %r (const u32 0)) (ret %r)))""", "brand q is not in scope")
        self.rejects("""(fun f (forall ($k) () u32 ()
          (entry () (= %n (const u32 0)) (ret %n))))
          (fun main u32 () (entry () (= %p (fn (f $static))) (= %r (call (%p $static))) (ret %r)))""",
                     "wrong number of type arguments")
        check_program(parse_program("""(fun f (forall ($k) () u32 ()
          (entry () (= %n (const u32 0)) (ret %n))))
          (fun main u32 () (entry () (= %p (fn (f $static))) (= %r (call %p)) (ret %r)))"""))

    def test_open_scopes_start_after_the_instruction(self):
        self.rejects("""(fun main u32 () (entry ()
          (= %n (sizeof (tuple u32 (brand $k))))
          (= %fresh (fresh)) (= %w (open %fresh ($k)))
          (= %r (const u32 0)) (ret %r)))""", "brand k is not in scope")
        check_program(parse_program("""(fun main u32 () (entry ()
          (= %fresh (fresh)) (= %w (open %fresh ($k)))
          (= %n (sizeof (tuple u32 (brand $k))))
          (= %r (const u32 0)) (ret %r)))"""))

    def test_open_in_one_block_does_not_bind_a_brand_in_its_sibling(self):
        self.rejects("""(fun main u32 ()
          (entry () (= %fresh (fresh)) (= %w (open %fresh ($k)))
                    (= %r (const u32 0)) (ret %r))
          (dead () (= %n (sizeof (brand $k))) (= %r (const u32 0)) (ret %r)))""",
                     "brand k is not in scope")

    def test_local_storage_requires_function_generic_brands(self):
        self.rejects("""(fun main u32 () (locals (&slot (brand $k)))
          (entry () (= %fresh (fresh)) (= %w (open %fresh ($k)))
          (= %u (store &slot %w)) (= %r (const u32 0)) (ret %r)))""", "local types may mention only")
        self.rejects("""(fun main u32 () (locals (&slot (brand $typo)))
          (entry () (= %r (const u32 0)) (ret %r)))""", "local types may mention only")
        self.rejects("""(fun main u32 () (locals (&slot (brand $k)))
          (entry () (= %fresh (fresh)) (= %w (open %fresh ($k)))
            (= %u (store &slot %w)) (= %r (const u32 0)) (ret %r))
          (dead () (= %w (load &slot)) (= %r (const u32 0)) (ret %r)))""",
                     "local types may mention only")
