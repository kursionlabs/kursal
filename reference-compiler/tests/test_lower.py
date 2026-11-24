"""Lowering, end to end: small programs are compiled from source to C, built
with the system C compiler, and run. Each program computes a checksum in its
exit code, which matches the expected value only if every construct behaved
as specified."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kursal.check import check_program
from kursal.cc import cc_command
from kursal.cgen import emit_c
from kursal.lower import lower
from kursal.resolve import LIB, resolve
from kursal.sigs import signatures
from kursal.sexpr import KursError


MANIFEST = '[package]\nname="t"\nkind="executable"\nentry="package::main"\nsource_root="."\n'
# The dependency of a test package that names `core::`, where the builtins are.
CORE = f'[dependencies.core]\npath="{LIB / "core"}"\n'

OPTION = "forall T. exposed enum Option { Some(T), None }\n"
ITER = "use package::option::Option;\nforall Self -> Item.\ntrait Iterator { fn next(&it: Self) -> Option[Item]; }\n"

FSTRING_BRANCHES = """
use std::slice::{StaticRead, bytes_eq};
fn main() -> i32 {
    var n = 5u32;
    let msg = f"{if n == 5 { 7u32 } else { 8u32 }}:{match n { 5 => 9u32, _ => 10u32 }}:{&n}:{n}";
    var exists 'opened. (tag, bytes, restore) = msg.open();
    var sr = StaticRead::new();
    let good = bytes_eq(&tag, bytes, &sr, "7:9:5:5");
    restore.restore(tag).destroy();
    if good && n == 5 { 0i32 } else { 1i32 }
}
"""

PROGRAMS = {
    "operand_order": {"lib.kur": """
fn bump(&x: u32) -> u32 { x = x + 1; x }
fn combine(a: u32, b: u32) -> u32 { a * 10 + b }
fn select(n: u32) -> fn(u32, u32) -> u32 {
    if n == 1 { combine } else { combine }
}
fn main() -> i32 {
    var n = 1u32;
    let bumped = bump(&n);
    let after = n;
    let a = combine(10, if true { 3 } else { 4 });
    let f = select(1);
    let b = f(4, if true { 5 } else { 6 });
    let c = combine(bump(&n), if true { n } else { 0 });
    ((after * 10 + bumped + a + b + c) as i32)
}
"""},
    "arithmetic": {"lib.kur": """
fn main() -> i32 {
    let a = 250u8;
    let b = a + 10u8;
    let c = (b as u32) * 1000u32;
    let d = -7i32 / 2i32;
    let e = -7i32 % 2i32;
    let f = 1u32 << 33u32;
    let g = !0u32 >> 28u32;
    let h = (0i64 - 5i64) >> 1i64;
    let m = -2147483648i32 / -1i32;
    let x = (c as i32) + d + e + (f as i32) + (g as i32) + (h as i32) + (m % 7i32);
    x % 251i32
}
fn big(a: u64) -> u64 { a * 6364136223846793005u64 + 1442695040888963407u64 }
"""},
    "loops": {"lib.kur": """
fn collatz(n: u32) -> u32 {
    var x = n;
    var steps = 0u32;
    while x != 1 {
        if x % 2 == 0 { x = x / 2; } else { x = 3 * x + 1; }
        steps = steps + 1;
    }
    steps
}
fn find(limit: u32) -> u32 {
    var i = 2u32;
    while i < limit {
        if i * i > limit { return i; }
        i = i + 1;
    }
    0
}
fn nested(n: u32) -> u32 {
    var total = 0u32;
    var i = 0u32;
    while i < n {
        var j = 0u32;
        while j < i {
            total = total + i * j;
            j = j + 1;
        }
        i = i + 1;
    }
    total
}
fn main() -> i32 { (((collatz(27) % 100 + find(200) + nested(9)) % 251) as i32) }
"""},
    "match": {"lib.kur": """
