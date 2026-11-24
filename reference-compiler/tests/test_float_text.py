"""Floats at run time: `as` between numbers, `core::num`'s float functions, and `core::fmt`'s shortest
round-trip formatting, checked against Python's `repr` (itself the shortest round trip) and read back
through `std::float::parse_f64`."""

import random
import struct
import unittest
from decimal import Decimal
from fractions import Fraction

from tests import test_std_runtime


def layout(digits: str, point: int) -> str:
    """The positive decimal 0.DIGITS * 10^point as `core::fmt` lays it out (ECMAScript's
    `Number::toString`, with a bare exponent)."""
    n = len(digits)
    if n <= point <= 21:
        return digits + "0" * (point - n)
    if 0 < point <= 21:
        return digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return "0." + "0" * -point + digits
    return digits[0] + ("." + digits[1:] if n > 1 else "") + "e" + str(point - 1)


def decompose(value: Decimal) -> tuple[str, int]:
    """The significant digits of a positive decimal, and where its point goes."""
    _, ds, exp = value.normalize().as_tuple()
    assert isinstance(exp, int)
    digits = "".join(map(str, ds))
    return digits, exp + len(digits)


def special(text: str, negative: bool) -> str:
    return ("-" if negative else "") + text


def expected_f64(bits: int) -> str:
    x = struct.unpack("<d", struct.pack("<Q", bits))[0]
    negative = bits >> 63 == 1
    if x != x:
        return "NaN"
    if abs(x) == float("inf"):
        return special("inf", negative)
    if x == 0:
        return special("0", negative)
    return special(layout(*decompose(Decimal(repr(abs(x))))), negative)


def f32_value(bits: int) -> float:
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def nearest_f32(q: Fraction) -> int:
    """The bits of the `f32` nearest the positive rational `q`, ties to even; 0x7f800000 past the
    largest."""
    lo, hi = 0, 0x7F800000
    while hi - lo > 1:                     # the largest f32 not above q
        mid = (lo + hi) // 2
        if Fraction(f32_value(mid)) <= q:
            lo = mid
        else:
            hi = mid
    below, above = Fraction(f32_value(lo)), (Fraction(f32_value(hi)) if hi < 0x7F800000 else None)
    if above is None:
        top = Fraction(f32_value(0x7F7FFFFF))
        return lo if q - top < (Fraction(2) ** 104) else 0x7F800000
    if q - below != above - q:
        return lo if q - below < above - q else hi
    return lo if lo % 2 == 0 else hi


def expected_f32(bits: int) -> str:
    """The shortest decimal that rounds to the `f32` of `bits`, and of those the nearest."""
    x = f32_value(bits)
    negative = bits >> 31 == 1
    if x != x:
        return "NaN"
    if abs(x) == float("inf"):
        return special("inf", negative)
    if x == 0:
        return special("0", negative)
    magnitude = bits & 0x7FFFFFFF
    v = Fraction(abs(x))
    for precision in range(1, 10):
        _, ds, exp = Decimal(f"{abs(x):.{precision - 1}e}").as_tuple()
        assert isinstance(exp, int)
        m = int("".join(map(str, ds)))
        fits = [(abs(Fraction(c) * Fraction(10) ** exp - v), c % 2, c) for c in (m - 1, m, m + 1)
                if c > 0 and nearest_f32(Fraction(c) * Fraction(10) ** exp) == magnitude]
        if fits:
            c = min(fits)[2]
            return special(layout(*decompose(Decimal(c).scaleb(exp))), negative)
    raise AssertionError(hex(bits))


