"""Place programs and the checker's verdict on each: (accepted, the rule a rejection names, program).
Each program is checked after TYPES."""

TYPES = """
(type Cur (struct (pos u32)))
(type H! (struct (n u32)))
(type Box! (struct (h H) (n u32)))
(type E! (enum (A H) (B u32)))
(type Holder! (struct (e E) (k u32)))
(fun touch u32 (H) (entry ((&h H)) (= %r (const u32 1)) (ret %r)))
(fun two u32 (H Box) (entry ((&h H) (&x Box)) (= %r (const u32 2)) (ret %r)))
(fun eat u32 (H) (entry ((%h H)) (= %n (destruct %h H)) (ret %n)))
(type Pk! (struct (pkg (exists ($h) (tuple u32 (builtin::Brand $h))))))
(fun peek_t (forall ($b) () u32 ((tuple u32 (builtin::Brand $b)))
  (entry ((&t (tuple u32 (builtin::Brand $b)))) (= %n (load &t.0)) (ret %n))))
"""

PACK = "(exists ($h) (tuple u32 (builtin::Brand $h)))"

CASES = {
    "copy_field": (True, None, """(fun bump u32 (Cur u32) (entry ((&c Cur) (%by u32))
   (= %pos (load &c.pos)) (= %old (copy %pos)) (= %new (add %pos %by)) (= %u (store &c.pos %new)) (ret %old)))"""),
    "move_and_store_back": (True, None, """(fun f u32 (Box) (entry ((&b Box))
   (= %h (load &b.h)) (= %u (store &b.h %h)) (= %r (const u32 0)) (ret %r)))"""),
    "return_while_empty": (False, 'place_parameter_restored', """(fun f H (Box) (entry ((&b Box)) (= %h (load &b.h)) (ret %h)))"""),
    "load_twice": (False, 'full', """(fun f u32 (Box) (entry ((&b Box))
   (= %h (load &b.h)) (= %h2 (load &b.h)) (= %u (store &b.h %h)) (= %r (call eat %h2)) (ret %r)))"""),
    "store_over_full": (False, 'storing_into_discard_linear', """(fun f u32 (Box) (entry ((&b Box))
   (= %n (const u32 1)) (= %h (construct H %n)) (= %u (store &b.h %h)) (= %r (const u32 0)) (ret %r)))"""),
    "store_whole_after_fields": (True, None, """(fun f u32 (Box) (entry ((&b Box))
   (= %h (load &b.h)) (= %n (load &b.n)) (= %nb (construct Box %h %n)) (= %u (store &b %nb)) (= %r (const u32 0)) (ret %r)))"""),
    "store_part_of_emptied_whole": (False, 'empty_ancestor_store_whole', """(fun f u32 (Box) (entry ((&b Box))
   (= %x (load &b)) (= %n (const u32 1)) (= %u (store &b.n %n)) (= %u2 (store &b %x)) (= %r (const u32 0)) (ret %r)))"""),
    "lend_field": (True, None, """(fun f u32 (Box) (entry ((&b Box)) (= %r (call touch &b.h)) (ret %r)))"""),
    "lend_overlapping": (False, 'lent_places_overlap', """(fun f u32 (Box) (entry ((&b Box)) (= %r (call two &b.h &b)) (ret %r)))"""),
    "lend_empty": (False, 'lent_place_full', """(fun f u32 (Box) (entry ((&b Box))
   (= %h (load &b.h)) (= %r (call touch &b.h)) (= %u (store &b.h %h)) (ret %r)))"""),
    "value_to_place_param": (False, 'argument_mode', """(fun f u32 (H) (entry ((%h H)) (= %r (call touch %h)) (ret %r)))"""),
    "place_to_value_param": (False, 'argument_mode', """(fun f u32 (Box) (entry ((&b Box)) (= %r (call eat &b.h)) (ret %r)))"""),
    "edge_declared": (True, None, """(fun f u32 (Box) (entry ((&b Box)) (= %h (load &b.h)) (jmp next %h))
   (next ((%h H)) (empty &b.h) (= %u (store &b.h %h)) (= %r (const u32 0)) (ret %r)))"""),
    "edge_undeclared": (False, 'empty_assumes_full', """(fun f u32 (Box) (entry ((&b Box)) (= %h (load &b.h)) (jmp next %h))
   (next ((%h H)) (= %u (store &b.h %h)) (= %r (const u32 0)) (ret %r)))"""),
    "local_place": (True, None, """(fun g H (H) (locals (&q H)) (entry ((%h H)) (= %u (store &q %h)) (= %v (load &q)) (ret %v)))"""),
    "local_left_full": (False, 'local_place_still_holds', """(fun g u32 (H) (locals (&q H)) (entry ((%h H)) (= %u (store &q %h)) (= %r (const u32 0)) (ret %r)))"""),
    "switch_place": (True, None, """(fun f u32 (Holder) (entry ((&x Holder)) (switch &x.e E (A a) (B b)))
   (a () (known &x.e A) (= %r (call touch &x.e.A)) (ret %r))
   (b () (known &x.e B) (= %v (load &x.e.B)) (ret %v)))"""),
    "switch_place_unknown_step": (False, 'known', """(fun f u32 (Holder) (entry ((&x Holder)) (switch &x.e E (A a) (B b)))
   (a () (= %r (call touch &x.e.A)) (ret %r))
   (b () (known &x.e B) (= %v (load &x.e.B)) (ret %v)))"""),
    "switch_place_wrong_variant": (False, 'known', """(fun f u32 (Holder) (entry ((&x Holder)) (switch &x.e E (A a) (B b)))
   (a () (known &x.e A) (= %v (load &x.e.B)) (ret %v))
   (b () (known &x.e B) (= %v (load &x.e.B)) (ret %v)))"""),
    "variant_change": (True, None, """(fun f u32 (Holder) (entry ((&x Holder)) (switch &x.e E (A a) (B b)))
   (a () (known &x.e A) (= %h (load &x.e.A)) (= %r (call eat %h)) (= %r2 (copy %r)) (= %nb (construct_variant E B %r2)) (= %u (store &x.e %nb)) (ret %r))
   (b () (known &x.e B) (= %v (load &x.e.B)) (ret %v)))"""),
    "variant_change_without_move": (False, 'storing_into_discard_linear', """(fun f u32 (Holder) (entry ((&x Holder)) (switch &x.e E (A a) (B b)))
   (a () (known &x.e A) (= %r (const u32 0)) (= %r2 (copy %r)) (= %nb (construct_variant E B %r2)) (= %u (store &x.e %nb)) (ret %r))
   (b () (known &x.e B) (= %v (load &x.e.B)) (ret %v)))"""),
    "known_lost_after_lend": (False, 'known', """(fun mod u32 (E) (entry ((&e E)) (= %r (const u32 0)) (ret %r)))
 (fun f u32 (Holder) (entry ((&x Holder)) (switch &x.e E (A a) (B b)))
   (a () (known &x.e A) (= %z (call mod &x.e)) (= %r (call touch &x.e.A)) (ret %r))
   (b () (known &x.e B) (= %v (load &x.e.B)) (ret %v)))"""),
    "place_name_not_declared": (False, 'place_function', """(fun f u32 (Box) (entry ((%b Box)) (= %h (load &b.h)) (ret %h)))"""),
    "join_drops_copy_part": (True, None, """(fun f u32 (Box) (entry ((&b Box)) (= %c (const bool true)) (jnz %c (l) (r)))
   (l () (= %h (load &b.h)) (= %r (call eat %h)) (jmp out %r))
   (r () (= %x (load &b)) (= (%h %n) (destruct %x Box)) (= %r (call eat %h)) (jmp out %r))
   (out ((%r u32)) (empty &b) (= %n1 (const u32 1)) (= %n2 (const u32 2)) (= %h1 (construct H %n1)) (= %nb (construct Box %h1 %n2)) (= %u (store &b %nb)) (ret %r)))"""),
    "join_claims_moved_copy_part": (False, 'empty_assumes_full', """(fun f u32 (Box) (entry ((&b Box)) (= %x (load &b)) (= (%h %n) (destruct %x Box)) (= %r (call eat %h)) (jmp out %r))
   (out ((%r u32)) (empty &b.h) (= %k (load &b.n)) (= %n1 (const u32 1)) (= %h1 (construct H %n1)) (= %u (store &b.h %h1)) (ret %r)))"""),
    "join_drops_linear": (False, 'holds_linear_contents_assumes', """(fun f u32 (Box) (entry ((&b Box)) (= %r (const u32 0)) (jmp out %r))
   (out ((%r u32)) (empty &b.h) (= %n1 (const u32 1)) (= %h1 (construct H %n1)) (= %u (store &b.h %h1)) (ret %r)))"""),
    "entry_declares_empty": (False, 'storing_into_discard_linear', """(fun f u32 (Box) (entry ((&b Box)) (empty &b.h)
   (= %n1 (const u32 1)) (= %h1 (construct H %n1)) (= %u (store &b.h %h1)) (= %r (const u32 0)) (ret %r)))"""),
    "entry_declares_known": (False, 'known', """(fun f u32 (Holder) (entry ((&x Holder)) (known &x.e B) (= %v (load &x.e.B)) (ret %v)))"""),
    "open_place_read": (True, None, """(fun f u32 (Pk) (entry ((&k Pk)) (= %u (open &k.pkg ($a))) (= %n (load &k.pkg.0))
   (= %u2 (pack PACK $a &k.pkg)) (ret %n)))"""),
    "open_place_lend": (True, None, """(fun f u32 (Pk) (entry ((&k Pk)) (= %u (open &k.pkg ($a)))
   (= %n (call (peek_t $a) &k.pkg)) (= %u2 (pack PACK $a &k.pkg)) (ret %n)))"""),
    "open_place_left_open": (False, 'place_parameter_sealed', """(fun f u32 (Pk) (entry ((&k Pk)) (= %u (open &k.pkg ($a))) (= %n (load &k.pkg.0)) (ret %n)))"""),
    "step_into_closed_package": (False, 'names_component', """(fun f u32 (Pk) (entry ((&k Pk)) (= %n (load &k.pkg.0)) (ret %n)))"""),
    "open_lost_at_edge": (False, 'opened_view_contract', """(fun f u32 (Pk) (entry ((&k Pk)) (= %u (open &k.pkg ($a))) (jmp next))
   (next () (= %n (load &k.pkg.0)) (ret %n)))"""),
    "pack_place_not_open": (False, 'open_at_those_brands', """(fun f u32 (Pk) (entry ((&k Pk)) (= %u (pack PACK $static &k.pkg)) (= %r (const u32 0)) (ret %r)))"""),
    "open_place_twice": (False, 'place_already_open', """(fun f u32 (Pk) (entry ((&k Pk)) (= %u (open &k.pkg ($a))) (= %u2 (open &k.pkg ($b))) (= %r (const u32 0)) (ret %r)))"""),
    "variant_tag_place": (True, None, """(fun f u32 (Holder) (entry ((&x Holder)) (= %t (variant_tag &x.e E)) (ret %t)))"""),
    "target_lend": (True, None, """(fun f u32 ((builtin::Pointer H)) (entry ((%p (builtin::Pointer H))) (unsafe (= %r (call touch &*%p))) (ret %r)))"""),
    "target_lend_needs_unsafe": (False, 'unsafe_authorization', """(fun f u32 ((builtin::Pointer H)) (entry ((%p (builtin::Pointer H))) (= %r (call touch &*%p)) (ret %r)))"""),
    # Every edge carries the place state, a one-variant arm of a switch over
    # a value too: here the arm would load `&b.h` a second time.
    "value_switch_arm_state": (False, 'empty_assumes_full', """(fun f u32 (Box E) (entry ((&b Box) (%e E))
   (= %h (load &b.h)) (= %k (call eat %h)) (switch %e E (A a) (B c)))
  (a ((%x H)) (= %h2 (load &b.h)) (= %u (store &b.h %h2)) (= %r (call eat %x)) (ret %r))
  (c ((%y u32)) (= %h3 (load &b.h)) (= %u (store &b.h %h3)) (ret %y)))"""),
    # A local place whose type names a block's own brand may not reach the
    # next block full: the `open` in the loop names the same brand each time,
    # so the witness kept in it would pass for the next package's.
    "local_block_brand_across_edge": (False, 'local types may mention only', """(fun g u32 () (locals (&q (builtin::Brand $a)))
  (entry () (= %f (builtin::fresh)) (= %b (open %f ($a))) (= %u (store &q %b)) (jmp loop))
  (loop () (= %f2 (builtin::fresh)) (= %b2 (open %f2 ($a))) (= %c (load &q)) (= %u2 (store &q %b2)) (jmp loop)))"""),
    # Even an empty declaration cannot mention a body-local brand.
    "local_block_brand_dropped": (False, 'local types may mention only', """(fun g u32 () (locals (&q (builtin::Brand $a)))
  (entry () (jmp loop))
  (loop () (empty &q) (= %f (builtin::fresh)) (= %b (open %f ($a))) (= %u (store &q %b)) (jmp loop)))"""),
    # Emptying or reading the slot before an edge does not repair its declaration.
    "local_block_brand_emptied": (False, 'local types may mention only', """(fun g u32 () (locals (&q (builtin::Brand $a)))
  (entry () (jmp loop))
  (loop () (empty &q) (= %f (builtin::fresh)) (= %b (open %f ($a))) (= %u (store &q %b)) (= %c (load &q)) (jmp loop)))"""),
}

CASES = {name: (accepted, message, program.replace("PACK", PACK))
         for name, (accepted, message, program) in CASES.items()}