exposed enum Shape { Circle(u32), Rect(u32, u32), Unit }
fn area(s: Shape) -> u32 {
    match s {
        Shape::Circle(r) => r * r * 3,
        Shape::Rect(w, h) => w * h,
        Shape::Unit => 1,
    }
}
fn kind(n: u32) -> u32 {
    match n { 0 => 10, 1..=9 => 20, 10 | 20 | 30 => 30, _ => 40 }
}
fn neg(n: i32) -> u32 {
    match n { -5..=-1 => 1, 0 => 2, _ => 3 }
}
fn main() -> i32 {
    let a = area(Shape::Circle.(2)) + area(Shape::Rect.(3, 4)) + area(Shape::Unit);
    let k = kind(0) + kind(5) + kind(20) + kind(99);
    ((a + k + neg(-3) + neg(0) + neg(9)) as i32)
}
"""},
    "aggregates": {"lib.kur": """
exposed struct Token! { count: u32, id: u32, tag: u32 }
fn bump(t: Token!) -> Token! {
    let { count, ..rest } = t;
    Token!.{ count: count + 1, ..rest }
}
fn swap(p: (u32, u32)) -> (u32, u32) { let (a, b) = p; (b, a) }
forall T. fn first(p: (T, u32)) -> T { let (a, b) = p; a }
exposed struct Pt { x: u32, y: u32 }
exposed struct Line { a: Pt, b: Pt }
fn main() -> i32 {
    let t = bump(bump(Token!.{ count: 1, id: 5, tag: 7 }));
    let { count, id, tag } = t;
    let (x, y) = swap((count, id));
    let pair = ((1u32, 2u32), (3u32, 4u32));
    let (l1, r1) = pair;
    let (a, b) = l1;
    var (c, d) = r1;
    let l = Line.{ a: Pt.{ x: 1, y: 2 }, b: Pt.{ x: 30, y: 40 } };
    let arr = [a, b, c, d];
    let [p, q, r, s] = arr;
    let w = l.b.y + l.a.x + first((9u32, 1u32));
    ((x * 10 + y + tag + a + b + c + d + w + p + q + r + s) as i32)
}
"""},
    "try": {"lib.kur": """
use core::option::Result;
fn half(n: u32) -> Result[u32, u32] {
    if n % 2 == 0 { Result::Ok.(n / 2) } else { Result::Err.(n) }
}
fn chain(n: u32) -> Result[u32, u32] {
    let a = half(n)?;
    let b = half(a)?;
    Result::Ok.(a + b)
}
fn code(r: Result[u32, u32]) -> u32 {
    match r { Result::Ok(v) => v, Result::Err(e) => 100 + e }
}
fn main() -> i32 { ((code(chain(12)) + code(chain(6)) + code(chain(7))) as i32) }
"""},
    "traits": {"lib.kur": """
forall Self. trait Scale { fn scale(self: Self, f: u32) -> Self; }
exposed struct Cube { side: u32 }
instance Scale[Cube] {
    fn scale(self: Cube, f: u32) -> Cube { let { side: s } = self; Cube.{ side: s * f } }
}
instance Scale[u32] { fn scale(self: u32, f: u32) -> u32 { self * f } }
forall T where Scale[T]. fn twice(x: T, f: u32) -> T { let once = x.scale(f); once.scale(f) }
impl Cube { fn volume(self) -> u32 { self.side * self.side * self.side } }
forall A, B. trait Pairing { fn pair(a: A, b: B) -> u32; }
instance Pairing[u32, bool] { fn pair(a: u32, b: bool) -> u32 { if b { a } else { 0 } } }
fn main() -> i32 {
    var c = twice(Cube.{ side: 2 }, 3);
    let v = c.volume();
    let n = twice(5u32, 2);
    var p = Pairing[u32, bool]::pair(7, true);
    (((v + n + p) % 251) as i32)
}
"""},
    "places": {"lib.kur": """
exposed struct Cur { pos: u32, steps: u32 }
fn advance(&c: Cur, by: u32) -> u32 {
    let old = c.pos;
    c.pos = old + by;
    c.steps = c.steps + 1;
    old
}
impl Cur {
    fn total(&self) -> u32 { self.pos + self.steps }
    fn reset(&self, to: u32) -> () { self.pos = to; self.steps = 0; }
}
exposed enum Mode { Idle, Run(u32), Stop(u32, u32) }
fn score(&m: Mode) -> u32 {
    match &m {
        Mode::Idle => 0,
        Mode::Run(n) => n,
        Mode::Stop(a, b) => a + b,
    }
}
fn main() -> i32 {
    var c = Cur.{ pos: 1, steps: 0 };
    var i = 0u32;
    var acc = 0u32;
    while i < 5 {
        acc = acc + advance(&c, i);
        i = i + 1;
    }
    let t = &c.total();
    &c.reset(3);
    let u = &c.total();
    var m = Mode::Run.(7);
    let s1 = score(&m);
    m = Mode::Stop.(2, 3);
    let s2 = score(&m);
    (((acc + t + u + s1 + s2) % 251) as i32)
}
"""},
    "linear_places": {"lib.kur": """
