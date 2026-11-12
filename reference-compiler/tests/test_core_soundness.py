"""Core programs that would forge a witness, skip a premise, run an unchecked body, manufacture authority,
or drop an obligation: each is rejected by the checker."""

import unittest

from kursal.check import check_program
from kursal.kurs import parse_program
from kursal.sexpr import KursError


REJECTED = {
    # A block brand parameter cannot rebind `static` or a function brand: the jump would turn a fresh
    # witness into `Brand[static]` or into the caller's brand.
    "block_brand_rebinds_static": ("cannot rebind a function brand", """
        (fun forge (builtin::Brand $static) ()
          (entry () (= %f (builtin::fresh)) (= %w (open %f ($a))) (jmp (next $a) %w))
          (next ($static) ((%x (builtin::Brand $static))) (ret %x)))"""),
    "block_brand_rebinds_generic": ("cannot rebind a function brand", """
        (fun g (forall ($b) () (builtin::Brand $b) ()
          (entry () (= %f (builtin::fresh)) (= %w (open %f ($a))) (jmp (next $a) %w))
          (next ($b) ((%x (builtin::Brand $b))) (ret %x))))"""),
    "static_as_generic_brand": ("static is not a parameter name", """
        (fun h (forall ($static) () u32 ((builtin::Brand $static))
          (entry ((%x (builtin::Brand $static))) (= %r (const u32 0)) (ret %r))))"""),

    # An instance method has no quantifiers of its own: its premises would never be discharged.
    "instance_method_premise": ("instance_method_has_own_quantifier", """
        (trait K (T) (keep T (T)))
        (trait Z (T))
        (instance K (u32) (method keep (forall () ((Z u32)) u32 (u32) (entry ((%x u32)) (ret %x)))))"""),

    # An instance has exactly its trait's methods, once each.
    "duplicate_instance_method": ("duplicate_instance_method", """
        (trait K (T) (keep T (T)))
        (instance K (u32) (method keep u32 (u32) (entry ((%x u32)) (ret %x)))
                          (method keep u32 (u32) (entry ((%x u32)) (ret %x))))"""),
    "extra_instance_method": ("instance_method_not_in_trait", """
        (trait K (T) (keep T (T)))
        (instance K (u32) (method keep u32 (u32) (entry ((%x u32)) (ret %x)))
                          (method extra u32 (u32) (entry ((%x u32)) (ret %x))))"""),
    "duplicate_trait_method": ("duplicate_trait_method", "(trait K (T) (keep T (T)) (keep T (T)))"),

    # Code with no module has no shape authority over a restricted declaration.
    "moduleless_destruct": ("not exposed", """
        (type (in (module 0 a) abstract) Secret (struct (k u32)))
        (fun h u32 (Secret) (entry ((%s Secret)) (= %k (destruct %s Secret)) (ret %k)))"""),
    "moduleless_construct": ("not exposed", """
        (type (in (module 0 a) abstract) Secret (struct (k u32)))
        (fun h Secret () (entry () (= %k (const u32 1)) (= %s (construct Secret %k)) (ret %s)))"""),
    "foreign_data_initializer": ("not exposed", """
        (type (in (module 0 a) abstract) Secret (struct (k u32)))
        (data (in (module 0 b)) forged Secret (7))"""),

    # Unsafe operations do not conjure Never, packages, brand witnesses or values of abstract types.
    "cast_to_never": ("unsafe_cast_cannot_produce", """
        (type L! (struct (a u32)))
        (fun (unsafe) h u32 (L u32) (entry ((%l L) (%x u32)) (= %n (builtin::unsafe::cast %x !)) (unreachable)))"""),
    "deref_to_never": ("unsafe_read_cannot_produce_never", """
        (fun (unsafe) h u32 ((builtin::Pointer !)) (entry ((%p (builtin::Pointer !)))
          (= %n (builtin::unsafe::deref %p)) (unreachable)))"""),
    "cast_to_static_brand": ("unsafe_cast_cannot_produce", """
        (fun (unsafe) h (builtin::Brand $static) (u32)
          (entry ((%x u32)) (= %b (builtin::unsafe::cast %x (builtin::Brand $static))) (ret %b)))"""),
    "cast_to_nested_package": ("unsafe_cast_cannot_produce", """
        (fun (unsafe) h (tuple (exists ($k) (builtin::Brand $k))) ((tuple u32))
          (entry ((%x (tuple u32))) (= %n (builtin::unsafe::cast %x (tuple (exists ($k) (builtin::Brand $k))))) (ret %n)))"""),
    "cast_from_abstract": ("unsafe_cast_cannot_produce", """
        (fun (unsafe) h (forall (T) () u32 (T) (entry ((%x T)) (= %n (builtin::unsafe::cast %x u32)) (ret %n))))"""),

    # Atomic read-modify-write takes integers; other atomics take integers, bools or pointers.
    "atomic_fetch_on_struct": ("atomic_pointee_type", """
        (type S (struct (a u32)))
        (fun (unsafe) h u32 ((builtin::Pointer S) S) (entry ((%p (builtin::Pointer S)) (%s S))
          (= %v (builtin::unsafe::atomic_fetch_add seq_cst %p %s)) (= %r (const u32 0)) (ret %r)))"""),

    # A function value carries no premises: they are discharged where the value is made.
    "function_type_premise": ("a function type has no premises", """
        (trait Q (T))
        (fun h u32 ((builtin::Fn (forall () ((Q u32)) u32 ())))
          (entry ((%f (builtin::Fn (forall () ((Q u32)) u32 ())))) (= %r (const u32 0)) (ret %r)))"""),

    # Pointer projection names the pointer's type exactly.
    "pointer_destruct_wrong_type": ("destruct", """
        (type S (struct (a u32)))
        (fun h u32 ((builtin::Pointer S)) (entry ((%p (builtin::Pointer S)))
          (= %a (destruct %p u64)) (= %r (const u32 0)) (ret %r)))"""),

    # Literals have one spelling.
    "unsigned_negative_zero": ("invalid", "(fun h u32 () (entry () (= %r (const u32 -0)) (ret %r)))"),
    "bool_capitalized": ("invalid", "(fun h bool () (entry () (= %r (const bool True)) (ret %r)))"),
    "bool_digit": ("invalid", "(fun h bool () (entry () (= %r (const bool 1)) (ret %r)))"),

    # Branching is on a bool, or on an enum with one arm per variant: integer matches, grouped arms and
    # wildcards are the frontend's to expand.
    "integer_jnz": ("jnz_bool_condition", """
        (fun h u32 (u32) (entry ((%c u32)) (jnz %c (a) (b)))
          (a () (= %r (const u32 0)) (ret %r)) (b () (= %r (const u32 1)) (ret %r)))"""),
    "integer_switch": ("switch_on_enum", """
        (fun h u32 (u32) (entry ((%c u32)) (switch %c u32 (0 a) (1 b)))
          (a () (= %r (const u32 0)) (ret %r)) (b () (= %r (const u32 1)) (ret %r)))"""),
    "wildcard_arm": ("switch_arm_per_variant", """
        (type E (enum (A u32) (B u32)))
        (fun h u32 (E) (entry ((%e E)) (switch %e E (A a) (_ b)))
          (a ((%n u32)) (ret %n)) (b ((%n u32)) (ret %n)))"""),
    "missing_arm": ("switch_arm_per_variant", """
        (type E (enum (A u32) (B u32)))
        (fun h u32 (E) (entry ((%e E)) (switch %e E (A a))) (a ((%n u32)) (ret %n)))"""),

    # Any number of whole roots may be lent at once, but at most two paths below roots, parting at a
    # struct or tuple.
    "three_nested_lends": ("at_most_two_nested_lends", """
        (type T3 (struct (a u32) (b u32) (c u32)))
        (fun three u32 (u32 u32 u32) (entry ((&a u32) (&b u32) (&c u32)) (= %r (const u32 0)) (ret %r)))
        (fun f u32 (T3) (entry ((&t T3)) (= %r (call three &t.a &t.b &t.c)) (ret %r)))"""),
    "nested_lends_through_a_variant": ("nested_lends_part_at_a_product", """
        (type E (enum (A (tuple u32 u32)) (B u32)))
        (fun two u32 (u32 u32) (entry ((&a u32) (&b u32)) (= %r (const u32 0)) (ret %r)))
        (fun f u32 (E) (entry ((&e E)) (switch &e E (A a) (B b)))
          (a () (known &e A) (= %r (call two &e.A.0 &e.A.1)) (ret %r))
          (b () (known &e B) (= %r (const u32 0)) (ret %r)))"""),

    # An edge newly assumes one path empty per root, or the leaves of one full struct/tuple tree.
    "edge_empties_through_a_variant": ("edge_empties_one_path_or_product_leaves", """
        (type E (enum (A (tuple u32 u32)) (B u32)))
        (fun f u32 (E) (entry ((&e E)) (switch &e E (A a) (B b)))
          (a () (known &e A) (jmp out))
          (out () (known &e A) (empty &e.A.0) (empty &e.A.1)
            (= %x (const u32 0)) (= %u (store &e.A.0 %x)) (= %y (const u32 0)) (= %v (store &e.A.1 %y))
            (= %r (const u32 1)) (ret %r))
          (b () (known &e B) (= %r (const u32 0)) (ret %r)))"""),

    # A view opened inside a place is closed before the place is loaded or lent.
    "load_over_an_open_view": ("nested_view_open", """
        (type Pk (struct (pkg (exists ($h) (tuple u32 (builtin::Brand $h))))))
        (fun f u32 (Pk) (entry ((&k Pk)) (= %o (open &k.pkg ($o))) (= %whole (load &k))
          (= (%pkg) (destruct %whole Pk)) (= %r (const u32 0)) (ret %r)))"""),
    "lend_over_an_open_view": ("nested_view_open", """
        (type Pk (struct (pkg (exists ($h) (tuple u32 (builtin::Brand $h))))))
        (fun g u32 (Pk) (entry ((&k Pk)) (= %r (const u32 0)) (ret %r)))
        (fun f u32 (Pk) (entry ((&k Pk)) (= %o (open &k.pkg ($o))) (= %r (call g &k))
          (= %c (pack (exists ($h) (tuple u32 (builtin::Brand $h))) $o &k.pkg)) (ret %r)))"""),

    # A zero-size type's memory form is not decided: nothing observes it.
    "sizeof_unit": ("zero_size_type_has_no_memory_form",
                    "(fun h usize () (entry () (= %n (builtin::sizeof (tuple))) (ret %n)))"),
    "alloca_empty_array": ("zero_size_type_has_no_memory_form", """
        (fun (unsafe) h u32 () (entry () (= %p (alloca (array u32 0))) (= %r (const u32 0)) (ret %r)))"""),
    "deref_unit": ("zero_size_type_has_no_memory_form", """
        (fun (unsafe) h u32 ((builtin::Pointer (tuple))) (entry ((%p (builtin::Pointer (tuple))))
          (= %v (builtin::unsafe::deref %p)) (= %r (const u32 0)) (ret %r)))"""),
    "extern_unit_parameter": ("zero_size_type_has_no_memory_form", "(extern fun f u32 ((tuple)))"),

    # Compiler-reserved traits are sealed, and a Copy premise names a parameter.
    "user_exposed_trait": ("trait_name_is_reserved", "(trait builtin::Exposed (T)) (instance builtin::Exposed (u32))"),
    "copy_premise_on_a_type": ("Copy requires a type parameter name",
                               "(fun h (forall (T) ((Copy u32)) u32 () (entry () (= %r (const u32 0)) (ret %r))))"),
    "copy_premise_on_an_unknown_name": ("Copy requires a type parameter name",
                                        "(fun h (forall (T) ((Copy U)) u32 () (entry () (= %r (const u32 0)) (ret %r))))"),
}


