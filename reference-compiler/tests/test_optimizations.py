"""What the C backend leaves out and shortens (kursal/plan.py, and tail calls and qualifiers in
kursal/cgen.py): the shape of the C, where each rule applies and where it must not, and the programs'
results, built with the C compiler every build uses and, for tail calls, without optimization."""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kursal.cc import c_compiler
from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.kurs import parse_program
from tests.test_lower import compile_and_run

CUR = "(type Cur (struct (pos u32)))\n"


def c_of(source: str) -> str:
    program = parse_program(source)
    return emit_c(program, check_program(program), "main")


def run(c: str, compiler: list[str] | None = None) -> int:
    """The exit status of `c`, built by `kursal.cc`'s command, or by `compiler` when given."""
    with tempfile.TemporaryDirectory(prefix="kursal-optimizations-") as base:
        path, binary = Path(base) / "program.c", Path(base) / "program"
        path.write_text(c)
        if compiler is None:
            status = compile_and_run(path, binary)
            assert isinstance(status, int), status
            return status
        built = subprocess.run([*compiler, str(path), "-o", str(binary)], capture_output=True, text=True)
        assert built.returncode == 0, built.stderr
        return subprocess.run([str(binary)], timeout=60).returncode


def head(c: str, name: str) -> str:
    """The parameters in the prototype of the function named `name`."""
    found = re.search(rf"^static [^\n]*\d{re.escape(name)}17h[0-9a-f]+E\(([^\n]*)\);$", c, re.M)
    assert found is not None, name
    return found.group(1)


def body(c: str, name: str) -> str:
    found = re.search(rf"^static [^\n]*\d{re.escape(name)}17h[0-9a-f]+E\([^\n]*\) {{\n(.*?)\n}}\n", c, re.M | re.S)
    assert found is not None, name
    return found.group(1)


class LeftOutTests(unittest.TestCase):
    def test_dead_values_copies_and_return_joins(self):
        c = c_of("""
(fun pick i32 (bool i32 i32)
  (entry ((%c bool) (%a i32) (%b i32)) (= %dead (const i32 5)) (= %a2 (copy %a)) (jnz %c (yes %a2) (no %b)))
  (yes ((%x i32)) (jmp out %x))
  (no ((%y i32)) (= %one (const i32 1)) (= %z (add %y %one)) (jmp out %z))
  (out ((%r i32)) (ret %r)))
(fun main i32 () (entry () (= %t (const bool true)) (= %a (const i32 40)) (= %b (const i32 7))
  (= %r (call pick %t %a %b)) (ret %r)))""")
        pick = body(c, "pick")
        self.assertNotIn("dead", pick)
        self.assertNotIn("a2", pick)
        self.assertRegex(pick, r"return entry_a(_\d+)?;")
        self.assertNotIn("L_out", pick)
        self.assertNotIn("L_yes", pick)
        self.assertEqual(run(c), 40)

    def test_unread_results_are_not_assigned(self):
        c = c_of(CUR + """
(fun bump u32 (Cur) (entry ((&c Cur)) (= %one (const u32 1)) (= %again (copy %one)) (= %u (store &c.pos %one)) (ret %again)))
(fun main i32 () (locals (&x Cur))
  (entry () (= %z (const u32 0)) (= %c (construct Cur %z)) (= %u (store &x %c)) (= %ignored (call bump &x))
    (= %p (load &x.pos)) (= %r (int_cast %p i32)) (ret %r)))""")
        main = body(c, "main")
        self.assertNotIn(".dummy", main)
        self.assertNotIn("ignored", main)
        self.assertRegex(main, r"\n  _ZN\w+bump\w+\(&p_x\);")
        self.assertEqual(run(c), 1)

    def test_a_place_stored_and_loaded_once_has_no_object(self):
        c = c_of("""
(fun main i32 () (locals (&t i32) (&k i32))
  (entry () (= %a (const i32 3)) (= %u (store &t %a)) (= %b (const i32 4)) (= %v (load &t)) (= %s (add %v %b))
    (= %w (store &k %s)) (= %x (load &k)) (= %y (load &k)) (= %r (add %x %y)) (ret %r)))""")
        main = body(c, "main")
        self.assertNotIn("p_t", main)
        self.assertIn("int32_t p_k;", main)
        self.assertEqual(run(c), 14)

    def test_edges_assign_directly_unless_they_overwrite_what_they_read(self):
        loop = """
(fun main i32 () (entry () (= %a (const i32 1)) (= %b (const i32 2)) (= %n (const i32 3)) (jmp loop %a %b %n))
  (loop ((%x i32) (%y i32) (%n i32)) (= %z (const i32 0)) (= %n2 (copy %n)) (= %more (ne %n2 %z)) (jnz %more (body %x %y %n) (done %x %y)))
  (body ((%x i32) (%y i32) (%n i32)) (= %one (const i32 1)) (= %m (sub %n %one)) (= %y2 (copy %y)) (= %s (add %x %y2)) (jmp loop %y %s %m))
  (done ((%x i32) (%y i32)) (= %t (const i32 10)) (= %p (mul %x %t)) (= %r (add %p %y)) (ret %r)))"""
        c = c_of(loop)
        self.assertNotIn("edge_", c)
        self.assertEqual(run(c), 58)
        swap = """
(fun main i32 () (entry () (= %a (const i32 1)) (= %b (const i32 2)) (= %n (const i32 3)) (jmp loop %a %b %n))
  (loop ((%x i32) (%y i32) (%n i32)) (= %z (const i32 0)) (= %n2 (copy %n)) (= %more (ne %n2 %z)) (= %one (const i32 1)) (= %m (sub %n %one))
    (jnz %more (loop %y %x %m) (done %x %y)))
  (done ((%x i32) (%y i32)) (= %t (const i32 10)) (= %p (mul %x %t)) (= %r (add %p %y)) (ret %r)))"""
        c = c_of(swap)
        self.assertIn("edge_", c)
        self.assertEqual(run(c), 21)


