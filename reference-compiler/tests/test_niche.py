"""`NonNull T` and the niche layout of enums: a `NonNull`'s all-zero value is none of its values, a
struct, tuple or non-empty array has the niche of its first component with one, and an enum of two
variants, one whose payload has a niche and one of a zero-size type, is its payload alone, the other
variant a null at the niche. Everything else keeps the tag. The layout follows from the types, per
specialization, never from a name. `examples/non_null` runs the same through the surface language."""
import re
import unittest

from kursal.check import check_program
from kursal.core import NonNull, Prim, Ptr, Struct, Tuple, Enum, Array, Named, Program, UNIT
from kursal.kurs import parse_program
from kursal.sexpr import KursError
from kursal.types import Types
from tests import test_c_names

NN = "(non_null u8)"
OPT = "(type Opt (forall (T) () (enum (None (tuple)) (Some T))))"
# A pointer to a `u8` holding 5, as a `NonNull`.
MAKE = """(unsafe (= %a (alloca u8))) (= %a2 (copy %a)) (= %v (const u8 5))
          (unsafe (= %st (ptr_store %a %v))) (unsafe (= %n (cast %a2 (non_null u8))))
          (= %n2 (copy %n)) (= %n3 (copy %n))"""


class NicheRuleTests(unittest.TestCase):
    """`Types.niche` and `Types.niche_variant` on shapes."""

    def setUp(self):
        self.types = Types(Program())
        self.nn = NonNull(Prim("u8"))

    def test_niche_sources_and_paths(self):
        niche = self.types.niche
        self.assertEqual(niche(self.nn), ())
        self.assertIsNone(niche(Ptr(Prim("u8"))))
        self.assertIsNone(niche(Prim("u64")))
        self.assertIsNone(niche(UNIT))
        self.assertEqual(niche(Struct((("len", Prim("u64")), ("p", self.nn), ("q", self.nn)))), ("p",))
        self.assertEqual(niche(Tuple((Prim("u32"), Tuple((UNIT, self.nn))))), ("1", "1"))
        self.assertEqual(niche(Array(self.nn, 3)), ("0",))
        self.assertIsNone(niche(Array(self.nn, 0)))
        self.assertIsNone(niche(Enum((("A", UNIT), ("B", self.nn)))))
        self.assertIsNone(niche(Named("T")))   # a type parameter: answered at each specialization

    def test_which_enums_get_the_niche_layout(self):
        variant = self.types.niche_variant
        self.assertEqual(variant((("None", UNIT), ("Some", self.nn))), 1)
        self.assertEqual(variant((("Some", self.nn), ("None", UNIT))), 0)
        self.assertEqual(variant((("Some", Struct((("p", self.nn),))), ("Empty", Tuple(())))), 0)
        self.assertIsNone(variant((("None", UNIT), ("Some", Ptr(Prim("u8"))))))
        self.assertIsNone(variant((("A", UNIT), ("B", UNIT), ("C", self.nn))))
        self.assertIsNone(variant((("A", self.nn), ("B", Prim("u32")))))
        self.assertIsNone(variant((("A", self.nn), ("B", self.nn))))
        self.assertIsNone(variant((("Some", self.nn),)))
        self.assertIsNone(variant((("None", UNIT), ("Some", Enum((("None", UNIT), ("Some", self.nn)))))))