class CoreSoundnessTests(unittest.TestCase):
    def test_rejected(self):
        for name, (error, source) in REJECTED.items():
            with self.subTest(case=name), self.assertRaisesRegex(KursError, error):
                check_program(parse_program(source))

    def test_the_sound_forms_are_accepted(self):
        for source in (
            # A block brand that is new to the function.
            """(fun f u32 ()
                 (entry () (= %f (builtin::fresh)) (= %w (open %f ($a))) (jmp (next $a) %w))
                 (next ($n) ((%x (builtin::Brand $n))) (= %r (const u32 0)) (ret %r)))""",
            # A restricted declaration used by its owner.
            """(type (in (module 0 a) abstract) Secret (struct (k u32)))
               (fun (in (module 0 a)) h u32 (Secret) (entry ((%s Secret)) (= %k (destruct %s Secret)) (ret %k)))
               (data (in (module 0 a)) made Secret (7))""",
            # Sizes are public: of a hidden type, of a type holding one, of a type parameter.
            """(type (in (module 0 a) abstract) Secret (struct (k u32)))
               (fun (in (module 0 b)) h usize () (entry () (= %n (builtin::sizeof Secret)) (ret %n)))
               (fun (in (module 0 b)) i usize () (entry () (= %n (builtin::sizeof (tuple u8 Secret))) (ret %n)))
               (fun (in (module 0 b)) g (forall (T) () usize () (entry () (= %n (builtin::sizeof T)) (ret %n))))""",
            # Two sibling fields lent together, beside a whole root.
            """(type T3 (struct (a u32) (b u32) (c u32)))
               (fun three u32 (u32 u32 T3) (entry ((&a u32) (&b u32) (&t T3)) (= %r (const u32 0)) (ret %r)))
               (fun f u32 (T3 T3) (entry ((&s T3) (&t T3)) (= %r (call three &s.a &s.c &t)) (ret %r)))""",
            # An edge newly assuming the leaves of one struct/tuple tree empty.
            """(type S (struct (a u32) (b u32) (c (tuple u32 u32))))
               (fun f u32 (S) (entry ((&s S)) (jmp out))
                 (out () (empty &s.a) (empty &s.c.0)
                   (= %x (const u32 0)) (= %u (store &s.a %x)) (= %y (const u32 0)) (= %v (store &s.c.0 %y))
                   (= %r (const u32 1)) (ret %r)))""",
            # Pointer projection at the pointer's own type.
            """(type S (struct (a u32)))
               (fun h u32 ((builtin::Pointer S)) (entry ((%p (builtin::Pointer S)))
                 (= %a (destruct %p (builtin::Pointer S))) (= %r (const u32 0)) (ret %r)))""",
        ):
            with self.subTest(source=source.split("\n")[0]):
                check_program(parse_program(source))


