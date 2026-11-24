"""Programs the fuzzer (tools/fuzz) found the front end accepting while the KURS checker rejected what it
lowered them to, minimized. The front end now rejects each, with a message about the source; the programs
beside them that it accepts, the checker accepts too. tests/test_compiler.py lowers the same packages
with the self-hosted compiler, which must agree."""

import tempfile
import unittest
from pathlib import Path

from kursal.check import check_program
from kursal.lower import lower
from kursal.resolve import LIB, resolve
from kursal.sexpr import KursError
from kursal.sigs import signatures

TOKEN = """\
exposed struct Token! { n: u32 }
fn make(n: u32) -> Token! { Token!.{ n } }
fn take(t: Token!) -> u32 { let { n } = t; n }
fn peek(&t: Token!) -> u32 { t.n }
exposed enum E! { A(Token!), B(u32) }
fn eat(e: E!) -> u32 { match e { E!::A(t) => take(t), E!::B(n) => n } }
"""

BRANDS = """\
forall T. exposed enum Opt { Some(T), None }
forall 'k. fn same(x: core::cap::Brand['k], y: core::cap::Brand['k]) -> u32 { 1u32 }
forall 'a. fn second(pk: exists 'b. (core::cap::Brand['b], core::cap::Brand['a])) -> core::cap::Brand['a] {
    var exists 'c. (x, y) = pk;
    y
}
"""

STEP = "forall Self. trait Step { fn step(x: Self) -> u32; }\n"
LEND_STEP = "forall Self. trait Step { fn step(&x: Self, y: u32) -> u32; }\n"

