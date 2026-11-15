"""Frontend shape use respects nominal exposure before core checking."""
import tempfile
import unittest
from pathlib import Path

from kursal.infer import InferError
from kursal.lower import lower
from kursal.resolve import resolve
from kursal.sigs import signatures
from kursal.check import check_program
from kursal.cgen import emit_c
from tests.test_lower import write, compile_and_run


DECLS = '''struct S { n: u32 }
    tuple Pair(u32, u32);
    enum E { A, B(u32) }
    fn make_s() -> S { S.{ n: 7u32 } }
    fn make_pair() -> Pair { Pair.(3u32, 4u32) }
    fn make_e() -> E { E::B.(7u32) }
'''
IMPORTS = 'use package::owner::{S, Pair, E, make_s, make_pair, make_e}; '


class NominalExposureTests(unittest.TestCase):
    def run_source(self, source, declarations=DECLS, extra=None):
        with tempfile.TemporaryDirectory(prefix='kursal-nominal-') as base:
            directory = Path(base)
            for name in extra or {}:
                (directory / name).parent.mkdir(parents=True, exist_ok=True)
            write(directory, {'owner.kur': declarations, 'lib.kur': source, **(extra or {})})
            resolver = resolve(directory)
            sigs = signatures(resolver)
            program = lower(resolver, sigs)
            checker = check_program(program)
            entry = sigs.name(resolver.packages[0].entry_decl)
            path = directory / 'program.c'
            path.write_text(emit_c(program, checker, entry))
            self.assertEqual(compile_and_run(path, directory / 'program'), 7)

    def reject(self, body, declarations=DECLS):
        with self.assertRaisesRegex(InferError, 'not exposed'):
            self.run_source(IMPORTS + 'fn main() -> i32 { ' + body + ' }', declarations)

    def test_opaque_construction(self):
        for expression in ('S.{ n: 7u32 }', 'Pair.(3u32, 4u32)', 'E::A', 'E::B.(7u32)'):
            with self.subTest(expression=expression):
                self.reject('let x = ' + expression + '; 7i32')

    def test_opaque_projection(self):
        for expression in ('make_s().n', 'make_pair().0'):
            with self.subTest(expression=expression):
                self.reject('let x = ' + expression + '; 7i32')
        self.reject('let s = make_s(); s.n as i32')

    def test_opaque_destructuring(self):
        self.reject('let { n } = make_s(); n as i32')
        self.reject('let (a, b) = make_pair(); (a + b) as i32')

    def test_opaque_match_including_wildcard(self):
        for scrutinee in ('e', '&e'):
            for arms in ('E::A => 7i32, E::B(n) => n as i32', '_ => 7i32'):
                with self.subTest(scrutinee=scrutinee, arms=arms):
                    self.reject('let e = make_e(); match ' + scrutinee + ' { ' + arms + ' }')

    def test_deferred_projection_still_checks_exposure(self):
        self.reject('let get = |x| x.n; get(make_s()) as i32')

    def test_owner_operations_remain_valid(self):
        self.run_source('fn main() -> i32 { package::owner::run() }', DECLS + '''
            fn run() -> i32 {
                let { n } = make_s();
                let (a, b) = make_pair();
                match make_e() { E::A => 0i32, E::B(x) => if n == x { (a + b) as i32 } else { 0i32 } }
            }''')

    def test_public_exposure_allows_client_operations(self):
        declarations = DECLS.replace('struct S', 'exposed struct S').replace('tuple Pair', 'exposed tuple Pair').replace('enum E', 'exposed enum E')
        self.run_source(IMPORTS + '''fn main() -> i32 {
            let s = S.{ n: 7u32 };
            let { n } = s;
            let (a, b) = Pair.(3u32, 4u32);
            match E::B.(n) { E::A => 0i32, E::B(x) => if x == s.n { (a + b) as i32 } else { 0i32 } }
        }''', declarations)

    def test_opaque_values_can_be_passed_and_returned(self):
        self.run_source(IMPORTS + '''fn identity(x: S) -> S { x }
            fn main() -> i32 { package::owner::read(identity(make_s())) as i32 }''',
            DECLS + 'fn read(x: S) -> u32 { x.n }')

    def test_scoped_exposure_allows_descendants_but_not_siblings(self):
        declarations = DECLS.replace('struct S', 'exposed(package::owner) struct S')
        self.run_source('fn main() -> i32 { package::owner::client::run() }', declarations,
                        {'owner/client.kur': 'use package::owner::S; fn run() -> i32 { S.{ n: 7u32 }.n as i32 }'})
        self.reject('make_s().n as i32', declarations)

    def test_private_shape_is_not_inherited_by_descendants(self):
        with self.assertRaisesRegex(InferError, 'not exposed'):
            self.run_source('fn main() -> i32 { package::owner::client::run() }', DECLS,
                            {'owner/client.kur': 'fn run() -> i32 { package::owner::make_s().n as i32 }'})

    def test_opaque_closure_code_cannot_be_eliminated(self):
        with self.assertRaisesRegex(InferError, 'not exposed'):
            self.run_source('fn main() -> i32 { package::owner::callable()() }', DECLS + '''
                struct Callable { env: i32, code: fn(i32) -> i32 }
                fn code(x: i32) -> i32 { x }
                fn callable() -> Callable { Callable.{ env: 7i32, code: code } }
            ''')

    def test_try_requires_exposed_variants(self):
        with self.assertRaisesRegex(InferError, 'not exposed'):
            self.run_source('''use package::owner::Opt;
                fn bad(x: Opt[u32]) -> Opt[u32] { let n = x?; x }
                fn main() -> i32 { 7i32 }''', DECLS + 'forall T. enum Opt { Some(T), None }')

    def test_opaque_tag_inspection(self):
        for operand in ('e', '&e'):
            with self.subTest(operand=operand):
                self.reject('let e = make_e(); let tag = core::mem::enum_variant(' + operand + '); 7i32')

    def test_core_tag_inspection_requires_exposure(self):
        from kursal import core as C
        from kursal.kurs import parse_program
        from kursal.sexpr import KursError
        for operand in ('%e', '&e'):
            parameter = f'({operand} E)'
            program = parse_program(f"""(type E (enum (A (tuple))))
                (fun tag u32 (E) (entry ({parameter})
                    (= %r (variant_tag {operand} E)) (ret %r)))""")
            owner = C.ModuleId(0, ('owner',))
            program.types['E'].module = owner
            program.types['E'].exposure = ('owner', owner)
            program.funs['tag'].module = C.ModuleId(0, ('client',))
            with self.assertRaises(KursError):
                check_program(program)
            program.funs['tag'].module = owner
            check_program(program)
