"""Places: the checker's verdict on the place corpus, and runnable
place programs whose C must exit with the expected status.

    uv run python tools/test.py tests.test_places
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from kursal.cc import cc_command
from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.kurs import parse_program
from kursal.sexpr import KursError
from tests.place_cases import CASES, TYPES

COMMON = """
(type Cur (struct (pos u32)))
(type H! (struct (n u32)))
(type Box! (struct (h H!) (n u32)))
(type E! (enum (A H!) (B u32)))
(type Holder! (struct (e E!) (k u32)))
(fun eat u32 (H!) (entry ((%h H!)) (= %n (destruct %h H!)) (ret %n)))
(fun bump u32 (Cur u32) (entry ((&c Cur) (%by u32))
  (= %pos (load &c.pos)) (= %old (copy %pos)) (= %new (add %pos %by)) (= %u (store &c.pos %new)) (ret %old)))
"""

# name -> (expected exit status, program defining `main`)
PROGRAMS = {
    # A local place lent around a loop; each call replaces its contents.
    "lend_in_loop": (15, """
(fun main i32 () (locals (&c Cur))
  (entry () (= %z (const u32 0)) (= %s (construct Cur %z)) (= %u (store &c %s)) (= %i (const u32 5)) (jmp head %i))
  (head ((%i u32)) (= %t (copy %i)) (= %zero (const u32 0)) (= %more (ne %t %zero)) (jnz %more (body %i) (done)))
  (body ((%i u32)) (= %by (copy %i)) (= %old (call bump &c %by)) (= %one (const u32 1)) (= %n (sub %i %one)) (jmp head %n))
  (done () (= %pos (load &c.pos)) (= %r (int_cast %pos i32)) (ret %r)))"""),
    # A linear field moved out, carried across an edge, and stored back.
    "partial_product_edge": (47, """
(fun take u32 (Box!) (entry ((&b Box!)) (= %h (load &b.h)) (jmp resume %h))
  (resume ((%h H!)) (empty &b.h) (= %u (store &b.h %h)) (= %n (load &b.n)) (ret %n)))
(fun main i32 () (locals (&b Box!))
  (entry () (= %a (const u32 5)) (= %h (construct H! %a)) (= %k (const u32 42)) (= %x (construct Box! %h %k))
    (= %u (store &b %x)) (= %n (call take &b)) (= %whole (load &b)) (= (%h2 %k2) (destruct %whole Box!))
    (= %m (call eat %h2)) (= %s (add %n %m)) (= %r (int_cast %s i32)) (ret %r)))"""),
    # A switch on a place: the arm knows its variant, reads through it, and
    # one arm replaces the sum with the other variant.
    "place_switch": (9, """
(fun step u32 (Holder!) (entry ((&x Holder!)) (switch &x.e E! (A a) (B b)))
  (a () (known &x.e A) (= %h (load &x.e.A)) (= %n (call eat %h)) (= %n2 (copy %n)) (= %v (construct_variant E! B %n2))
     (= %u (store &x.e %v)) (ret %n))
  (b () (known &x.e B) (= %v (load &x.e.B)) (ret %v)))
(fun main i32 () (locals (&x Holder!))
  (entry () (= %four (const u32 4)) (= %h (construct H! %four)) (= %e (construct_variant E! A %h)) (= %k (const u32 1))
    (= %hold (construct Holder! %e %k)) (= %u (store &x %hold)) (= %t (variant_tag &x.e E!))
    (= %first (call step &x)) (= %second (call step &x)) (= %t2 (variant_tag &x.e E!))
    (= %whole (load &x)) (= (%e2 %k2) (destruct %whole Holder!)) (switch %e2 E! (A fin_a %first %second %t %t2) (B fin_b %first %second %t %t2)))
  (fin_a ((%h H!) (%first u32) (%second u32) (%t u32) (%t2 u32)) (empty &x) (= %n (call eat %h)) (= %r (const i32 99)) (ret %r))
  (fin_b ((%n u32) (%first u32) (%second u32) (%t u32) (%t2 u32)) (empty &x)
    (= %s (add %first %second)) (= %s2 (add %s %t)) (= %s3 (add %s2 %t2)) (= %r (int_cast %s3 i32)) (ret %r)))"""),
    # Sibling fields lent to one call; a place destructured whole.
    "sibling_lends": (11, """
