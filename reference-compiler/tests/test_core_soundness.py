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
        (fun forge (brand $static) ()
          (entry () (= %f (fresh)) (= %w (open %f ($a))) (jmp (next $a) %w))
          (next ($static) ((%x (brand $static))) (ret %x)))"""),
    "block_brand_rebinds_generic": ("cannot rebind a function brand", """
        (fun g (forall ($b) () (brand $b) ()
          (entry () (= %f (fresh)) (= %w (open %f ($a))) (jmp (next $a) %w))
          (next ($b) ((%x (brand $b))) (ret %x))))"""),
    "static_as_generic_brand": ("static is not a parameter name", """
        (fun h (forall ($static) () u32 ((brand $static))
          (entry ((%x (brand $static))) (= %r (const u32 0)) (ret %r))))"""),

    # A block's `known` contract names a variant of the enum at its path.
    "known_unknown_variant": ("known_contract_names_a_variant", """
        (type L! (struct (a u32)))
        (type E! (enum (A L!) (B u32)))
        (fun f u32 ()
          (locals (&x E!))
          (entry () (= %n (const u32 0)) (ret %n))
          (done ((%r u32)) (known &x Nope) (ret %r)))"""),
    "known_not_an_enum": ("known_contract_names_a_variant", """
        (fun f u32 ()
          (locals (&x u32))
          (entry () (= %n (const u32 0)) (ret %n))
          (done ((%r u32)) (known &x A) (ret %r)))"""),

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
    "instance_method_place_mode": ("method_does_match_trait", """
        (trait K (T) (keep u32 ((& T))))
        (instance K (u32) (method keep u32 (u32) (entry ((%x u32)) (ret %x))))"""),
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
        (fun (unsafe) h u32 (L! u32) (entry ((%l L!) (%x u32)) (= %n (cast %x !)) (unreachable)))"""),
    "deref_to_never": ("unsafe_read_cannot_produce_never", """
        (fun (unsafe) h u32 ((ptr !)) (entry ((%p (ptr !)))
          (= %n (ptr_load %p)) (unreachable)))"""),
    # Nor a value that holds Never: a named type's fields are its value.
    "deref_to_struct_holding_never": ("unsafe_read_cannot_produce_never", """
        (type L! (struct (a u32)))
        (type S (struct (n !)))
        (fun (unsafe) h u32 (L! (ptr S)) (entry ((%l L!) (%p (ptr S)))
          (= %s (ptr_load %p)) (= %n (destruct %s S)) (unreachable)))"""),
    "lend_target_holding_never": ("unsafe_read_cannot_produce_never", """
        (type S (struct (n !)))
        (fun get u32 (S) (entry ((&s S)) (= %v (load &s)) (= %n (destruct %v S)) (unreachable)))
        (fun h u32 ((ptr S)) (entry ((%p (ptr S))) (unsafe (= %r (call get &*%p))) (ret %r)))"""),
    "deref_to_tuple_holding_never": ("unsafe_read_cannot_produce_never", """
        (type L! (struct (a u32)))
        (fun (unsafe) h u32 (L! (ptr (tuple !))) (entry ((%l L!) (%p (ptr (tuple !))))
          (= %s (ptr_load_volatile %p)) (= %n (destruct %s (tuple !))) (unreachable)))"""),
    "cast_to_static_brand": ("unsafe_cast_cannot_produce", """
        (fun (unsafe) h (brand $static) (u32)
          (entry ((%x u32)) (= %b (cast %x (brand $static))) (ret %b)))"""),
    "cast_to_nested_package": ("unsafe_cast_cannot_produce", """
        (fun (unsafe) h (tuple (exists ($k) (brand $k))) ((tuple u32))
          (entry ((%x (tuple u32))) (= %n (cast %x (tuple (exists ($k) (brand $k))))) (ret %n)))"""),
    "cast_to_named_function": ("unsafe_cast_cannot_produce", """
        (type S (struct (f (fn () u32))))
        (fun (unsafe) h S (u64) (entry ((%x u64)) (= %s (cast %x S)) (ret %s)))"""),
    "cast_to_nested_named_brand": ("unsafe_cast_cannot_produce", """
        (type W (struct (b (brand $static))))
        (type O (struct (n u64) (w W)))
        (fun (unsafe) h O (u64) (entry ((%x u64)) (= %o (cast %x O)) (ret %o)))"""),
    "cast_from_named_function": ("unsafe_cast_cannot_produce", """
        (type S (struct (f (fn () u32))))
        (fun (unsafe) h u64 (S) (entry ((%s S)) (= %x (cast %s u64)) (ret %x)))"""),
    "cast_from_abstract": ("unsafe_cast_cannot_produce", """
        (fun (unsafe) h (forall (T) () u32 (T) (entry ((%x T)) (= %n (cast %x u32)) (ret %n))))"""),

    # A raw pointer is cast only to or from a pointer or an integer, named types unfolded: a struct
    # holding a pointer is not one.
    "cast_pointer_to_struct_of_pointer": ("pointer_cast_pointer_or_integer", """
        (type W (struct (p (ptr u8))))
        (fun (unsafe) h W ((ptr u8)) (entry ((%p (ptr u8))) (= %w (cast %p W)) (ret %w)))"""),
    "cast_struct_of_pointer_to_pointer": ("pointer_cast_pointer_or_integer", """
        (type W (struct (p (ptr u8))))
        (fun (unsafe) h (ptr u8) (W) (entry ((%w W)) (= %p (cast %w (ptr u8))) (ret %p)))"""),
    "cast_pointer_to_float": ("pointer_cast_pointer_or_integer", """
        (fun (unsafe) h f64 ((ptr u8)) (entry ((%p (ptr u8))) (= %f (cast %p f64)) (ret %f)))"""),

    # Re-entering a nominal type's premises must descend. A brand an existential binds is a different
    # variable from the function's brand, however each is spelled: here the reentry into A changes its
    # brand argument while shrinking the other, so it does not descend under either spelling.
    "descent_through_shadowing_binder": ("non-descending nominal premise formation for A", """
        (trait P (T))
        (trait Q (T))
        (type A (forall ($a T) ((P T)) (tuple)))
        (type B (forall ()
          ((Q (exists ($a) (tuple (A $a u32) (brand $a)))))
          (tuple)))
        (instance P (u32))
        (instance Q ((exists ($c) (tuple (A $c u32) (brand $c)))))
        (instance P ((tuple u32 B)))
        (fun f (forall ($a) () u32 ((A $a (tuple u32 B)))
          (entry ((%x (A $a (tuple u32 B)))) (= %r (const u32 1)) (ret %r))))"""),
    "descent_through_renamed_binder": ("non-descending nominal premise formation for A", """
        (trait P (T))
        (trait Q (T))
        (type A (forall ($a T) ((P T)) (tuple)))
        (type B (forall ()
          ((Q (exists ($b) (tuple (A $b u32) (brand $b)))))
          (tuple)))
        (instance P (u32))
        (instance Q ((exists ($c) (tuple (A $c u32) (brand $c)))))
        (instance P ((tuple u32 B)))
        (fun f (forall ($a) () u32 ((A $a (tuple u32 B)))
          (entry ((%x (A $a (tuple u32 B)))) (= %r (const u32 1)) (ret %r))))"""),

    # Atomic read-modify-write takes integers; other atomics take integers, bools or pointers.
    "atomic_fetch_on_struct": ("atomic_pointee_type", """
        (type S (struct (a u32)))
        (fun (unsafe) h u32 ((ptr S) S) (entry ((%p (ptr S)) (%s S))
          (= %v (ptr_atomic_fetch_add seq_cst %p %s)) (= %r (const u32 0)) (ret %r)))"""),

    # A foreach type has an expansion at every instantiation: `*F` matches only pointer components, so its
    # source must show them as written, whatever its type arguments are, and not through a name.
    "pointer_pattern_over_leaf": ("foreach \\*F needs a source written with pointer components", """
        (fun main i32 ()
          (entry () (= %n (sizeof (foreach ((ptr F) bool) bool))) (= %z (const i32 0)) (ret %z)))"""),
    "pointer_pattern_over_parameter": ("foreach \\*F needs a source written with pointer components", """
        (fun g (forall (T) () usize () (entry () (= %n (sizeof (foreach ((ptr F) T) F))) (ret %n))))"""),
    "pointer_pattern_over_mixed_tuple": ("foreach \\*F needs a source written with pointer components", """
        (fun g (forall (T) () usize () (entry () (= %n (sizeof (foreach ((ptr F) (tuple (ptr u8) T)) F))) (ret %n))))"""),
    "pointer_pattern_over_sum": ("foreach \\*F needs a source written with pointer components", """
        (type Opt (enum (none (tuple)) (some (ptr u8))))
        (fun g usize () (entry () (= %n (sizeof (foreach ((ptr F) Opt) F))) (ret %n)))"""),
    "pointer_pattern_through_a_name": ("foreach \\*F needs a source written with pointer components", """
        (type P (struct (a (ptr u8)) (b (ptr u16))))
        (fun g usize () (entry () (= %n (sizeof (foreach ((ptr F) P) F))) (ret %n)))"""),
    "pointer_pattern_in_declaration": ("foreach \\*F needs a source written with pointer components in type Holder!", """
        (type Holder! (forall (T) () (struct (f (foreach ((ptr F) T) F)))))"""),
    # A pointer source points to a product, an array or a leaf: a pointer to a sum is opaque, and no
    # foreach type or traversal has one as its source, through a name or with either pattern.
    "foreach_type_over_pointer_to_sum": ("type formation: a foreach source cannot be a pointer to a sum", """
        (type Opt (enum (none (tuple)) (some u8)))
        (fun g usize () (entry () (= %n (sizeof (foreach ((ptr F) (ptr Opt)) F))) (ret %n)))"""),
    "foreach_value_pattern_over_pointer_to_sum": ("type formation: a foreach source cannot be a pointer to a sum", """
        (type Opt (enum (none (tuple)) (some u8)))
        (type O (struct (o Opt)))
        (type W (enum (only O)))
        (fun g usize () (entry () (= %n (sizeof (foreach (F (ptr W)) F))) (ret %n)))"""),
    "foreach_through_pointer_to_sum": ("type formation: a foreach source cannot be a pointer to a sum", """
        (type Opt (enum (none (tuple)) (some u8)))
        (fun mapper (forall (F) () u32 ((ptr F)) (entry ((%x (ptr F))) (= %z (const u32 0)) (ret %z))))
        (fun walk (foreach ((ptr F) (ptr Opt)) u32) ((ptr Opt))
          (entry ((%p (ptr Opt))) (unsafe (= %out (foreach (foreach ((ptr F) (ptr Opt)) u32) %p mapper))) (ret %out)))"""),

    # A function type quantifies nothing: a function's brands and premises are settled where its value is
    # made.
    "function_type_quantifier": ("a function type quantifies nothing", """
        (fun h u32 ((fn (forall ($k) () u32 ((brand $k)))))
          (entry ((%f (fn (forall ($k) () u32 ((brand $k)))))) (= %r (const u32 0)) (ret %r)))"""),

    # Pointer projection names the pointer's type exactly.
    "pointer_destruct_wrong_type": ("destruct", """
        (type S (struct (a u32)))
        (fun h u32 ((ptr S)) (entry ((%p (ptr S)))
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
        (type Pk (struct (pkg (exists ($h) (tuple u32 (brand $h))))))
        (fun f u32 (Pk) (entry ((&k Pk)) (= %o (open &k.pkg ($o))) (= %whole (load &k))
          (= (%pkg) (destruct %whole Pk)) (= %r (const u32 0)) (ret %r)))"""),
    "lend_over_an_open_view": ("nested_view_open", """
        (type Pk (struct (pkg (exists ($h) (tuple u32 (brand $h))))))
        (fun g u32 (Pk) (entry ((&k Pk)) (= %r (const u32 0)) (ret %r)))
        (fun f u32 (Pk) (entry ((&k Pk)) (= %o (open &k.pkg ($o))) (= %r (call g &k))
          (= %c (pack (exists ($h) (tuple u32 (brand $h))) $o &k.pkg)) (ret %r)))"""),

    # A package's binder is its own brand, never a free brand it shares a name with: packing 'q as $p0
    # does not also hide the function's 'p0.
    "pack_binder_is_not_a_free_brand": ("packing", """
        (fun f (forall ($p0 $q) () u32 ((brand $p0) (brand $q))
          (entry ((%a (brand $p0)) (%b (brand $q)))
            (= %v (construct (tuple (brand $q) (brand $p0)) %b %a))
            (= %k (pack (exists ($p0) (tuple (brand $p0) (brand $p0))) ($q) %v))
            (= %r (const u32 0)) (ret %r))))"""),

    # `fn` names every brand of its function, each in scope where the value is made.
    "function_value_unnamed_brand": ("_ is not a type", """
        (fun g (forall ($b T) () T ((brand $b) T)
          (entry ((%w (brand $b)) (%x T)) (ret %x))))
        (fun h (forall ($b) () (fn ((brand $b) u32) u32) ()
          (entry () (= %f (fn (g _ u32))) (ret %f))))"""),
    "function_value_brand_out_of_scope": ("brand q is not in scope", """
        (fun g (forall ($b T) () T ((brand $b) T)
          (entry ((%w (brand $b)) (%x T)) (ret %x))))
        (fun h (fn ((brand $static) u32) u32) ()
          (entry () (= %f (fn (g $q u32))) (ret %f)))"""),

    # A parameter never has a declared type's name: it would read as the declaration wherever a type is
    # unfolded, so a field of type Foo would be returned as whatever f's Foo stands for, and dup's Box
    # would copy a linear argument as the plain struct Box.
    "parameter_named_as_type": ("Foo is a declared type, not a parameter name", """
        (type Foo (struct (a u64)))
        (type Bar (struct (f Foo)))
        (fun f (forall (Foo) () Foo (Bar)
          (entry ((%b Bar)) (= %x (destruct %b Bar)) (ret %x))))"""),
    "parameter_named_as_copy_type": ("Box is a declared type, not a parameter name", """
        (type Box (struct (k u32)))
        (fun dup (forall (Box) () (tuple Box Box) (Box)
          (entry ((%b Box))
            (= %k (destruct %b Box)) (= %k2 (copy %k))
            (= %b1 (construct Box %k)) (= %b2 (construct Box %k2))
            (= %r (construct (tuple Box Box) %b1 %b2)) (ret %r))))"""),
    "foreach_binder_named_as_type": ("Foo is a declared type, not a parameter name", """
        (type Foo (struct (a u64)))
        (type T! (forall (S) () (struct (f (foreach (Foo S) Foo)))))"""),
    "existential_binder_named_as_type": ("Foo is a declared type, not a parameter name", """
        (type Foo (struct (a u64)))
        (type T (struct (f (exists ($Foo) (brand $Foo)))))"""),
    "block_brand_named_as_type": ("Foo is a declared type, not a parameter name", """
        (type Foo (struct (a u64)))
        (fun f u32 ()
          (entry () (= %f (fresh)) (= %w (open %f ($a))) (jmp (next $a) %w))
          (next ($Foo) ((%x (brand $Foo))) (= %r (const u32 0)) (ret %r)))"""),
    "trait_parameter_named_as_type": ("Foo is a declared type, not a parameter name", """
        (type Foo (struct (a u64)))
        (trait K (Foo) (keep Foo (Foo)))"""),
    "instance_parameter_named_as_type": ("Foo is a declared type, not a parameter name", """
        (type Foo (struct (a u64)))
        (trait K (T))
        (instance K (forall (Foo) () ((ptr Foo))))"""),

    # A mapper is checked against a foreach's binders as new variables, never as the caller's type
    # parameters of the same name: m's X is the binder F, a u64, not the caller's F, so the row
    # (F, F) would turn a u64 into an F, and the premise K[F] would hold of a u64.
    "foreach_binder_is_not_a_caller_parameter": ("mapper_does_map", """
        (fun m (forall (X Y) () (tuple X Y) (X Y)
          (entry ((%x X) (%y Y)) (= %t (construct (tuple X Y) %x %y)) (ret %t))))
        (fun f (forall (F) ((Copy F)) u64 (F)
          (entry ((%c F))
            (= %z (const u64 0)) (= %s (construct (tuple u64) %z))
            (= %r (foreach (foreach (F (tuple u64)) (tuple F F)) %s (m F) %c))
            (= %row (destruct %r (foreach (F (tuple u64)) (tuple F F))))
            (= (%a %b) (destruct %row (tuple u64 u64))) (ret %b))))"""),
    "foreach_binder_is_not_a_caller_parameter_renamed": ("mapper_does_map", """
        (fun m (forall (X Y) () (tuple X Y) (X Y)
          (entry ((%x X) (%y Y)) (= %t (construct (tuple X Y) %x %y)) (ret %t))))
        (fun f (forall (F) ((Copy F)) u64 (F)
          (entry ((%c F))
            (= %z (const u64 0)) (= %s (construct (tuple u64) %z))
            (= %r (foreach (foreach (G (tuple u64)) (tuple G G)) %s (m F) %c))
            (= %row (destruct %r (foreach (G (tuple u64)) (tuple G G))))
            (= (%a %b) (destruct %row (tuple u64 u64))) (ret %b))))"""),
    "foreach_binder_takes_no_caller_premise": ("unsatisfied_premise", """
        (trait K (T) (k u32 (T)))
        (fun m (forall (X) ((K X)) u32 (X)
          (entry ((%x X)) (= %r (trait_call K k (X) %x)) (ret %r))))
        (fun f (forall (F) ((K F)) (foreach (F (tuple u64)) u32) ()
          (entry ()
            (= %z (const u64 0)) (= %s (construct (tuple u64) %z))
            (= %r (foreach (foreach (F (tuple u64)) u32) %s m)) (ret %r))))"""),
    "foreach_binder_takes_no_caller_premise_renamed": ("unsatisfied_premise", """
        (trait K (T) (k u32 (T)))
        (fun m (forall (X) ((K X)) u32 (X)
          (entry ((%x X)) (= %r (trait_call K k (X) %x)) (ret %r))))
        (fun f (forall (F) ((K F)) (foreach (G (tuple u64)) u32) ()
          (entry ()
            (= %z (const u64 0)) (= %s (construct (tuple u64) %z))
            (= %r (foreach (foreach (G (tuple u64)) u32) %s m)) (ret %r))))"""),

    # A foreach type is flat (TY-FLAT): a whole value's type, a function's parameter or result, or a
    # declaration's body, never anywhere else, a declaration whose body is one included.
    "foreach_source_is_a_foreach": ("a foreach source expands to a foreach type", """
        (fun f usize () (entry () (= %n (sizeof (foreach (G (foreach (F (tuple u32)) F)) G))) (ret %n)))"""),
    "foreach_body_is_a_foreach": ("a foreach body expands to a foreach type", """
        (fun f usize () (entry () (= %n (sizeof (foreach (G (tuple u32)) (foreach (F (tuple G)) F)))) (ret %n)))"""),
    "foreach_component": ("a component expands to a foreach type", """
        (fun f usize () (entry () (= %n (sizeof (tuple (foreach (F (tuple u32)) F) u8))) (ret %n)))"""),
    "foreach_field_through_a_declaration": ("a component expands to a foreach type in type P", """
        (type W (foreach (F (tuple u32)) F))
        (type P (struct (w W)))"""),
    "foreach_pointee": ("a pointee expands to a foreach type", """
        (fun f usize () (entry () (= %n (sizeof (ptr (foreach (F (tuple u32)) F)))) (ret %n)))"""),
    "foreach_package_body": ("a package body expands to a foreach type", """
        (fun f usize () (entry () (= %n (sizeof (exists ($k) (foreach (F (tuple (brand $k))) F)))) (ret %n)))"""),
    "foreach_type_argument": ("a type argument expands to a foreach type", """
        (type W (foreach (F (tuple u32)) F))
        (fun id (forall (T) () T (T) (entry ((%x T)) (ret %x))))
        (fun f W (W) (entry ((%w W)) (= %v (call (id W) %w)) (ret %v)))"""),
    "foreach_premise_argument": ("a type argument expands to a foreach type", """
        (fun f (forall (T) ((Exposed (foreach (F T) F))) u32 () (entry () (= %r (const u32 0)) (ret %r))))"""),
    "foreach_stack_slot": ("a pointee expands to a foreach type", """
        (fun (unsafe) f u32 () (entry ()
          (= %p (alloca (foreach (F (tuple u32)) F))) (= %r (const u32 0)) (ret %r)))"""),

    # An enum has at least one variant: a sum of none would be a type with no value.
    "empty_enum_declaration": ("an enum has at least one variant", "(type E (enum))"),
    # A data item is Copy: its static object is never written, so a load through a pointer to it may not
    # move a linear value out (DECL-DATA), a forced-linear type's whose fields are all Copy included.
    "data_linear": ("data_type_not_copy", """
        (type L! (struct (n u32)))
        (data d L! (7))"""),
    "data_holding_linear": ("data_type_not_copy", """
        (type L! (struct (n u32)))
        (data d (tuple u8 (array L! 2)) (1 ((2) (3))))"""),

    "empty_enum_written": ("an enum has at least one variant", """
        (fun f usize () (entry () (= %n (sizeof (tuple (enum)))) (ret %n)))"""),

    # A load or store of a zero-size type reads and writes nothing (MEM-ZERO-SIZE): it is formed only at a
    # type with an erased value. A load would conjure a brand witness, a package or a linear value, and a
    # store would make one vanish, a capability's included; a Brand is Copy, and still has none.
    "zero_size_linear_store": ("zero_size_access_not_erasable", """
        (type Cap! (struct))
        (fun (unsafe) f (tuple) ((ptr Cap!) Cap!) (entry ((%p (ptr Cap!)) (%c Cap!))
          (= %u (ptr_store %p %c)) (ret %u)))"""),
    "zero_size_linear_volatile_store": ("zero_size_access_not_erasable", """
        (type Cap! (struct))
        (fun (unsafe) f (tuple) ((ptr (tuple Cap! (tuple))) (tuple Cap! (tuple)))
          (entry ((%p (ptr (tuple Cap! (tuple)))) (%c (tuple Cap! (tuple))))
            (= %u (ptr_store_volatile %p %c)) (ret %u)))"""),
    "zero_size_linear_load": ("zero_size_access_not_erasable", """
        (type Cap! (struct))
        (fun (unsafe) f Cap! ((ptr Cap!)) (entry ((%p (ptr Cap!))) (= %c (ptr_load %p)) (ret %c)))"""),
    "zero_size_witness_load": ("zero_size_access_not_erasable", """
        (type W (forall ($k) () (struct (b (brand $k)) (u (tuple)))))
        (fun (unsafe) f (forall ($k) () (W $k) ((ptr (W $k))) (entry ((%p (ptr (W $k)))) (= %w (ptr_load_volatile %p)) (ret %w))))"""),
    "zero_size_witness_store": ("zero_size_access_not_erasable", """
        (fun (unsafe) f (forall ($k) () (tuple) ((ptr (brand $k)) (brand $k))
          (entry ((%p (ptr (brand $k))) (%b (brand $k))) (= %u (ptr_store %p %b)) (ret %u))))"""),
    "zero_size_package_load": ("zero_size_access_not_erasable", """
        (fun (unsafe) f (exists ($a) (brand $a)) ((ptr (exists ($a) (brand $a))))
          (entry ((%p (ptr (exists ($a) (brand $a))))) (= %w (ptr_load %p)) (ret %w)))"""),

    # A zero-size type is erased: an unsafe cast has no representation of one to reinterpret, and C has
    # no zero-size parameter or object for an extern. Its loads and stores at an erasable type are accepted
    # (test_zero_size).
    "cast_to_unit": ("zero_size_type_has_no_memory_form", """
        (fun (unsafe) h (tuple) (u8) (entry ((%x u8)) (= %u (cast %x (tuple))) (ret %u)))"""),
    "cast_from_unit": ("zero_size_type_has_no_memory_form", """
        (fun (unsafe) h u8 ((tuple)) (entry ((%u (tuple))) (= %x (cast %u u8)) (ret %x)))"""),
    "extern_unit_parameter": ("zero_size_type_has_no_memory_form", "(extern fun f u32 ((tuple)))"),
    # There is no extern value: a foreign datum is reached through a function that returns a pointer to it.
    "extern_value": ("an extern is \\(extern fun NAME RET \\(PTYPE\\*\\)\\)", "(extern value v (tuple u64 u8))"),

    # What foreign code hands back is reinterpreted from C's bytes, as a cast's result is: it holds
    # nothing only checked code makes, and no layout the compiler chooses.
    "extern_result_brand": ("extern_result_cannot_produce", """
        (type W (struct (n u64) (b (brand $static))))
        (extern fun forge W ())"""),
    "extern_result_safe_function": ("extern_result_cannot_produce", "(extern fun make (fn () u32) ())"),
    "extern_result_package": ("extern_result_cannot_produce", "(extern fun make (tuple u64 (exists ($k) (brand $k))) ())"),
    "extern_result_holding_never": ("extern_result_cannot_produce", "(extern fun make (tuple u64 !) ())"),
    "extern_result_enum": ("extern_result_layout_chosen", """
        (type E (enum (A u32) (B u64)))
        (extern fun make (tuple u64 E) ())"""),
    "extern_result_non_null": ("extern_result_layout_chosen", "(extern fun make (non_null u8) ())"),

    # Compiler-reserved traits are sealed, and a Copy premise names a parameter.
    "user_exposed_trait": ("trait_name_is_reserved", "(trait Exposed (T)) (instance Exposed (u32))"),
    "user_sized_trait": ("trait_name_is_reserved", "(trait Sized (T)) (instance Sized (u32))"),

    # `Sized[T]`: a finite layout of non-zero size. It holds of a type parameter only by the caller's own
    # premise, never of a zero-size type, and it is no premise of a type declaration.
    "sized_parameter_without_premise": ("unsatisfied_premise", """
        (fun need (forall (T) ((Sized T)) u32 () (entry () (= %r (const u32 0)) (ret %r))))
        (fun f (forall (U) () u32 () (entry () (= %r (call (need U))) (ret %r))))"""),
    "sized_holding_a_parameter": ("unsatisfied_premise", """
        (fun need (forall (T) ((Sized T)) u32 () (entry () (= %r (const u32 0)) (ret %r))))
        (fun f (forall (U) ((Sized U)) u32 () (entry () (= %r (call (need (tuple U u32)))) (ret %r))))"""),
    "sized_zero_size": ("unsatisfied_premise", """
        (fun need (forall (T) ((Sized T)) u32 () (entry () (= %r (const u32 0)) (ret %r))))
        (fun f u32 () (entry () (= %r (call (need (tuple (tuple) (array u64 0))))) (ret %r)))"""),
    "sized_brand_witness": ("unsatisfied_premise", """
        (fun need (forall (T) ((Sized T)) u32 () (entry () (= %r (const u32 0)) (ret %r))))
        (fun f u32 () (entry () (= %r (call (need (brand $static)))) (ret %r)))"""),
    "sized_premise_on_a_type": ("Sized is not a premise of a type declaration",
                                "(type W (forall (T) ((Sized T)) (struct (t T))))"),
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
            # A pointer gives pointer components whatever its pointee is but a sum: a leaf, a product
            # holding a sum, or a type parameter. A pointer to a sum is a value still, and a component.
            """(type Opt (enum (none (tuple)) (some u8)))
               (fun f usize ((ptr Opt)) (entry ((%p (ptr Opt)))
                 (= %a (sizeof (foreach ((ptr F) (ptr bool)) F)))
                 (= %b (sizeof (foreach ((ptr F) (ptr (tuple u8 Opt))) F)))
                 (= %c (sizeof (foreach (F (tuple (ptr Opt) u8)) F)))
                 (= %d (sizeof (foreach ((ptr F) (tuple (ptr u8) (ptr u16))) F)))
                 (ret %a)))
               (fun g (forall (T) () usize () (entry () (= %n (sizeof (foreach ((ptr F) (ptr T)) F))) (ret %n))))""",
            # A block brand that is new to the function.
            """(fun f u32 ()
                 (entry () (= %f (fresh)) (= %w (open %f ($a))) (jmp (next $a) %w))
                 (next ($n) ((%x (brand $n))) (= %r (const u32 0)) (ret %r)))""",
            # A restricted declaration used by its owner.
            """(type (in (module 0 a) abstract) Secret (struct (k u32)))
               (fun (in (module 0 a)) h u32 (Secret) (entry ((%s Secret)) (= %k (destruct %s Secret)) (ret %k)))
               (data (in (module 0 a)) made Secret (7))""",
            # An unsafe cast may make and take apart a restricted type outside its module: the type's
            # invariants are the cast's obligation (lib/std/CONTRACTS.md, section 4).
            """(type (in (module 0 a) abstract) Secret (struct (k u32)))
               (fun (in (module 0 b) unsafe) make Secret (u32) (entry ((%x u32)) (= %s (cast %x Secret)) (ret %s)))
               (fun (in (module 0 b) unsafe) open u32 (Secret) (entry ((%s Secret)) (= %x (cast %s u32)) (ret %x)))""",
            # Sizes are public: of a hidden type, of a type holding one, of a type parameter.
            """(type (in (module 0 a) abstract) Secret (struct (k u32)))
               (fun (in (module 0 b)) h usize () (entry () (= %n (sizeof Secret)) (ret %n)))
               (fun (in (module 0 b)) i usize () (entry () (= %n (sizeof (tuple u8 Secret))) (ret %n)))
               (fun (in (module 0 b)) g (forall (T) () usize () (entry () (= %n (sizeof T)) (ret %n))))""",
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
            # A package beside a free brand of the same spelling, as a pack's own binders are written:
            # opening it gives the hidden brand a new name and leaves the function's $p0 itself.
            """(fun f (forall ($p0 $q) () u32 ((brand $p0) (brand $q))
                 (entry ((%a (brand $p0)) (%b (brand $q)))
                   (= %v (construct (tuple (brand $q) (brand $p0)) %b %a))
                   (= %k (pack (exists ($p0#) (tuple (brand $p0#) (brand $p0))) ($q) %v))
                   (= %o (open %k ($t)))
                   (= (%x %y) (destruct %o (tuple (brand $t) (brand $p0))))
                   (= %r (const u32 0)) (ret %r))))""",
            # A generic read is checked at its type parameter, not at what instantiates it: that a read
            # through a pointer never produces Never, here at `rd[!]`, is the unsafe read's obligation
            # (lib/std/CONTRACTS.md, section 2.3), which nothing checks.
            """(type L! (struct (a u32)))
               (fun (unsafe) rd (forall (T) () T ((ptr T)) (entry ((%p (ptr T))) (= %v (ptr_load %p)) (ret %v))))
               (fun (unsafe) h u32 (L! (ptr !)) (entry ((%l L!) (%p (ptr !))) (= %n (call (rd !) %p)) (unreachable)))""",
            # A pointer read out of memory is a value; what it points at is not.
            """(type S (struct (n !)))
               (fun (unsafe) h (ptr S) ((ptr (ptr S))) (entry ((%p (ptr (ptr S)))) (= %q (ptr_load %p)) (ret %q)))""",
            # A foreign function that does not return, one that hands back an `unsafe fn` value (calling
            # it needs `unsafe`), and parameters, which the result rule does not reach.
            """(extern fun stop ! ())
               (extern fun callback (tuple (fn (unsafe) ((ptr u8)) (ptr u8)) u64) ())
               (extern fun take u32 ((fn () u32) (non_null u8)))""",
            # The walk of a value follows its by-value layout: not what a function value takes and
            # returns, and not a named type's arguments but through its fields, so a type that holds
            # itself behind a function or a pointer wrapper ends it.
            """(type A (struct (callback (fn (unsafe) (A) A)) (n u64)))
               (type Ref (forall (T) () (struct (ptr (ptr T)))))
               (type Node (forall (T) () (struct (value T) (next (Ref (Node T))))))
               (extern fun make A ())
               (fun (unsafe) read A ((ptr A)) (entry ((%p (ptr A))) (= %a (ptr_load %p)) (ret %a)))
               (fun (unsafe) cast (Node u64) ((tuple u64 (ptr u8))) (entry ((%t (tuple u64 (ptr u8))))
                 (= %n (cast %t (Node u64))) (ret %n)))""",
            # A cast of a type holding itself in an array of length 0, which holds no value.
            """(type R (struct (a (array R 0)) (b u32)))
               (fun (unsafe) h (tuple u64 R) ((tuple u64 R)) (entry ((%t (tuple u64 R)))
                 (= %s (cast %t (tuple u64 R))) (ret %s)))""",
            # A foreach body names the caller's type parameter beside its own binder.
            """(fun m (forall (X Y) () (tuple X Y) (X Y)
                 (entry ((%x X) (%y Y)) (= %t (construct (tuple X Y) %x %y)) (ret %t))))
               (fun f (forall (F) ((Copy F)) F (F)
                 (entry ((%c F))
                   (= %z (const u64 0)) (= %s (construct (tuple u64) %z))
                   (= %r (foreach (foreach (G (tuple u64)) (tuple G F)) %s (m F) %c))
                   (= %row (destruct %r (foreach (G (tuple u64)) (tuple G F))))
                   (= (%a %b) (destruct %row (tuple u64 F))) (ret %b))))""",
            # A foreach type as a declaration's body, a function's parameter and result, and a parameter
            # of a function type.
            """(type W (foreach (F (tuple u32 u8)) (tuple F)))
               (fun keep W (W (fn ((foreach (F (tuple u8)) F)) W))
                 (entry ((%w W) (%g (fn ((foreach (F (tuple u8)) F)) W))) (ret %w)))""",
            # `Sized` of a concrete type of non-zero size, a zero-size field included, and of a type
            # parameter under the caller's own premise.
            """(type P (struct (a (tuple)) (b u8)))
               (fun need (forall (T) ((Sized T)) u32 () (entry () (= %r (const u32 0)) (ret %r))))
               (fun f u32 () (entry () (= %a (call (need P))) (= %b (call (need (ptr (tuple))))) (ret %a)))
               (fun g (forall (U) ((Sized U)) u32 () (entry () (= %r (call (need U))) (ret %r))))""",
            # Pointer projection at the pointer's own type.
            """(type S (struct (a u32)))
               (fun h u32 ((ptr S)) (entry ((%p (ptr S)))
                 (= %a (destruct %p (ptr S))) (= %r (const u32 0)) (ret %r)))""",
        ):
            with self.subTest(source=source.split("\n")[0]):
                check_program(parse_program(source))


