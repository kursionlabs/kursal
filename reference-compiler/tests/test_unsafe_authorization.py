"""Source authorization is operation-local, including raw-target lends."""
import unittest
import tempfile
from pathlib import Path

from kursal.lower import LowerError
from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.lower import lower
from kursal.resolve import resolve
from kursal.sigs import signatures
from tests.test_lower import write, compile_and_run


class UnsafeAuthorizationTests(unittest.TestCase):
    def run_source(self, source, expected):
        with tempfile.TemporaryDirectory(prefix="kursal-unsafe-") as base:
            directory = Path(base)
            write(directory, {"lib.kur": source}, core=True)  # pointer methods take core::atomic::Ordering
            resolver = resolve(directory)
            sigs = signatures(resolver)
            program = lower(resolver, sigs)
            checker = check_program(program)
            entry = sigs.name(resolver.packages[0].entry_decl)
            path = directory / "program.c"
            path.write_text(emit_c(program, checker, entry))
            self.assertEqual(compile_and_run(path, directory / "program"), expected)

    def reject(self, source):
        with self.assertRaisesRegex(LowerError, 'unsafe authorization'):
            self.run_source(source + ' fn main() -> i32 { 0i32 }', 0)

    def test_direct_and_foreign_calls_require_authorization(self):
        self.reject('unsafe fn danger() -> u32 { 7u32 } fn bad() -> u32 { danger() }')
        self.reject('extern "C" fn abs(x: i32) -> i32; fn bad() -> i32 { abs(-7i32) }')

    def test_function_values_require_authorization(self):
        self.reject('fn bad(f: unsafe fn() -> u32) -> u32 { f() }')

    def test_closure_code_requires_authorization(self):
        self.reject('''exposed struct Callable { env: u32, code: unsafe fn(u32) -> u32 }
            fn bad(f: Callable) -> u32 { f() }''')

    def test_trait_call_requires_authorization(self):
        self.reject('''forall T. trait Read { unsafe fn read(x: T) -> u32; }
            forall T where Read[T]. fn bad(x: T) -> u32 { Read[T]::read(x) }''')

    def test_dereference_and_pointer_methods_require_authorization(self):
        for body, result in (('*p', 'u32'), ('p.load()', 'u32'), ('p.store(7u32)', '()')):
            with self.subTest(body=body):
                self.reject(f'fn bad(p: *u32) -> {result} {{ {body} }}')

    def test_volatile_builtins_require_authorization(self):
        for body, result in (('core::ptr::load_volatile(p)', 'u32'),
                             ('core::ptr::store_volatile(p, 7u32)', '()')):
            with self.subTest(body=body):
                self.reject(f'fn bad(p: *u32) -> {result} {{ {body} }}')

    def test_raw_lend_needs_authorization(self):
        self.reject('fn read(&x: u32) -> u32 { x } fn bad(p: *u32) -> u32 { read(&*p) }')

    def test_machine_builtins_require_authorization(self):
        self.reject('fn bad() -> *u32 { core::mem::alloca[u32]() }')
        self.reject("fn bad() -> core::cap::Brand['static] { core::cap::static_brand() }")
        self.reject('fn bad(p: *u32) -> u32 { p.atomic_load(core::atomic::Ordering::Relaxed) }')

    def test_authorized_memory_and_raw_lends(self):
        for lend in ('read(unsafe &*p)', 'unsafe read(&*p)'):
            with self.subTest(lend=lend):
                self.run_source('''fn read(&x: u32) -> u32 { x = x + 1u32; x }
                    fn main() -> i32 {
                        let p = unsafe core::mem::alloca[u32]();
                        unsafe *p = 5u32;
                        let a = ''' + lend + ''';
                        let store = unsafe p.store(a + 1u32);
                        let b = unsafe core::ptr::load_volatile(p);
                        let c = unsafe *p;
                        if b == c { b as i32 } else { 0i32 }
                    }''', 7)

    def test_raw_lend_authorization_does_not_extend_to_other_arguments(self):
        self.reject('fn read(&x: u32, y: u32) -> u32 { x + y } '
                    'fn bad(p: *u32) -> u32 { read(unsafe &*p, *p) }')

    def test_authorized_trait_call(self):
        self.run_source('''forall T. trait Read { unsafe fn read(x: T) -> i32; }
            instance Read[i32] { unsafe fn read(x: i32) -> i32 { x } }
            fn main() -> i32 { unsafe Read[i32]::read(7i32) }''', 7)

    def test_authorized_raw_lend_does_not_authorize_unsafe_callee(self):
        self.reject('unsafe fn read(&x: u32) -> u32 { x } '
                    'fn bad(p: *u32) -> u32 { read(unsafe &*p) }')

    def test_outer_call_does_not_authorize_nested_call_or_dereference(self):
        self.reject('unsafe fn read(x: u32) -> u32 { x } '
                    'fn bad(p: *u32) -> u32 { unsafe read(*p) }')
        self.reject('unsafe fn read(x: u32) -> u32 { x } '
                    'fn bad() -> u32 { unsafe read(read(7u32)) }')

    def test_unsafe_context_and_lifted_closure(self):
        self.run_source('''unsafe fn add(x: i32) -> i32 { x + 1 }
            unsafe fn run() -> i32 {
                let f = |x: i32| add(x);
                f(6i32)
            }
            fn main() -> i32 { unsafe run() }''', 7)

    def test_authorized_indirect_and_foreign_calls(self):
        self.run_source('''extern "C" fn abs(x: i32) -> i32;
            fn run(f: unsafe fn(i32) -> i32) -> i32 { unsafe f(-7i32) }
            fn main() -> i32 { run(abs) }''', 7)

    def test_unreachable_tail_does_not_emit_unsafe_call(self):
        self.run_source('''unsafe fn read(x: i32) -> i32 { x }
            fn main() -> i32 { return 7i32; read(1i32) }''', 7)