(type Two (struct (a Cur) (b Cur)))
(fun both u32 (Cur Cur) (entry ((&p Cur) (&q Cur))
  (= %three (const u32 3)) (= %x (call bump &p %three)) (= %four (const u32 4)) (= %y (call bump &q %four)) (= %s (add %x %y)) (ret %s)))
(fun main i32 () (locals (&t Two))
  (entry () (= %one (const u32 1)) (= %c1 (construct Cur %one)) (= %one2 (const u32 1)) (= %c2 (construct Cur %one2))
    (= %two (construct Two %c1 %c2)) (= %u (store &t %two)) (= %old (call both &t.a &t.b))
    (= (%a %b) (destruct &t Two)) (= (%pa) (destruct %a Cur)) (= (%pb) (destruct %b Cur))
    (= %s (add %pa %pb)) (= %s2 (add %s %old)) (= %r (int_cast %s2 i32)) (ret %r)))"""),
    # A package in a place, opened in place, read and lent at its view, closed.
    "open_place": (14, """
(type Pk (struct (pkg (exists ($h) (tuple u32 (brand $h))))))
(fun peek_t (forall ($b) () u32 ((tuple u32 (brand $b)))
  (entry ((&t (tuple u32 (brand $b)))) (= %n (load &t.0)) (ret %n))))
(fun main i32 () (locals (&k Pk))
  (entry () (= %f (fresh)) (= %w (open %f ($a))) (= %seven (const u32 7)) (= %pl (construct (tuple u32 (brand $a)) %seven %w))
    (= %p (pack (exists ($h) (tuple u32 (brand $h))) $a %pl)) (= %pk (construct Pk %p)) (= %u (store &k %pk))
    (= %u1 (open &k.pkg ($o))) (= %n (load &k.pkg.0)) (= %m (call (peek_t $o) &k.pkg))
    (= %u2 (pack (exists ($h) (tuple u32 (brand $h))) $o &k.pkg))
    (= %s (add %n %m)) (= %r (int_cast %s i32)) (ret %r)))"""),
    # A raw target lent as a place, through stack storage.
    "raw_target": (8, """
(fun main i32 ()
  (entry () (unsafe (= %p (alloca Cur))) (= %five (const u32 5)) (= %c (construct Cur %five)) (= %q (copy %p))
    (unsafe (= %u (ptr_store %p %c))) (= %three (const u32 3)) (= %q2 (copy %q))
    (unsafe (= %old (call bump &*%q %three))) (unsafe (= %after (ptr_load %q2)))
    (= (%pos) (destruct %after Cur)) (= %r (int_cast %pos i32)) (ret %r)))"""),
    # Projection through a raw pointer: a struct field and array elements, read back through the projections.
    "pointer_projection": (47, """
(type Pair (struct (a u32) (b (array u32 2))))
(fun main i32 ()
  (entry () (unsafe (= %p (alloca Pair)))
    (= %x (const u32 40)) (= %y (const u32 3)) (= %z (const u32 4))
    (= %arr (construct (array u32 2) %y %z)) (= %pair (construct Pair %x %arr))
    (= %q (copy %p)) (unsafe (= %u (ptr_store %p %pair)))
    (= (%pa %pb) (destruct %q (ptr Pair)))
    (= (%b0 %b1) (destruct %pb (ptr (array u32 2))))
    (unsafe (= %a (ptr_load %pa))) (unsafe (= %c (ptr_load %b1)))
    (unsafe (= %d (ptr_load %b0)))
    (= %s (add %a %c)) (= %t (add %s %d)) (= %r (int_cast %t i32)) (ret %r)))"""),
}


# Further rejected programs, checked after the corpus's TYPES.
REJECTED = {
    "edge_to_entry": """(fun f u32 (Box!) (entry ((&b Box!)) (jmp entry)))""",
    "pack_place_wrong_brand": """(fun f u32 (Pk) (entry ((&k Pk)) (= %u (open &k.pkg ($a)))
   (= %u2 (pack (exists ($h) (tuple u32 (brand $h))) $static &k.pkg)) (= %r (const u32 0)) (ret %r)))""",
    "store_wrong_type": """(fun f u32 (Cur) (entry ((&c Cur)) (= %x (const u64 1)) (= %u (store &c.pos %x)) (= %r (const u32 0)) (ret %r)))""",
    "variant_tag_of_empty": """(fun f u32 (Holder!) (entry ((&x Holder!)) (= %e (load &x.e)) (= %t (variant_tag &x.e E!))
   (= %u (store &x.e %e)) (ret %t)))""",
    "known_lost_after_store": """(fun f u32 (Holder!) (entry ((&x Holder!)) (switch &x.e E! (A a) (B b)))
  (a () (known &x.e A) (= %h (load &x.e.A)) (= %n (call eat %h)) (= %v (construct_variant E! B %n)) (= %u (store &x.e %v))
     (= %w (load &x.e.B)) (ret %w))
  (b () (known &x.e B) (= %v (load &x.e.B)) (ret %v)))""",
    "raw_lend_wrong_type": """(fun f u32 ((ptr u32)) (entry ((%p (ptr u32))) (unsafe (= %r (call touch &*%p))) (ret %r)))""",
}


def check(text):
    program = parse_program(text)
    return program, check_program(program)


class PlaceCorpusTests(unittest.TestCase):
    def test_loading_opened_place_retains_its_root_view(self):
        check("""(type Pk (struct (pkg (exists ($h) (tuple u32 (brand $h))))))