# One instruction of each unsafe operation, in a safe function with `%p: (ptr u32)` and `%x, %y: u32`.
UNSAFE_INSTRUCTIONS = {
    "cast": "(= %v (cast %p (ptr i32)))",
    "alloca": "(= %v (alloca u32))",
    "static_brand": "(= %v (static_brand))",
    "ptr_load": "(= %v (ptr_load %p))",
    "ptr_store": "(= %v (ptr_store %p %x))",
    "ptr_load_volatile": "(= %v (ptr_load_volatile %p))",
    "ptr_store_volatile": "(= %v (ptr_store_volatile %p %x))",
    "ptr_atomic_load": "(= %v (ptr_atomic_load acquire %p))",
    "ptr_atomic_store": "(= %v (ptr_atomic_store release %p %x))",
    "ptr_atomic_swap": "(= %v (ptr_atomic_swap seq_cst %p %x))",
    "ptr_atomic_compare_exchange": "(= %v (ptr_atomic_compare_exchange seq_cst/seq_cst %p %x %y))",
    "ptr_atomic_fetch_add": "(= %v (ptr_atomic_fetch_add seq_cst %p %x))",
    "ptr_atomic_fetch_sub": "(= %v (ptr_atomic_fetch_sub seq_cst %p %x))",
    "ptr_atomic_fetch_and": "(= %v (ptr_atomic_fetch_and seq_cst %p %x))",
    "ptr_atomic_fetch_or": "(= %v (ptr_atomic_fetch_or seq_cst %p %x))",
    "ptr_atomic_fetch_xor": "(= %v (ptr_atomic_fetch_xor seq_cst %p %x))",
    "fence": "(= %v (fence seq_cst))",
}


