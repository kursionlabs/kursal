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


class SharingTests(unittest.TestCase):
    def test_a_value_one_edge_reads_is_made_in_the_parameter(self):
        c = c_of("""
(type Res (enum (Ok (tuple u32 u32)) (Err u32)))
(fun make Res (bool) (entry ((%c bool)) (jnz %c (yes) (no)))
  (yes () (= %a (const u32 3)) (= %b (const u32 4)) (= %t (construct (tuple u32 u32) %a %b))
    (= %r (construct_variant Res Ok %t)) (jmp out %r))
  (no () (= %e (const u32 9)) (= %r (construct_variant Res Err %e)) (jmp out %r))
  (out ((%v Res)) (switch %v Res (Ok ok) (Err err)))
  (ok ((%p (tuple u32 u32))) (= (%x %y) (destruct %p (tuple u32 u32))) (= %y2 (copy %y)) (= %s (add %x %y2))
    (= %f (construct (tuple u32 u32) %s %y)) (= %g (construct_variant Res Ok %f)) (ret %g))
  (err ((%q u32)) (= %g (construct_variant Res Err %q)) (ret %g)))
(fun main i32 () (entry () (= %t (const bool true)) (= %r (call make %t)) (switch %r Res (Ok a) (Err b)))
  (a ((%p (tuple u32 u32))) (= (%x %y) (destruct %p (tuple u32 u32))) (= %i (int_cast %x i32)) (ret %i))
  (b ((%q u32)) (= %i (int_cast %q i32)) (ret %i)))""")
        make = body(c, "make")
        # Both arms build the enum in the join's variable; the switch's arms read the payload where it lies.
        self.assertRegex(make, r"out_v\w*\.payload\.v_Ok = ")
        self.assertRegex(make, r"out_v\w*\.payload\.v_Err = ")
        self.assertNotRegex(make, r"(ok_p|err_q)\w* = ")
        self.assertRegex(make, r"out_v\w*\.payload\.v_Ok\.e0\)")
        self.assertEqual(run(c), 7)

    def test_a_local_holding_what_was_moved_out_of_a_place_shares_it(self):
        moved = """
(type Lin! (struct (pos u32)))
(type Two! (struct (a Lin!) (b u32)))
(fun bump u32 (Lin!) (entry ((&c Lin!)) (= %p (load &c.pos)) (= %one (const u32 1)) (= %q (add %p %one))
  (= %q2 (copy %q)) (= %u (store &c.pos %q2)) (ret %q)))
(fun twice u32 (Two!) (locals (&a Lin!) (&b u32))
  (entry ((&t Two!)) (= %v (load &t)) (= (%x %y) (destruct %v Two!)) (= %u1 (store &a %x)) (= %u2 (store &b %y))
    (= %n (call bump &a)) (jmp next %n))
  (next ((%m u32)) (empty &t) (= %x2 (load &a)) (= %y2 (load &b)) (= %w (construct Two! %x2 %y2)) (= %u (store &t %w))
    (ret %m)))
(fun main i32 () (locals (&z Two!))
  (entry () (= %five (const u32 5)) (= %c (construct Lin! %five)) (= %seven (const u32 7)) (= %t (construct Two! %c %seven))
    (= %u (store &z %t)) (= %r (call twice &z)) (= %q (load &z.a.pos)) (= %s (add %r %q))
    (= %v (load &z)) (= (%x %y) (destruct %v Two!)) (= (%p) (destruct %x Lin!))
    (= %i (int_cast %s i32)) (ret %i)))"""
        c = c_of(moved)
        twice = body(c, "twice")
        # `a` and `b` live in `t`'s members, which `t` gave up: nothing is copied out or back.
        self.assertNotIn("p_a", twice)
        self.assertRegex(twice, r"bump\w*\(&\(\*entry_t\w*\)\.f_a\)")
        self.assertNotRegex(twice, r"\(\*entry_t\w*\)[.\w]* = ")
        self.assertEqual(run(c), 12)
        # A local whose value the C reads keeps a variable of its own.
        c = c_of(moved.replace("(next ((%m u32)) (empty &t) (= %x2 (load &a))",
                               "(next ((%m u32)) (empty &t) (= %k (load &a.pos)) (= %x2 (load &a))"))
        self.assertIn("p_a", body(c, "twice"))
        self.assertEqual(run(c), 12)

    def test_a_construction_of_parts_where_they_lie_is_not_written(self):
        c = c_of(CUR + """
(type Two (struct (a Cur) (b u32)))
(fun peek u32 (Two) (entry ((&t Two)) (= %v (load &t)) (= (%a %b) (destruct %v Two)) (= (%p) (destruct %a Cur))
  (= %a2 (construct Cur %p)) (= %b2 (copy %b)) (= %w (construct Two %a2 %b)) (= %u (store &t %w)) (ret %b2)))
(fun main i32 () (locals (&x Two))
  (entry () (= %one (const u32 1)) (= %c (construct Cur %one)) (= %two (const u32 2)) (= %t (construct Two %c %two))
    (= %u (store &x %t)) (= %r (call peek &x)) (= %q (load &x.a.pos)) (= %s (add %r %q)) (= %i (int_cast %s i32)) (ret %i)))""")
        self.assertNotRegex(body(c, "peek"), r"\(\*entry_t\w*\)[.\w]* = ")
        self.assertRegex(head(c, "peek"), r"const\*")
        self.assertEqual(run(c), 3)