class NicheLayoutTests(unittest.TestCase):
    run_core = test_c_names.CNameTests.run_core

    def test_an_enum_over_a_non_null_is_a_bare_pointer(self):
        output = self.run_core(OPT + f'''
            (fun read u32 ((Opt {NN}))
              (entry ((%o (Opt {NN}))) (switch %o (Opt {NN}) (None none) (Some some)))
              (none ((%u (tuple))) (= %r (const u32 100)) (ret %r))
              (some ((%n {NN})) (= %p (to_ptr %n (ptr u8)))
                (unsafe (= %b (ptr_load %p))) (= %r (int_cast %b u32)) (ret %r)))
            (fun main i32 () (entry () {MAKE}
              (= %o (construct_variant (Opt {NN}) Some %n))
              (= %u (construct (tuple))) (= %z (construct_variant (Opt {NN}) None %u))
              (= %t (variant_tag %z (Opt {NN})))
              (= %x (call read %o)) (= %y (call read %z)) (= %xy (add %x %y)) (= %xyt (add %xy %t))
              (= %size (sizeof (Opt {NN}))) (= %word (sizeof (ptr u8)))
              (= %same (eq %size %word)) (= %r (int_cast %xyt i32)) (jnz %same (done %r) (bad)))
            (done ((%r i32)) (ret %r))
            (bad () (= %r (const i32 1)) (ret %r)))''', 105)
        self.assertRegex(output, r"static uint32_t \w+\(uint8_t\* entry_o_1\)")
        self.assertIn("switch ((entry_o_1 == 0 ? 0 : 1)) {", output)
        self.assertIn("entry_z = 0;", output)
        # The cast's obligation, told to the C compiler.
        self.assertIn("entry_n = (uint8_t*)entry_a;\n  if (entry_n == 0) __builtin_unreachable();", output)
        self.assertNotRegex(output, r"struct K_Opt \{")

    def test_a_struct_with_a_non_null_field_inside_an_enum(self):
        output = self.run_core(OPT + f'''
            (type Run (struct (len u64) (unit (tuple)) (start {NN}) (end {NN})))
            (fun len u64 ((Opt Run))
              (entry ((%o (Opt Run))) (switch %o (Opt Run) (None none) (Some some)))
              (none ((%u (tuple))) (= %r (const u64 0)) (ret %r))
              (some ((%run Run)) (= (%l %u %s %e) (destruct %run Run)) (ret %l)))
            (fun main i32 () (entry () {MAKE}
              (= %seven (const u64 7)) (= %u (construct (tuple)))
              (= %run (construct Run %seven %u %n %n2))
              (= %o (construct_variant (Opt Run) Some %run))
              (= %u2 (construct (tuple))) (= %z (construct_variant (Opt Run) None %u2))
              (= %x (call len %o)) (= %y (call len %z)) (= %xy (add %x %y))
              (= %size (sizeof (Opt Run))) (= %run-size (sizeof Run))
              (= %same (eq %size %run-size)) (= %r (int_cast %xy i32)) (jnz %same (done %r) (bad)))
            (done ((%r i32)) (ret %r))
            (bad () (= %r (const i32 1)) (ret %r)))''', 7)
        self.assertIn("struct K_Run { uint64_t f_len; uint8_t* f_start; uint8_t* f_end; };", output)
        # The three-word enum is taken by pointer, and its niche read through it.
        self.assertIn("switch (((*entry_o_1).f_start == 0 ? 0 : 1)) {", output)
        self.assertIn("entry_z.f_start = 0;", output)

    def test_niches_in_tuples_and_arrays(self):
        output = self.run_core(OPT + f'''
            (fun main i32 () (entry () {MAKE}
              (= %one (const u32 1)) (= %pair (construct (tuple u32 {NN}) %one %n))
              (= %o (construct_variant (Opt (tuple u32 {NN})) Some %pair))
              (= %t (variant_tag %o (Opt (tuple u32 {NN}))))
              (= %arr (construct (array {NN} 2) %n2 %n3)) (= %u (construct (tuple)))
              (= %z (construct_variant (Opt (array {NN} 2)) None %u))
              (= %s (variant_tag %z (Opt (array {NN} 2))))
              (= %x1 (sizeof (Opt (tuple u32 {NN})))) (= %y1 (sizeof (tuple u32 {NN})))
              (= %x2 (sizeof (Opt (array {NN} 2)))) (= %y2 (sizeof (array {NN} 2)))
              (= %sa (add %x1 %y1)) (= %sb (add %x2 %y2)) (= %ab (add %sa %sb)) (= %ts (add %t %s))
              (= %wide (int_cast %ts usize)) (= %all (add %ab %wide))
              (= %r (int_cast %all i32)) (ret %r)))''', 4 * 16 + 1)
        self.assertIn(".e1 == 0 ? 0 : 1)", output)
        self.assertIn(".items[0] = 0;", output)

    def test_enums_that_keep_their_tag(self):
        output = self.run_core(OPT + f'''
            (type Three (enum (A (tuple)) (B (tuple)) (C {NN})))
            (type Two (enum (A {NN}) (B u32)))
            (fun main i32 () (entry ()
              (= %raw (sizeof (Opt (ptr u8))))
              (= %three (sizeof Three)) (= %two (sizeof Two))
              (= %nested (sizeof (Opt (Opt {NN})))) (= %number (sizeof (Opt u32)))
              (= %a (add %raw %three)) (= %b (add %a %two)) (= %c (add %b %nested)) (= %d (add %c %number))
              (= %r (int_cast %d i32)) (ret %r)))''', 4 * 16 + 8)
        self.assertIn("struct K_Three { uint32_t tag; union { uint8_t* v_C; } payload; };", output)
        self.assertIn("struct K_Two { uint32_t tag; union { uint8_t* v_A; uint32_t v_B; } payload; };", output)
        # Opt at a raw pointer, at Opt[NonNull] (whose pointer is a value of the inner enum) and at u32.
        self.assertIn("{ uint32_t tag; union { uint8_t* v_Some; } payload; };", output)
        self.assertIn("{ uint32_t tag; union { uint32_t v_Some; } payload; };", output)

    def test_each_specialization_is_laid_out_by_its_arguments(self):
        output = self.run_core(OPT + f'''
            (fun some (forall (T) () (Opt T) (T) (entry ((%x T)) (= %o (construct_variant (Opt T) Some %x)) (ret %o))))
            (fun tag (forall (T) ((Copy T)) u32 ((Opt T))
              (entry ((%o (Opt T))) (= %t (variant_tag %o (Opt T))) (ret %t))))
            (fun main i32 () (entry () {MAKE}
              (= %o (call (some {NN}) %n)) (= %k (const u32 3)) (= %q (call (some u32) %k))
              (= %x (call (tag {NN}) %o)) (= %y (call (tag u32) %q))
              (= %xy (add %x %y)) (= %r (int_cast %xy i32)) (ret %r)))''', 2)
        self.assertRegex(output, r"static uint8_t\* \w+\(uint8_t\* entry_x_\d\);")
        self.assertRegex(output, r"static K_Opt \w+\(uint32_t entry_x_\d\);")
        self.assertRegex(output, r"static uint32_t \w+\(uint8_t\* entry_o_\d\);")
        self.assertRegex(output, r"= \(entry_o_\d == 0 \? 0 : 1\);")
        self.assertRegex(output, r"= entry_o_\d\.tag;")

    def test_a_place_under_its_known_variant_is_the_whole_object(self):
        output = self.run_core(OPT + f'''
            (fun bump (tuple) ((Opt u64))
              (entry ((&o (Opt u64))) (switch &o (Opt u64) (None none) (Some some)))
              (none () (known &o None) (= %u (construct (tuple))) (ret %u))
              (some () (known &o Some) (= %x (load &o.Some)) (= %one (const u64 1)) (= %y (add %x %one))
                (= %s (store &o.Some %y)) (= %u (construct (tuple))) (ret %u)))
            (fun peek u8 ((Opt {NN}))
              (entry ((&o (Opt {NN}))) (switch &o (Opt {NN}) (None none) (Some some)))
              (none () (known &o None) (= %r (const u8 0)) (ret %r))
              (some () (known &o Some) (= %n (load &o.Some)) (= %p (to_ptr %n (ptr u8)))
                (unsafe (= %r (ptr_load %p))) (ret %r)))
            (fun main i32 () (locals (&o (Opt {NN})) (&c (Opt u64))) (entry () {MAKE}
              (= %o (construct_variant (Opt {NN}) Some %n)) (= %s (store &o %o)) (= %b (call peek &o))
              (= %six (const u64 6)) (= %c (construct_variant (Opt u64) Some %six)) (= %t (store &c %c))
              (= %u (call bump &c)) (= %r (int_cast %b i32)) (ret %r)))''', 5)
        self.assertRegex(output, r"static uint8_t \w+\(uint8_t\* const\* restrict entry_o_\d\);")
        self.assertRegex(output, r"switch \(\(\(\*entry_o_\d\) == 0 \? 0 : 1\)\) \{")
        self.assertRegex(output, r"switch \(\(\*entry_o_\d\)\.tag\) \{")


