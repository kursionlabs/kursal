"""Zero-size types are erased: no C member, parameter, local, argument or stored value has one; `sizeof`
of one is 0; no raw load, store or `alloca` is at one, and unsafe casts and externs reject them
(tests/lower_cases.py, test_core_soundness). A value with zero-size parts beside bytes goes through
memory and the standard library's containers, which the runtime tests here run. `examples/zero_size`
runs the same through the surface language, for both compilers."""
import re
import unittest

from tests import test_c_names, test_std_runtime


class ZeroSizeTests(unittest.TestCase):
    run_core = test_c_names.CNameTests.run_core
    run_program = test_std_runtime.StdRuntimeTests.run_program
    expect = test_std_runtime.StdRuntimeTests.expect

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
        self.assertRegex(output, r"static const char d0_empty;")

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

    def test_loads_and_stores_of_a_value_with_zero_size_parts(self):
        # A load or store, volatile or not, and one a specialization chooses, of a value whose zero-size
        # parts sit beside bytes moves only the bytes.
        output = self.run_core('''(type Marker (struct))
            (type Mixed (struct (m Marker) (n u32) (u (tuple))))
            (fun (unsafe) read (forall (T) ((Copy T) (Sized T)) T ((ptr T)) (entry ((%p (ptr T))) (= %v (ptr_load %p)) (ret %v))))
            (fun main i32 () (entry ()
              (unsafe (= %p (alloca Mixed)))
              (= %p1 (copy %p)) (= %p2 (copy %p)) (= %p3 (copy %p))
              (= %m (construct Marker)) (= %n (const u32 5)) (= %u (construct (tuple)))
              (= %x (construct Mixed %m %n %u)) (unsafe (= %s (ptr_store_volatile %p1 %x)))
              (unsafe (= %y (call (read Mixed) %p2))) (unsafe (= %z (ptr_load_volatile %p3)))
              (= (%m2 %n2 %u2) (destruct %z Mixed)) (= %r (int_cast %n2 i32)) (ret %r)))''', 5)
        self.assertIn("struct K_Mixed { uint32_t f_n; };", output)
        self.assertIn("volatile", output)

    def test_containers_of_values_with_zero_size_parts(self):
        # The containers take `Sized` of their elements: a pair of a marker and a number has bytes.
        self.expect('''
            use std::arena::{Domain!, ref_in};
            use std::collections::arena_vec::ArenaVec;
            use std::collections::hashmap::OwnedMap!;
            use std::collections::vec::OwnedVec!;
            use std::mem::Ref;
            use std::owned::Owned!;
            exposed struct Marker {}
            fn main() -> i32 {
                var v = OwnedVec!::new[(Marker, u32)]();
                var i = 0u32;
                while i < 100 {
                    &v.push((Marker.{}, i));
                    i = i + 1;
                }
                let last = &v.pop();
                v.destroy();
                let (m, seven) = Owned!::new((Marker.{}, 7u32)).into_value();
                var map = OwnedMap!::new[u32, ((), u8)](std::process::hash_seed());
                let old = &map.insert(3u32, ((), 4u8));
                var key = 3u32;
                let present = &map.contains(&key);
                map.destroy();
                var exists 'a. cx = Domain!::new(std::process::hash_seed());
                let r = ref_in(&cx, ((), 9u32));
                let (u, nine) = r.copy(&cx);
                let pairs: ArenaVec[(Marker, u8), 'a] = ArenaVec::filled(&cx, 10usize, (Marker.{}, 1u8));
                let ten = pairs.len(&cx);
                cx.destroy();
                if !last.is_some() || seven != 7u32 || !present || nine != 9u32 || ten != 10usize { 1 } else { 0 }
            }''')

if __name__ == "__main__":
    unittest.main()
