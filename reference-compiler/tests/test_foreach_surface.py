"""A source `foreach` maps each component of a concrete struct, tuple or enum on its own."""

import unittest

from kursal.lower import LowerError
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

    def test_a_body_uses_no_outer_local(self):
        with self.assertRaisesRegex(LowerError, "cannot use the outer local bias"):
            self.run_source('''fn main() -> i32 {
                let bias = 1u32;
                let mapped = foreach item of (3u32, 4u32) { (item, bias) };
                0i32
            }''', 0)

    def test_an_array_is_not_a_source(self):
        with self.assertRaisesRegex(LowerError, "struct, tuple or enum whose shape is known here"):
            self.run_source('''fn main() -> i32 {
                let mapped = foreach item of [1u32, 2u32] { (item, 1u32) };
                0i32
            }''', 0)

    def test_no_premises_to_inherit(self):
        with self.assertRaisesRegex(LowerError, "function with premises"):
            self.run_source('''forall T. trait Show { fn show(x: T) -> u32; }
                forall T where Show[T]. fn walk(x: T) -> () { let mapped = foreach item of (1u32,) { () }; () }
                fn main() -> i32 { 0i32 }''', 0)


if __name__ == "__main__":
    unittest.main()
