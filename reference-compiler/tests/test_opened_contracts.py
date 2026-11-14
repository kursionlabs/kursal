"""Opened-place contracts survive control flow without inventing witness identities."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from kursal import core as C
from kursal.cc import FLAGS, c_compiler
from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.kurs import parse_program
from kursal.placeflow import PlaceFlow
from kursal.sexpr import KursError


PACKAGE = "(exists ($h) (tuple u32 (brand $h)))"
SETUP = f"""
  (= %fresh (fresh)) (= %w (open %fresh ($a)))
  (= %seven (const u32 7)) (= %body (construct (tuple u32 (brand $a)) %seven %w))
  (= %pkg (pack {PACKAGE} $a %body)) (= %holder (construct Pk %pkg)) (= %u (store &k %holder))
"""
BRANCH = f"""
(type Pk (struct (pkg {PACKAGE})))
(fun main i32 () (locals (&k Pk))
  (entry () {SETUP} (= %opened (open &k.pkg ($o)))
    (= %c (const bool true)) (jnz %c ((left $o)) ((right $o))))
  (left ($l) () (opened &k.pkg ($l))
    (= %v (load &k.pkg.0)) (= %n (const u32 7)) (= %r (add %v %n)) (jmp (join $l) %r))
  (right ($r) () (opened &k.pkg ($r))
    (= %v (load &k.pkg.0)) (= %n (const u32 3)) (= %r (mul %v %n)) (jmp (join $r) %r))
  (join ($j) ((%n u32)) (opened &k.pkg ($j))
    (= %closed (pack {PACKAGE} $j &k.pkg)) (= %r (int_cast %n i32)) (ret %r)))