ADAPTED = CUR + """
(type Rec (struct (cur Cur) (n u32)))
(fun bump u32 (Cur) (entry ((&c Cur)) (= %p (load &c.pos)) (= %one (const u32 1)) (= %q (add %p %one))
  (= %q2 (copy %q)) (= %u (store &c.pos %q2)) (ret %q)))
(fun bump#lend (tuple Cur u32) (Cur) (locals (&slot0 Cur))
  (entry ((%a0 Cur)) (= %unit0 (store &slot0 %a0)) (= %result (call bump &slot0)) (= %restored0 (load &slot0))
    (= %out (construct (tuple Cur u32) %restored0 %result)) (ret %out)))
(fun step u32 (Rec) (entry ((&r Rec)) (= %v (load &r)) (= (%c %n) (destruct %v Rec)) (= %o (call bump#lend %c))
  (= (%c2 %k) (destruct %o (tuple Cur u32))) (= %w (construct Rec %c2 %n)) (= %u (store &r %w)) (ret %k)))
(fun peek u32 (Rec) (entry ((&r Rec)) (= %v (load &r)) (= (%c %n) (destruct %v Rec)) (= %o (call bump#lend %c))
  (= (%c2 %k) (destruct %o (tuple Cur u32))) (= %old (load &r.cur.pos)) (= %w (construct Rec %c2 %n)) (= %u (store &r %w))
  (= %s (add %k %old)) (ret %s)))
(fun twice u32 (Cur) (entry ((%c Cur)) (= %o (call bump#lend %c)) (= (%c2 %k) (destruct %o (tuple Cur u32)))
  (= %o2 (call bump#lend %c2)) (= (%c3 %j) (destruct %o2 (tuple Cur u32))) (= %p (destruct %c3 Cur)) (= %s (add %j %p)) (ret %s)))
(fun main i32 () (locals (&x Rec))
  (entry () (= %z (const u32 10)) (= %c (construct Cur %z)) (= %c1 (copy %c)) (= %n (const u32 1)) (= %r (construct Rec %c1 %n))
    (= %u (store &x %r)) (= %a (call step &x)) (= %b (call step &x)) (= %e (call peek &x)) (= %p (load &x.cur.pos))
    (= %f (call twice %c)) (= %s (add %a %b)) (= %t (add %s %p)) (= %t2 (add %t %e)) (= %t3 (add %t2 %f))
    (= %i (int_cast %t3 i32)) (ret %i)))"""


