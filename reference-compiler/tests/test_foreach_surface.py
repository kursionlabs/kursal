"""A source `foreach` maps each component of its source, and may read Copy locals of its function."""

import unittest

from tests import test_closure_boundary


class ForeachSurfaceTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def test_components_map_on_their_own(self):
        self.run_source('''fn main() -> i32 {
            let mapped = foreach item of (3u32, 4u32) { (item, 1u32) };
            let (a, b) = mapped;
            let (x, y) = a;
            let (z, w) = b;
            (x + y + z + w) as i32
        }''', 9)

    def test_a_body_reads_copy_locals(self):
        self.run_source('''fn main() -> i32 {
            let bias = 2u32;
            let mapped = foreach item of (3u32, 4u32) { (item, bias) };
            let (a, b) = mapped;
            let (x, y) = a;
            let (z, w) = b;
            (x + y + z + w) as i32
        }''', 11)

    def test_a_mapper_runs_under_its_function_premises(self):
        self.run_source('''forall T. trait Size { fn size(x: T) -> u32; }
            instance Size[u32] { fn size(x: u32) -> u32 { 4u32 } }
            forall T where Size[T]. fn count(x: T) -> u32 {
                let s = foreach item of (5u32, 6u32) { (item, 1u32) };
                let (a, b) = s;
                let (p, q) = a;
                let (r, t) = b;
                q + t + Size::size(x)
            }
            fn main() -> i32 { count[u32](1u32) as i32 }''', 6)

    def test_the_size_of_a_foreach_type_over_an_exposed_parameter(self):
        self.run_source('''forall T where Exposed[T]. fn size() -> usize { core::mem::sizeof[foreach F of T => u8]() }
            fn main() -> i32 { (size[(u8, u16, u32)]() + size[u64]()) as i32 }''', 4)

    def test_a_value_of_a_foreach_type_is_mapped(self):
        # The map takes each component of the value, at the source's rows, and keeps the source.
        self.run_source('''exposed struct P { a: u32, b: u8 }
            fn main() -> i32 {
                let tagged = foreach item of P.{ a: 3u32, b: 4u8 } { (item, 5u32) };
                let swapped = foreach pair: X of tagged { let (x, n) = pair; let y: X = x; (n, y) };
                let { a, b } = swapped;
                let (m, x) = a;
                let (n, y) = b;
                (m + x + n + (y as u32)) as i32
            }''', 17)

    def test_a_map_consumes_each_linear_component_once(self):
        self.run_source('''exposed struct L! { n: u32 }
            fn main() -> i32 {
                let wrapped = foreach t of (L!.{ n: 2u32 }, L!.{ n: 3u32 }) { (t, 1u8) };
                let back = foreach w of wrapped { let (t, k) = w; t };
                let (x, y) = back;
                let { n: m } = x;
                let { n } = y;
                (m + n) as i32
            }''', 5)

    def test_a_map_of_a_sum_maps_the_variant_it_holds(self):
        self.run_source('''exposed enum E { A(u32), B(u8) }
            fn main() -> i32 {
                let parts = foreach x of E::B.(7u8) { (x, 1u32) };
                let flipped = foreach p of parts { let (x, n) = p; (n, x) };
                match flipped {
                    A(p) => { let (n, x) = p; 0i32 },
                    B(p) => { let (n, x) = p; (n + (x as u32)) as i32 },
                }
            }''', 8)


if __name__ == "__main__":
    unittest.main()
