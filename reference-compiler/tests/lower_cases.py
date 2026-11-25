"""Cases for the self-hosted compiler's lowering (tests/test_compiler.py): programs whose lowering, place
flow or promotion the reference rejects, each with a fragment of the expected message, written for the
rejections the programs the reference's own tests lower do not reach. Those programs are compared too;
tests/test_compiler.py records them as the tests run.

A case is a source, lowered as the one module of a package `p` (or, when it names `std::`, as a single
file depending on the standard library), or a dict of files, of which the package in `a/` is lowered."""

TOKEN = "exposed struct Token! { n: u32 }\nfn take(t: Token!) -> u32 { var { n } = t; n }\n"
OPTION = "forall T. exposed enum Option { Some(T), None }\n"
BOX = "exposed struct Box! { t: Token!, n: u32 }\n"

LOWER_ERRORS: tuple[tuple[str | dict[str, str], str], ...] = (
    # `as` converts between numbers; any other cast is unsafe.
    ("fn f() -> u8 { true as u8 }\n", "1:16: a safe `as` converts only between number types and from a NonNull to its pointer; "
     "any other cast reinterprets bits and needs unsafe"),
    # A match names the variants it misses.
    ("exposed enum L { R, A, G }\nfn f(l: L) -> u32 { match l { R => 1u32, G => 2u32 } }\n",
     "2:21: a match must cover every enum variant; it misses A"),
    # `{x}` hands its value to `Format::format` and drops the copy it gets back.
    ("use std::buffer::Buffer!;\nuse std::io::print;\nfn main() {\n"
     "    let name = Buffer!::new();\n    print(f\"hi {name}\\n\");\n}\n",
     "5:17: {name} in an f-string drops a linear value; lend it with {&name}"),
    ("use std::buffer::Buffer!;\nuse std::io::print;\nstruct P! { b: Buffer! }\nfn main() {\n"
     "    let p = P!.{ b: Buffer!::new() };\n    print(f\"hi {p.b}\\n\");\n}\n",
     "6:17: {p.b} in an f-string drops a linear value; lend it with {&p.b}"),
    ("use std::buffer::Buffer!;\nuse std::io::print;\nfn main() {\n"
     "    print(f\"hi {Buffer!::new()}\\n\");\n}\n",
     "4:17: a value in an f-string is formatted and dropped, and this one is linear; bind it to a var and lend that"),
    # lowering
    ("fn f() -> i8 { -129i8 }\n", "does not fit"),
    ("fn f() -> u8 { var x: u8 = 300; x }\n", "does not fit"),
    ("fn f(p: *u32) -> u32 { *p }\n", "reading through a raw pointer needs unsafe"),
    ("fn f() -> *u32 { core::mem::alloca[u32]() }\n", "calling alloca needs unsafe"),
    ("fn bump(&x: u32) -> u32 { x = x + 1u32; x }\nfn add(a: u32, b: u32) -> u32 { a + b }\n"
     "fn f() -> u32 { var n = 1u32; add(n, bump(&n)) }\n", "a plain read overlaps a lend"),
    ("struct P { a: u32, b: u32 }\nfn f(p: P) -> u32 { let { a, ..rest } = p; let q = rest; a }\n", "bundle"),
    ("fn f() -> u32 { let exists 'a. w = core::cap::fresh(); let g = || pack 'a. 7u32; 0u32 }\n", "block-local brand 'a"),
    ("fn f() -> u32 { let exists 'a. w = core::cap::fresh(); let p = pack 'a. w; let c = unsafe (p as u32); 0u32 }\n",
     "existential package"),
    ("fn f(x: u32) -> u32 { let p = unsafe (x as exists 'a. core::cap::Brand['a]); 0u32 }\n", "existential package"),
    # A named type's fields are part of its value: a cast neither makes nor takes apart one that holds a
    # function value or a brand witness, however deep.
    ("struct S { f: fn() -> () }\nfn f(x: u64) -> S { unsafe (x as S) }\n", "an unsafe cast cannot produce or take apart"),
    ("struct S { f: fn() -> () }\nfn f(s: S) -> u64 { unsafe (s as u64) }\n", "an unsafe cast cannot produce or take apart"),
    ("struct I { f: fn() -> () }\nstruct O { a: u32, i: I }\nfn f(x: u64) -> O { unsafe (x as O) }\n",
     "an unsafe cast cannot produce or take apart"),
    ("forall 'k.\nstruct P { x: u64, b: core::cap::Brand['k] }\nforall 'k.\nfn f(x: u64) -> P['k] { unsafe (x as P['k]) }\n",
     "an unsafe cast cannot produce or take apart"),
    # A raw pointer is cast only to or from a pointer or an integer, as C converts one.
    ("fn f(p: *u8) -> bool { unsafe (p as bool) }\n", "an unsafe cast converts a pointer only to or from a pointer or an integer"),
    ("fn f(p: *u8) -> f64 { unsafe (p as f64) }\n", "an unsafe cast converts a pointer only to or from a pointer or an integer"),
    ("exposed struct W { p: *u8 }\nfn f(p: *u8) -> W { unsafe (p as W) }\n", "an unsafe cast converts a pointer only"),
    ("exposed struct W { p: *u8 }\nfn f(w: W) -> *u8 { unsafe (w as *u8) }\n", "an unsafe cast converts a pointer only"),
    # What an extern hands back holds nothing only checked code makes, and no layout the compiler chooses.
    ('extern "C" fn get_cb() -> fn() -> u32;\n', "1:27: extern get_cb cannot return a package, a brand witness, a safe function"),
    ('struct W { n: u64, f: fn() -> u32 }\nextern "C" fn make() -> W;\n', "extern make cannot return a package"),
    ('extern "C" fn make() -> (u64, !);\n', "extern make cannot return a package"),
    ('forall T. exposed enum Opt { Some(T), None }\nextern "C" fn make() -> Opt[u32];\n',
     "extern make cannot return an enum or a NonNull, whose layout the compiler chooses"),
    ('extern "C" fn make() -> std::ptr::NonNull[u8];\n', "extern make cannot return an enum or a NonNull"),
    # An extern's signature is closed, and every function type in it is an `unsafe fn` type, anywhere,
    # whose parameters and result foreign code makes or takes as it does an extern's.
    ('forall T. extern "C" fn f(x: *T) -> u32;\n', "extern f takes no type or brand parameters"),
    ('extern "C" fn f(&x: u32) -> u32;\n', "1:17: extern f takes a place parameter; foreign code takes values"),
    ('struct Cap! {}\nextern "C" fn make() -> Cap!;\n',
     "extern make returns a zero-size linear type, which foreign code would make from nothing"),
    ('extern "C" fn f(n: u64, &x: u32) -> u32;\nfn g() -> u32 { var x = 1u32; unsafe f(2u64, &x) }\n',
     "1:25: extern f takes a place parameter; foreign code takes values"),
    ('extern "C" fn f(x: fn() -> u32) -> u32;\n', "extern f has a safe function type in its signature"),
    ('struct Cb { run: fn() -> u32 }\nextern "C" fn f(x: *Cb) -> u32;\n', "extern f has a safe function type in its signature"),
    ('extern "C" fn f(x: unsafe fn(u8, ()) -> u8) -> u32;\n',
     "1:17: extern f has an unsafe fn type in its signature that takes a zero-size type"),
    ("forall 'k.\nstruct W { b: core::cap::Brand['k], n: u64 }\nextern \"C\" fn f(x: unsafe fn(W['static]) -> u8) -> u32;\n",
     "extern f has an unsafe fn type in its signature that takes or returns a package, a brand witness"),
    ('enum E { A(u8), B(u16) }\nextern "C" fn f() -> unsafe fn() -> unsafe fn(E) -> u8;\n',
     "2:22: extern f has an unsafe fn type in its signature that takes or returns an enum or a NonNull"),
    # No memory holds a value that holds Never, however deep.
    ("struct S { n: ! }\nfn f(p: *S) -> u32 { var s = unsafe *p; 0u32 }\n",
     "an unsafe read cannot produce a value that holds Never"),
    ("fn f(p: *(u32, !)) -> u32 { var s = unsafe *p; 0u32 }\n", "an unsafe read cannot produce a value that holds Never"),
    ("struct S { n: ! }\nfn get(&s: S) -> u32 { 0u32 }\nfn f(p: *S) -> u32 { get(unsafe &*p) }\n",
     "an unsafe lend of a pointer's target cannot lend a value that holds Never"),
    # A raw read, write or alloca is never at a zero-size type, Copy or not, volatile or not; at a type
    # with parameters it needs Sized of the type, by an exact premise or a part with bytes whatever the
    # parameters are, discharged where the function is called.
    ("fn f(p: *()) -> () { unsafe *p }\n", "a raw read, write or alloca at a zero-size type"),
    ("exposed struct Cap! {}\nfn f(p: *Cap!) -> Cap! { unsafe *p }\n", "a raw read, write or alloca at a zero-size type"),
    ("exposed struct Cap! {}\nfn f(p: *(Cap!, ()), c: Cap!) { unsafe *p = (c, ()); }\n",
     "a raw read, write or alloca at a zero-size type"),
    ("forall 'k.\nexposed struct W { b: core::cap::Brand['k], u: () }\nforall 'k.\nfn f(p: *W['k]) -> W['k] { unsafe *p }\n",
     "a raw read, write or alloca at a zero-size type"),
    ("forall 'k.\nfn f(p: *core::cap::Brand['k], b: core::cap::Brand['k]) { unsafe p.store_volatile(b); }\n",
     "a raw read, write or alloca at a zero-size type"),
    ("fn f() -> *[(); 3] { unsafe core::mem::alloca[[(); 3]]() }\n", "a raw read, write or alloca at a zero-size type"),
    ("forall T where Copy[T].\nfn get(p: *T) -> T { unsafe *p }\n", "a raw read, write or alloca at T needs Sized[T]"),
    ("forall T.\nfn make() -> *T { unsafe core::mem::alloca[T]() }\n", "a raw read, write or alloca at T needs Sized[T]"),
    ("forall T, U where Copy[T], Copy[U], Sized[T], Sized[U].\nfn get(p: *(T, U)) -> (T, U) { unsafe *p }\n",
     "a raw read, write or alloca at (T, U) needs Sized[(T, U)]"),
    ("forall 'k.\nexposed struct W { b: core::cap::Brand['k], u: () }\n"
     "forall T where Copy[T], Sized[T].\nfn get(p: *T) -> T { unsafe *p }\n"
     "fn f(p: *W['static]) -> W['static] { get(p) }\n", "Sized requires an exact premise"),
    ("use std::owned::Owned!;\nfn main() -> i32 { let o = Owned!::new(()); 0 }\n", "Sized requires an exact premise"),
    ("forall T.\nfn take(&x: T) -> u32 { 0u32 }\nforall T.\nfn f(p: *T) -> u32 { take(unsafe &*p) }\n",
     "a raw read, write or alloca at T needs Sized[T]"),
    ("fn take(&x: ()) -> u32 { 0u32 }\nfn f(p: *()) -> u32 { take(unsafe &*p) }\n",
     "a raw read, write or alloca at a zero-size type"),
    # `sizeof` of a foreach type over a pointer to a parameter needs `Exposed` of the source, as the
    # core's does (test_core_soundness).
    ("forall T.\nfn f() -> usize { core::mem::sizeof[foreach *F of *T => (F,)]() }\n",
     "the shape of *T is not visible here"),
    # `sizeof` of a foreach type needs `Exposed` of its source, whatever that is: a parameter, a type whose
    # shape is restricted to another module, a foreach type, a pointer to a sum.
    ("forall T.\nfn f() -> usize { core::mem::sizeof[foreach F of T => u8]() }\n",
     "2:19: the shape of T is not visible here: write `where Exposed[T]`"),
    ("fn f() -> usize { std::mem::sizeof[foreach F of std::io::File! => u8]() }\n",
     "1:19: the shape of File! is not visible here"),
    ("fn f() -> usize { core::mem::sizeof[foreach F of (foreach G of (u8,) => G) => u8]() }\n",
     "1:19: a foreach source cannot be a foreach type"),
    ("exposed enum E { A, B(u8) }\nfn f() -> usize { core::mem::sizeof[foreach *F of *E => F]() }\n",
     "2:19: a foreach source cannot be a pointer to a sum"),
    ("use std::arena::{Domain!, ref_in};\nfn main() -> i32 {\n"
     "    var exists 'a. cx = Domain!::new(std::process::hash_seed());\n    let r = ref_in(&cx, ());\n"
     "    cx.destroy();\n    0\n}\n", "Sized requires an exact premise"),
    # Every formed type is smaller than the address space, 2^64 bytes.
    ("fn f() -> usize { core::mem::sizeof[[[u8; 9223372036854775808]; 2]]() }\n",
     "is too large: its size is not below 2^64 bytes"),
    ("fn f(p: *[[u8; 9223372036854775808]; 2]) -> u32 { 0u32 }\n", "is too large: its size is not below 2^64 bytes"),
    (TOKEN + "fn f() -> u32 { (Token!.{ n: 1u32 }, Token!.{ n: 2u32 }).0.n }\n", "linear sibling"),
    ("unsafe fn danger() -> u32 { 0u32 }\nfn f() -> u32 { danger() }\n", "calling an unsafe function"),
    ("fn take(&x: u32) -> u32 { x }\nfn f(p: *u32) -> u32 { take(&*p) }\n", "lending a raw pointer's target needs unsafe"),
    ("use std::atomic::Ordering;\nfn f(p: *u32) -> u32 { unsafe p.atomic_load(Ordering::Release) }\n", "does not support ordering"),
    ("use std::atomic::Ordering;\nfn f(p: *u32) -> u32 { unsafe p.atomic_store(1u32, Ordering::Acquire); 0u32 }\n",
     "does not support ordering"),
    ("use std::atomic::Ordering;\nfn f() -> u32 { unsafe std::atomic::fence(Ordering::Relaxed); 0u32 }\n", "does not support ordering"),
    ("use std::atomic::Ordering;\nfn f(p: *u32) -> (u32, bool) { unsafe p.compare_exchange(1u32, 2u32, Ordering::Relaxed, Ordering::Acquire) }\n",
     "failure ordering"),
    ("use std::atomic::Ordering;\nfn f(p: *u32) -> (u32, bool) { unsafe p.compare_exchange(1u32, 2u32, Ordering::Acquire, Ordering::SeqCst) }\n",
     "failure ordering"),
    ("use std::atomic::Ordering;\nfn f(p: *u32, o: Ordering) -> u32 { unsafe p.atomic_load(o) }\n", "memory ordering"),
    (TOKEN + "fn f(t: Token!) -> u32 { let _ = t; 0u32 }\n", "cannot discard a linear value"),
    (TOKEN + "fn f(t: Token!) -> u32 { let (a, _) = (1u32, t); a }\n", "cannot discard a linear value"),
    (TOKEN + "exposed struct Pair! { t: Token!, n: u32 }\nfn f(p: Pair!) -> u32 { let { n } = p; n }\n",
     "cannot discard a linear value"),
    (TOKEN + OPTION + "fn f(o: Option[Token!]) -> u32 { match o { Option::Some(_) => 0u32, Option::None => 1u32 } }\n",
     "cannot discard a linear value"),
    (TOKEN + OPTION + "fn f(o: Option[Token!]) -> u32 { match o { _ => 1u32 } }\n", "cannot discard a linear value"),
    ("fn f(n: u32) -> u32 { match n { 1 => 1u32, 2 => 2u32 } }\n", "must end with a wildcard"),
    ("fn f(n: u8) -> u32 { match n { 300 => 1u32, _ => 2u32 } }\n", "match ranges must fit"),
    ("fn f(n: u8) -> u32 { match n { 1..=5 => 1u32, 3 => 3u32, _ => 2u32 } }\n", "match ranges must fit"),
    ("fn f(n: u64) -> u32 { match n { 18446744073709551615 => 1u32, 0..=18446744073709551615 => 3u32, _ => 2u32 } }\n",
     "match ranges must fit"),
    ("fn f(n: i64) -> u32 { match n { -9223372036854775808..=-1 => 1u32, 0..=9223372036854775807 => 3u32, _ => 2u32 } }\n"
     "fn g(n: i64) -> u32 { match n { -9223372036854775809 => 1u32, _ => 2u32 } }\n", "match ranges must fit"),
    ("fn f(n: i8) -> u32 { match n { -129 => 1u32, _ => 2u32 } }\n", "match ranges must fit"),
    ("fn f(n: u8) -> u32 { match n { 5..=1 => 1u32, _ => 2u32 } }\n", "match ranges must fit"),
    (OPTION + "fn f(o: Option[u32]) -> u32 { match o { Option::None => 0u32, Option::None => 1u32, _ => 2u32 } }\n",
     "must not repeat"),
    (OPTION + "fn f(o: Option[u32]) -> u32 { match o { Option::None => 0u32 } }\n", "cover every enum variant"),
    ("fn f(b: bool) -> u32 { match b { _ => 0u32 } }\n", "integer or enum scrutinee"),
    ("forall T where Copy[T]. fn f(x: T) -> u32 { match x { _ => 0u32 } }\n", "abstract type"),
    (TOKEN + "fn make() -> Token! { Token!.{ n: 1u32 } }\nfn f() -> u32 { make(); 0u32 }\n", "cannot discard a linear expression"),
    # place flow
    (TOKEN + "fn f() -> u32 { var t = Token!.{ n: 7u32 }; 0u32 }\n", "is not consumed before return"),
    (TOKEN + "fn f(t: Token!) -> u32 { 0u32 }\n", "is not consumed before return"),
    # A body with no tail returns at its `}`.
    (TOKEN + "fn f() {\n    var t = Token!.{ n: 7u32 };\n}\n", "5:1: "),
    (TOKEN + "fn f() -> u32 { var t = Token!.{ n: 7u32 }; let n = take(t); take(t) }\n", "has been moved"),
    (TOKEN + "fn f(t: Token!, c: bool) -> u32 { if c { let n = take(t); }; 0u32 }\n", "remains live on some paths"),
    (TOKEN + "fn f() -> u32 { var t = Token!.{ n: 7u32 }; t = Token!.{ n: 9u32 }; take(t) }\n", "cannot overwrite live linear value"),
    (TOKEN + "fn bad(&t: Token!) -> u32 { take(t) }\n", "must be restored before return"),
    (TOKEN + "fn peek(&t: Token!) -> u32 { t.n }\nfn f() -> u32 { var t = Token!.{ n: 7u32 }; let n = take(t); peek(&t) }\n",
     "cannot be lent"),
    (TOKEN + OPTION + "fn f(o: Option[Token!]) -> u32 { var o = o; let x = o; let tag = core::mem::enum_variant(&o); match x { Option::Some(t) => take(t), Option::None => tag } }\n",
     "its tag is unavailable"),
    (TOKEN + BOX + "fn f(b: Box!) -> u32 { let { t, n } = b; let m = take(t); take(t) }\n", "has been moved"),
    (TOKEN + "fn pair(a: Token!, b: u32) -> u32 { take(a) + b }\nfn make() -> Token! { Token!.{ n: 1u32 } }\n"
     "fn f() -> u32 { pair(make(), { return 0u32; 2u32 }) }\n", "temporary expression result"),
    ("use std::iter::{Iterator, range};\nfn f() -> u32 { var s = 0u32; for i in &range(0usize, 3usize) { s = s + 1u32; } s }\n",
     "iterator is not a place"),
    ("use std::buffer::Buffer!;\nfn g() -> u32 { 1u32 }\nfn f() -> u32 { var b = f\"{&g()}\"; 0u32 }\n", "needs a local or a place"),
    # place flow names a closure's body by the function it is in
    (TOKEN + "fn f() -> u32 {\n    let g = |x: u32| {\n        let t = Token!.{ n: x };\n        take(t) + take(t)\n    };\n    g(1u32)\n}\n",
     "6:24: p::f: value 't' has been moved or is only partially available"),
    # promotion of locally branded values
    (OPTION + "fn f() -> u32 { let exists 'a. w = core::cap::fresh(); var o = Option::Some.(w); let t = core::mem::enum_variant(&o); match o { Option::Some(x) => t, Option::None => 0u32 } }\n",
     "frontend limitation: locally branded value 'o' requires"),
    ("forall 'k. fn two(&a: (core::cap::Brand['k], u32), &b: core::cap::Brand['k]) -> u32 { 0u32 }\n"
     "fn f() -> u32 { let exists 'a. w = core::cap::fresh(); var p = (w, 1u32); two(&p, &p.0) }\n", "overlap"),
)
