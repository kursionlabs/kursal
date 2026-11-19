"""Zero-size types are erased: no C member, parameter, local, argument or stored value has one; `sizeof`
of one is 0; the unsafe memory operations, casts and externs reject them, also where a specialization
chooses one. `examples/zero_size` runs the same through the surface language and the standard
library."""
import re
import tempfile
import unittest
from pathlib import Path

from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.kurs import parse_program
from kursal.lower import lower
from kursal.resolve import LIB, resolve
from kursal.sexpr import KursError
from kursal.sigs import signatures
from tests import test_c_names


class ZeroSizeTests(unittest.TestCase):
    run_core = test_c_names.CNameTests.run_core

    def test_zero_size_components_have_no_members(self):
        output = self.run_core('''(type Marker (struct))
            (type Tagged (forall ($k) () (struct (brand (brand $k)) (unit (tuple)) (empty (array u64 0))
              (value u32) (markers (array Marker 3)))))
            (type Shape (enum (Empty (tuple)) (Mark Marker)))
            (fun main i32 () (entry ()
              (unsafe (= %b (static_brand))) (= %u (construct (tuple))) (= %e (construct (array u64 0)))
              (= %v (const u32 7)) (= %m (construct Marker)) (= %ms (construct (array Marker 3) %m))
              (= %t (construct (Tagged $static) %b %u %e %v %ms))
              (= (%b2 %u2 %e2 %v2 %ms2) (destruct %t (Tagged $static)))
              (= %m2 (construct Marker)) (= %s (construct_variant Shape Mark %m2)) (= %n (variant_tag %s Shape))
              (= %w (int_cast %n u32)) (= %x (add %v2 %w))
              (= %r (int_cast %x i32)) (ret %r)))''', 8)
        self.assertNotIn("KurUnit", output)
        self.assertIn("struct K_Tagged { uint32_t f_value; };", output)
        self.assertIn("struct K_Shape { uint32_t tag; };", output)
        self.assertNotIn("K_Marker", output)

    def test_zero_size_parameters_results_and_places_pass_nothing(self):
        output = self.run_core('''(type Marker (struct))
            (fun mark Marker () (entry () (= %m (construct Marker)) (ret %m)))
            (fun keep (tuple) (Marker u32 (tuple)) (entry ((&m Marker) (&n u32) (%u (tuple)))
              (= %x (load &n)) (= %one (const u32 1)) (= %y (add %x %one)) (= %s (store &n %y))
              (= %m2 (call mark)) (= %t (store &m %m2)) (ret %u)))
            (fun main i32 () (locals (&m Marker) (&n u32))
              (entry () (= %m (call mark)) (= %a (store &m %m)) (= %z (const u32 6)) (= %b (store &n %z))
                (= %u (construct (tuple))) (= %f (fn keep)) (= %v (call keep &m &n %u))
                (= %u2 (construct (tuple))) (= %w (call %f &m &n %u2))
                (= %x (load &n)) (= %r (int_cast %x i32)) (ret %r)))''', 8)
        keep = re.search(r"static void (\w+)\(uint32_t\* entry_n\)", output)
        self.assertIsNotNone(keep, output)
        self.assertRegex(output, r"static void \w+\(void\)")
        self.assertRegex(output, r"typedef void \(\*K_fn\)\(uint32_t\*\);")
        self.assertIn(f"{keep.group(1)}(&p_n);", output)
        self.assertIn("entry_f(&p_n);", output)
        self.assertNotIn("p_m", output)

    def test_sizeof_of_a_zero_size_type_is_zero(self):
        output = self.run_core('''(type Marker (struct))
            (fun size (forall (T) () usize () (entry () (= %n (sizeof T)) (ret %n))))
            (fun main i32 () (entry ()
              (= %a (call (size (tuple)))) (= %b (call (size (array u64 0)))) (= %c (call (size (tuple Marker (tuple)))))
              (= %d (sizeof (tuple u32 Marker))) (= %e (sizeof u32))
              (= %ab (add %a %b)) (= %abc (add %ab %c)) (= %r (int_cast %abc i32)) (= %same (eq %d %e))
              (jnz %same (done %r) (wrong)))
            (done ((%x i32)) (ret %x))
            (wrong () (= %w (const i32 9)) (ret %w)))''', 0)
        self.assertIn("= UINT64_C(0);", output)

    def test_zero_size_data_has_an_address(self):
        output = self.run_core('''(data empty (array u8 0) ())
            (fun main i32 () (entry ()
              (= %p (data empty)) (= %z (const usize 0)) (unsafe (= %q (cast %p usize)))
              (= %b (ne %q %z)) (jnz %b (yes) (no)))
            (yes () (= %r (const i32 1)) (ret %r))
            (no () (= %r (const i32 0)) (ret %r)))''', 1)
        self.assertRegex(output, r"static char d0_empty;")

    def test_enum_payloads_of_zero_size_types_are_tags(self):
        self.run_core('''(type Marker (struct))
            (type Shape (enum (Empty (tuple)) (Mark Marker) (Value u32)))
            (fun score u32 (Shape) (entry ((%s Shape)) (switch %s Shape (Empty empty) (Mark mark) (Value value)))
              (empty ((%u (tuple))) (= %r (const u32 1)) (ret %r))
              (mark ((%m Marker)) (= %r (const u32 2)) (ret %r))
              (value ((%v u32)) (ret %v)))
            (fun main i32 () (entry ()
              (= %u (construct (tuple))) (= %m (construct Marker)) (= %v (const u32 4))
              (= %a (construct_variant Shape Empty %u)) (= %b (construct_variant Shape Mark %m))
              (= %c (construct_variant Shape Value %v))
              (= %x (call score %a)) (= %y (call score %b)) (= %z (call score %c))
              (= %xy (add %x %y)) (= %n (add %xy %z)) (= %r (int_cast %n i32)) (ret %r)))''', 7)

    def test_a_specialization_at_a_zero_size_type_is_void(self):
        output = self.run_core('''(fun same (forall (T) () T (T) (entry ((%x T)) (ret %x))))
            (fun main i32 () (entry ()
              (= %u (construct (tuple))) (= %v (call (same (tuple)) %u)) (= %n (const i32 3))
              (= %r (call (same i32) %n)) (ret %r)))''', 3)
        self.assertRegex(output, r"static void \w+\(void\) \{\n  L_entry: ;\n  return;\n\}")

    def test_a_zero_size_instantiation_is_reported_at_the_call_that_chose_it(self):
        # The library's containers read and write their elements through pointers, so they do not build
        # at a zero-size element type. The error names the program's own file and the call that chose the
        # type, then the memory operation in the library it reaches.
        cases = {
            "Ref::new(&cx, ())": "arena.kur",
            "OwnedVec!::new[()]().push(()).free()": "collections/vec.kur",
            "{ var m = OwnedMap!::new[u32, ()](); let old = &m.insert(1u32, ()); m.free() }": "collections/hashmap.kur",
            "Owned!::new(()).into_value()": "owned.kur",
        }
        for expression, library in cases.items():
            with self.subTest(expression=expression), tempfile.TemporaryDirectory(prefix="kursal-zero-size-") as base:
                directory = Path(base)
                (directory / "kur.toml").write_text(f'[package]\nname="p"\nsource_root="."\nkind="executable"\n'
                                                    f'entry="main"\n[dependencies.std]\npath="{LIB / "std"}"\n')
                (directory / "lib.kur").write_text('''use std::alloc::arena::Domain!;
use std::alloc::owned::Owned!;
use std::collections::{hashmap::OwnedMap!, vec::OwnedVec!};
use std::mem::Ref;
fn main() -> i32 {
    let exists 'a. cx = Domain!::new();
    let r = ''' + expression + ''';
    cx.destroy();
    0
}
''')
                resolver = resolve(directory)
                sigs = signatures(resolver)
                program = lower(resolver, sigs)
                checked = check_program(program)
                here = re.escape(str(directory / "lib.kur"))
                there = re.escape(str(LIB / "alloc" / library))
                with self.assertRaisesRegex(KursError, f"^{here}:7:\\d+: a zero-size type has no memory form, and this call "
                                            f"reaches a read or write of one through a pointer at {there}:\\d+:\\d+$"):
                    emit_c(program, checked, sigs.name(resolver.packages[0].entry_decl))


if __name__ == "__main__":
    unittest.main()