exposed struct Box! { v: u32, n: u32 }
exposed struct Pool! { free: u32, used: u32 }
impl Pool! {
    fn take(&self) -> u32 {
        let { free, used } = self;
        self = Pool!.{ free: free - 1, used: used + 1 };
        free
    }
    fn size(&self) -> u32 {
        let { free, used } = self;
        self = Pool!.{ free, used };
        free + used
    }
}
fn open(&b: Box!) -> u32 { let { v, n } = b; b = Box!.{ v: v + 1, n }; v + n }
fn main() -> i32 {
    var p = Pool!.{ free: 10, used: 0 };
    var b = Box!.{ v: 4, n: 5 };
    var i = 0u32;
    var sum = 0u32;
    while i < 4 {
        sum = sum + &p.take() + open(&b);
        i = i + 1;
    }
    let size = &p.size();
    let { free, used } = p;
    let { v, n } = b;
    (((sum + size + free + used + v + n) % 251) as i32)
}
"""},
    "for": {"option.kur": OPTION, "iter.kur": ITER, "lib.kur": """
use package::iter::Iterator;
use package::option::Option;
exposed struct Count { n: u32, end: u32 }
instance Iterator[Count -> u32] {
    fn next(&it: Count) -> Option[u32] {
        if it.n >= it.end { Option::None } else {
            let v = it.n;
            it = Count.{ n: v + 1, end: it.end };
            Option::Some.(v)
        }
    }
}
fn main() -> i32 {
    var it = Count.{ n: 0, end: 6 };
    var s = 0u32;
    for x in &it { s = s + x * x; }
    var j = Count.{ n: 3, end: 5 };
    for y in &j { s = s + y; }
    ((s % 251) as i32)
}
"""},
    "brands": {"lib.kur": """
