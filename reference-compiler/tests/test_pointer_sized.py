"""`usize` and `isize` are pointer-sized: the C names them `uintptr_t` and `intptr_t`, and leaves their
constant arithmetic to the C compiler, which folds it at the target's width. The same C is right on a
32-bit target, where it is built and run when the C compiler can build for one."""

import functools
import subprocess
import tempfile
import unittest
from pathlib import Path

from kursal import core as C
from kursal.cc import cc_command
from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.lower import lower
from kursal.readable import emit_kurs
from kursal.resolve import resolve
from kursal.sigs import SigError, signatures
from tests.test_lower import compile_and_run, write

USIZE, ISIZE, U32 = C.Prim("usize"), C.Prim("isize"), C.Prim("u32")

# Every operation on `usize` and `isize`, checked against the width the program finds at run time.
WIDTH_CHECKS = '''use core::mem::sizeof;
const MAX: usize = !0usize;
const IMAX: isize = (MAX >> 1usize) as isize;
const IMIN: isize = -IMAX - 1isize;
const WRAPS: bool = MAX + 1usize == 0usize;
const HIGH: usize = 1usize << 31usize << 1usize;
const BIG: usize = 0x1_0000_0005u64 as usize;
const MINUS_TWO: i64 = (0isize - 2isize) as i64;
const ALL: i64 = MAX as i64;
const SHIFTED: isize = IMIN >> 3isize;
const BOTH: bool = WRAPS && HIGH != 0usize || !(MAX > 0usize);

fn run(bits: usize, wide: bool) -> i32 {
    var maximum = 0usize;
    var i = 0usize;
    while i < bits { maximum = (maximum << 1usize) | 1usize; i = i + 1usize; }
    if MAX != maximum { return 1i32; }
    if (MAX >> (bits - 1usize)) != 1usize { return 2i32; }
    if IMAX + IMIN != -1isize || IMIN >= 0isize || (IMAX as usize) != MAX / 2usize { return 3i32; }
    if !WRAPS || BOTH != wide { return 4i32; }
    if wide != (HIGH != 0usize) { return 5i32; }
    if BIG != (if wide { 0x1_0000_0005usize } else { 5usize }) { return 6i32; }
    if MINUS_TWO != -2i64 { return 7i32; }
    if ALL != (if wide { -1i64 } else { 4294967295i64 }) { return 8i32; }
    if SHIFTED >= 0isize || SHIFTED != IMIN / 8isize { return 9i32; }
    if (1usize << bits) != 1usize || (MAX + MAX) != MAX - 1usize { return 10i32; }
    0i32
}

fn main() -> i32 {
    let bits = sizeof[usize]() * 8usize;
    if bits != 32usize && bits != 64usize { return 20i32; }
    run(bits, bits == 64usize)
}
'''


def build(source: str) -> tuple[object, str, str]:
    """The signatures, readable KURS and C of a package of `source`."""
    with tempfile.TemporaryDirectory(prefix="kursal-pointer-sized-") as base:
        directory = Path(base)
        write(directory, {"lib.kur": source})
        resolver = resolve(directory)
        sigs = signatures(resolver)
        program = lower(resolver, sigs)
        checker = check_program(program)
        c = emit_c(program, checker, sigs.name(resolver.packages[0].entry_decl))
        return sigs, emit_kurs(program), c


def const(sigs, name):
    return next(v for d, v in sigs.consts.items() if d.name == name)


@functools.cache
def builds_32_bit() -> bool:
    with tempfile.TemporaryDirectory(prefix="kursal-m32-") as base:
        source = Path(base) / "probe.c"
        source.write_text("#include <stdint.h>\nint main(void) { return sizeof(uintptr_t) == 4 ? 0 : 1; }\n")
        out = Path(base) / "probe"
        try:
            built = subprocess.run(cc_command([str(source)], str(out), "-m32"), capture_output=True)
            return built.returncode == 0 and subprocess.run([str(out)]).returncode == 0
        except OSError:
            return False