REJECTED = {
    "to_ptr_other_pointee": ("to_ptr_converts_non_null", f"""
        (fun f (ptr u16) ({NN}) (entry ((%n {NN}))
          (= %p (to_ptr %n (ptr u16))) (ret %p)))"""),
    "to_ptr_of_a_pointer": ("to_ptr_converts_non_null", """
        (fun f (ptr u8) ((ptr u8)) (entry ((%n (ptr u8)))
          (= %p (to_ptr %n (ptr u8))) (ret %p)))"""),
    "cast_to_non_null_is_unsafe": ("unsafe_authorization", f"""
        (fun f {NN} ((ptr u8)) (entry ((%p (ptr u8)))
          (= %n (cast %p {NN})) (ret %n)))"""),
    "integer_to_non_null": ("non_null_cast_pointer", f"""
        (fun f {NN} (usize) (entry ((%p usize)) (unsafe (= %n (cast %p {NN}))) (ret %n)))"""),
    "non_null_to_integer": ("non_null_cast_pointer", f"""
        (fun f usize ({NN}) (entry ((%n {NN})) (unsafe (= %p (cast %n usize))) (ret %p)))"""),
    "enum_to_pointer": ("unsafe_cast_layout_chosen", OPT + f"""
        (fun f (ptr u8) ((Opt {NN})) (entry ((%o (Opt {NN})))
          (unsafe (= %p (cast %o (ptr u8)))) (ret %p)))"""),
    "pointer_to_enum": ("unsafe_cast_layout_chosen", OPT + f"""
        (fun f (Opt {NN}) ((ptr u8)) (entry ((%p (ptr u8)))
          (unsafe (= %o (cast %p (Opt {NN})))) (ret %o)))"""),
    "tagged_enum_to_integer": ("unsafe_cast_layout_chosen", OPT + """
        (fun f u64 ((Opt u32)) (entry ((%o (Opt u32))) (unsafe (= %p (cast %o u64))) (ret %p)))"""),
    "struct_holding_non_null": ("unsafe_cast_layout_chosen", f"""
        (type Box (struct (p {NN})))
        (fun f Box ((tuple (ptr u8))) (entry ((%t (tuple (ptr u8))))
          (unsafe (= %b (cast %t Box))) (ret %b)))"""),
    "non_null_order": ("does_apply", f"""
        (fun f bool ({NN} {NN}) (entry ((%a {NN}) (%b {NN})) (= %r (lt %a %b)) (ret %r)))"""),
    "non_null_construct": ("not a product", f"""
        (fun f {NN} ((ptr u8)) (entry ((%p (ptr u8)))
          (= %n (construct {NN} %p)) (ret %n)))"""),
}