forall 'k. exposed struct Tag! { marker: core::cap::Brand['k] }
fn mint() -> exists 'r. Tag!['r] {
    let exists 'a. w: core::cap::Brand['a] = core::cap::fresh();
    pack 'a. Tag!.{ marker: w }
}
forall 'k. fn read(t: Tag!['k]) -> (Tag!['k], u32) { (t, 5) }
fn use_tag(p: exists 'r. Tag!['r]) -> u32 {
    var exists 'x. t = p;
    let (t2, n) = read(t);
    let { marker } = t2;
    n
}
fn main() -> i32 { ((use_tag(mint()) + use_tag(mint())) as i32) }
"""},
    "temporaries": {"lib.kur": """
fn add3(a: u32, b: u32, c: u32) -> u32 { a * 100 + b * 10 + c }
fn pick(x: u32) -> u32 { if x > 3 { 1 } else { 2 } }
fn main() -> i32 {
    let x = 5u32;
    let y = 2u32;
    let a = add3(pick(x), match y { 0 => 7, 1..=3 => 4, _ => 9 }, if x > 1 && y > 1 { 3 } else { 8 });
    var t = (if x > 9 { 1u32 } else { 2u32 }, match y { 2 => 5u32, _ => 6u32 }, x);
    let (p, q, r) = t;
    (((a + p + q + r) % 251u32) as i32)
}
"""},
    "lend_order": {"lib.kur": """
exposed struct Tag! { id: u32 }
exposed struct Ctx! { tag: Tag!, count: u32, extra: u32 }
fn bump(&t: Tag!) -> u32 { let { id } = t; t = Tag!.{ id: id + 1 }; id }
fn run(&ctx: Ctx!) -> u32 {
    var { tag, ..rest } = ctx;
    let count = bump(&tag);
    ctx = Ctx!.{ tag, count, ..rest };
    let { tag, count, extra } = ctx;
    ctx = Ctx!.{ tag, count: count + 1, extra };
    count
}
fn main() -> i32 {
    var c = Ctx!.{ tag: Tag!.{ id: 7 }, count: 0, extra: 9 };
    let a = run(&c);
    let b = run(&c);
    let { tag, count, extra } = c;
    let { id } = tag;
    (((a + b + id * 10 + count * 100 + extra) % 251u32) as i32)
}
"""},
    "closures": {"lib.kur": """
exposed struct Cur { n: u32 }
exposed struct Tok! { id: u32 }
forall Env where Copy[Env].
fn twice(f: closure[Env](u32) -> u32, x: u32) -> u32 { f(f(x)) }
forall Env.
fn with_cur(&c: Cur, f: closure[Env](&_: Cur) -> u32) -> u32 { f(&c) }
forall Env.
fn once(f: closure[Env](u32) -> u32, x: u32) -> u32 { f(x) }
fn adder(base: u32) -> u32 {
    let add = |x: u32| x + base;
    add(5u32)
}
fn main() -> i32 {
    let k = 10u32;
    let a = twice(|x| x * 2 + k, 3);
    var c = Cur.{ n: 7 };
    let b = with_cur(&c, |&cur| -> u32 { cur.n = cur.n + 1; cur.n * 2 });
    let t = Tok!.{ id: 4 };
    let d = once(|x: u32| -> u32 { let { id } = t; id + x }, 3);
    let { n } = c;
    (((a + b + d + adder(1) + n) % 251) as i32)
}
"""},
    "fn_values": {"lib.kur": """
exposed struct Cur { n: u32 }
fn bump(&c: Cur, by: u32) -> u32 { let old = c.n; c.n = old + by; old }
fn plain(x: u32) -> u32 { x * 3 }
forall Env where Copy[Env]. fn run(f: closure[Env](Cur, u32) -> (Cur, u32), g: fn(u32) -> u32, c: Cur) -> u32 {
    let (c2, a) = f(c, 2);
    let (c3, b) = f(c2, 3);
    a + b + c3.n + g(4)
}
fn main() -> i32 {
    (run(|c: Cur, by: u32| { var cur = c; let result = bump(&cur, by); (cur, result) },
         plain, Cur.{ n: 1 }) as i32)
}
"""},
    "foreach": {"lib.kur": """
forall T. exposed struct W { v: T }
forall T. fn wrap(x: T) -> W[T] { W.{ v: x } }
exposed struct S { a: u32, b: u32 }
fn main() -> i32 {
    let s = S.{ a: 7u32, b: 9u32 };
    let parts = foreach f of s { wrap(f) };
    let { a: x, b: y } = parts;
    let { v: p } = x;
    let { v: q } = y;
    ((p * 10 + q) as i32)
}
"""},
    "function_entries": {"lib.kur": """
exposed struct Cur { n: u32 }
exposed struct Pair { a: u32, b: u32 }
fn add(amount: u32, &c: Cur) -> u32 { c.n = c.n + amount; c.n }
fn main() -> i32 {
    var c = Cur.{ n: 1 };
    let bias = 4u32;
    let f = |amount: u32, &cur: Cur| -> u32 { add(amount + bias, &cur) };
    let x = f(2, &c);
    let p = Pair.{ a: 2, b: 3 };
    let mapped = foreach item of p { (item, bias) };
    let { a, b } = mapped;
    let (av, ac) = a;
    let (bv, bc) = b;
    ((x + c.n + av + bv + ac + bc) as i32)
}
"""},
    "capture_order": {"lib.kur": """
fn main() -> i32 {
    let a = 3u32;
    let b = 7u32;
    let outer = |x: u32| {
        let a = 10u32;
        let inner = |y: u32| b * 100 + a * 10 + x + y;
        inner(2) + b
    };
    let mapped = foreach item of (1u32, 2u32) { (item, b, a, b) };
    let (left, right) = mapped;
    let (p, q, r, s) = left;
    (((outer(a) + p + q + r + s) % 251u32) as i32)
}
"""},
    "direct_try": {"lib.kur": """
use core::option::{Option, Result};
exposed struct Cur { n: u32 }
fn get(o: Option[(u32, bool)], &c: Cur) -> Option[u32] {
    c.n = c.n + 1;
    let (n, b) = o?;
    c.n = c.n + 10;
    Option::Some.(if b { n } else { 0 })
}
fn code(o: Option[u32]) -> u32 { match o { Option::Some(n) => n, Option::None => 100 } }
fn transform(r: Result[u32, u32]) -> Result[bool, u32] { let n = r?; Result::Ok.(n == 0) }
fn score(r: Result[bool, u32]) -> u32 {
    match r { Result::Ok(b) => if b { 1 } else { 2 }, Result::Err(n) => n + 10 }
}
fn main() -> i32 {
    var c = Cur.{ n: 0 };
    let a = code(get(Option::Some.((3, true)), &c));
    let b = code(get(Option::None, &c));
    ((a + b + c.n + score(transform(Result::Ok.(0))) + score(transform(Result::Err.(7)))) as i32)
}
"""},
    "unsafe_pointers": {"lib.kur": """
extern "C" fn malloc(size: u64) -> *u8;
extern "C" fn free(p: *u8) -> ();
exposed struct Pair { a: u32, b: u32 }
fn main() -> i32 {
    let raw = unsafe malloc(8u64);
    let p = unsafe (raw as *Pair);
    unsafe *p = Pair.{ a: 20, b: 22 };
    let v = unsafe *p;
    let { a, b } = v;
    let w = unsafe free(raw);
    ((a + b) as i32)
}
"""},
}


# The exit code each program in PROGRAMS must produce.
CHECKSUMS = {
    "operand_order": 203,
    "arithmetic": 243,
    "loops": 70,
    "match": 131,
    "aggregates": 130,
    "try": 219,
    "traits": 86,
    "places": 46,
    "linear_places": 109,
    "for": 62,
    "brands": 10,
    "temporaries": 155,
    "lend_order": 10,
    "closures": 79,
    "fn_values": 22,
    "foreach": 79,
    "function_entries": 27,
    "capture_order": 77,
    "direct_try": 133,
    "unsafe_pointers": 42,
}


def write(directory, files, core=False):
    """A test package of `files`; it depends on `core` when asked to or when a file names it."""
    core = core or any("core::" in text for text in files.values())
    (directory / "kur.toml").write_text(MANIFEST + (CORE if core else ""))
    for name, text in files.items():
        (directory / name).write_text(text)


def compile_and_run(c_file, out):
    built = subprocess.run(cc_command([str(c_file)], str(out)), capture_output=True, text=True)
    if built.returncode:
        return "cc failed: " + built.stderr[:300]
    return subprocess.run([str(out)], capture_output=True, timeout=20).returncode


class LoweringTests(unittest.TestCase):
    @unittest.skipUnless((LIB / "std" / "kur.toml").exists(), "needs the standard library")
    def test_fstring_writer_survives_branching_and_lent_interpolations(self):
        with tempfile.TemporaryDirectory(prefix="kursal-fstring-") as base:
            directory = Path(base)
            write(directory, {"lib.kur": FSTRING_BRANCHES})
            with (directory / "kur.toml").open("a") as manifest:
                manifest.write(f'[dependencies.std]\npath="{LIB / "std"}"\n')
            resolver = resolve(directory)
            sigs = signatures(resolver)
            program = lower(resolver, sigs)
            c_file = directory / "main.c"
            c_file.write_text(emit_c(program, check_program(program), sigs.name(resolver.packages[0].entry_decl)))
            self.assertEqual(compile_and_run(c_file, directory / "main"), 0)

    @unittest.skipUnless((LIB / "std" / "kur.toml").exists(), "needs the standard library")
    def test_an_fstring_rejects_a_linear_value_it_would_drop_where_it_is_written(self):
        with tempfile.TemporaryDirectory(prefix="kursal-fstring-") as base:
            path = Path(base) / "fstr_lin.kur"
            path.write_text("use std::buffer::Buffer!;\nuse std::io::print_buffer;\nfn main() {\n"
                            "    let name = Buffer!::new().push_static(\"x\");\n    print_buffer(f\"hi {name}\\n\");\n}\n")
            resolver = resolve(path)
            with self.assertRaisesRegex(KursError, r"fstr_lin\.kur:5:24: \{x\} in an f-string drops a linear value; "
                                                   r"lend it with \{&x\}$"):
                lower(resolver, signatures(resolver))

    def test_match_payloads_bind_values_and_alias_places(self):
        source = """
exposed enum E { Pair(u32, u32), Single(u32), Unit }
fn whole(e: E) -> u32 {
    match e {
        E::Pair(pair) => { let (a, b) = pair; a * 10 + b },
        E::Single(x) => x,
        E::Unit => 0,
    }
}
fn change(&e: E) -> u32 {
    match &e {
        E::Pair(pair) => { let (a, b) = pair; pair = (a + 1, b + 2); a + b },
        E::Single(x) => { x = x + 3; x },
        E::Unit => 0,
    }
}
fn split(&e: E) -> u32 {
    match &e { E::Pair(a, _) => { a = a + 4; a }, E::Single(_) => 0, E::Unit => 0 }
}
fn first(e: E) -> u32 {
    match e { E::Pair(a, _) => a, E::Single(_) => 0, E::Unit => 0 }
}
fn main() -> i32 {
    var e = E::Pair.(2, 3);
    let a = whole(e);
    let b = change(&e);
    let c = split(&e);
    let d = whole(e);
    let f = first(e);
    var single = E::Single.(1);
    var unit = E::Unit;
    ((a + b + c + d + f + change(&single) + change(&unit) + whole(unit)) as i32)
}
"""
        with tempfile.TemporaryDirectory(prefix="kursal-match-payload-") as base:
            directory = Path(base)
            write(directory, {"lib.kur": source})
            resolver = resolve(directory)
            sigs = signatures(resolver)
            program = lower(resolver, sigs)
            c_file = directory / "main.c"
            c_file.write_text(emit_c(program, check_program(program), sigs.name(resolver.packages[0].entry_decl)))
            self.assertEqual(compile_and_run(c_file, directory / "main"), 121)

    def test_named_lending_function_values_preserve_pointer_calls(self):
        source = """
exposed struct Cur { n: u32 }
fn bump(&c: Cur, by: u32) -> u32 { let old = c.n; c.n = old + by; old }
fn invoke(f: fn(&_: Cur, u32) -> u32, &c: Cur) -> u32 { f(&c, 2) }
fn main() -> i32 {
    var c = Cur.{ n: 1 };
    let f: fn(&_: Cur, u32) -> u32 = bump;
    let a = invoke(f, &c);
    let b = f(&c, 3);
    ((a + b + c.n) as i32)
}
"""
        with tempfile.TemporaryDirectory(prefix="kursal-fn-lend-") as base:
            directory = Path(base)
            write(directory, {"lib.kur": source})
            resolver = resolve(directory)
            sigs = signatures(resolver)
            program = lower(resolver, sigs)
            self.assertFalse(any("#adapter" in name for name in program.funs))
            c_file = directory / "main.c"
            c_file.write_text(emit_c(program, check_program(program), sigs.name(resolver.packages[0].entry_decl)))
            self.assertEqual(compile_and_run(c_file, directory / "main"), 10)

    def test_trait_implementation_preserves_place_parameters(self):
        """An instance method lends where its trait's does, and nowhere else: the front end rejects one
        that differs (as the checker would its core, test_core_soundness)."""
        for declared, implemented in (("&", ""), ("", "&")):
            with self.subTest(declared=declared, implemented=implemented):
                with tempfile.TemporaryDirectory(prefix="kursal-trait-mode-") as base:
                    directory = Path(base)
                    write(directory, {"lib.kur": f"""
forall Self. trait Step {{ fn step({declared}x: Self) -> u32; }}
instance Step[u32] {{ fn step({implemented}x: u32) -> u32 {{ x + 1 }} }}
fn main() -> i32 {{ 0 }}
"""})
                    resolver = resolve(directory)
                    with self.assertRaisesRegex(KursError, "method step does not match its declaration"):
                        signatures(resolver)

    def test_programs_exit_with_their_checksums(self):
        sys.setrecursionlimit(20000)
        with tempfile.TemporaryDirectory(prefix="kursal-lower-") as base:
            for name, files in PROGRAMS.items():
                with self.subTest(program=name):
                    directory = Path(base) / name
                    directory.mkdir()
                    write(directory, files)
                    resolver = resolve(directory)
                    sigs = signatures(resolver)
                    program = lower(resolver, sigs)
                    checker = check_program(program)
                    c_file = directory / "main.c"
                    c_file.write_text(emit_c(program, checker, sigs.name(resolver.packages[0].entry_decl)))
                    self.assertEqual(compile_and_run(c_file, directory / "main"), CHECKSUMS[name])


if __name__ == "__main__":
    unittest.main()
