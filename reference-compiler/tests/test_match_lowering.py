"""Match coverage and explicit payload discard are frontend obligations."""
import unittest

from kursal.lower import LowerError
from tests import test_closure_boundary


class MatchLoweringTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def test_enum_coverage_errors_are_upstream(self):
        for arms in ('E::A => 1i32',
                     'E::A => 1i32, E::A => 2i32, E::B => 3i32',
                     'E::A | E::A => 1i32, E::B => 3i32'):
            with self.subTest(arms=arms), self.assertRaisesRegex(LowerError, 'match'):
                self.run_source('exposed enum E { A, B }\n'
                                'fn main() -> i32 { match E::A { ' + arms + ' } }', 1)

    def test_integer_coverage_errors_are_upstream(self):
        for arms in ('0 => 1i32', '0..=4 => 1i32, 4..=6 => 2i32, _ => 3i32',
                     '256 => 1i32, _ => 2i32', '4..=2 => 1i32, _ => 2i32'):
            with self.subTest(arms=arms), self.assertRaisesRegex(LowerError, 'match'):
                self.run_source('fn main() -> i32 { match 0u8 { ' + arms + ' } }', 1)

    def test_linear_payload_discard_is_upstream(self):
        for arms in ('E!::A => 0i32, E!::B => 1i32',
                     'E!::A(_) => 0i32, E!::B => 1i32',
                     '_ => 0i32', 'E!::A | E!::B => 0i32'):
            with self.subTest(arms=arms), self.assertRaisesRegex(LowerError, 'linear'):
                self.run_source('''exposed struct Token! { n: u32 }
                    exposed enum E! { A(Token!), B }
                    fn main() -> i32 { match E!::A.(Token!.{ n: 7u32 }) { ''' + arms + ' } }', 0)

    def test_linear_pattern_discard_is_upstream(self):
        for pattern in ('_', '(x, _)', '{ n }'):
            value = {'_': 'Token!.{ n: 7u32 }', '(x, _)': '(1u32, Token!.{ n: 7u32 })',
                     '{ n }': 'Pair!.{ n: 1u32, token: Token!.{ n: 7u32 } }'}[pattern]
            with self.subTest(pattern=pattern), self.assertRaisesRegex(LowerError, 'linear'):
                self.run_source('''exposed struct Token! { n: u32 }
                    exposed struct Pair! { n: u32, token: Token! }
                    fn main() -> i32 { let ''' + pattern + ' = ' + value + '; 0i32 }', 0)

    def test_linear_payload_is_consumed_in_each_arm(self):
        self.run_source('''exposed struct Token! { n: u32 }
            exposed enum E! { A(Token!), B(Token!) }
            fn take(e: E!) -> u32 {
                match e {
                    E!::A(t) => { let { n } = t; n },
                    E!::B(t) => { let { n } = t; n + 1 }
                }
            }
            fn main() -> i32 {
                ((take(E!::A.(Token!.{ n: 7u32 })) + take(E!::B.(Token!.{ n: 9u32 }))) as i32)
            }''', 17)

    def test_lent_wildcard_preserves_linear_payload(self):
        self.run_source('''exposed struct Token! { n: u32 }
            exposed enum E! { A(Token!), B }
            fn main() -> i32 {
                var e = E!::A.(Token!.{ n: 7u32 });
                let n = match &e { _ => 3u32 };
                let m = match e { E!::A(t) => { let { n } = t; n }, E!::B => 0u32 };
                ((n + m) as i32)
            }''', 10)

    def test_core_rejects_a_repeated_variant_arm(self):
        from kursal.check import CheckError, check_program
        from kursal.kurs import parse_program
        with self.assertRaisesRegex(CheckError, 'switch_arm_per_variant'):
            check_program(parse_program('''
                (type E (enum (A (tuple)) (B (tuple))))
                (fun f i32 (E)
                  (entry ((%e E)) (switch %e E (A a) (A a) (B b)))
                  (a ((%unit (tuple))) (= %n (const i32 1)) (ret %n))
                  (b ((%unit (tuple))) (= %n (const i32 2)) (ret %n)))
            '''))

    def test_grouped_copy_payloads_of_linear_enum_can_be_discarded(self):
        self.run_source('''exposed enum E! { A(u32), B(u32) }
            fn main() -> i32 {
                match E!::A.(4u32) { E!::A | E!::B => 7i32 }
            }''', 7)

    def test_integer_ranges_execute(self):
        self.run_source('''fn classify(n: i8) -> i32 {
                match n { -4..=-1 => 2i32, 0 | 1 => 3i32, _ => 5i32 }
            }
            fn main() -> i32 { classify(-2i8) + classify(1i8) + classify(9i8) }
            ''', 10)

    def test_core_switches_only_on_enums(self):
        from kursal.check import CheckError, check_program
        from kursal.kurs import parse_program
        for ty, arms in (('bool', '(true done)'), ('u8', ''), ('u8', '(0 done)')):
            with self.subTest(ty=ty), self.assertRaises(CheckError):
                check_program(parse_program('(fun f i32 (' + ty + ') '
                    '(entry ((%x ' + ty + ')) (switch %x ' + ty + ' ' + arms + ')) '
                    '(done () (= %n (const i32 1)) (ret %n)))'))

    def test_wildcard_does_not_make_bool_a_matchable_type(self):
        with self.assertRaisesRegex(LowerError, 'match requires'):
            self.run_source('fn main() -> i32 { match true { _ => 1i32 } }', 1)

    def test_lent_payload_can_be_taken_and_restored(self):
        self.run_source('''exposed struct Token! { n: u32 }
            exposed enum E! { A(Token!), B }
            fn main() -> i32 {
                var e = E!::A.(Token!.{ n: 7u32 });
                let old = match &e {
                    E!::A(t) => { let { n } = t; t = Token!.{ n: n + 1 }; n },
                    E!::B => 0u32
                };
                let new = match e { E!::A(t) => { let { n } = t; n }, E!::B => 0u32 };
                ((old + new) as i32)
            }''', 15)

    def test_tuple_payload_can_discard_only_copy_component(self):
        self.run_source('''exposed struct Token! { n: u32 }
            exposed enum E! { A(Token!, u32), B }
            fn main() -> i32 {
                match E!::A.(Token!.{ n: 7u32 }, 9u32) {
                    E!::A(t, _) => { let { n } = t; (n as i32) },
                    E!::B => 0i32
                }
            }''', 7)

    def test_struct_pattern_can_omit_copy_fields(self):
        self.run_source('''exposed struct Token! { n: u32 }
            exposed struct Pair! { token: Token!, count: u32 }
            fn main() -> i32 {
                let { token } = Pair!.{ token: Token!.{ n: 7u32 }, count: 9u32 };
                let { n } = token;
                (n as i32)
            }''', 7)