# A program that prints `d BITS TEXT` for many doubles and `f BITS TEXT` for many floats, TEXT being
# `{x}`, and reads every TEXT back through `parse_f64`, counting those that do not give the same bits.
FORMAT_PROGRAM = '''
use std::buffer::Buffer!;
use std::float::parse_f64;
use std::io::print_buffer;
use std::option::Option;

// TEXT read back: 0 when it gives `bits` again (some NaN for a NaN), else 1.
fn misread(text: Buffer!, bits: u64, single: bool) -> u32 {
    var exists 'b. (tag, bytes, restore) = text.open();
    let parsed = parse_f64(&tag, bytes);
    restore.restore(tag).destroy();
    let x = match parsed {
        Option::Some(x) => x,
        Option::None => return 1u32,
    };
    let same = if single {
        let y = x.to_f32();
        y.to_bits() == (bits as u32) || y.is_nan() && f32::from_bits((bits as u32)).is_nan()
    } else {
        x.to_bits() == bits || x.is_nan() && f64::from_bits(bits).is_nan()
    };
    if same { 0u32 } else { 1u32 }
}

fn double(out: Buffer!, bits: u64) -> (Buffer!, u32) {
    let x = f64::from_bits(bits);
    (out.push_buffer(f"d {bits} {x}\\n"), misread(f"{x}", bits, false))
}

fn single(out: Buffer!, bits: u32) -> (Buffer!, u32) {
    let x = f32::from_bits(bits);
    (out.push_buffer(f"f {bits} {x}\\n"), misread(f"{x}", (bits as u64), true))
}

fn parsed(text: Buffer!) -> f64 {
    var exists 'b. (tag, bytes, restore) = text.open();
    let x = parse_f64(&tag, bytes);
    restore.restore(tag).destroy();
    match x {
        Option::Some(x) => x,
        Option::None => 0.0,
    }
}

fn main() -> i32 {
    var out = Buffer!::new();
    var bad = 0u32;
    // Every binade's smallest, next, middle and largest, both signs.
    var e = 0u64;
    while e < 2048u64 {
        let fractions = (0u64, 1u64, 2251799813685248u64, 4503599627370495u64);
        let (o1, b1) = double(out, (e << 52u64) | fractions.0);
        let (o2, b2) = double(o1, (e << 52u64) | fractions.1);
        let (o3, b3) = double(o2, (e << 52u64) | fractions.2);
        let (o4, b4) = double(o3, (e << 52u64) | fractions.3 | 9223372036854775808u64);
        out = o4;
        bad = bad + b1 + b2 + b3 + b4;
        e = e + 1u64;
    }
    var e32 = 0u32;
    while e32 < 256u32 {
        let (o1, b1) = single(out, (e32 << 23u32));
        let (o2, b2) = single(o1, (e32 << 23u32) | 1u32);
        let (o3, b3) = single(o2, (e32 << 23u32) | 4194304u32);
        let (o4, b4) = single(o3, (e32 << 23u32) | 8388607u32 | 2147483648u32);
        out = o4;
        bad = bad + b1 + b2 + b3 + b4;
        e32 = e32 + 1u32;
    }
    // Powers of ten and their neighbours, and the halves between them.
    var k = -330i32;
    while k <= 310i32 {
        let ten = parsed(f"1e{k}");
        let five = parsed(f"5e{k}");
        let bits = ten.to_bits();
        let (o1, b1) = double(out, bits);
        let (o2, b2) = double(o1, bits + 1u64);
        let (o3, b3) = double(o2, bits - 1u64);
        let (o4, b4) = double(o3, five.to_bits());
        let single_bits = ten.to_f32().to_bits();
        let (o5, b5) = single(o4, single_bits);
        let (o6, b6) = single(o5, single_bits + 1u32);
        let (o7, b7) = single(o6, single_bits - 1u32);
        out = o7;
        bad = bad + b1 + b2 + b3 + b4 + b5 + b6 + b7;
        k = k + 1i32;
    }
    // Small integers, and around 2^53 and 2^24.
    var n = 0u64;
    while n < 1100u64 {
        let (o1, b1) = double(out, (n as f64).to_bits());
        let (o2, b2) = double(o1, ((9007199254740992u64 + n) as f64).to_bits());
        let (o3, b3) = single(o2, (n as f32).to_bits());
        let (o4, b4) = single(o3, ((16777216u64 + n) as f32).to_bits());
        out = o4;
        bad = bad + b1 + b2 + b3 + b4;
        n = n + 1u64;
    }
    // Bit patterns from xorshift64.
    var state = 88172645463325252u64;
    var i = 0u32;
    while i < 30000u32 {
        state = state ^ (state << 13u64);
        state = state ^ (state >> 7u64);
        state = state ^ (state << 17u64);
        let (o1, b1) = double(out, state);
        let (o2, b2) = single(o1, (state >> 32u64) as u32);
        out = o2;
        bad = bad + b1 + b2;
        i = i + 1u32;
    }
    print_buffer(out.push_buffer(f"misread {bad}\\n"));
    0i32
}
'''