class ResidenceTests(unittest.TestCase):
    def test_values_are_read_where_they_were_loaded(self):
        c = c_of(CUR + """
(type Two (struct (a Cur) (b u32)))
(fun sum u32 (Two) (entry ((&t Two)) (= %v (load &t)) (= (%a %b) (destruct %v Two)) (= %a2 (copy %a)) (= %p (destruct %a2 Cur))
  (= %s (add %p %b)) (= %s2 (copy %s)) (= %w (construct Two %a %s2)) (= %u (store &t %w)) (ret %s)))
(fun main i32 () (locals (&x Two))
  (entry () (= %one (const u32 1)) (= %c (construct Cur %one)) (= %two (const u32 2)) (= %t (construct Two %c %two))
    (= %u (store &x %t)) (= %r (call sum &x)) (= %q (load &x.b)) (= %s (add %r %q)) (= %i (int_cast %s i32)) (ret %i)))""")
        sum_ = body(c, "sum")
        # Nothing is copied out of the place; the store writes the one member that changed.
        self.assertNotRegex(sum_, r"K_Two \w+;")
        self.assertRegex(sum_, r"\(\*entry_t\w*\)\.f_b = ")
        self.assertNotRegex(sum_, r"\(\*entry_t\w*\)\.f_a = ")
        self.assertEqual(run(c), 6)

    def test_a_lend_adapter_lends_the_place_its_value_lies_in(self):
        c = c_of(ADAPTED)
        self.assertRegex(body(c, "step"), r"bump\w*\(&\(\*entry_r\w*\)\.f_cur\);")
        self.assertNotRegex(body(c, "step"), r"\(\*entry_r\w*\)(\.f_cur)? = ")
        # A value in a C variable is lent there, twice in a row.
        self.assertRegex(body(c, "twice"), r"bump\w*\(&entry_c\w*\);(.|\n)*bump\w*\(&entry_c\w*\);")
        # `peek` reads the place between the lend and the store, so it lends a copy, through the adapter.
        self.assertRegex(body(c, "peek"), r"bump_23lend\w*\(")
        self.assertEqual(run(c), 11 + 12 + 13 + (13 + 12) + (12 + 12))


SUM = """
(fun sum u64 (u64 u64) (entry ((%n u64) (%acc u64)) (= %z (const u64 0)) (= %n2 (copy %n)) (= %done (eq %n2 %z)) (jnz %done (fin %acc) (more %n %acc)))
  (fin ((%a u64)) (ret %a))
  (more ((%n u64) (%acc u64)) (= %one (const u64 1)) (= %n2 (copy %n)) (= %m (sub %n2 %one)) (= %s (add %acc %n)) (= %r (call sum %m %s)) (ret %r)))
(fun main i32 () (entry () (= %n (const u64 10000000)) (= %z (const u64 0)) (= %s (call sum %n %z)) (= %k (const u64 256))
  (= %m (mod %s %k)) (= %r (int_cast %m i32)) (ret %r)))"""

PARITY = """
(fun even bool (u64) (entry ((%n u64)) (= %z (const u64 0)) (= %n2 (copy %n)) (= %done (eq %n2 %z)) (jnz %done (yes) (no %n)))
  (yes () (= %t (const bool true)) (ret %t))
  (no ((%n u64)) (= %one (const u64 1)) (= %m (sub %n %one)) (= %r (call odd %m)) (ret %r)))
(fun odd bool (u64) (entry ((%n u64)) (= %z (const u64 0)) (= %n2 (copy %n)) (= %done (eq %n2 %z)) (jnz %done (yes) (no %n)))
  (yes () (= %f (const bool false)) (ret %f))
  (no ((%n u64)) (= %one (const u64 1)) (= %m (sub %n %one)) (= %r (call even %m)) (ret %r)))
(fun main i32 () (entry () (= %n (const u64 10000001)) (= %e (call even %n)) (jnz %e (t) (f)))
  (t () (= %one (const i32 1)) (ret %one))
  (f () (= %two (const i32 2)) (ret %two)))"""