class ByPointerTests(unittest.TestCase):
    def test_aggregates_are_passed_by_pointer(self):
        c = c_of("""
(type Big (struct (a u64) (b u64) (c u64) (d u64) (e u64)))
(fun sum u64 (Big) (entry ((%p Big)) (= (%a %b %c %d %e) (destruct %p Big)) (= %x (add %a %b)) (= %y (add %c %d))
  (= %w (add %x %y)) (= %r (add %w %e)) (ret %r)))
(fun count u64 (Big u64) (entry ((%p Big) (%n u64)) (= %z (const u64 0)) (= %n2 (copy %n)) (= %done (eq %n2 %z))
  (jnz %done (fin %p) (more %p %n)))
  (fin ((%q Big)) (= %r (call sum %q)) (ret %r))
  (more ((%q Big) (%n u64)) (= %one (const u64 1)) (= %m (sub %n %one)) (= %r (call count %q %m)) (ret %r)))
(fun peek u64 (Big) (entry ((&b Big)) (= %v (load &b)) (= %r (call sum %v)) (ret %r)))
(fun main i32 () (locals (&x Big))
  (entry () (= %one (const u64 1)) (= %two (const u64 2)) (= %three (const u64 3)) (= %four (const u64 4))
    (= %five (const u64 5)) (= %big (construct Big %one %two %three %four %five)) (= %b1 (copy %big)) (= %b2 (copy %big)) (= %b3 (copy %big))
    (= %s (call sum %big)) (= %u (store &x %b1)) (= %t (call peek &x)) (= %f (fn sum)) (= %g (call %f %b2))
    (= %k (const u64 3)) (= %h (call count %b3 %k))
    (= %st (add %s %t)) (= %gh (add %g %h)) (= %all (add %st %gh)) (= %i (int_cast %all i32)) (ret %i)))""")
        self.assertRegex(head(c, "sum"), r"^K_Big const\* entry_p\w*$")
        # `count` calls itself in tail position, assigning its parameter: it reads a copy of it.
        self.assertRegex(body(c, "count"), r"entry_p\w* = \*entry_p\w*_ref;")
        self.assertIn("goto L_entry;", body(c, "count"))
        # A value lying under a place parameter is copied for the callee.
        self.assertRegex(body(c, "peek"), r"arg_\d+ = \(\*entry_b\w*\);")
        # A function value calls the by-value entry.
        self.assertRegex(c, r"static uint64_t \w*sum\w*_value\(K_Big entry_p\w*\) \{\n  return \w*sum\w*\(&entry_p\w*\);")
        self.assertEqual(run(c), 60)

    def test_a_payload_lent_in_place_is_lent_from_a_copy_of_a_parameter_taken_by_pointer(self):
        # The arm reads its payload where the scrutinee lies: in the parameter taken by pointer, or in a
        # component of it. A lend of the payload in place writes there, so the function copies the
        # parameter into its own variable first, and the caller's value, read again by `g`, is unchanged.
        def program(param: str, scrutinee: str, made: str) -> str:
            return f"""
(type T5 (tuple u64 u64 u64 u64 u64))
(type Big (enum (A T5) (B u32)))
(fun bump u32 (T5) (entry ((&t T5)) (= %n (const u64 99)) (= %u (store &t.0 %n)) (= %r (const u32 0)) (ret %r)))
(fun adapt (tuple T5 u32) (T5) (locals (&s T5))
  (entry ((%v T5)) (= %u (store &s %v)) (= %r (call bump &s)) (= %w (load &s)) (= %o (construct (tuple T5 u32) %w %r)) (ret %o)))
(fun f u64 ({param})
  (entry ((%q {param})) {scrutinee} (switch %p Big (A a) (B b)))
  (a ((%pl T5)) (= %x (call adapt %pl)) (= (%w %r) (destruct %x (tuple T5 u32)))
     (= (%a0 %a1 %a2 %a3 %a4) (destruct %w T5)) (ret %a0))
  (b ((%k u32)) (= %z (const u64 0)) (ret %z)))
(fun g u64 ({param})
  (entry ((%q {param})) {scrutinee} (switch %p Big (A a) (B b)))
  (a ((%pl T5)) (= (%a0 %a1 %a2 %a3 %a4) (destruct %pl T5)) (ret %a0))
  (b ((%k u32)) (= %z (const u64 0)) (ret %z)))
(fun main i32 ()
  (entry ()
    (= %1 (const u64 1)) (= %2 (const u64 2)) (= %3 (const u64 3)) (= %4 (const u64 4)) (= %5 (const u64 5))
    (= %t (construct T5 %1 %2 %3 %4 %5)) (= %bg (construct_variant Big A %t)) {made} (= %q2 (copy %q))
    (= %x (call f %q)) (= %y (call g %q2)) (= %s (add %x %y)) (= %r (int_cast %s i32)) (ret %r)))"""
        whole = program("Big", "(= %p (copy %q))", "(= %q (copy %bg))")
        component = program("(tuple u64 Big)", "(= (%n %p) (destruct %q (tuple u64 Big)))",
                            "(= %z (const u64 0)) (= %q (construct (tuple u64 Big) %z %bg))")
        for source in (whole, component):
            c = c_of(source)
            self.assertRegex(body(c, "f"), r"entry_q\w* = \*entry_q\w*_ref;")
            self.assertEqual(run(c), 100)
            self.assertEqual(run(c, [c_compiler(), *UNOPTIMIZED]), 100)


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


