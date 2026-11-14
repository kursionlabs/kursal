"""Distinct checked core identities must remain distinct after C naming."""
import tempfile
import unittest
from pathlib import Path

from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.kurs import parse_program
from tests.test_lower import compile_and_run


class CNameTests(unittest.TestCase):
    def run_core(self, source, expected, foreign=''):
        program = parse_program(source)
        output = emit_c(program, check_program(program), 'main')
        with tempfile.TemporaryDirectory(prefix='kursal-c-names-') as base:
            path = Path(base) / 'program.c'
            path.write_text(output + '\n' + foreign)
            self.assertEqual(compile_and_run(path, Path(base) / 'program'), expected)
        return output

    def test_place_names_colliding_after_sanitization(self):
        self.run_core('''(fun main i32 () (locals (&a-b i32) (&a_b i32))
            (entry () (= %x (const i32 7)) (= %y (const i32 9))
              (= %u (store &a-b %x)) (= %v (store &a_b %y))
              (= %a (load &a-b)) (= %b (load &a_b)) (= %r (add %a %b)) (ret %r)))''', 16)

    def test_places_and_ssa_share_c_namespace(self):
        self.run_core('''(fun main i32 () (locals (&x i32))
            (entry () (= %n (const i32 7)) (= %u (store &x %n)) (jmp p))
            (p () (= %x (const i32 9)) (= %v (load &x)) (= %r (add %x %v)) (ret %r)))''', 16)

    def test_block_labels_colliding_after_sanitization(self):
        self.run_core('''(fun main i32 ()
            (entry () (= %c (const bool true)) (jnz %c (a-b) (a_b)))
            (a-b () (= %r (const i32 7)) (ret %r))
            (a_b () (= %r (const i32 9)) (ret %r)))''', 7)

    def test_edge_scratch_and_ssa_names_do_not_collide(self):
        self.run_core('''(fun main i32 ()
            (entry () (= %n (const i32 7)) (jmp edge %n))
            (edge ((%0 i32)) (ret %0)))''', 7)

    def test_struct_fields_with_colliding_spellings(self):
        self.run_core('''(type Pair (struct (x i32) (_x i32)))
            (fun main i32 () (locals (&p Pair))
              (entry () (= %a (const i32 7)) (= %b (const i32 9))
                (= %p (construct Pair %a %b)) (= %u (store &p %p))
                (= %x (load &p.x)) (= %y (load &p._x)) (= %r (add %x %y)) (ret %r)))''', 16)

    def test_enum_variants_with_colliding_spellings(self):
        self.run_core('''(type E (enum (A i32) (_A i32)))
            (fun main i32 ()
              (entry () (= %n (const i32 7)) (= %e (construct_variant E _A %n))
                (switch %e E (A a) (_A b)))
              (a ((%n i32)) (ret %n)) (b ((%n i32)) (ret %n)))''', 7)

    def test_foreign_name_is_not_shadowed_by_local(self):
        self.run_core('''(extern fun entry_x i32 ())
            (fun main i32 () (entry () (= %x (const i32 9))
              (unsafe (= %r (call entry_x))) (ret %r)))''', 7, 'int32_t entry_x(void) { return 7; }')

    def test_foreign_name_is_not_reused_for_specialization(self):
        self.run_core('''(extern fun f0_main i32 ())
            (fun main i32 () (entry () (unsafe (= %r (call f0_main))) (ret %r)))''', 7,
                      'int32_t f0_main(void) { return 7; }')

    def test_foreign_name_is_not_reused_for_typedef(self):
        self.run_core('''(extern fun K_fn i32 ())
            (fun identity i32 (i32) (entry ((%x i32)) (ret %x)))
            (fun main i32 () (entry () (= %f (fn identity)) (= %n (const i32 7))
              (= %a (call %f %n)) (unsafe (= %b (call K_fn))) (= %r (add %a %b)) (ret %r)))''',
                      16, 'int32_t K_fn(void) { return 9; }')

    def test_foreign_adapter_parameters_do_not_hide_callee(self):
        self.run_core('''(extern fun a0 (tuple) (i32))
            (fun main i32 () (entry () (= %f (fn a0)) (= %n (const i32 7))
              (unsafe (= %u (call %f %n))) (= %r (const i32 3)) (ret %r)))''',
                      3, 'void a0(int32_t value) { (void)value; }')

    def test_numeric_block_names_generate_valid_c_identifiers(self):
        self.run_core('''(fun main i32 ()
            (1 () (= %n (const i32 7)) (ret %n)))''', 7)

    def test_preprocessor_names_are_not_used_for_ssa(self):
        self.run_core('''(fun main i32 ()
            (UINT8 () (= %MAX (const i32 7)) (ret %MAX)))''', 7)

    def test_foreign_names_conflicting_with_c_are_diagnosed(self):
        from kursal.sexpr import KursError
        for name in ('for', 'not-a-c-name', 'KUR_FIELD', 'kur_dec_i8', 'UINT8_MAX', 'main'):
            with self.subTest(name=name):
                program = parse_program('(extern fun ' + name + ' i32 ()) '
                    '(fun entry i32 () (entry () (= %r (const i32 7)) (ret %r)))')
                checker = check_program(program)
                with self.assertRaisesRegex(KursError, 'backend: foreign symbol'):
                    emit_c(program, checker, 'entry')

    def test_foreign_name_is_not_reused_for_data(self):
        self.run_core('''(extern fun d0_blob i32 ()) (data blob i32 7)
            (fun main i32 () (entry () (= %p (data blob))
              (unsafe (= %a (builtin::unsafe::deref %p)))
              (unsafe (= %b (call d0_blob))) (= %r (add %a %b)) (ret %r)))''',
                      16, 'int32_t d0_blob(void) { return 9; }')

    def test_later_specialization_avoids_earlier_local_name(self):
        self.run_core('''(fun helper i32 () (entry () (= %r (const i32 7)) (ret %r)))
            (fun main i32 () (f1 () (= %helper (const i32 9))
              (= %n (call helper)) (= %r (add %helper %n)) (ret %r)))''', 16)