UNOPTIMIZED = ["-O0", "-w", "-fno-builtin"]


class TailCallTests(unittest.TestCase):
    def test_a_call_of_itself_loops(self):
        c = c_of(SUM)
        self.assertIn("goto L_entry;", body(c, "sum"))
        self.assertNotRegex(body(c, "sum"), r"sum\w*\(")
        self.assertEqual(run(c), 64)
        # Ten million frames would overflow the stack: the loop needs no optimizer.
        self.assertEqual(run(c, [c_compiler(), *UNOPTIMIZED]), 64)

    @unittest.skipUnless(shutil.which("clang"), "clang is not installed")
    def test_mutual_tail_calls_must_be_tail_calls(self):
        c = c_of(PARITY)
        self.assertRegex(body(c, "even"), r"KUR_MUSTTAIL return _ZN\w+odd\w+\(\w+\);")
        self.assertRegex(body(c, "odd"), r"KUR_MUSTTAIL return _ZN\w+even\w+\(\w+\);")
        self.assertEqual(run(c), 2)
        self.assertEqual(run(c, ["clang", *UNOPTIMIZED]), 2)

    @unittest.skipUnless(shutil.which("gcc"), "gcc is not installed")
    def test_other_c_compilers_ignore_musttail(self):
        self.assertEqual(run(c_of(PARITY), ["gcc", "-O2", "-w", "-fno-builtin"]), 2)

    def test_no_musttail_where_signatures_differ(self):
        c = c_of("""
(fun f u64 (u64) (entry ((%n u64)) (= %k (const u32 3)) (= %r (call g %n %k)) (ret %r)))
(fun g u64 (u64 u32) (entry ((%n u64) (%k u32)) (ret %n)))
(fun main i32 () (entry () (= %n (const u64 5)) (= %r (call f %n)) (= %i (int_cast %r i32)) (ret %i)))""")
        self.assertNotIn("KUR_MUSTTAIL return", c)
        self.assertEqual(run(c), 5)

    def test_no_musttail_for_aggregates(self):
        c = c_of("""
(type P (struct (a u64) (b u64)))
(fun f u64 (P) (entry ((%p P)) (= %r (call g %p)) (ret %r)))
(fun g u64 (P) (entry ((%p P)) (= (%a %b) (destruct %p P)) (= %r (add %a %b)) (ret %r)))
(fun main i32 () (entry () (= %a (const u64 2)) (= %b (const u64 3)) (= %p (construct P %a %b)) (= %r (call f %p))
  (= %i (int_cast %r i32)) (ret %i)))""")
        self.assertNotIn("KUR_MUSTTAIL return", c)
        self.assertEqual(run(c), 5)

    def test_no_tail_call_from_a_frame_that_lends_its_own_storage(self):
        # `f`'s alloca would be freed by a tail call; `g` lends its local place to itself.
        c = c_of(CUR + """
(fun f u64 (u64) (entry ((%n u64)) (unsafe (= %p (alloca u64))) (= %r (call h %n)) (ret %r)))
(fun h u64 (u64) (entry ((%n u64)) (ret %n)))
(fun g u32 (Cur u32) (locals (&mine Cur))
  (entry ((&c Cur) (%n u32)) (= %z (const u32 0)) (= %n2 (copy %n)) (= %d (eq %n2 %z)) (jnz %d (fin) (more %n)))
  (fin () (empty &mine) (= %p (load &c.pos)) (ret %p))
  (more ((%n u32)) (empty &mine) (= %one (const u32 1)) (= %m (sub %n %one)) (= %q (load &c.pos)) (= %fresh (construct Cur %q))
    (= %u (store &mine %fresh)) (= %r (call g &mine %m)) (= %back (load &mine)) (= %v (store &c %back)) (ret %r)))
(fun main i32 () (locals (&x Cur))
  (entry () (= %seven (const u32 7)) (= %c (construct Cur %seven)) (= %u (store &x %c)) (= %n (const u32 3))
    (= %r (call g &x %n)) (= %k (const u64 4)) (= %s (call f %k)) (= %s32 (int_cast %s u32)) (= %t (add %r %s32))
    (= %i (int_cast %t i32)) (ret %i)))""")
        self.assertNotIn("KUR_MUSTTAIL", body(c, "f"))
        self.assertNotIn("goto L_entry", body(c, "g"))
        self.assertEqual(run(c), 11)