BIG = "(type Big (struct (a u64) (b u64) (c u64) (d u64) (e u64)))"


def big(name: str, value: int) -> str:
    """Instructions making `%name`, a `Big` with `value` in every field."""
    copies = " ".join(f"(= %{name}_{k} (copy %{name}_v))" for k in range(5))
    fields = " ".join(f"%{name}_{k}" for k in range(5))
    return f"(= %{name}_v (const u64 {value})) {copies} (= %{name} (construct Big {fields}))"


class OutResultTests(unittest.TestCase):
    def test_big_elements_of_a_tuple_result_are_written_through_out_pointers(self):
        c = c_of("""
(type Big (struct (a u64) (b u64) (c u64) (d u64) (e u64)))
(fun sum u64 (Big) (entry ((%p Big)) (= (%a %b %c %d %e) (destruct %p Big)) (= %x (add %a %b)) (= %y (add %c %d))
  (= %w (add %x %y)) (= %r (add %w %e)) (ret %r)))
(fun make (tuple Big u64) (u64) (entry ((%n u64)) (= %one (const u64 1)) (= %n0 (copy %n)) (= %m (add %n0 %one))
  (= %n1 (copy %n)) (= %n2 (copy %n)) (= %n3 (copy %n)) (= %n4 (copy %n)) (= %big (construct Big %n1 %n2 %n3 %n4 %m))
  (= %r (construct (tuple Big u64) %big %n)) (ret %r)))
(fun pass (tuple Big u64) (u64) (entry ((%n u64)) (= %r (call make %n)) (ret %r)))
(fun twice u64 (u64) (entry ((%n u64)) (= %f (fn make)) (= %t (call %f %n)) (= (%b %k) (destruct %t (tuple Big u64)))
  (= %s (call sum %b)) (= %r (add %s %k)) (ret %r)))
(fun main i32 () (locals (&x Big))
  (entry () (= %z1 (const u64 2)) (= %z2 (const u64 2)) (= %z3 (const u64 2)) (= %z4 (const u64 2)) (= %z5 (const u64 2))
    (= %b0 (construct Big %z1 %z2 %z3 %z4 %z5)) (= %u (store &x %b0)) (= %old (load &x))
    (= %s0 (call sum %old)) (= %s1 (copy %s0)) (= %s2 (copy %s0)) (= %c (eq %s1 %s2)) (jnz %c (left) (right)))
  (left () (empty &x) (= %k (const u64 3)) (= %r (call pass %k)) (jmp join %r))
  (right () (empty &x) (= %k (const u64 4)) (= %k1 (copy %k)) (= %k2 (copy %k)) (= %k3 (copy %k)) (= %k4 (copy %k))
    (= %k5 (copy %k)) (= %b (construct Big %k1 %k2 %k3 %k4 %k5)) (= %t (construct (tuple Big u64) %b %k)) (jmp join %t))
  (join ((%p (tuple Big u64))) (empty &x) (= (%big %n) (destruct %p (tuple Big u64))) (= %u2 (store &x %big))
    (= %v (load &x)) (= %s (call sum %v)) (= %seven (const u64 7)) (= %w (call twice %seven))
    (= %a1 (add %s %n)) (= %a2 (add %a1 %w)) (= %i (int_cast %a2 i32)) (ret %i)))""")
        # The callee writes the big element through its out pointer, at the construction it returns.
        self.assertRegex(head(c, "make"), r"^uint64_t entry_n\w*, K_Big\* out_0\w*$")
        self.assertRegex(body(c, "make"), r"\*out_0\w* = entry_big\w*;\n  return \(K_tuple_\d+\)\{ \.e0 = entry_r\w*\.e1 \};")
        # A result returned as it is gets the caller's own out pointer.
        self.assertRegex(body(c, "pass"), r"= _ZN4kurs4make\w*\(entry_n\w*, out_0\w*\);")
        self.assertNotIn("*out_0", body(c, "pass"))
        # Each arm writes the joined element straight into the emptied place, which the join stores no more.
        self.assertRegex(body(c, "main"), r"= _ZN4kurs4pass\w*\(left_k\w*, &p_x\w*\);")
        self.assertRegex(body(c, "main"), r"\n  p_x\w* = right_b\w*;\n  goto L_join;")
        self.assertNotRegex(body(c, "main"), r"p_x\w* = join_")
        # A function value calls the by-value entry, which returns the whole tuple.
        self.assertRegex(c, r"static K_tuple \w*make\w*_value\(uint64_t entry_n\w*\) \{\n  K_Big out_0\w*;\n")
        self.assertEqual(run(c), 62)

    def test_an_out_pointer_never_points_where_an_argument_lies(self):
        c = c_of(BIG + f"""
(fun f (tuple Big Big) (Big) (entry ((%b Big)) {big("n", 50)} (= %t (construct (tuple Big Big) %n %b)) (ret %t)))
(fun main i32 () (locals (&x Big))
  (entry () {big("b0", 1)} (= %u (store &x %b0)) (= %c (const bool true)) (jnz %c (left) (right)))
  (left () (= %v (load &x)) (= %r (call f %v)) (jmp join %r))
  (right () (= %v (load &x)) {big("m", 7)} (= %t (construct (tuple Big Big) %m %v)) (jmp join %t))
  (join ((%p (tuple Big Big))) (empty &x) (= (%a %b) (destruct %p (tuple Big Big))) (= %u2 (store &x %a))
    (= (%ba %bb %bc %bd %be) (destruct %b Big)) (= %w (load &x)) (= (%wa %wb %wc %wd %we) (destruct %w Big))
    (= %s (add %wa %ba)) (= %i (int_cast %s i32)) (ret %i)))""")
        # `f` writes its first element into `x` while it reads its argument, which lay in `x`: it reads a copy.
        self.assertRegex(body(c, "main"), r"(arg_\w+) = p_x\w*;\n  _ZN4kurs1f\w*\(&\1, &p_x\w*, &join_p\w*\.e1\);")
        self.assertEqual(run(c), 51)

    def test_no_value_is_made_in_the_variable_a_payload_is_read_in(self):
        # The loop's state lies in its parameter's variable, and the arm reads its payload there: what the
        # arm makes for the next round, by a construction or through out pointers, is made elsewhere.
        def program(arm: str) -> str:
            return BIG + f"""
(type St (enum (A (tuple Big Big)) (C (tuple Big Big))))
(fun step (tuple St Big) ((tuple Big Big)) (entry ((%p (tuple Big Big))) (= (%x %y) (destruct %p (tuple Big Big)))
  {big("n1", 50)} {big("n2", 60)} (= %np (construct (tuple Big Big) %n1 %n2)) (= %s2 (construct_variant St C %np))
  (= %t2 (construct (tuple St Big) %s2 %y)) (ret %t2)))
(fun main i32 () (locals (&acc u64))
  (entry () (= %z (const u64 0)) (= %u0 (store &acc %z)) {big("b1", 1)} {big("b2", 6)}
    (= %pay (construct (tuple Big Big) %b1 %b2)) (= %s (construct_variant St A %pay)) {big("g0", 100)}
    (= %t (construct (tuple St Big) %s %g0)) (jmp loop %t))
  (loop ((%q (tuple St Big))) (= (%s %g) (destruct %q (tuple St Big))) (= (%ga %gb %gc %gd %ge) (destruct %g Big))
    (= %old (load &acc)) (= %new (add %old %ga)) (= %u (store &acc %new)) (switch %s St (A arm) (C fin)))
  (arm ((%pay (tuple Big Big))) {arm})
  (fin ((%pay (tuple Big Big))) (= %v (load &acc)) (= %i (int_cast %v i32)) (ret %i)))"""
        made = program(f"""(= (%x %y) (destruct %pay (tuple Big Big))) {big("n1", 50)} {big("n2", 60)}
    (= %np (construct (tuple Big Big) %n1 %n2)) (= %s2 (construct_variant St C %np))
    (= %t2 (construct (tuple St Big) %s2 %y)) (jmp loop %t2)""")
        called = program("(= %r (call step %pay)) (jmp loop %r)")
        for source in (made, called):
            c = c_of(source)
            self.assertRegex(body(c, "main"), r"loop_q\w*\.e0\.payload\.v_A")
            self.assertNotRegex(body(c, "main"), r"loop_q\w*\.e0 = arm_|&loop_q\w*\.e0[,)]")
            self.assertEqual(run(c), 106)

    def test_no_value_is_made_in_the_variable_a_payload_is_read_in_through_a_copy(self):
        # The switch is on a copy of the loop's parameter, which reads the parameter's variable: the
        # arm reads its payload there, so the state it makes for the next round is made elsewhere, and
        # the payload's first component is read before it is overwritten.
        c = c_of(BIG + f"""
(type St (enum (A (tuple Big Big)) (C (tuple Big Big))))
(fun main i32 () (locals (&acc u64))
  (entry () (= %z (const u64 0)) (= %u0 (store &acc %z)) {big("b1", 1)} {big("b2", 6)}
    (= %pay (construct (tuple Big Big) %b1 %b2)) (= %s (construct_variant St A %pay)) (jmp loop %s))
  (loop ((%st St)) (= %c (copy %st)) (switch %c St (A arm) (C fin)))
  (arm ((%pay (tuple Big Big))) (= (%x %y) (destruct %pay (tuple Big Big))) {big("n1", 50)}
    (= %np (construct (tuple Big Big) %n1 %y)) (= %s2 (construct_variant St C %np))
    (= (%xa %xb %xc %xd %xe) (destruct %x Big)) (= %old (load &acc)) (= %new (add %old %xa)) (= %u (store &acc %new))
    (jmp loop %s2))
  (fin ((%pay (tuple Big Big))) (= (%x %y) (destruct %pay (tuple Big Big))) (= (%ya %yb %yc %yd %ye) (destruct %y Big))
    (= %v (load &acc)) (= %w (add %v %ya)) (= %i (int_cast %w i32)) (ret %i)))""")
        self.assertNotRegex(body(c, "main"), r"loop_st\w*\.payload\.v_C = ")
        self.assertEqual(run(c), 7)
        self.assertEqual(run(c, [c_compiler(), *UNOPTIMIZED]), 7)

    def test_a_foreach_mapper_writing_out_results_is_called_through_its_by_value_entry(self):
        c = c_of(BIG + f"""
(fun mk (forall (F) () (tuple Big F) (F u64) (entry ((%x F) (%k u64))
  (= %k1 (copy %k)) (= %k2 (copy %k)) (= %k3 (copy %k)) (= %k4 (copy %k)) (= %k5 (copy %k))
  (= %b (construct Big %k1 %k2 %k3 %k4 %k5)) (= %t (construct (tuple Big F) %b %x)) (ret %t))))
(fun main i32 ()
  (entry () (= %u (const u32 1)) (= %v (const u32 2)) (= %s (construct (tuple u32 u32) %u %v)) (= %k (const u64 3))
    (= %m (foreach (foreach (F (tuple u32 u32)) (tuple Big F)) %s (mk) %k))
    (= (%p %q) (destruct %m (foreach (F (tuple u32 u32)) (tuple Big F))))
    (= (%pb %pk) (destruct %p (tuple Big u32))) (= (%a %b %c %d %e) (destruct %pb Big))
    (= (%qb %qk) (destruct %q (tuple Big u32))) (= (%a2 %b2 %c2 %d2 %e2) (destruct %qb Big))
    (= %x1 (add %a %e2)) (= %x2 (add %pk %qk)) (= %y (int_cast %x2 u64)) (= %x (add %x1 %y))
    (= %i (int_cast %x i32)) (ret %i)))""")
        self.assertRegex(head(c, "mk"), r"K_Big\* out_0\w*$")
        self.assertRegex(body(c, "main"), r"entry_m\w*\.e0 = _ZN4kurs2mk\w*_value\(entry_s\w*\.e0, entry_k\w*\);")
        self.assertEqual(run(c), 3 + 3 + 1 + 2)