ACCEPTED = {
    "equality_and_copy": f"""
        (fun f bool ({NN} {NN}) (entry ((%a {NN}) (%b {NN}))
          (= %r (eq %a %b)) (ret %r)))
        (fun g bool ({NN} {NN}) (entry ((%a {NN}) (%b {NN})) (= %c (copy %a)) (= %r (ne %a %b)) (ret %r)))""",
    "pointer_casts": f"""
        (fun f (non_null u16) ({NN}) (entry ((%n {NN}))
          (unsafe (= %m (cast %n (non_null u16)))) (ret %m)))""",
    "non_null_of_anything": OPT + f"""
        (fun f (ptr (Opt {NN})) ((non_null (Opt {NN}))) (entry ((%n (non_null (Opt {NN}))))
          (= %p (to_ptr %n (ptr (Opt {NN})))) (ret %p)))""",
}


class NonNullCheckerTests(unittest.TestCase):
    def test_rejected(self):
        for name, (message, source) in REJECTED.items():
            with self.subTest(name), self.assertRaisesRegex(KursError, re.escape(message)):
                check_program(parse_program(source))

    def test_accepted(self):
        for name, source in ACCEPTED.items():
            with self.subTest(name):
                check_program(parse_program(source))


if __name__ == "__main__":
    unittest.main()