# (source, a fragment of the front end's message)
REJECTED = (
    # A binder of `match &e` names a place in `e`'s payload; once `e` is assigned, the variant is no longer
    # known, so reading, lending or assigning the binder is reading another variant's storage.
    (TOKEN + "fn f() -> u32 { var e = E!::B.(1u32); "
     "let r = match &e { E!::A(t) => 0u32, E!::B(n) => { e = E!::A.(make(n)); n } }; eat(e) + r }\n",
     "'e.B' is used after its enum was assigned; match it again"),
    (TOKEN + "fn f() -> u32 { var e = E!::B.(1u32); "
     "let r = match &e { E!::A(t) => 0u32, E!::B(n) => { e = E!::A.(make(n)); n = 3u32; 0u32 } }; eat(e) + r }\n",
     "'e.B' is used after its enum was assigned"),
    (TOKEN + "fn f() -> u32 { var e = E!::A.(make(1u32)); "
     "let r = match &e { E!::A(t) => { var x = eat(e); e = E!::A.(make(x)); peek(&t) }, E!::B(n) => n }; eat(e) + r }\n",
     "'e.A' is used after its enum was assigned"),
    (TOKEN + "fn f(&e: E!) -> u32 { match &e { E!::A(t) => 0u32, E!::B(n) => { e = E!::B.(7u32); n } } }\n",
     "'e.B' is used after its enum was assigned"),
    # An instance's methods are its trait's, each with the trait's signature at the instance's arguments.
    (STEP + "instance Step[u32] { fn step(&x: u32) -> u32 { x + 1u32 } }\n",
     "method step does not match its declaration in trait Step"),
    (LEND_STEP + "instance Step[u32] { fn step(x: u32, &y: u32) -> u32 { x + y } }\n",
     "method step does not match its declaration"),
    (STEP + "instance Step[u32] { fn step(x: u64) -> u32 { 1u32 } }\n", "method step does not match"),
    (STEP + "instance Step[u32] { fn step(x: u32) -> u64 { 1u64 } }\n", "method step does not match"),
    (STEP + "instance Step[u32] { unsafe fn step(x: u32) -> u32 { x } }\n", "method step does not match"),
    (STEP + "instance Step[u32] { }\n", "the instance lacks trait method step"),
    (STEP + "instance Step[u32] { fn step(x: u32) -> u32 { x } fn other(x: u32) -> u32 { x } }\n",
     "other is not a method of trait Step"),
    (STEP + "instance Step[u32] { fn step(x: u32) -> u32 { x } fn step(x: u32) -> u32 { x } }\n",
     "method step is defined twice"),
    ("forall Self. trait K { fn k(x: Self) -> u32; fn k(x: Self) -> u32; }\n", "method k is declared twice"),
    # Reading an enum's tag needs the whole enum, not one whose payload is moved out.
    (TOKEN + "fn f() -> u32 { var e = E!::A.(make(7u32)); "
     "let tag = match &e { E!::A(t) => { let moved = t; let tag = core::mem::enum_variant(&e); t = moved; tag }, "
     "E!::B(n) => n }; eat(e) + tag }\n",
     "enum value 'e' has been moved or is only partially available; its tag is unavailable"),
    # The places one call lends are apart: no two overlap, and beside whole locals at most two parts of
    # places are lent, which part at a struct or tuple field reached through no enum.
    (TOKEN + "fn two(&a: Token!, &b: Token!) -> u32 { 0u32 }\n"
     "fn f() -> u32 { var x = make(1u32); let r = two(&x, &x); take(x) + r }\n",
     "'x' overlaps another place the same call lends"),
    (TOKEN + "exposed struct Box! { t: Token!, n: u32 }\nfn two(&a: Box!, &b: Token!) -> u32 { 0u32 }\n"
     "fn f(&b: Box!) -> u32 { two(&b, &b.t) }\n",
     "'b.t' overlaps another place the same call lends"),
    ("fn two(&a: u32, &b: u32) -> u32 { a + b }\nfn f() -> u32 { var x = 1u32; two(&x, &x) }\n",
     "'x' overlaps another place the same call lends"),
    ("fn three(&a: u32, &b: u32, &c: u32) -> u32 { 0u32 }\n"
     "fn f() -> u32 { var x = (1u32, 2u32); var y = (3u32, 4u32); var z = (5u32, 6u32); three(&x.0, &y.0, &z.0) }\n",
     "a call lends at most two parts of places; 'z.0' is a third"),
    (TOKEN + "fn two(&a: Token!, &b: u32) -> u32 { 0u32 }\n"
     "fn f(&e: E!, &p: (u32, u32)) -> u32 { match &e { E!::A(t) => two(&t, &p.0), E!::B(n) => 0u32 } }\n",
     "'e.A' and another part of a place are lent by one call, but they do not part at a struct or tuple field"),
    # An `unsafe` marker does not lift the core's rules for memory: a cast takes and produces no zero-size
    # type, conjures no package, brand, function, transform, type parameter or Never and reinterprets no
    # layout the compiler chooses, an extern takes no zero-size type, a raw read, write or alloca is never
    # at one, and an atomic operation works on an integer, a bool or a pointer.
    ("fn g(x: ()) -> u32 { unsafe (x as u32) }\n", "a zero-size type has no memory form"),
    ("fn g() -> u32 { let p = unsafe core::mem::alloca[()](); 0u32 }\n", "a raw read, write or alloca at a zero-size type"),
    ("fn g(p: *()) -> u32 { let x = unsafe *p; 0u32 }\n", "a raw read, write or alloca at a zero-size type"),
    ("fn g(p: *[u8; 0]) -> u32 { unsafe *p = []; 0u32 }\n", "a raw read, write or alloca at a zero-size type"),
    ('extern "C" fn ext(x: ()) -> u32;\n', "extern ext takes a zero-size type, which has no memory form"),
    ("forall T, U. unsafe fn convert(x: T) -> U { unsafe (x as U) }\n", "an unsafe cast cannot produce or take apart"),
    ("fn g(x: u64) -> core::cap::Brand['static] { unsafe (x as core::cap::Brand['static]) }\n",
     "an unsafe cast cannot produce or take apart"),
    ("fn g(f: fn(u32) -> u32) -> u64 { unsafe (f as u64) }\n", "an unsafe cast cannot produce or take apart"),
    ("struct S { f: fn() -> () }\nfn g(x: usize) -> S { unsafe (x as S) }\n", "an unsafe cast cannot produce or take apart"),
    ("forall T. exposed enum Opt { Some(T), None }\nfn g(x: u64) -> Opt[u32] { unsafe (x as Opt[u32]) }\n",
     "an unsafe cast cannot reinterpret an enum or a NonNull"),
    ("fn g(x: u64) -> core::ptr::NonNull[u8] { unsafe (x as core::ptr::NonNull[u8]) }\n",
     "an unsafe cast converts a NonNull only to or from a pointer"),
    ("exposed struct P { a: u32 }\nfn g(p: *P) -> P { unsafe p.atomic_load(core::atomic::Ordering::SeqCst) }\n",
     "atomic load works on a pointer to an integer, a bool or a pointer"),
    ("fn g(p: *bool) -> bool { unsafe p.fetch_add(true, core::atomic::Ordering::SeqCst) }\n",
     "atomic fetch_add works on a pointer to an integer"),
    # A pack's own binders name no brand the payload can mention: the first opening makes the package's
    # type, and `slot`'s type, learned after it, names the caller's 'p0, not the packed 'q.
    (BRANDS + "forall 'p0, 'q. fn f(a: core::cap::Brand['p0], b: core::cap::Brand['q]) -> u32 {\n"
     "    var slot = Opt::None; let pk = pack 'q. (b, slot); let exists 'r. (x, s) = pk;\n"
     "    slot = Opt::Some.(a); let exists 't. (x2, s2) = pk;\n"
     "    match s2 { Opt::Some(y) => same(x2, y), Opt::None => 0u32 } }\n",
     "brand mismatch: Brand['p0] and Brand['t]"),
    # Unifying two packages solves no outside variable to a brand they bind: `second`'s 'a is neither
    # package's brand, whatever the binders are called.
    (BRANDS + "forall 'p0, 'z. fn f(w: core::cap::Brand['p0], wz: core::cap::Brand['z]) -> u32 {\n"
     "    let pk = pack 'z. (wz, wz); let exists 'r. (u, v) = pk; let y = second(pk); same(y, w) }\n",
     r"type mismatch: exists 'p0\. \(Brand['p0], Brand['p0]\) and exists 'b\."),
    # The same where the package's binder is spelled as the function's brand: `twin`'s 'b is not `f`'s.
    (BRANDS + "forall 'z. fn twin(wz: core::cap::Brand['z]) -> exists 'b. (core::cap::Brand['b], core::cap::Brand['b]) {\n"
     "    pack 'z. (wz, wz) }\n"
     "forall 'b, 'z. fn f(wb: core::cap::Brand['b], wz: core::cap::Brand['z]) -> u32 {\n"
     "    let pk = twin(wz); let y = second(pk); same(y, wb) }\n",
     "type mismatch"),
)

