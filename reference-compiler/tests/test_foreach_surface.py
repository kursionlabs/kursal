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


if __name__ == "__main__":
    unittest.main()