"""


class OpenedContractTests(unittest.TestCase):
    def runs(self, program, status):
        c = emit_c(program, check_program(program), "main")
        with tempfile.TemporaryDirectory() as directory:
            binary = str(Path(directory) / "test")
            built = subprocess.run([c_compiler(), *FLAGS, "-x", "c", "-o", binary, "-"],
                                   input=c, text=True, capture_output=True)
            self.assertEqual(built.returncode, 0, built.stderr)
            self.assertEqual(subprocess.run([binary]).returncode, status)

    def rejects(self, source, error):
        with self.assertRaisesRegex(KursError, error):
            check_program(parse_program(source))

    def test_explicit_contracts_rename_brands_across_both_branch_edges_and_join(self):
        for value, status in (("true", 14), ("false", 21)):
            with self.subTest(value=value):
                program = parse_program(BRANCH.replace("const bool true", f"const bool {value}"))
                self.assertEqual(program.funs["main"].blocks[1].opened, (("&k.pkg", ("l",)),))
                self.runs(program, status)

    def test_flow_infers_the_same_contracts_after_edge_renaming(self):
        program = parse_program(BRANCH)
        fun = program.funs["main"]
        for block in fun.blocks:
            block.opened = ()
        PlaceFlow(program).plan(fun)
        self.assertEqual([b.opened for b in fun.blocks],
                         [(), (("&k.pkg", ("l",)),), (("&k.pkg", ("r",)),), (("&k.pkg", ("j",)),)])
        self.runs(program, 14)

    def test_predecessors_must_supply_the_requested_view_and_witnesses(self):
        closed = BRANCH.replace("(= %opened (open &k.pkg ($o)))", "").replace("$o", "$a")
        self.rejects(closed, "opened_view_contract")
        wrong = BRANCH.replace("((left $o))", "((left $a))")
        self.rejects(wrong, "opened_view_contract")
        one_closed = BRANCH.replace("(jmp (join $l) %r)", f"(= %close (pack {PACKAGE} $l &k.pkg)) (jmp (join $l) %r)")
        self.rejects(one_closed, "opened_view_contract")

    def test_join_candidate_cannot_implicitly_close_an_incoming_view(self):
        source = BRANCH.replace("(jmp (join $l) %r)",
                                f"(= %close (pack {PACKAGE} $l &k.pkg)) (jmp (join $l) %r)")
        source = source.replace(f"(= %closed (pack {PACKAGE} $j &k.pkg))", "")
        program = parse_program(source)
        for block in program.funs["main"].blocks:
            block.opened = ()
        PlaceFlow(program).plan(program.funs["main"])
        self.assertEqual(program.funs["main"].blocks[-1].opened, ())
        with self.assertRaisesRegex(KursError, "opened_view_contract"):
            check_program(program)

    def test_untransmitted_brands_are_not_invented_by_flow(self):
        source = f"""(type Pk (struct (pkg {PACKAGE})))
          (fun main i32 () (locals (&k Pk))
            (entry () {SETUP} (= %open (open &k.pkg ($o))) (jmp done))
            (done () (= %n (const i32 0)) (ret %n)))"""
        program = parse_program(source)
        PlaceFlow(program).plan(program.funs["main"])
        self.assertEqual(program.funs["main"].blocks[1].opened, ())
        with self.assertRaisesRegex(KursError, "opened_view_contract"):
            check_program(program)

    def test_view_contracts_are_scoped_unique_and_package_shaped(self):
        self.rejects(BRANCH.replace("(opened &k.pkg ($l))", "(opened &k.pkg ($missing))"),
                     "brand missing is not in scope")
        self.rejects(BRANCH.replace("(opened &k.pkg ($l))", "(opened &k.pkg ($l)) (opened &k.pkg ($l))"),
                     "duplicate_opened_view")
        self.rejects(f"""(type Two (struct (a {PACKAGE}) (b {PACKAGE})))
            (fun main i32 () (locals (&k Two)) (entry () (= %r (const i32 0)) (ret %r))
              (dead ($l) () (opened &k.a ($l)) (opened &k.b ($l)) (= %r (const i32 0)) (ret %r)))""",
                     "opened_contract_brand_names_one_view")
        self.rejects(BRANCH.replace("(entry ()", "(entry () (opened &k.pkg ($static))"),
                     "entry_cannot_assume_opened_views")
        for path, brands in (("&k", ("q",)), ("&k.pkg", ()), ("&k.pkg", ("q", "q"))):
            with self.subTest(path=path, brands=brands):
                program = parse_program(BRANCH)
                dead = C.Block("dead", (), (C.Instr(("%n",), "const", ty=C.Prim("i32"), lit="0"),),
                               C.Ret("%n"), brands=("q",), opened=((path, brands),))
                program.funs["main"].blocks += (dead,)
                with self.assertRaisesRegex(KursError, "opened_contract_requires_package_brands"):
                    check_program(program)

    def test_reader_rejects_malformed_contract_shapes(self):
        for contract in ("(opened &p)", "(opened &p $q)", "(opened &p (q))", "(opened (&p) ($q))"):
            with self.subTest(contract=contract), self.assertRaises(KursError):
                parse_program(f"(fun f u32 () (entry () {contract} (= %n (const u32 0)) (ret %n)))")

    def test_partial_linear_payload_survives_a_branch_and_is_restored(self):
        pkg = "(exists ($k) (tuple H (brand $k)))"
        source = f"""(type H! (struct (n u32)))
          (fun f u32 ({pkg}) (entry ((&p {pkg}))
            (= %u (open &p ($o))) (= %h (load &p.0)) (= %c (const bool true))
            (jnz %c ((left $o) %h) ((right $o) %h)))
            (left ($l) ((%h H)) (opened &p ($l)) (empty &p.0)
              (= %u (store &p.0 %h)) (= %p (pack {pkg} $l &p)) (= %n (const u32 0)) (ret %n))
            (right ($r) ((%h H)) (opened &p ($r)) (empty &p.0)
              (= %u (store &p.0 %h)) (= %p (pack {pkg} $r &p)) (= %n (const u32 0)) (ret %n)))"""
        program = parse_program(source)
        check_program(program)
        for block in program.funs["f"].blocks:
            block.empty, block.opened = (), ()
        PlaceFlow(program).plan(program.funs["f"])
        self.assertEqual(program.funs["f"].blocks[1].empty, ("&p.0",))
        check_program(program)

    def test_loop_backedges_preserve_an_opened_view(self):
        source = f"""(type Pk (struct (pkg {PACKAGE})))
          (fun main i32 () (locals (&k Pk))
            (entry () {SETUP} (= %open (open &k.pkg ($o))) (= %n (const u32 0)) (jmp (loop $o) %n))
            (loop ($b) ((%n u32))
              (= %copy (copy %n)) (= %limit (const u32 3)) (= %c (lt %copy %limit))
              (jnz %c ((body $b) %n) ((done $b))))
            (body ($c) ((%n u32)) (= %v (load &k.pkg.0)) (= %one (const u32 1))
              (= %inc (add %v %one)) (= %u (store &k.pkg.0 %inc))
              (= %tick (const u32 1)) (= %next (add %n %tick)) (jmp (loop $c) %next))
            (done ($d) () (= %v (load &k.pkg.0)) (= %p (pack {PACKAGE} $d &k.pkg))
              (= %r (int_cast %v i32)) (ret %r)))"""
        program = parse_program(source)
        PlaceFlow(program).plan(program.funs["main"])
        self.assertEqual(program.funs["main"].blocks[1].opened, (("&k.pkg", ("b",)),))
        self.runs(program, 10)

    def test_nested_views_are_installed_parent_first_and_close_with_the_parent(self):
        outer = f"(exists ($a) (tuple (brand $a) {PACKAGE}))"
        source = f"""(type Pk (struct (pkg {outer})))
          (fun main i32 () (locals (&k Pk))
            (entry () (= %fa (fresh)) (= %wa (open %fa ($a)))
              (= %fb (fresh)) (= %wb (open %fb ($b))) (= %n (const u32 7))
              (= %ib (construct (tuple u32 (brand $b)) %n %wb))
              (= %inner (pack {PACKAGE} $b %ib))
              (= %ob (construct (tuple (brand $a) {PACKAGE}) %wa %inner))
              (= %pkg (pack {outer} $a %ob)) (= %root (construct Pk %pkg)) (= %u (store &k %root))
              (= %op (open &k.pkg ($o))) (= %ip (open &k.pkg.1 ($i))) (jmp (next $o $i)))
            (next ($x $y) () (opened &k.pkg.1 ($y)) (opened &k.pkg ($x))
              (= %v (load &k.pkg.1.0)) (= %ip (pack {PACKAGE} $y &k.pkg.1))
              (= %op (pack {outer} $x &k.pkg)) (jmp done %v))
            (done ((%v u32)) (= %r (int_cast %v i32)) (ret %r)))"""
        self.runs(parse_program(source), 7)
        # Closing the parent also implicitly closes its nested view.
        source = source.replace(f"(= %ip (pack {PACKAGE} $y &k.pkg.1))", "")
        program = parse_program(source)
        for block in program.funs["main"].blocks:
            block.opened = ()
        PlaceFlow(program).plan(program.funs["main"])
        self.assertEqual(program.funs["main"].blocks[-1].opened, ())
        self.runs(program, 7)

    def test_switch_edges_preserve_views_and_known_variants_together(self):
        pkg = "(exists ($h) (tuple E (brand $h)))"
        source = f"""(type E (enum (A u32) (B u32))) (type Pk (struct (pkg {pkg})))
          (fun main i32 () (locals (&k Pk))
            (entry () (= %fresh (fresh)) (= %w (open %fresh ($a))) (= %n (const u32 7))
              (= %e (construct_variant E A %n)) (= %body (construct (tuple E (brand $a)) %e %w))
              (= %pkg (pack {pkg} $a %body)) (= %root (construct Pk %pkg)) (= %u (store &k %root))
              (= %op (open &k.pkg ($o))) (switch &k.pkg.0 E (A (left $o)) (B (right $o))))
            (left ($l) () (= %v (load &k.pkg.0.A)) (jmp (done $l) %v))
            (right ($r) () (= %v (load &k.pkg.0.B)) (jmp (done $r) %v))
            (done ($d) ((%v u32)) (= %p (pack {pkg} $d &k.pkg))
              (= %r (int_cast %v i32)) (ret %r)))"""
        program = parse_program(source)
        PlaceFlow(program).plan(program.funs["main"])
        left = program.funs["main"].blocks[1]
        self.assertEqual(left.opened, (("&k.pkg", ("l",)),))
        self.assertEqual(left.known, (("&k.pkg.0", "A"),))
        self.runs(program, 7)

    def test_closing_a_view_checks_the_package_type(self):
        other = "(exists ($h) (tuple bool (brand $h)))"
        self.rejects(BRANCH.replace(f"(pack {PACKAGE} $j &k.pkg)", f"(pack {other} $j &k.pkg)"), "packing")
        same_payload = "(exists ($h) (tuple u32 (brand $j)))"
        self.rejects(BRANCH.replace(f"(pack {PACKAGE} $j &k.pkg)", f"(pack {same_payload} $j &k.pkg)"),
                     "existential brand binder is unused")
        pkg = "(exists ($a) (tuple (brand $a)))"
        self.rejects(f"""(fun f u32 ({pkg}) (entry ((&p {pkg}))
          (= %a (open &p ($x))) (= %b (open &p ($y))) (= %n (const u32 0)) (ret %n)))""",
                     "place_already_open")

    def test_an_empty_successor_cannot_implicitly_close_an_open_payload(self):
        pkg = "(exists ($k) (tuple H (brand $k)))"
        source = f"""(type H! (struct (n u32)))
          (fun f u32 ({pkg}) (entry ((&p {pkg}))
            (= %u (open &p ($o))) (= %h (load &p.0)) (jmp next %h))
            (next ((%h H)) (empty &p)
              (= %fresh (fresh)) (= %w (open %fresh ($new)))
              (= %body (construct (tuple H (brand $new)) %h %w))
              (= %pkg (pack {pkg} $new %body)) (= %u (store &p %pkg))
              (= %r (const u32 0)) (ret %r)))"""
        self.rejects(source, "opened_view_contract")

    def test_explicit_close_on_both_predecessors_allows_a_sealed_join(self):
        source = BRANCH.replace("(jmp (join $l) %r)",
                                f"(= %close (pack {PACKAGE} $l &k.pkg)) (jmp (join $l) %r)")
        source = source.replace("(jmp (join $r) %r)",
                                f"(= %close (pack {PACKAGE} $r &k.pkg)) (jmp (join $r) %r)")
        source = source.replace(f"(= %closed (pack {PACKAGE} $j &k.pkg))", "")
        source = source.replace("(opened &k.pkg ($j))", "")
        check_program(parse_program(source))

    def test_loading_whole_payload_retains_the_view_until_explicit_close(self):
        pkg = "(exists ($k) (tuple H (brand $k)))"
        source = f"""(type H! (struct (n u32)))
          (fun f (tuple) ({pkg}) (entry ((&p {pkg}))
            (= %o (open &p ($a))) (= %body (load &p)) (jmp (next $a) %body))
          (next ($b) ((%body (tuple H (brand $b))))
            (opened &p ($b)) (empty &p)
            (= %s (store &p %body)) (= %closed (pack {pkg} $b &p)) (ret %closed)))"""
        program = parse_program(source)
        check_program(program)
        for block in program.funs["f"].blocks:
            block.empty, block.opened = (), ()
        PlaceFlow(program).plan(program.funs["f"])
        self.assertEqual(program.funs["f"].blocks[1].opened, (("&p", ("b",)),))
        check_program(program)

    def test_close_needs_the_whole_payload(self):
        pkg = "(exists ($k) (tuple H (brand $k)))"
        partial = f"""(type H! (struct (n u32)))
          (fun f H ({pkg}) (entry ((&p {pkg}))
            (= %o (open &p ($a))) (= %h (load &p.0))
            (= %closed (pack {pkg} $a &p)) (ret %h)))"""
        self.rejects(partial, "pack_place_full")
        # Nor can a root whose entire payload moved be closed empty.
        empty = f"""(type H! (struct (n u32)))
          (fun f {pkg} ({pkg}) (locals (&p {pkg})) (entry ((%input {pkg}))
            (= %s (store &p %input)) (= %o (open &p ($a))) (= %body (load &p))
            (= %closed (pack {pkg} $a &p)) (= %result (pack {pkg} $a %body))
            (ret %result)))"""
        self.rejects(empty, "pack_place_full")

    def test_full_lent_package_must_be_closed_before_return(self):
        source = f"""(fun f u32 ({PACKAGE}) (entry ((&p {PACKAGE}))
            (= %o (open &p ($a))) (= %n (load &p.0)) (ret %n)))"""
        self.rejects(source, "place_parameter_sealed")

    def test_opened_contract_uses_block_parameters_not_function_brands(self):
        source = f"""(fun f (forall ($a) () u32 ()
            (locals (&p {PACKAGE})) (entry () (= %n (const u32 0)) (ret %n))
            (dead () (opened &p ($a)) (= %n (const u32 0)) (ret %n))))"""
        self.rejects(source, "opened_contract_requires_block_brands")