class UnsafeOperationTests(unittest.TestCase):
    """Each operation of `UNSAFE_OPS` needs the `(unsafe ...)` marker in a safe function, and only those do."""

    def program(self, instr: str) -> str:
        return f"(fun f u32 ((ptr u32) u32 u32) (entry ((%p (ptr u32)) (%x u32) (%y u32)) {instr} (= %r (const u32 0)) (ret %r)))"

    def test_every_unsafe_operation_is_covered(self):
        from kursal.core import UNSAFE_OPS
        self.assertEqual(set(UNSAFE_INSTRUCTIONS), set(UNSAFE_OPS))

    def test_a_missing_marker_is_rejected(self):
        for op, instr in UNSAFE_INSTRUCTIONS.items():
            with self.subTest(op=op):
                check_program(parse_program(self.program(f"(unsafe {instr})")))
                with self.assertRaisesRegex(KursError, "unsafe_authorization"):
                    check_program(parse_program(self.program(instr)))

    def test_an_unsafe_function_needs_no_marker(self):
        for op, instr in UNSAFE_INSTRUCTIONS.items():
            with self.subTest(op=op):
                check_program(parse_program(self.program(instr).replace("(fun f", "(fun (unsafe) f", 1)))

    def test_safe_operations_need_no_marker(self):
        for instr in ("(= %v (int_cast %x u64))", "(= %v (float_cast %x f64))", "(= %v (sizeof u32))", "(= %v (fresh))"):
            with self.subTest(instr=instr):
                check_program(parse_program(self.program(instr)))