class PointerSizedTests(unittest.TestCase):
    def test_c_types_are_pointer_sized(self):
        _, _, c = build("fn f(a: usize, b: isize) -> usize { a + (b as usize) }\n"
                        "fn main() -> i32 { (f(2usize, 3isize) as i32) - 5i32 }\n")
        self.assertRegex(c, r"static uintptr_t \w*f\w*\(uintptr_t \w+, intptr_t \w+\)")
        self.assertNotIn("uint64_t a", c)
        self.assertIn("kur_dec_isize", c)

    def test_constant_arithmetic_is_left_to_the_c_compiler(self):
        sigs, kurs, c = build("const N: usize = 3usize * 5usize;\nconst M: u32 = (N as u32) + 1u32;\n"
                              "const K: u32 = 3u32 * 5u32;\nconst ONE: usize = 1usize;\n"
                              "fn main() -> i32 { (M as i32) - (K as i32) - (ONE as i32) }\n")
        n = ("op", ("mul", (("int", 3, USIZE), ("int", 5, USIZE))), USIZE)
        self.assertEqual(const(sigs, "N"), n)
        self.assertEqual(const(sigs, "M"), ("op", ("add", (("op", ("int_cast", (n,)), U32), ("int", 1, U32))), U32))
        self.assertEqual(const(sigs, "K"), ("int", 15, U32))
        self.assertEqual(const(sigs, "ONE"), ("int", 1, USIZE))
        # N is 3 * 5 in the KURS and in the C; K is folded.
        self.assertRegex(kurs, r"%(\w+) = const usize, 3;\n\s*%(\w+) = const usize, 5;\n\s*%\w+ = mul %\1, %\2;")
        self.assertRegex(c, r"(\w+) = UINT64_C\(3\);\n\s*(\w+) = UINT64_C\(5\);\n"
                            r"\s*\w+ = \(uintptr_t\)\(\(uintptr_t\)\(\1\) \* \(uintptr_t\)\(\2\)\);")
        self.assertEqual(c.count("UINT64_C(15)"), 1)

    def test_division_by_a_literal_zero_is_still_an_error(self):
        for source in ("const A: usize = 1usize / 0usize;", "const A: isize = (5isize % 0isize) + 1isize;"):
            with self.subTest(source=source), self.assertRaisesRegex(SigError, "constant division by zero"):
                build(source + "\nfn main() -> i32 { 0i32 }\n")

    def test_array_length_cannot_use_pointer_sized_arithmetic(self):
        with self.assertRaisesRegex(SigError, "an array length cannot use arithmetic on usize or isize"):
            build("const N: usize = 2usize * 2usize;\nfn f(a: [u8; N]) -> u8 { 0u8 }\nfn main() -> i32 { 0i32 }\n")
        sigs, _, _ = build("const N: usize = 4usize;\nfn f(a: [u8; N], b: [u8; 2u64 * 3u64]) -> u8 { 0u8 }\n"
                           "fn main() -> i32 { 0i32 }\n")
        f = next(s for d, s in sigs.sigs.items() if d.name == "f")
        self.assertEqual(f.ptypes, (C.Array(C.Prim("u8"), 4), C.Array(C.Prim("u8"), 6)))

    def test_widths_at_run_time(self):
        _, _, c = build(WIDTH_CHECKS)
        with tempfile.TemporaryDirectory(prefix="kursal-pointer-sized-") as base:
            path = Path(base) / "program.c"
            path.write_text(c)
            self.assertEqual(compile_and_run(path, Path(base) / "program"), 0)

    def test_the_same_c_runs_on_a_32_bit_target(self):
        if not builds_32_bit():
            self.skipTest("the C compiler does not build for a 32-bit target (-m32)")
        _, _, c = build(WIDTH_CHECKS)
        with tempfile.TemporaryDirectory(prefix="kursal-pointer-sized-") as base:
            path, out = Path(base) / "program.c", Path(base) / "program"
            path.write_text(c)
            built = subprocess.run(cc_command([str(path)], str(out), "-m32"), capture_output=True, text=True)
            self.assertEqual(built.returncode, 0, built.stderr[:300])
            self.assertEqual(subprocess.run([str(out)], timeout=20).returncode, 0)


if __name__ == "__main__":
    unittest.main()