class ZeroSizeTests(unittest.TestCase):
    def test_a_specialization_cannot_choose_a_zero_size_type(self):
        from kursal.cgen import emit_c
        program = parse_program("""(fun size (forall (T) () usize () (entry () (= %n (builtin::sizeof T)) (ret %n))))
            (fun main i32 () (entry () (= %n (call (size (tuple)))) (= %r (const i32 0)) (ret %r)))""")
        with self.assertRaisesRegex(KursError, "zero-size type has no memory form"):
            emit_c(program, check_program(program), "main")


class EntryTests(unittest.TestCase):
    """The hosted entry is a safe, non-generic `fn() -> i32` or `fn(i32, *(*u8)) -> i32` without premises."""

    def emit(self, source):
        from kursal.cgen import emit_c
        program = parse_program(source)
        return emit_c(program, check_program(program), "main")

    def test_the_two_entry_signatures(self):
        self.assertIn("int main(void)", self.emit("(fun main i32 () (entry () (= %r (const i32 0)) (ret %r)))"))
        self.assertIn("int main(int argc, char **argv)", self.emit(
            """(fun main i32 (i32 (builtin::Pointer (builtin::Pointer u8)))
                 (entry ((%n i32) (%v (builtin::Pointer (builtin::Pointer u8)))) (ret %n)))"""))

    def test_other_entries_are_rejected(self):
        for source in (
            "(fun main u32 () (entry () (= %r (const u32 0)) (ret %r)))",
            "(fun (unsafe) main i32 () (entry () (= %r (const i32 0)) (ret %r)))",
            "(fun main (forall (T) () i32 () (entry () (= %r (const i32 0)) (ret %r))))",
            "(fun main i32 (i32) (entry ((%n i32)) (ret %n)))",
            "(trait P (T)) (instance P (u32)) (fun main (forall () ((P u32)) i32 () (entry () (= %r (const i32 0)) (ret %r))))",
        ):
            with self.subTest(source=source), self.assertRaisesRegex(KursError, "the entry main must be"):
                self.emit(source)


if __name__ == "__main__":
    unittest.main()
