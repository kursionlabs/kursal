"""Exercise capture lifetime and lifted closure calls through generated C."""
import tempfile
import unittest
from pathlib import Path

from kursal import core as C
from kursal.cgen import emit_c
from kursal.infer import InferError
from kursal.check import check_program
from kursal.lower import lower
from kursal.resolve import resolve
from kursal.sexpr import KursError
from kursal.sigs import signatures
from tests.test_lower import write, compile_and_run


class ClosureBoundaryTests(unittest.TestCase):
    def run_source(self, source: str, expected: int) -> C.Program:
        with tempfile.TemporaryDirectory(prefix="kursal-closure-boundary-") as base:
            directory = Path(base)
            write(directory, {"lib.kur": source})
            resolver = resolve(directory)
            sigs = signatures(resolver)
            program = lower(resolver, sigs)
            checker = check_program(program)
            entry = sigs.name(resolver.packages[0].entry_decl)
            path = directory / "program.c"
            path.write_text(emit_c(program, checker, entry))
            self.assertEqual(compile_and_run(path, directory / "program"), expected)
            return program

    def test_capture_is_snapshot(self):
        self.run_source('''fn main() -> i32 {
            var n = 3u32;
            let f = |x: u32| n + x;
            n = 10;
            (f(2) as i32)
        }''', 5)

    def test_a_closure_neither_assigns_nor_lends_what_it_captures(self):
        # The closure holds a copy, so a change would reach the copy alone.
        with self.assertRaisesRegex(InferError, "3:32: n is captured, and a body cannot assign what it captures; "
                                                "assign it outside, or give a closure a place parameter for it$"):
            self.run_source('''fn main() -> i32 {
            var n = 3u32;
            let f = |x: u32| { n = n + x; n };
            ((f(2) + n) as i32)
        }''', 0)

    def test_nested_capture_preserves_binding_identity(self):
        self.run_source('''fn main() -> i32 {
            let n = 3u32;
            let outer = |x: u32| {
                let inner = |y: u32| n + x + y;
                let n = 100u32;
                inner(2) + n
            };
            (outer(4) as i32)
        }''', 109)

    def test_linear_environment_consumed_once(self):
        prefix = '''exposed struct Token! { n: u32 }
        fn main() -> i32 {
            let token = Token!.{ n: 7u32 };
            let once = |x: u32| { let { n } = token; n + x };
        '''
        self.run_source(prefix + '(once(2) as i32) }', 9)
        with self.assertRaises(KursError):
            self.run_source(prefix + 'let a = once(2); ((a + once(3)) as i32) }', 19)

    def test_lifted_place_parameter_follows_environment(self):
        program = self.run_source('''
        forall Env. fn invoke(f: closure[Env](&_: u32) -> u32, &x: u32) -> u32 { f(&x) }
        fn main() -> i32 {
            let increment = 4u32;
            var n = 3u32;
            let f = |&x: u32| { x = x + increment; x };
            let result = invoke(f, &n);
            ((result + n) as i32)
        }''', 14)
        lifted = [f for name, f in program.funs.items() if '#closure' in name]
        self.assertEqual(len(lifted), 1)
        self.assertEqual(lifted[0].places, frozenset({1}))

    def test_enclosing_generic_brand_is_lifted(self):
        self.run_source('''forall 'a. fn use_brand(w: core::cap::Brand['a]) -> u32 {
            let f = |n: u32| { let witness = w; n + 1u32 };
            f(4)
        }
        fn main() -> i32 {
            let exists 'a. witness = core::cap::fresh();
            (use_brand(witness) as i32)
        }''', 5)

    def test_block_local_brand_capture_is_rejected(self):
        with self.assertRaisesRegex(KursError, 'block-local brand'):
            self.run_source('''fn main() -> i32 {
                let exists 'a. witness = core::cap::fresh();
                let f = |n: u32| { let w = witness; n };
                (f(4) as i32)
            }''', 4)

    def test_type_only_outer_brand_in_closure_is_rejected_upstream(self):
        from kursal.lower import LowerError
        with self.assertRaisesRegex(LowerError, 'block-local brand'):
            self.run_source('''fn main() -> i32 {
                let exists 'a. witness = core::cap::fresh();
                let f = || core::mem::sizeof[(u32, core::cap::Brand['a])]();
                (f() as i32)
            }''', 0)

    def test_malformed_structural_closures_are_rejected_upstream(self):
        from kursal.infer import InferError
        cases = [
            ('code: fn(u32) -> u32', 'code: code'),
            ('code: fn(u32) -> u32, env: u32', 'code: code, env: 4u32'),
            ('env: u32, code: fn(u32) -> u32, extra: u32',
             'env: 4u32, code: code, extra: 5u32'),
            ('env: bool, code: fn(u32) -> u32', 'env: true, code: code'),
        ]
        for fields, values in cases:
            with self.subTest(fields=fields), self.assertRaisesRegex(InferError, 'closure|type mismatch'):
                self.run_source('''exposed struct Callable { ''' + fields + ''' }
                    fn code(env: u32) -> u32 { env }
                    fn main() -> i32 {
                        let f = Callable.{ ''' + values + ''' };
                        (f() as i32)
                    }''', 4)

    def test_canonical_structural_closure_runs(self):
        self.run_source('''exposed struct Callable { env: u32, code: fn(u32) -> u32 }
            fn code(env: u32) -> u32 { env + 1u32 }
            fn main() -> i32 {
                let f = Callable.{ env: 4u32, code: code };
                (f() as i32)
            }''', 5)

    def test_invalid_closure_code_parameter_modes(self):
        from kursal.infer import InferError
        cases = [
            ('fn() -> u32', 'fn code() -> u32 { 4u32 }'),
            ('fn(&_: u32) -> u32', 'fn code(&env: u32) -> u32 { env }'),
        ]
        for ty, function in cases:
            with self.subTest(ty=ty), self.assertRaisesRegex(InferError, 'closure'):
                self.run_source('exposed struct Callable { env: u32, code: ' + ty + ' }\n' + function + '''
                    fn main() -> i32 {
                        let code_value = code;
                        let f = Callable.{ env: 4u32, code: code_value };
                        (f() as i32)
                    }''', 4)

    def test_type_only_generic_brand_in_closure_runs(self):
        self.run_source('''forall 'a. fn measure(w: core::cap::Brand['a]) -> usize {
                let f = || core::mem::sizeof[(u32, core::cap::Brand['a])]();
                f()
            }
            fn main() -> i32 {
                let exists 'a. witness = core::cap::fresh();
                if measure(witness) == core::mem::sizeof[(u32, core::cap::Brand['a])]() { 7i32 } else { 1i32 }
            }''', 7)

    def test_brand_opened_inside_closure_runs(self):
        self.run_source('''fn main() -> i32 {
                let f = || {
                    let exists 'a. witness = core::cap::fresh();
                    let size = core::mem::sizeof[(u32, core::cap::Brand['a])]();
                    if size == core::mem::sizeof[(u32, core::cap::Brand['a])]() { 7i32 } else { 1i32 }
                };
                f()
            }''', 7)

    def test_type_only_outer_brand_in_foreach_is_rejected_upstream(self):
        from kursal.lower import LowerError
        with self.assertRaisesRegex(LowerError, 'block-local brand'):
            self.run_source('''fn main() -> i32 {
                let exists 'a. witness = core::cap::fresh();
                let parts = foreach item of (1u32, 2u32) {
                    let size = core::mem::sizeof[(u32, core::cap::Brand['a])]();
                    item
                };
                let (x, y) = parts;
                ((x + y) as i32)
            }''', 3)
