"""Zero-size types are erased: no C member, parameter, local, argument or stored value has one; `sizeof`
of one is 0; a load or store of one through a pointer writes no C, and `alloca` of one is a pointer that
is not null; unsafe casts and externs reject them. So the standard library's containers work at a
zero-size element type, which the runtime tests here run. `examples/zero_size` runs the same through
the surface language, for both compilers."""
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

    def test_loads_and_stores_of_a_zero_size_type_write_nothing(self):
        # `alloca` of one is not null; a load or store through it, volatile or not, and one a specialization
        # chooses, is no C at all.
        output = self.run_core('''(type Marker (struct))
            (fun (unsafe) read (forall (T) () T ((ptr T)) (entry ((%p (ptr T))) (= %v (ptr_load %p)) (ret %v))))
            (fun main i32 () (entry ()
              (unsafe (= %p (alloca Marker))) (unsafe (= %q (alloca (array u64 0))))
              (= %p1 (copy %p)) (= %p2 (copy %p)) (= %p3 (copy %p)) (= %p4 (copy %p))
              (= %m (construct Marker)) (unsafe (= %u (ptr_store %p1 %m))) (unsafe (= %m2 (ptr_load_volatile %p2)))
              (unsafe (= %v (ptr_store_volatile %p3 %m2))) (unsafe (= %e (ptr_load %q)))
              (unsafe (= %m3 (call (read Marker) %p4)))
              (unsafe (= %a (cast %p usize))) (= %z (const usize 0)) (= %b (ne %a %z)) (jnz %b (yes) (no)))
            (yes () (= %r (const i32 5)) (ret %r))
            (no () (= %r (const i32 0)) (ret %r)))''', 5)
        self.assertIn("= (void*)(uintptr_t)1;", output)
        self.assertNotIn("alloca", output)
        self.assertNotIn("volatile", output)
        self.assertRegex(output, r"static void \w+\(void\* \w+\) \{\n  L_entry: ;\n  return;\n\}")

    def test_an_owned_hash_set(self):
        # `OwnedMap![K, ()]` is a set: of owned buffers, looked up by a view, and of numbers.
        self.expect('''
            use std::buffer::Buffer!;
            use std::collections::hashmap::OwnedMap!;
            use std::option::Option;
            fn word(n: u64) -> Buffer! {
                Buffer!::new().push_static("w").push_u64(n)
            }
            fn forget(old: Option[(Buffer!, ())]) -> u32 {
                match old {
                    Option::Some(pair) => {
                        let (key, unit) = pair;
                        key.destroy();
                        1
                    },
                    Option::None => 0,
                }
            }
            fn drop_key(key: Buffer!, unit: ()) {
                key.destroy()
            }
            fn main() -> i32 {
                var set = OwnedMap!::new[Buffer!, ()]();
                var i = 0u64;
                var again = 0u32;
                while i < 200 {
                    again = again + forget(&set.insert(word(i % 100), ()));
                    i = i + 1;
                }
                var probe = word(42);
                let found = &set.find(&probe);
                let removed = forget(&set.remove(&probe));
                let still = &set.contains(&probe);
                probe.destroy();
                var visited = 0usize;
                &set.each_in(&visited, |&key, &unit, &count| { count = count + 1usize; });
                let size = &set.len();
                set.destroy(drop_key);
                var numbers = OwnedMap!::new[u32, ()]();
                var n = 0u32;
                while n < 1000 {
                    let old = &numbers.insert(n * 3, ());
                    n = n + 1;
                }
                var key = 300u32;
                let hit = &numbers.get(&key);
                let taken = &numbers.remove(&key);
                key = 301u32;
                let miss = &numbers.contains(&key);
                let count = &numbers.len();
                numbers.free();
                if again != 100 || !found.is_some() || removed != 1 || still || visited != 99usize || size != 99usize {
                    1
                } else if !hit.is_some() || !taken.is_some() || miss || count != 999usize {
                    2
                } else {
                    0
                }
            }''')

    def test_a_vector_of_units_counts_without_allocating(self):
        # One block of no bytes, which a vector of zero-size elements never grows; its elements are pushed,
        # moved and popped as nothing.
        self.expect('''
            use std::collections::vec::OwnedVec!;
            use std::option::Option;
            extern "C" fn probe_allocations() -> u64;
            fn main() -> i32 {
                let before = unsafe probe_allocations();
                var v = OwnedVec!::new[()]();
                var i = 0u32;
                while i < 100000 {
                    v = v.push(());
                    i = i + 1;
                }
                let allocations = unsafe probe_allocations() - before;
                let pushed = &v.len();
                &v.insert(5usize, ());
                &v.swap(0usize, 99usize);
                &v.reverse();
                let removed = &v.remove(3usize);
                let last = &v.get(99999usize);
                let past = &v.get(100000usize);
                var popped = 0u32;
                var going = true;
                while going {
                    match &v.pop() {
                        Option::Some(unit) => { popped = popped + 1; },
                        Option::None => { going = false; },
                    }
                }
                let empty = &v.is_empty();
                v.free();
                if allocations != 1u64 || pushed != 100000usize { 1 }
                else if !removed.is_some() || !last.is_some() || past.is_some() { 2 }
                else if popped != 100000 || !empty { 3 }
                else { 0 }
            }''', counted=True)

    def test_an_owned_unit(self):
        self.expect('''
            use std::owned::Owned!;
            fn main() -> i32 {
                var o = Owned!::new(());
                let three = &o.update(|&unit| 3u32);
                let exists 'r. (record, region) = o.open();
                let back = record.close(region);
                let unit = back.into_value();
                if three == 3u32 { 0 } else { 1 }
            }''')

    def test_the_branded_vector_and_map(self):
        self.expect('''
            use std::collections::hashmap::HashMap!;
            use std::collections::vec::Vec!;
            fn main() -> i32 {
                let exists 'v. (tag, vec) = Vec!::new[()]();
                let exists 'w. (tag, vec) = vec.push(tag, ());
                let exists 'x. (tag, vec) = vec.push(tag, ());
                let view = &vec.as_slice(&tag);
                let exists 'y. (tag, vec, out) = vec.pop(tag);
                let left = &vec.len(&tag);
                vec.free(tag);
                let exists 'h. (tag, map) = HashMap!::new[u32, ()]();
                let exists 'i. (tag, map, old) = map.insert(tag, 1u32, ());
                var one = 1u32;
                let present = &map.contains(&tag, &one);
                let exists 'j. (tag, map, taken) = map.remove(tag, &one);
                let size = &map.len(&tag);
                map.free(tag);
                if view.len() != 2usize || !out.is_some() || left != 1usize { 1 }
                else if !present || old.is_some() || !taken.is_some() || size != 0usize { 2 }
                else { 0 }
            }''')

    def test_the_arena_at_a_zero_size_type(self):
        self.expect('''
            use std::arena::Domain!;
            use std::collections::arena_map::ArenaMap;
            use std::collections::arena_vec::ArenaVec;
            use std::mem::Ref;
            use std::option::Option;
            fn main() -> i32 {
                let exists 'a. cx = Domain!::new();
                let unit = Ref::new(&cx, ());
                let copied = unit.copy(&cx);
                let units: ArenaVec[(), 'a] = ArenaVec::new(&cx);
                var i = 0u32;
                while i < 1000 {
                    units.push(&cx, ());
                    i = i + 1;
                }
                let last = units.at(&cx, 999usize);
                let popped = units.pop(&cx);
                let left = units.len(&cx);
                let set: ArenaMap[u32, (), 'a] = ArenaMap::new(&cx);
                var k = 0u32;
                while k < 100 {
                    let old = set.insert(&cx, k, ());
                    k = k + 1;
                }
                let has = set.contains(&cx, 50u32) && !set.contains(&cx, 500u32);
                let removed = set.remove(&cx, 50u32);
                let size = set.len(&cx);
                let { tag, arena } = cx;
                let many = &arena.alloc_slice(&tag, 1000000usize, ());
                let found = match many.get_ref(999999usize) {
                    Option::Some(r) => { let u = r.copy(&tag); true },
                    Option::None => false,
                };
                let cx = Domain!.{ tag, arena };
                cx.destroy();
                if !popped.is_some() || left != 999usize { 1 }
                else if !has || !removed.is_some() || size != 99usize { 2 }
                else if many.len() != 1000000usize || !found { 3 }
                else { 0 }
            }''')

    def test_a_brand_witness_through_a_pointer(self):
        # A zero-size struct that holds a brand witness, stored and read back through a raw pointer, an
        # `Owned!` and the arena: the brand it carries is the one put in.
        self.expect('''
            use std::arena::Domain!;
            use std::cap::{Brand, Exclusive!};
            use std::mem::{Ref, alloca};
            use std::owned::Owned!;
            forall 'k.
            exposed struct Witness {
                brand: Brand['k],
                unit: (),
            }
            forall 'k.
            fn same(a: Brand['k], b: Brand['k]) -> bool {
                true
            }
            fn main() -> i32 {
                let exists 'b. (brand, cap) = Exclusive!::fresh();
                let p = unsafe alloca[Witness['b]]();
                let address = unsafe (p as usize);
                unsafe *p = Witness.{ brand, unit: () };
                let { brand: read, unit: u1 } = unsafe *p;
                let boxed = Owned!::new(Witness.{ brand: read, unit: () });
                let { brand: unboxed, unit: u2 } = boxed.into_value();
                let exists 'a. cx = Domain!::new();
                let held = Ref::new(&cx, Witness.{ brand: unboxed, unit: () });
                let { brand: again, unit: u3 } = held.copy(&cx);
                cx.destroy();
                cap.retire();
                if address != 0usize && same(again, brand) { 0 } else { 1 }
            }''')


if __name__ == "__main__":
    unittest.main()