ACCEPTED = (
    # The binder is not used after the store, or the arm puts a value back through it.
    TOKEN + "fn f() -> u32 { var e = E!::A.(make(1u32)); "
    "let r = match &e { E!::A(t) => { var x = eat(e); e = E!::B.(x); 0u32 }, E!::B(n) => n }; eat(e) + r }\n",
    TOKEN + "fn f() -> u32 { var e = E!::A.(make(1u32)); "
    "let r = match &e { E!::A(t) => { e = E!::B.(take(t)); 1u32 }, E!::B(n) => { n = n + 1u32; n } }; eat(e) + r }\n",
    TOKEN + "fn f(&e: E!) -> u32 { match &e { E!::A(t) => { let n = take(t); t = make(n + 1u32); n }, "
    "E!::B(n) => { let m = n; e = E!::B.(m + 1u32); m } } }\n",
    STEP + "instance Step[u32] { fn step(x: u32) -> u32 { x + 1u32 } }\nfn g() -> u32 { Step::step(2u32) }\n",
    LEND_STEP + "instance Step[u32] { fn step(&x: u32, y: u32) -> u32 { x = x + y; x } }\n"
    "fn g() -> u32 { var a = 1u32; Step::step(&a, 2u32) }\n",
    "fn two(&a: u32, &b: u32) -> u32 { a + b }\n"
    "fn f() -> u32 { var x = (1u32, 2u32); var y = 3u32; two(&x.0, &x.1) + two(&x.0, &y) }\n",
    "fn three(&a: u32, &b: u32, &c: u32) -> u32 { 0u32 }\n"
    "fn f() -> u32 { var x = (1u32, 2u32); var y = 3u32; three(&x.0, &y, &x.1) }\n",
    'extern "C" fn ext(x: (u32, ())) -> u32;\n',
    "fn g(x: u64) -> (u32, u32) { unsafe (x as (u32, u32)) }\n",
    "fn g(p: core::ptr::NonNull[u8]) -> *u8 { unsafe (p as *u8) }\n",
    "fn g(p: *bool) -> bool { unsafe p.atomic_load(core::atomic::Ordering::SeqCst) }\n",
    "fn g(p: **u8) -> *u8 { unsafe p.atomic_swap(unsafe *p, core::atomic::Ordering::SeqCst) }\n",
    "fn g(p: *u32) -> u32 { unsafe p.fetch_add(1u32, core::atomic::Ordering::SeqCst) }\n",
    # A brand the payload mentions but the pack does not hide stays itself, even spelled 'p0.
    BRANDS + "forall 'p0, 'q. fn f(a: core::cap::Brand['p0], b: core::cap::Brand['q]) -> u32 {\n"
    "    let pk = pack 'q. (a, b); let exists 'r. (x, y) = pk; same(x, a) + same(y, y) }\n",
    BRANDS + "forall 'p0, 'q. fn f(a: core::cap::Brand['p0], b: core::cap::Brand['q]) -> u32 {\n"
    "    var slot = Opt::None; let pk = pack 'q. (b, slot); let exists 'r. (x, s) = pk;\n"
    "    slot = Opt::Some.(a); let exists 't. (x2, s2) = pk;\n"
    "    match s2 { Opt::Some(y) => same(y, a), Opt::None => 0u32 } }\n",
    BRANDS + "forall 'p0, 'z. fn f(w: core::cap::Brand['p0], wz: core::cap::Brand['z]) -> u32 {\n"
    "    let pk = pack 'z. (wz, w); let exists 'r. (u, v) = pk; let y = second(pk); same(y, w) }\n",
    BRANDS + "forall 'p0. fn f(w: core::cap::Brand['p0]) -> u32 {\n"
    "    let pk: exists 'q. (core::cap::Brand['q], core::cap::Brand['q]) = pack 'p0. (w, w);\n"
    "    let exists 'r. (x, y) = pk; same(x, y) }\n",
    # A package whose binder is spelled as the function's brand, from a signature written elsewhere.
    BRANDS + "forall 'z. fn twin(wz: core::cap::Brand['z]) -> exists 'b. (core::cap::Brand['b], core::cap::Brand['b]) {\n"
    "    pack 'z. (wz, wz) }\n"
    "forall T. fn id(x: T) -> T { x }\n"
    "forall 'b, 'z. fn f(wb: core::cap::Brand['b], wz: core::cap::Brand['z]) -> u32 {\n"
    "    let pk = id(twin(wz)); let exists 'r. (x, y) = pk; same(x, y) }\n",
)


def front_end(source: str, directory: Path):
    """The lowered program of a package `p` whose module is `source`; it depends on `core` if it names it."""
    manifest = '[package]\nname="p"\nsource_root="."\n'
    if "core::" in source:
        manifest += f'[dependencies.core]\npath="{LIB / "core"}"\n'
    (directory / "kur.toml").write_text(manifest)
    (directory / "lib.kur").write_text(source)
    resolver = resolve(directory)
    return lower(resolver, signatures(resolver))


class FuzzRegressionTests(unittest.TestCase):
    def test_the_front_end_rejects_what_the_checker_would(self):
        for source, message in REJECTED:
            with self.subTest(source=source), tempfile.TemporaryDirectory(prefix="kursal-fuzz-") as base:
                with self.assertRaisesRegex(KursError, message.replace("[", r"\[")):
                    front_end(source, Path(base))

    def test_what_the_front_end_accepts_the_checker_accepts(self):
        for source in ACCEPTED:
            with self.subTest(source=source), tempfile.TemporaryDirectory(prefix="kursal-fuzz-") as base:
                check_program(front_end(source, Path(base)))


if __name__ == "__main__":
    unittest.main()