(fun f u32 (Pk) (entry ((&k Pk))
  (= %u1 (open &k.pkg ($o)))
  (= %payload (load &k.pkg))
  (= %u2 (store &k.pkg %payload))
  (= %u3 (pack (exists ($h) (tuple u32 (brand $h))) $o &k.pkg))
  (= %r (const u32 0)) (ret %r)))""")

    def test_variant_and_tuple_component_can_share_numeric_spelling(self):
        check("""(type E! (enum (0 u32) (1 u32)))
(fun f u32 (E! (tuple u32 u32))
  (entry ((&e E!) (&t (tuple u32 u32))) (switch &e E! (0 a) (1 b)))
  (a () (known &e 0) (= %n (load &e.0)) (= %m (load &t.0))
    (= %r (add %n %m)) (ret %r))
  (b () (known &e 1) (= %n (load &e.1)) (= %m (load &t.0))
    (= %r (add %n %m)) (ret %r)))""")

    def test_tuple_path_rejects_noncanonical_and_oversized_indices(self):
        for index in ("00", "9" * 5000):
            with self.subTest(index_length=len(index)):
                source = ("(fun f u32 ((tuple u32)) "
                          f"(entry ((&t (tuple u32))) (= %n (load &t.{index})) (ret %n)))")
                with self.assertRaisesRegex(KursError, "names_component"):
                    check(source)

    def test_verdicts(self):
        for name, (accepted, rule, program) in CASES.items():
            with self.subTest(case=name):
                try:
                    check(TYPES + program)
                    error = None
                except KursError as e:
                    error = e
                self.assertEqual(error is None, accepted, str(error))
                if error is not None:
                    self.assertIn(rule, str(error))

    def test_rejected(self):
        for name, program in REJECTED.items():
            with self.subTest(case=name), self.assertRaises(KursError):
                check(TYPES + program)


class PlaceProgramTests(unittest.TestCase):
    def test_pointer_projection_never_dereferences(self):
        program, checker = check(COMMON + PROGRAMS["pointer_projection"][1])
        c = emit_c(program, checker, "main")
        body = "\n".join(line for line in c.splitlines() if not line.startswith("#define KUR_FIELD"))
        self.assertIn("KUR_FIELD(", body)
        self.assertNotIn("->", body)

    def test_programs_run(self):
        with tempfile.TemporaryDirectory(prefix="kursal-places-") as directory:
            for name, (status, text) in PROGRAMS.items():
                with self.subTest(program=name):
                    program, checker = check(COMMON + text)
                    c_file, exe = Path(directory) / f"{name}.c", Path(directory) / name
                    c_file.write_text(emit_c(program, checker, "main"))
                    built = subprocess.run(cc_command([str(c_file)], str(exe)), capture_output=True, text=True)
                    self.assertEqual(built.returncode, 0, built.stderr)
                    self.assertEqual(subprocess.run([str(exe)]).returncode, status)


if __name__ == "__main__":
    unittest.main()