class HomedParameterTests(unittest.TestCase):
    """A component written where it ends up never overwrites what a later write of the same edge or
    construction reads."""

    def test_a_swap_through_a_join_homes_one_component(self):
        c = c_of("""
(type T (struct (v u64)))
(fun main i32 () (locals (&s (tuple T T)))
  (entry () (= %x (const u64 1)) (= %y (const u64 2)) (= %tx (construct T %x)) (= %ty (construct T %y))
    (= %t0 (construct (tuple T T) %tx %ty)) (= %u (store &s %t0))
    (= (%a0 %b0) (destruct &s (tuple T T))) (= %t (construct (tuple T T) %a0 %b0)) (jmp join %t))
  (join ((%p (tuple T T))) (empty &s.0 &s.1) (= (%a %b) (destruct %p (tuple T T))) (= %u1 (store &s.1 %a))
    (= %u2 (store &s.0 %b)) (= %v (load &s)) (= (%c %d) (destruct %v (tuple T T))) (= (%cv) (destruct %c T))
    (= (%dv) (destruct %d T)) (= %i (int_cast %cv i32)) (ret %i)))""")
        # The edge writes `s.1` from `s.0`; `s.1`, which it overwrote, goes through the join's variable.
        self.assertRegex(body(c, "main"), r"join_p\w*\.e1 = p_s\w*\.e1;\n  p_s\w*\.e1 = p_s\w*\.e0;\n  goto L_join;")
        self.assertEqual(run(c), 2)

    def test_a_swap_through_a_construction_homes_one_component(self):
        c = c_of("""
(type T (struct (v u64)))
(fun main i32 () (locals (&s (tuple T T)))
  (entry () (= %x (const u64 1)) (= %y (const u64 2)) (= %tx (construct T %x)) (= %ty (construct T %y))
    (= %t0 (construct (tuple T T) %tx %ty)) (= %u (store &s %t0))
    (= (%a0 %b0) (destruct &s (tuple T T))) (= %t (construct (tuple T T) %b0 %a0)) (jmp join %t))
  (join ((%p (tuple T T))) (empty &s.0 &s.1) (= (%a %b) (destruct %p (tuple T T))) (= %u1 (store &s.0 %a))
    (= %u2 (store &s.1 %b)) (= %v (load &s)) (= (%c %d) (destruct %v (tuple T T))) (= (%cv) (destruct %c T))
    (= (%dv) (destruct %d T)) (= %i (int_cast %cv i32)) (ret %i)))""")
        self.assertRegex(body(c, "main"), r"join_p\w*\.e1 = p_s\w*\.e0;\n  p_s\w*\.e0 = p_s\w*\.e1;\n  goto L_join;")
        self.assertEqual(run(c), 2)

    def test_a_component_lying_in_a_variable_the_edge_assigns_is_not_homed(self):
        c = c_of("""
(type T (struct (v u64)))
(fun main i32 () (locals (&s (tuple T T)) (&acc u64))
  (entry () (= %z (const u64 0)) (= %u0 (store &acc %z))
    (= %e (const u64 5)) (= %f (const u64 6)) (= %te (construct T %e)) (= %tf (construct T %f))
    (= %s0 (construct (tuple T T) %te %tf)) (= %u5 (store &s %s0)) (= (%g1 %g2) (destruct &s (tuple T T)))
    (= %x (const u64 10)) (= %y (const u64 20)) (= %tx (construct T %x)) (= %ty (construct T %y))
    (= %p0 (construct (tuple T T) %tx %ty))
    (= %x1 (const u64 1)) (= %y1 (const u64 2)) (= %tx1 (construct T %x1)) (= %ty1 (construct T %y1))
    (= %q0 (construct (tuple T T) %tx1 %ty1)) (= %n (const u64 2)) (jmp loop %p0 %q0 %n))
  (loop ((%p (tuple T T)) (%q (tuple T T)) (%n u64)) (empty &s.0 &s.1)
    (= (%a %b) (destruct %p (tuple T T))) (= %u1 (store &s.0 %a)) (= %u2 (store &s.1 %b))
    (= (%c %d) (destruct &s (tuple T T))) (= (%cv) (destruct %c T)) (= (%dv) (destruct %d T))
    (= %old (load &acc)) (= %new (add %old %cv)) (= %u3 (store &acc %new))
    (= %one (const u64 1)) (= %n1 (sub %n %one)) (= %n2 (copy %n1)) (= %zero (const u64 0)) (= %go (ne %n1 %zero))
    (= %h (const u64 100)) (= %k (const u64 200)) (= %th (construct T %h)) (= %tk (construct T %k))
    (= %nq (construct (tuple T T) %th %tk)) (jnz %go (loop %q %nq %n2) (done)))
  (done () (= %r (load &acc)) (= %i (int_cast %r i32)) (ret %i)))""")
        # The round's `q` is the next round's `p`: the edge assigns `q` before it could write `p`'s components.
        self.assertRegex(body(c, "main"), r"loop_p\w* = loop_q\w*;\n    loop_q\w* = loop_nq\w*;")
        self.assertEqual(run(c), 10 + 1)

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