class EntryTests(unittest.TestCase):
    """The hosted entry is a safe, non-generic `fn() -> i32`, or `fn(A) -> i32` with A a struct of a `*(*u8)` and a
    `usize` (`std::process::Args`), without premises."""

    def emit(self, source):
        from kursal.cgen import emit_c
        program = parse_program(source)
        return emit_c(program, check_program(program), "main")

    def test_the_two_entry_signatures(self):
        self.assertIn("int main(void)", self.emit("(fun main i32 () (entry () (= %r (const i32 0)) (ret %r)))"))
        self.assertIn("int main(int argc, char **argv)", self.emit(
            """(type Args (struct (argv (ptr (ptr u8))) (count usize)))
               (fun main i32 (Args)
                 (entry ((%a Args)) (= %r (const i32 0)) (ret %r)))"""))

    def test_other_entries_are_rejected(self):
        for source in (
            "(fun main u32 () (entry () (= %r (const u32 0)) (ret %r)))",
            "(fun (unsafe) main i32 () (entry () (= %r (const i32 0)) (ret %r)))",
            "(fun main (forall (T) () i32 () (entry () (= %r (const i32 0)) (ret %r))))",
            "(fun main i32 (i32) (entry ((%n i32)) (ret %n)))",
            "(fun main i32 (i32 (ptr (ptr u8))) (entry ((%n i32) (%v (ptr (ptr u8)))) (ret %n)))",
            "(type A (struct (count usize) (argv (ptr (ptr u8))))) (fun main i32 (A) (entry ((%a A)) (= %r (const i32 0)) (ret %r)))",
            "(trait P (T)) (instance P (u32)) (fun main (forall () ((P u32)) i32 () (entry () (= %r (const i32 0)) (ret %r))))",
        ):
            with self.subTest(source=source), self.assertRaisesRegex(KursError, "the entry main must be"):
                self.emit(source)


if __name__ == "__main__":
    unittest.main()