QUALIFIED = CUR + """
(fun peek u32 (Cur) (entry ((&c Cur)) (= %p (load &c.pos)) (ret %p)))
(fun poke u32 (Cur) (entry ((&c Cur)) (= %one (const u32 1)) (= %u (store &c.pos %one)) (= %r (call peek &c))
  (= %two (const u32 2)) (= %s (add %r %two)) (ret %s)))
(fun inner u32 (Cur) (entry ((&c Cur)) (= %p (load &c.pos)) (ret %p)))
(fun outer u32 (Cur) (entry ((&c Cur)) (= %r (call inner &c)) (= %two (const u32 2)) (= %s (add %r %two)) (ret %s)))
(fun pointed u32 (Cur) (entry ((&c Cur)) (= %p (load &c.pos)) (ret %p)))
(fun main i32 () (locals (&x Cur))
  (entry () (= %z (const u32 0)) (= %c (construct Cur %z)) (= %u (store &x %c)) (= %a (call poke &x))
    (unsafe (= %raw (alloca Cur))) (= %raw2 (copy %raw)) (= %five (const u32 5)) (= %d (construct Cur %five)) (unsafe (= %w (ptr_store %raw2 %d)))
    (unsafe (= %b (call outer &*%raw))) (= %f (fn pointed)) (= %e (call %f &x))
    (= %ab (add %a %b)) (= %s (add %ab %e)) (= %r (int_cast %s i32)) (ret %r)))"""


class QualifierTests(unittest.TestCase):
    def test_place_parameters_lent_from_locals(self):
        c = c_of(QUALIFIED)
        self.assertRegex(head(c, "peek"), r"^K_Cur const\* restrict entry_c\w*$")
        self.assertRegex(head(c, "poke"), r"^K_Cur\* restrict entry_c\w*$")
        self.assertEqual(run(c), 3 + 7 + 1)

    def test_no_restrict_behind_a_raw_target_or_a_function_value(self):
        c = c_of(QUALIFIED)
        # `outer` is lent memory behind a pointer, and lends it on to `inner`.
        self.assertRegex(head(c, "outer"), r"^K_Cur const\* entry_c\w*$")
        self.assertRegex(head(c, "inner"), r"^K_Cur const\* entry_c\w*$")
        # A call through a function value may lend anything, and has no qualifiers in its type.
        self.assertRegex(head(c, "pointed"), r"^K_Cur\* entry_c\w*$")

    def test_no_restrict_where_a_tail_call_moves_a_place(self):
        c = c_of(CUR + """
(fun swapdown u32 (Cur Cur u32) (entry ((&a Cur) (&b Cur) (%n u32)) (= %z (const u32 0)) (= %n2 (copy %n)) (= %d (eq %n2 %z)) (jnz %d (fin) (more %n)))
  (fin () (= %p (load &a.pos)) (ret %p))
  (more ((%n u32)) (= %one (const u32 1)) (= %m (sub %n %one)) (= %r (call swapdown &b &a %m)) (ret %r)))
(fun main i32 () (locals (&x Cur) (&y Cur))
  (entry () (= %one (const u32 1)) (= %c (construct Cur %one)) (= %u (store &x %c)) (= %two (const u32 2))
    (= %d (construct Cur %two)) (= %v (store &y %d)) (= %n (const u32 3)) (= %r (call swapdown &x &y %n))
    (= %i (int_cast %r i32)) (ret %i)))""")
        self.assertIn("goto L_entry;", body(c, "swapdown"))
        self.assertNotIn("restrict", head(c, "swapdown"))
        self.assertEqual(run(c), 2)


if __name__ == "__main__":
    unittest.main()