class FloatTextTests(unittest.TestCase):
    run_program = test_std_runtime.StdRuntimeTests.run_program
    expect = test_std_runtime.StdRuntimeTests.expect

    def test_shortest_round_trip_matches_python(self):
        result = self.run_program(FORMAT_PROGRAM)
        self.assertEqual(result.returncode, 0, result.stderr[:500])
        lines = result.stdout.splitlines()
        self.assertEqual(lines[-1], "misread 0")
        checked = {"d": 0, "f": 0}
        mismatches = []
        for line in lines[:-1]:
            kind, bits, text = line.split(" ")
            want = expected_f64(int(bits)) if kind == "d" else expected_f32(int(bits))
            checked[kind] += 1
            if text != want:
                mismatches.append((kind, hex(int(bits)), text, want))
        self.assertEqual(mismatches[:10], [])
        self.assertGreater(checked["d"], 40000)
        self.assertGreater(checked["f"], 35000)

    def test_python_model_of_the_layout(self):
        # The cases the module notes of `core::fmt` give.
        cases = {0x4004000000000000: "2.5", 0x3FB999999999999A: "0.1", 0x3FD3333333333334: "0.30000000000000004",
                 0x4059000000000000: "100", 0x444B1AE4D6E2EF50: "1e21", 0x4415AF1D78B58C40: "100000000000000000000",
                 0x3EB0C6F7A0B5ED8D: "0.000001", 0x3E7AD7F29ABCAF48: "1e-7", 0x0000000000000001: "5e-324",
                 0x8000000000000000: "-0", 0x7FF0000000000000: "inf", 0xFFF0000000000000: "-inf",
                 0x7FF8000000000000: "NaN", 0x7FEFFFFFFFFFFFFF: "1.7976931348623157e308"}
        for bits, text in cases.items():
            with self.subTest(bits=hex(bits)):
                self.assertEqual(expected_f64(bits), text)
        self.assertEqual(expected_f32(0x3DCCCCCD), "0.1")
        self.assertEqual(expected_f32(0x7F7FFFFF), "3.4028235e38")
        self.assertEqual(expected_f32(0x00000001), "1e-45")
        rng = random.Random(7)
        for _ in range(200):
            bits = rng.getrandbits(32)
            text = expected_f32(bits)
            if text not in ("NaN", "inf", "-inf"):
                self.assertEqual(struct.unpack("<I", struct.pack("<f", float(text)))[0], bits)

    def test_interpolation_and_writers(self):
        self.expect('''
            use std::buffer::Buffer!;
            use std::io::print_buffer;
            fn main() -> i32 {
                let x = 2.5f64;
                let y = 0.1f32;
                let z = 0.1f64 + 0.2f64;
                let (nan, low, zero, big, tiny) = (f64::NAN, f64::NEG_INFINITY, -0.0f64, f32::MAX, f64::MIN_POSITIVE);
                print_buffer(f"{x} {y} {z} {nan} {low} {zero} {big} {tiny}");
                0i32
            }''', stdout="2.5 0.1 0.30000000000000004 NaN -inf -0 3.4028235e38 2.2250738585072014e-308")

    def test_number_functions(self):
        self.expect('''
            use std::buffer::Buffer!;
            use std::io::print_buffer;
            use std::option::Option;
            fn checked(x: f64) -> i64 {
                match x.to_i64_checked() { Option::Some(v) => v, Option::None => 42i64 }
            }
            fn main() -> i32 {
                let a = (2.5f64.trunc(), (-2.5f64).trunc(), (-0.5f64).trunc(), 2.5f64.floor(), (-2.5f64).floor());
                let b = (2.5f64.ceil(), (-2.5f64).ceil(), (-0.5f64).ceil(), 2.5f64.round(), (-2.5f64).round());
                let c = (0.49999999999999994f64.round(), (-0.4f64).round(), 4503599627370497.0f64.floor(), f64::INFINITY.floor());
                let d = (1.5f32.round(), (-1.5f32).floor(), 0.7f32.trunc(), 8388609.0f32.ceil());
                let e = (f64::NAN.is_nan(), f64::INFINITY.is_infinite(), f64::MAX.is_finite(), f64::NAN.is_finite(),
                         (-0.0f64).is_sign_negative(), (-3.0f64).abs(), 3.0f64.copysign(-0.0f64));
                let f = (checked(-9223372036854775808.0f64), checked(9223372036854775808.0f64), checked(f64::NAN),
                         checked(-2.9f64), (f64::NAN as i64), (f64::INFINITY as u32), (f64::NEG_INFINITY as i8));
                let g = (f64::EPSILON == f64::from_bits(4372995238176751616u64), f64::MIN == -f64::MAX,
                         f32::EPSILON == f32::from_bits(872415232u32), f32::MIN_POSITIVE == f32::from_bits(8388608u32),
                         2.5f64.to_bits(), f64::from_bits(4612811918334230528u64), 1.0f32.to_bits());
                print_buffer(f"{a.0} {a.1} {a.2} {a.3} {a.4}|{b.0} {b.1} {b.2} {b.3} {b.4}|{c.0} {c.1} {c.2} {c.3}|");
                print_buffer(f"{d.0} {d.1} {d.2} {d.3}|{e.0} {e.1} {e.2} {e.3} {e.4} {e.5} {e.6}|");
                print_buffer(f"{f.0} {f.1} {f.2} {f.3} {f.4} {f.5} {f.6}|{g.0} {g.1} {g.2} {g.3} {g.4} {g.5} {g.6}");
                0i32
            }''', stdout="2 -2 -0 2 -3|3 -2 -0 3 -3|0 -0 4503599627370497 inf|"
                         "2 -2 0 8388609|true true true false true 3 -3|"
                         "-9223372036854775808 42 42 -2 0 4294967295 -128|"
                         "true true true true 4612811918334230528 2.5 1065353216")


if __name__ == "__main__":
    unittest.main()
