"""Named ownership errors are diagnosed from the frontend's inferred place flow."""
import unittest

from kursal.placeflow import OwnershipError
from tests import test_closure_boundary


PREFIX = '''exposed struct Token! { n: u32 }
fn take(t: Token) -> u32 { let { n } = t; n }
'''


class LinearDiagnosticTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def rejects(self, source, message):
        with self.assertRaisesRegex(OwnershipError, message):
            self.run_source(PREFIX + source, 0)

    def test_unused_local(self):
        self.rejects('fn main() -> i32 { let t = Token.{ n: 7u32 }; 0i32 }', "linear value 't'.*not consumed")

    def test_unused_parameter(self):
        self.rejects('fn unused(t: Token) -> u32 { 0u32 } fn main() -> i32 { 0i32 }', "linear value 't'.*not consumed")

    def test_reused_local(self):
        self.rejects('''fn main() -> i32 {
            let t = Token.{ n: 7u32 }; let n = take(t); (take(t) as i32)
        }''', "value 't'.*moved")

    def test_consumed_on_only_one_branch(self):
        self.rejects('''fn f(t: Token, c: bool) -> u32 {
            if c { let n = take(t); } else { }; 0u32
        } fn main() -> i32 { 0i32 }''', "linear value 't'.*some paths")

    def test_repeated_loop_consumption(self):
        self.rejects('''fn f(t: Token, c: bool) -> u32 {
            while c { let n = take(t); }; 0u32
        } fn main() -> i32 { 0i32 }''', "(linear value 't'.*some paths|value 't'.*moved)")

    def test_overwriting_live_linear_value(self):
        self.rejects('''fn main() -> i32 {
            var t = Token.{ n: 7u32 }; t = Token.{ n: 9u32 }; (take(t) as i32)
        }''', "overwrite.*linear value 't'")

    def test_lent_parameter_must_be_restored(self):
        self.rejects('''fn bad(&t: Token) -> u32 { take(t) }
            fn main() -> i32 { 0i32 }''', "lent value 't'.*restored")

    def test_both_branches_consume(self):
        self.run_source(PREFIX + '''fn f(t: Token, c: bool) -> u32 {
            if c { take(t) } else { take(t) + 1 }
        }
        fn main() -> i32 {
            ((f(Token.{ n: 7u32 }, true) + f(Token.{ n: 9u32 }, false)) as i32)
        }''', 17)

    def test_loop_restores_its_value(self):
        self.run_source(PREFIX + '''fn main() -> i32 {
            var t = Token.{ n: 7u32 }; var n = 0u32;
            while n < 3 { let v = take(t); t = Token.{ n: v + 1 }; n = n + 1; }
            (take(t) as i32)
        }''', 10)

    def test_linear_value_can_be_returned(self):
        self.run_source(PREFIX + '''fn identity(t: Token) -> Token { t }
            fn main() -> i32 { (take(identity(Token.{ n: 7u32 })) as i32) }''', 7)

    def test_same_value_in_two_arguments(self):
        self.rejects('''fn pair(a: Token, b: Token) -> u32 { take(a) + take(b) }
            fn main() -> i32 { let t = Token.{ n: 7u32 }; (pair(t, t) as i32) }
            ''', "value 't'.*moved")

    def test_lending_after_move(self):
        self.rejects('''fn peek(&t: Token) -> u32 { t.n }
            fn main() -> i32 {
                let t = Token.{ n: 7u32 }; let n = take(t); (peek(&t) as i32)
            }''', "value 't'.*moved.*lent")

    def test_partial_move_preserves_disjoint_copy_field(self):
        self.run_source(PREFIX + '''exposed struct Pair! { token: Token, count: u32 }
            fn main() -> i32 {
                let p = Pair.{ token: Token.{ n: 7u32 }, count: 3u32 };
                let n = take(p.token);
                ((n + p.count) as i32)
            }''', 10)

    def test_early_return_consumes_on_both_paths(self):
        self.run_source(PREFIX + '''fn f(t: Token, c: bool) -> u32 {
                if c { return take(t); }
                take(t) + 1
            }
            fn main() -> i32 {
                ((f(Token.{ n: 7u32 }, true) + f(Token.{ n: 9u32 }, false)) as i32)
            }''', 17)

    def test_survey_records_ownership_error(self):
        import tempfile
        from pathlib import Path
        from kursal.lower import lower
        from kursal.resolve import resolve
        from kursal.sigs import signatures
        from tests.test_lower import write
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, {'lib.kur': PREFIX + 'fn main() -> i32 { let t = Token.{ n: 7u32 }; 0i32 }'})
            resolver = resolve(root)
            program = lower(resolver, signatures(resolver), strict=False)
            self.assertEqual(len(program.errors), 1)
            self.assertIsInstance(program.errors[0][1], OwnershipError)
