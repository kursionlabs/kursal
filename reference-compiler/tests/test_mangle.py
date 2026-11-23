"""Logical specialization identities in Itanium nested-name symbols."""
import re
import shutil
import subprocess
import unittest
from unittest.mock import patch

from kursal import core as C
from kursal.cgen import Backend
from kursal.check import check_program
from kursal.kurs import parse_program
from kursal.mangle import mangle, path_segments, signature_key, type_key
from tests import test_c_names


class ManglingTests(unittest.TestCase):
    run_core = test_c_names.CNameTests.run_core

    def backend(self):
        program = parse_program('''(fun alpha i32 () (entry () (= %n (const i32 1)) (ret %n)))
            (fun beta i32 () (entry () (= %n (const i32 2)) (ret %n)))''')
        return Backend(program, check_program(program))

    def test_request_order_and_reparse_do_not_change_names(self):
        a, b = self.backend(), self.backend()
        left = {n: a.request(f, {}) for n, f in a.program.funs.items()}
        right = {n: b.request(f, {}) for n, f in reversed(b.program.funs.items())}
        self.assertEqual(left, right)
        self.assertNotEqual(left['alpha'], left['beta'])
        for symbol in left.values():
            self.assertRegex(symbol, r'^_ZN4kurs.*17h[0-9a-f]{16}E$')

    def test_bound_names_normalized_and_free_names_distinct(self):
        def signature(a, b):
            ty = C.Fn((C.Brand(C.Skolem(a)),), C.Exists((b,), C.Tuple((C.Skolem(a), C.Skolem(b)))))
            return signature_key((C.Param(a, 'brand'),), ty)
        self.assertEqual(signature('a', 'b'), signature('x', 'y'))
        self.assertNotEqual(signature('a', 'b'), signature('a', 'a'))
        self.assertNotEqual(type_key(C.Skolem('a')), type_key(C.Skolem('b')))
        self.assertNotEqual(type_key(C.Named('a')), type_key(C.Named('b')))
        self.assertNotEqual(type_key(C.Fn((C.U32,), C.U32)),
                            type_key(C.Fn((C.U32,), C.U32, places=frozenset({0}))))
        self.assertEqual(type_key(C.Foreach('T', False, C.UNIT, C.Named('T'))),
                         type_key(C.Foreach('U', False, C.UNIT, C.Named('U'))))

    def test_specialization_uses_logical_types(self):
        backend = self.backend()
        fun = backend.program.funs['alpha']
        fun.params = (C.Param('T'),)
        types = (C.U32, C.Prim('i32'), C.Brand(C.Skolem('a')))
        symbols = [backend.request(fun, {'T': t}) for t in types]
        self.assertEqual(len(set(symbols)), len(types))
        self.assertEqual(symbols[-1], backend.request(fun, {'T': C.Brand(C.Skolem('b'))}))
        x = backend.request(fun, {'T': C.Exists(('a',), C.Brand(C.Skolem('a')))})
        y = backend.request(fun, {'T': C.Exists(('b',), C.Brand(C.Skolem('b')))})
        self.assertEqual(x, y)

    def test_paths_are_c_identifiers_and_preserve_segments(self):
        paths = ['a-b', 'a_b', 'a_2db', 'm::f', 'm_f', 'café', '', '123']
        encoded = [path_segments(p) for p in paths]
        self.assertEqual(len(set(encoded)), len(paths))
        for p in encoded:
            self.assertRegex(mangle(p, 'identity'), r'^[A-Za-z_][A-Za-z_0-9]*$')
        self.assertEqual(path_segments('m666f6f::d626172'), ('foo', 'bar'))

    @unittest.skipUnless(shutil.which('c++filt'), 'c++filt unavailable')
    def test_standard_demangler_recovers_path(self):
        symbol = mangle(('example', 'identity'), 'identity')
        result = subprocess.check_output(['c++filt', '-s', 'gnu-v3', symbol], text=True).strip()
        self.assertRegex(result, r'^kurs::example::identity::h[0-9a-f]{16}$')

    def test_collision_with_foreign_symbol_keeps_valid_mangling(self):
        occupied = mangle(('main',), 'occupied')
        def collide(path, identity, salt=0):
            return occupied if not salt else mangle(path, identity, salt)
        with patch('kursal.cgen.mangle', side_effect=collide):
            output = self.run_core(f'''(extern fun {occupied} i32 ())
                (fun main i32 () (entry () (unsafe (= %r (call {occupied}))) (ret %r)))''',
                7, f'int32_t {occupied}(void) {{ return 7; }}')
        symbols = set(re.findall(r'_ZN\w+E', output))
        self.assertEqual(len(symbols), 2)
        self.assertTrue(all(re.fullmatch(r'_ZN\w+17h[0-9a-f]{16}E', s) for s in symbols))

    def test_instance_heads_distinguish_same_named_methods(self):
        source = '''(trait Test (Self) (run u32 ()))
          (instance Test (u32) (fun run u32 () (entry () (= %n (const u32 1)) (ret %n))))
          (instance Test (i32) (fun run u32 () (entry () (= %n (const u32 2)) (ret %n))))'''
        def names(order):
            p = parse_program(source)
            b = Backend(p, check_program(p))
            result = {}
            for t in order:
                f, sub = b.resolve_trait('Test', 'run', (t,))
                result[t] = b.request(f, sub)
            return result
        forward = names([C.U32, C.Prim('i32')])
        self.assertEqual(forward, names([C.Prim('i32'), C.U32]))
        self.assertEqual(len(set(forward.values())), 2)

    def test_function_value_names_stable_across_processes(self):
        import os
        import sys
        script = '''from kursal.kurs import parse_program
from kursal.check import check_program
from kursal.cgen import emit_c
p = parse_program("""(fun f (forall ($k) () u32 ()
  (entry () (= %n (const u32 0)) (ret %n))))
  (fun main u32 () (entry () (= %p (fn (f $static))) (= %r (const u32 0)) (ret %r)))""")
print(emit_c(p, check_program(p)))'''
        outputs = [subprocess.check_output([sys.executable, '-c', script], text=True,
                   env={**os.environ, 'PYTHONHASHSEED': seed}) for seed in ('1', '2')]
        self.assertEqual(*outputs)
        self.assertRegex(outputs[0], r'_ZN4kurs1f17h[0-9a-f]{16}E')

    def test_nested_type_encoding_does_not_expand_exponentially(self):
        ty = C.U32
        for _ in range(80):
            ty = C.Ptr(ty)
        key = type_key(ty)
        self.assertLess(len(key), 2000)
        self.assertEqual(key, type_key(ty))
