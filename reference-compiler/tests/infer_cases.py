"""Cases for the self-hosted compiler's inference (tests/test_compiler.py): programs inference accepts,
and programs it rejects with a fragment of the expected message.

A case is a source, inferred as the one module of a package `p` (or, when it names `std::`, as a single
file depending on the standard library), or a dict of files, of which the package in `a/` is inferred.
Most were collected from the programs the reference compiler's own tests infer."""

from kursal.resolve import LIB


def manifest(name: str, deps: tuple[str, ...] = ()) -> str:
    """`core` is the library's; any other dependency is a sibling directory."""
    return f'[package]\nname="{name}"\nsource_root="."\n' + "".join(
        f'[dependencies.{d}]\npath="{LIB / "core" if d == "core" else "../" + d}"\n' for d in deps)


SHOW = "trait Show { fn show(x: Self) -> u32; }\ninstance Show[u32] { fn show(x: u32) -> u32 { x } }\n"
OTHER = "trait Other { fn show(x: Self) -> u32; }\ninstance Other[u32] { fn show(x: u32) -> u32 { 0u32 } }\n"
BRAND_VALUE = "forall 'b, T. fn g(w: core::cap::Brand['b], t: T) -> T { t }\n"
NEXT = ("trait Next { fn step(x: Self) -> u32; }\ninstance Next[u32] { fn step(x: u32) -> u32 { x } }\n"
        "forall I where Next[I]. impl I { fn twice(self) -> u32 { 0u32 } }\n")


def methods(source: str, **modules: str) -> dict[str, str]:
    """A package `a` whose lib.kur is `source`, with more modules."""
    return {"a/kur.toml": manifest("a"), "a/lib.kur": source, **{f"a/{m}.kur": text for m, text in modules.items()}}


def chained(source: str) -> dict[str, str]:
    """`a` depends on `b`, which re-exports the blanket impl's trait from `c`."""
    return {"a/kur.toml": manifest("a", ("b",)), "a/lib.kur": source,
            "b/kur.toml": manifest("b", ("c",)), "b/lib.kur": "use c::Next;\nexport Next;\n",
            "c/kur.toml": manifest("c"), "c/lib.kur": NEXT}


# `b` declares the trait and calls a method that only `c`, which depends on `b`, declares.
UNSEEN_IMPL = {"a/kur.toml": manifest("a", ("b", "c")), "a/lib.kur": "",
               "b/kur.toml": manifest("b"),
               "b/lib.kur": "trait Next { fn step(x: Self) -> u32; }\nexport Next;\n"
                            "instance Next[u32] { fn step(x: u32) -> u32 { x } }\nfn f(x: u32) -> u32 { x.twice() }\n",
               "c/kur.toml": manifest("c", ("b",)),
               "c/lib.kur": "use b::Next;\nforall I where Next[I]. impl I { fn twice(self) -> u32 { 0u32 } }\n"}


# Method lookup sees the traits, and blanket impls, visible in the calling module.
VISIBILITY = (
    methods("use package::m::Show;\nfn f(x: u32) -> u32 { x.show() }\n", m=SHOW),
    methods("use package::m::*;\nfn f(x: u32) -> u32 { x.show() }\n", m=SHOW),
    methods("use package::m::Show;\nfn f(x: u32) -> u32 { x.show() }\n", m=SHOW, n=OTHER),
    methods("fn f(x: u32) -> u32 { package::m::Show::show(x) }\n", m=SHOW),
    methods("use package::m::Next;\nfn f(x: u32) -> u32 { x.twice() }\n", m=NEXT),
    chained("use b::Next;\nfn f(x: u32) -> u32 { x.twice() }\n"),
)

VISIBILITY_ERRORS = (
    (methods("use package::m;\nfn f(x: u32) -> u32 { x.show() }\n", m=SHOW), "u32 has no method show"),
    (methods("use package::m::Show;\nuse package::n::Other;\nfn f(x: u32) -> u32 { x.show() }\n", m=SHOW, n=OTHER),
     "u32 has several methods named show"),
    (methods("fn f(x: u32) -> u32 { x.twice() }\n", m=NEXT), "u32 has no method twice"),
    (UNSEEN_IMPL, "u32 has no method twice"),
)

# What a case that writes an f-string through a domain's context imports: its first three lines.
CONTEXT = "use std::arena::Domain!;\nuse std::buffer::Buffer!;\nuse std::raw::Span;\n"

INFERENCES = (
    # `f"...".write(&cx, out)` reads each `{span}` through the context lent from a var's place.
    CONTEXT + "forall 'k. fn f(&cx: Domain!['k], s: Span['k], n: u32) -> Buffer! { f\"{s}: {n}\".write(&cx, Buffer!::new()) }\n"
    "fn g() -> u32 { var exists 'k. cx = Domain!::new(std::process::hash_seed()); let s = Span::from_static(&cx, \"x\");\n"
    "    let b = f\"{s}{s}\".write(&cx, f\"[{s}]\".write(&cx, Buffer!::new())); b.destroy(); cx.destroy(); 0u32 }\n"
    "forall 'k. fn h(&p: (u32, Domain!['k]), n: u32, s: Span['k]) -> Buffer! { f\"{n} {s} {\"static\"}\".write(&p.1, Buffer!::new()) }\n",
    # A type alias stands for its body in a body's annotations, casts and type arguments too.
    "forall T. type Pair = (T, T);\nforall T. fn id(x: T) -> T { x }\n"
    "fn g(p: Pair[u32]) -> u64 { let q: Pair[u32] = p; let r = q.0 as u64; let n = core::mem::sizeof[Pair[u8]]();\n"
    "    let t = id[Pair[u8]]((1u8, 2u8)); let c = |x: Pair[u8]| -> Pair[u8] { x }; r + (n as u64) + (c(t).0 as u64) }\n",
    'use std::iter::{Iterator, range};\nuse std::option::{Option, Result};\nuse std::atomic::Ordering;\nuse std::buffer::Buffer!;\nfn total(n: u32) -> u32 { var sum = 0u32; var it = range(0usize, 4usize); for i in &it { sum = sum + (i as u32); } sum }\nfn first(o: Option[u32]) -> Option[u32] { let v = o?; Option::Some.(v + 1) }\nfn both(r: Result[u32, bool]) -> Result[(u32, u32), bool] { let v = r?; Result::Ok.((v, v)) }\nfn text(n: u32) -> Buffer! { f"n = {n}" }\nfn written(n: u32) -> Buffer! { f"n = {n}".write(Buffer!::new()) }\nfn atomics(p: *u32) -> u32 { std::atomic::fence(Ordering::SeqCst); let old = unsafe p.fetch_add(1u32, Ordering::Relaxed); let (v, ok) = unsafe p.compare_exchange(old, 2u32, Ordering::SeqCst, Ordering::Relaxed); unsafe p.store(v); std::ptr::store_volatile(p, 3u32); std::ptr::load_volatile(p) }\nfn tag(o: Option[u32]) -> u32 { var x = o; let t = std::mem::enum_variant(&x); let (y, u) = std::mem::enum_variant(x); t + u }\nfn mint() -> exists \'r. std::cap::Brand[\'r] { let exists \'a. w = std::cap::fresh(); pack \'a. w }\nfn use_mint() -> u32 { let exists \'b. w = mint(); let s = std::cap::static_brand(); var q = pack \'b. w; 0u32 }\n',
    'exposed struct P { x: u32, y: u32, z: u8 }\nforall T. exposed struct Box { value: T }\nforall T. impl Box[T] { forall U. fn map(self, u: U) -> (T, U) { (self.value, u) } }\ntrait Size { fn size(x: Self) -> usize; }\ninstance Size[u32] { fn size(x: u32) -> usize { 4usize } }\nforall T where Size[T]. fn size_of(x: T) -> usize { T::size(x) }\nforall T. fn id(x: T) -> T { x }\nfn f(p: P, q: *u32) -> u32 {\n  const K: u32 = 3u32 * 2u32;\n  var { x, ..rest } = p; let r = P.{ x: x + K, ..rest };\n  let a: [u32; 4] = [x, ..]; let [a0, a1, a2, a3] = a;\n  let n = core::mem::sizeof[P]() + size_of(a0); let c = (n as u32) + (r.z as u32);\n  let slot = core::mem::alloca[u32](); unsafe *slot = c; let back = unsafe *slot;\n  let bias = 2u32; let g = |y: u32| y + bias + back; let h = id; let i = id[u32](g(1u32));\n  let b = Box[u32]::map[bool](Box.{ value: i }, true);\n  let items = foreach item of (1u32, 2u32) { (item, bias) };\n  let m = match a0 { 0 => 1u32, 1..=3 => 2u32, _ => 3u32 };\n  var v = p; v.x = m; v.y = b.0; v.x + h(v.y)\n}\n',
    "forall T. exposed enum Opt { Some(T), None }\nfn f(n: u32) -> u32 { let o: Opt[u32] = Opt::None; var k = n; while k > 0u32 { k = k - 1u32; if k == 3u32 { return k; } } var q = Opt::Some.(k); let w = match &q { Opt::Some(v) => v, Opt::None => 0u32 }; let pk = pack 'static. q; w }\n",
    *VISIBILITY,
    "forall T. fn pass(x: exists 'a. (core::cap::Brand['a], T)) -> T {\n                let exists 'a. (witness, value) = x;\n                value\n            }\n            fn main() -> i32 {\n                let exists 'a. witness = core::cap::fresh();\n                pass(pack 'a. (witness, 7i32))\n            }",
    'exposed struct Token! { n: i32 }\n            forall T. fn pass(x: T, flag: bool) -> T {\n                let pair = (x, if flag { 1u32 } else { 2u32 });\n                let (value, count) = pair;\n                if count == 1u32 { value } else { value }\n            }\n            fn main() -> i32 {\n                let { n } = pass(Token!.{ n: 7i32 }, true);\n                n\n            }',
    'forall T. fn bad(x: T) -> () { var x = x; match &x { _ => () } } fn main() -> i32 { 7i32 }',
    # A receiver taken by value is lent once it is a var, as any parameter is.
    'exposed struct C { n: u32 }\nimpl C {\n    fn bump(&self) -> u32 { self.n = self.n + 1u32; self.n }\n'
    '    fn twice(self) -> u32 { var self = self; let a = &self.bump(); &self.bump() + a }\n}\n',
    'forall T where Copy[T]. fn bad(x: T) -> () { var x = x; match &x { _ => () } } fn main() -> i32 { 7i32 }',
    'fn bad(x: bool) -> f32 { unsafe ((x as u32) as f32) } fn main() -> i32 { 0i32 }',
    "fn bad(x: u32) -> exists 'a. core::cap::Brand['a] { unsafe (x as exists 'a. core::cap::Brand['a]) } fn main() -> i32 { 0i32 }",
    'fn main() -> i32 {\n            let x = ((return 7i32)) as *u32;\n            0i32\n        }',
    'fn bad(x: bool) -> u32 { x as u32 } fn main() -> i32 { 0i32 }',
    'fn bad(x: u32) -> *u32 { x as *u32 } fn main() -> i32 { 0i32 }',
    'fn bad(x: (u32,)) -> u32 { x as u32 } fn main() -> i32 { 0i32 }',
    'fn main() -> i32 {\n            let x = -1i8;\n            if (x as u32) == 4294967295u32 && (255u32 as i8) == -1i8 { 7i32 } else { 0i32 }\n        }',
    'fn main() -> i32 { let x = unsafe ((unsafe (0usize as *u32)) as f32); 0i32 }',
    'forall T, U. unsafe fn convert(x: T) -> U { unsafe (x as U) }\n                fn main() -> i32 { (unsafe convert[(u32, u32), (i32, i32)]((7u32, 9u32))).0 }\n            ',
    "fn main() -> i32 {\n                let f = || {\n                    let exists 'a. witness = core::cap::fresh();\n                    let size = core::mem::sizeof[(u32, core::cap::Brand['a])]();\n                    if size == core::mem::sizeof[(u32, core::cap::Brand['a])]() { 7i32 } else { 1i32 }\n                };\n                f()\n            }",
    'fn main() -> i32 {\n            var n = 3u32;\n            let f = |x: u32| n + x;\n            n = 10;\n            (f(2) as i32)\n        }',
    "forall 'a. fn use_brand(w: core::cap::Brand['a]) -> u32 {\n            let f = |n: u32| { let witness = w; n + 1u32 };\n            f(4)\n        }\n        fn main() -> i32 {\n            let exists 'a. witness = core::cap::fresh();\n            (use_brand(witness) as i32)\n        }",
    'exposed struct Token! { n: u32 }\n        fn main() -> i32 {\n            let token = Token!.{ n: 7u32 };\n            let once = |x: u32| { let { n } = token; n + x };\n        (once(2) as i32) }',
    'fn main() -> i32 {\n            let n = 3u32;\n            let outer = |x: u32| {\n                let inner = |y: u32| n + x + y;\n                let n = 100u32;\n                inner(2) + n\n            };\n            (outer(4) as i32)\n        }',
    "fn main() -> i32 {\n                let exists 'a. witness = core::cap::fresh();\n                let f = || core::mem::sizeof[(u32, core::cap::Brand['a])]();\n                (f() as i32)\n            }",
    'const A: u8 = (-1i32 as u8);\n            const B: i8 = (255u32 as i8);\n            const C: u64 = (-1i8 as u64);\n            fn main() -> i32 {\n                let a = -1i32; let b = 255u32; let c = -1i8;\n                if (a as u8) == A && (b as i8) == B && (c as u64) == C { 7i32 } else { 1i32 }\n            }',
    'const X: bool = (1u8 << 9) == 2u8;\n            const Y: bool = (-8i16 >> 1) == -4i16;\n            const Z: bool = (1u8 << 9) == (2u8 << 0);\n            fn main() -> i32 { if X && Y && Z { 7i32 } else { 1i32 } }\n            ',
    'use std::slice::Slice;\n            const GREETING: Slice[u8, \'static] = "hello";\n            fn main() -> i32 {\n                const LOCAL: Slice[u8, \'static] = "hi";\n                (GREETING.len() + LOCAL.len()) as i32\n            }',
    "forall 'k, T. exposed struct Tagged { witness: core::cap::Brand['k], value: T }\n                    fn main() -> i32 {\n                        let exists 'a. witness = core::cap::fresh();\n                        let tagged = Tagged['a].{ witness, value: 7u32 };\n                        tagged.value as i32\n                    }",
    'forall T, U. exposed struct S { first: T, second: U }\n    forall T, U. exposed tuple Pair(T, U);\n    forall T, U. exposed enum E { Item(T, U), Empty }\n    exposed struct Plain { n: u32 }\n    exposed tuple Single(u32);\n    exposed enum Flag { On }\nfn main() -> i32 {\n                    let s = S.{ first: 3u32, second: 4u32 };\n                    let (a, b) = Pair.(s.first, s.second);\n                    match E::Item.(a, b) {\n                        E::Item(x, y) => (x + y) as i32, E::Empty => 0i32\n                    }\n                }',
    'forall T, U. exposed struct S { first: T, second: U }\n    forall T, U. exposed tuple Pair(T, U);\n    forall T, U. exposed enum E { Item(T, U), Empty }\n    exposed struct Plain { n: u32 }\n    exposed tuple Single(u32);\n    exposed enum Flag { On }\nfn main() -> i32 {\n                    let s = S[u32, u32].{ first: 3u32, second: 4u32 };\n                    let (a, b) = Pair[u32, u32].(s.first, s.second);\n                    match E[u32, u32]::Item.(a, b) {\n                        E::Item(x, y) => (x + y) as i32, E::Empty => 0i32\n                    }\n                }',
    'forall T -> U where Copy[U]. trait Get { fn get(x: T) -> U; }\n            instance Get[u32 -> i32] { fn get(x: u32) -> i32 { x as i32 } }\n            fn main() -> i32 { Get::get(7u32) }',
    'forall T where Copy[T]. trait Ready { fn read(x: T) -> i32; }\n                    instance Ready[i32] { fn read(x: i32) -> i32 { x } }\n                    forall T. exposed struct Box { value: T }\n                    forall T. impl Box[T] {\n                        forall U where Ready[T], Copy[T], Copy[U].\n                        fn read(self: Box[T], unused: U) -> i32 { Ready[T]::read(self.value) }\n                    }\n                    fn main() -> i32 { Box::read(Box.{ value: 7i32 }, ()) }',
    'exposed enum E! { A, B(u32) }\n            fn main() -> i32 {\n                let f = |x| core::mem::enum_variant(x);\n                var (value, tag) = f(E!::B.(6u32));\n                match value { E!::A => 0i32, E!::B(n) => (n + tag) as i32 }\n            }',
    'exposed enum E { A, B(u32) }\n            fn main() -> i32 {\n                var (value, tag) = core::mem::enum_variant(E::B.(6u32));\n                match value { E::A => 0i32, E::B(n) => (n + tag) as i32 }\n            }',
    'exposed enum E! { A }\n                fn main() -> i32 { core::mem::enum_variant(E!::A); 7i32 }',
    'exposed enum E! { A }\n                fn main() -> i32 {\n                    var value = E!::A;\n                    let moved = value;\n                    let tag = core::mem::enum_variant(&value);\n                    match moved { E!::A => tag as i32 }\n                }',
    "fn main() -> i32 {\n            var package = core::cap::fresh();\n            var n = 0u32;\n            while n < 3 {\n                let exists 'a. witness = package;\n                package = pack 'a. witness;\n                n = n + 1;\n            }\n            (n as i32)\n        }",
    "fn main() -> i32 {\n            let exists 'a. outer = core::cap::fresh();\n            let p = {\n                let exists 'c. inner = core::cap::fresh();\n                pack 'c. inner\n            };\n            let exists 'b. reopened = p;\n            let p = pack 'a. outer;\n            7i32\n        }",
    "\n        forall 'a. fn same(x: core::cap::Brand['a], y: core::cap::Brand['a]) -> u32 { 7u32 }\n        fn main() -> i32 {\n            let exists 'a. outer = core::cap::fresh();\n            let exists 'b. inner = core::cap::fresh();\n            ((same(outer, outer) + same(inner, inner)) as i32)\n        }",
    '\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nforall T where Exposed[T]. fn need(x: T) -> T { x }\nforall T where Exposed[T]. fn forward(x: T) -> T { need[T](x) }',
    'fn main() -> i32 {\n            let bias = 2u32;\n            let mapped = foreach item of (3u32, 4u32) { (item, bias) };\n            let (a, b) = mapped;\n            let (x, y) = a;\n            let (z, w) = b;\n            (x + y + z + w) as i32\n        }',
    'fn main() -> i32 {\n            let mapped = foreach item of (3u32, 4u32) { (item, 1u32) };\n            let (a, b) = mapped;\n            let (x, y) = a;\n            let (z, w) = b;\n            (x + y + z + w) as i32\n        }',
    "forall 'a. exposed struct Tagged { witness: core::cap::Brand['a], value: i32 }\n            forall 'a. impl Tagged['a] { fn get(self: Tagged['a]) -> i32 { self.value } }\n            fn main() -> i32 {\n                let exists 'a. witness = core::cap::fresh();\n                let f = Tagged['a]::get;\n                f(Tagged.{ witness, value: 7i32 })\n            }",
    'forall T. fn id(x: T) -> T { x }\n    forall T. exposed struct Box { value: T }\n    forall T. impl Box[T] { fn get(self: Box[T]) -> T { self.value } }\nfn main() -> i32 { let f = Box::get[i32]; f(Box.{ value: 7i32 }) }',
    'forall T. fn id(x: T) -> T { x }\n    forall T. exposed struct Box { value: T }\n    forall T. impl Box[T] { fn get(self: Box[T]) -> T { self.value } }\nuse package::id as identity;\n            fn main() -> i32 {\n                let f = package::id[i32];\n                identity[i32](f(7i32))\n            }',
    'forall T. fn bad(x: T, y: T, flag: bool) -> T { if flag { x } else { y } } fn main() -> i32 { 7i32 }',
    'forall T. fn bad(x: T, y: T) -> T { var value = x; value = y; value } fn main() -> i32 { 7i32 }',
    'forall T. fn bad(x: T) -> () { let y = x; () } fn main() -> i32 { 7i32 }',
    'forall T. fn bad(x: T) -> () { let f = || { let y = x; () }; f() } fn main() -> i32 { 7i32 }',
    '\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nfn f() -> u32 {\n  let a = 3u32; let b = 7u32;\n  let outer = |x: u32| { let a = 10u32;\n    let inner = |y: u32| b * 100 + a * 10 + x + y; inner(2) + b\n  };\n  let mapped = foreach item of (1u32, 2u32) { (item, b, a, b) }; outer(a)\n}\n',
    '\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nfn f() -> u32 { let x = id[u32](7); x }\n',
    '\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nexposed tuple Pair(u32, bool);\nfn f(o: Opt[u32]) -> Pair { var p = o; let n = match &p { Opt::Some(v) => v, Opt::None => 0u32 }; Pair.(n, true) }\n',
    '\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nfn f(c: Cur) -> u32 { var d = c; let a = &d.bump(1u32); let b = d.get(); a + b }\n',
    "\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nforall 'k. struct Tag! { b: core::cap::Brand['k] }\nfn mint() -> exists 'r. Tag!['r] { let exists 'a. w = core::cap::fresh(); pack 'a. Tag!.{ b: w } }\nfn f() -> u32 { var exists 'x. t = mint(); let { b } = t; 0u32 }\n",
    '\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nfn f(o: core::option::Option[(u32, bool)]) -> core::option::Option[u32] { let (n, b) = o?; core::option::Option::Some.(n) }\nfn g(r: core::option::Result[u32, u32]) -> core::option::Result[bool, u32] { let n = r?; core::option::Result::Ok.(n == 0) }\n',
    '\nforall Self -> Item. trait P {}\nforall Item. instance P[u32 -> Item] {}\n',
    'exposed struct Token! { n: u32 }\nfn take(t: Token!) -> u32 { let { n } = t; n }\nfn f(t: Token!, c: bool) -> u32 {\n            if c { let n = take(t); } else { }; 0u32\n        } fn main() -> i32 { 0i32 }',
    'exposed struct Token! { n: u32 }\nfn take(t: Token!) -> u32 { let { n } = t; n }\nfn peek(&t: Token!) -> u32 { t.n }\n            fn main() -> i32 {\n                var t = Token!.{ n: 7u32 }; let n = take(t); (peek(&t) as i32)\n            }',
    'exposed struct Token! { n: u32 }\nfn take(t: Token!) -> u32 { let { n } = t; n }\nfn identity(t: Token!) -> Token! { t }\n            fn main() -> i32 { (take(identity(Token!.{ n: 7u32 })) as i32) }',
    'exposed struct Token! { n: u32 }\nfn take(t: Token!) -> u32 { let { n } = t; n }\nfn main() -> i32 {\n            var t = Token!.{ n: 7u32 }; t = Token!.{ n: 9u32 }; (take(t) as i32)\n        }',
    'exposed struct Token! { n: u32 }\nfn take(t: Token!) -> u32 { let { n } = t; n }\nfn f(t: Token!, c: bool) -> u32 {\n            while c { let n = take(t); }; 0u32\n        } fn main() -> i32 { 0i32 }',
    'exposed struct Token! { n: u32 }\nfn take(t: Token!) -> u32 { var { n } = t; n }\nfn pair(a: Token!, b: Token!) -> u32 { take(a) + take(b) }\n            fn main() -> i32 { let t = Token!.{ n: 7u32 }; (pair(t, t) as i32) }\n            ',
    'exposed struct Token! { n: u32 }\nfn take(t: Token!) -> u32 { var { n } = t; n }\nfn unused(t: Token!) -> u32 { 0u32 } fn main() -> i32 { 0i32 }',
    'exposed struct Token! { n: u32 }\nfn make(n: u32) -> Token! { Token!.{ n } }\nfn take(t: Token!) -> u32 { var { n } = t; n }\nfn main() -> i32 {\n    var t = make(7u32);\n    t = make(\n        9u32\n    );\n    (take(t) as i32)\n}',
    'fn main() -> i32 { 7u32; (1u32, 2u32); 3i32 }',
    'exposed struct Token! { n: u32 }\nfn make(n: u32) -> Token! { Token!.{ n } }\nfn take(t: Token!) -> u32 { var { n } = t; n }\nfn main() -> i32 { Token!.{ n: 7u32 }; 0i32 }',
    'exposed struct Token! { n: u32 }\nfn make(n: u32) -> Token! { Token!.{ n } }\nfn take(t: Token!) -> u32 { var { n } = t; n }\nfn main() -> i32 { if true { make(7u32) } else { make(9u32) }; 0i32 }',
    'exposed struct Token! { n: u32 }\nfn make(n: u32) -> Token! { Token!.{ n } }\nfn take(t: Token!) -> u32 { var { n } = t; n }\nfn combine(t: Token!, n: u32) -> u32 { take(t) + n }\n            fn f(c: bool) -> u32 { combine(make(7u32), if c { 2u32 } else { 3u32 }) }\n            fn main() -> i32 { ((f(true) + f(false)) as i32) }\n            ',
    'exposed struct Token! { n: u32 }\nfn make(n: u32) -> Token! { Token!.{ n } }\nfn take(t: Token!) -> u32 { var { n } = t; n }\nfn pair() -> (Token!, u32) { (make(7u32), 3u32) }\n            fn main() -> i32 { (take(pair().0) as i32) }',
    'exposed struct Token! { n: u32 }\nfn make(n: u32) -> Token! { Token!.{ n } }\nfn take(t: Token!) -> u32 { var { n } = t; n }\nfn main() -> i32 {\n    let t = make(7u32);\n    0i32\n}',
    "forall 'a. exposed struct Tagged { witness: core::cap::Brand['a], n: u32 }\n            fn bump(&n: u32) -> () { n = n + 1u32; }\n            fn main() -> i32 {\n                var exists 'a. w = core::cap::fresh();\n                var tagged = Tagged.{ witness: w, n: 6u32 };\n                bump(&tagged.n);\n                tagged.n as i32\n            }",
    "forall 'a. fn touch(&x: core::cap::Brand['a]) -> u32 { 7u32 }\n            fn main() -> i32 { var exists 'a. w = core::cap::fresh(); touch(&w) as i32 }",
    "exposed struct H! { value: u32 }\n            forall 'a. exposed struct Tagged! { witness: core::cap::Brand['a], h: H! }\n            fn main() -> i32 {\n                var exists 'a. w = core::cap::fresh();\n                var tagged = Tagged!.{ witness: w, h: H!.{ value: 7u32 } };\n                let moved = tagged.h;\n                if true { let snapshot = tagged.witness; } else { () };\n                tagged.h = moved;\n                let { witness, h } = tagged;\n                let { value } = h;\n                value as i32\n            }",
    "forall 'a. trait Touch { fn touch(&x: core::cap::Brand['a]) -> u32; }\n            forall 'a. instance Touch['a] {\n                fn touch(&x: core::cap::Brand['a]) -> u32 { 7u32 }\n            }\n            fn main() -> i32 {\n                var exists 'a. w = core::cap::fresh();\n                Touch['a]::touch(&w) as i32\n            }",
    '\nexposed enum E { Pair(u32, u32), Single(u32), Unit }\nfn whole(e: E) -> u32 {\n    match e {\n        E::Pair(pair) => { let (a, b) = pair; a * 10 + b },\n        E::Single(x) => x,\n        E::Unit => 0,\n    }\n}\nfn change(&e: E) -> u32 {\n    match &e {\n        E::Pair(pair) => { let (a, b) = pair; pair = (a + 1, b + 2); a + b },\n        E::Single(x) => { x = x + 3; x },\n        E::Unit => 0,\n    }\n}\nfn split(&e: E) -> u32 {\n    match &e { E::Pair(a, _) => { a = a + 4; a }, E::Single(_) => 0, E::Unit => 0 }\n}\nfn first(e: E) -> u32 {\n    match e { E::Pair(a, _) => a, E::Single(_) => 0, E::Unit => 0 }\n}\nfn main() -> i32 {\n    var e = E::Pair.(2, 3);\n    let a = whole(e);\n    let b = change(&e);\n    let c = split(&e);\n    let d = whole(e);\n    let f = first(e);\n    var single = E::Single.(1);\n    var unit = E::Unit;\n    ((a + b + c + d + f + change(&single) + change(&unit) + whole(unit)) as i32)\n}\n',
    '\nfn bump(&x: u32) -> u32 { x = x + 1; x }\nfn combine(a: u32, b: u32) -> u32 { a * 10 + b }\nfn select(n: u32) -> fn(u32, u32) -> u32 {\n    if n == 1 { combine } else { combine }\n}\nfn main() -> i32 {\n    var n = 1u32;\n    let bumped = bump(&n);\n    let after = n;\n    let a = combine(10, if true { 3 } else { 4 });\n    let f = select(1);\n    let b = f(4, if true { 5 } else { 6 });\n    let c = combine(bump(&n), if true { n } else { 0 });\n    ((after * 10 + bumped + a + b + c) as i32)\n}\n',
    '\nfn collatz(n: u32) -> u32 {\n    var x = n;\n    var steps = 0u32;\n    while x != 1 {\n        if x % 2 == 0 { x = x / 2; } else { x = 3 * x + 1; }\n        steps = steps + 1;\n    }\n    steps\n}\nfn find(limit: u32) -> u32 {\n    var i = 2u32;\n    while i < limit {\n        if i * i > limit { return i; }\n        i = i + 1;\n    }\n    0\n}\nfn nested(n: u32) -> u32 {\n    var total = 0u32;\n    var i = 0u32;\n    while i < n {\n        var j = 0u32;\n        while j < i {\n            total = total + i * j;\n            j = j + 1;\n        }\n        i = i + 1;\n    }\n    total\n}\nfn main() -> i32 { (((collatz(27) % 100 + find(200) + nested(9)) % 251) as i32) }\n',
    '\nexposed struct Token! { count: u32, id: u32, tag: u32 }\nfn bump(t: Token!) -> Token! {\n    let { count, ..rest } = t;\n    Token!.{ count: count + 1, ..rest }\n}\nfn swap(p: (u32, u32)) -> (u32, u32) { var (a, b) = p; (b, a) }\nforall T. fn first(p: (T, u32)) -> T { var (a, b) = p; a }\nexposed struct Pt { x: u32, y: u32 }\nexposed struct Line { a: Pt, b: Pt }\nfn main() -> i32 {\n    let t = bump(bump(Token!.{ count: 1, id: 5, tag: 7 }));\n    let { count, id, tag } = t;\n    let (x, y) = swap((count, id));\n    let pair = ((1u32, 2u32), (3u32, 4u32));\n    let (l1, r1) = pair;\n    var (a, b) = l1;\n    let (c, d) = r1;\n    let l = Line.{ a: Pt.{ x: 1, y: 2 }, b: Pt.{ x: 30, y: 40 } };\n    let arr = [a, b, c, d];\n    var [p, q, r, s] = arr;\n    let w = l.b.y + l.a.x + first((9u32, 1u32));\n    ((x * 10 + y + tag + a + b + c + d + w + p + q + r + s) as i32)\n}\n',
    '\nforall Self. trait Scale { fn scale(self: Self, f: u32) -> Self; }\nexposed struct Cube { side: u32 }\ninstance Scale[Cube] {\n    fn scale(self: Cube, f: u32) -> Cube { let { side: s } = self; Cube.{ side: s * f } }\n}\ninstance Scale[u32] { fn scale(self: u32, f: u32) -> u32 { self * f } }\nforall T where Scale[T]. fn twice(x: T, f: u32) -> T { let once = x.scale(f); once.scale(f) }\nimpl Cube { fn volume(self) -> u32 { self.side * self.side * self.side } }\nforall A, B. trait Pairing { fn pair(a: A, b: B) -> u32; }\ninstance Pairing[u32, bool] { fn pair(a: u32, b: bool) -> u32 { if b { a } else { 0 } } }\nfn main() -> i32 {\n    var c = twice(Cube.{ side: 2 }, 3);\n    let v = c.volume();\n    let n = twice(5u32, 2);\n    var p = Pairing[u32, bool]::pair(7, true);\n    (((v + n + p) % 251) as i32)\n}\n',
    '\nexposed struct Box! { v: u32, n: u32 }\nexposed struct Pool! { free: u32, used: u32 }\nimpl Pool! {\n    fn take(&self) -> u32 {\n        let { free, used } = self;\n        self = Pool!.{ free: free - 1, used: used + 1 };\n        free\n    }\n    fn size(&self) -> u32 {\n        let { free, used } = self;\n        self = Pool!.{ free, used };\n        free + used\n    }\n}\nfn open(&b: Box!) -> u32 { let { v, n } = b; b = Box!.{ v: v + 1, n }; v + n }\nfn main() -> i32 {\n    var p = Pool!.{ free: 10, used: 0 };\n    var b = Box!.{ v: 4, n: 5 };\n    var i = 0u32;\n    var sum = 0u32;\n    while i < 4 {\n        sum = sum + &p.take() + open(&b);\n        i = i + 1;\n    }\n    let size = &p.size();\n    let { free, used } = p;\n    let { v, n } = b;\n    (((sum + size + free + used + v + n) % 251) as i32)\n}\n',
    '\nfn add3(a: u32, b: u32, c: u32) -> u32 { a * 100 + b * 10 + c }\nfn pick(x: u32) -> u32 { if x > 3 { 1 } else { 2 } }\nfn main() -> i32 {\n    let x = 5u32;\n    let y = 2u32;\n    let a = add3(pick(x), match y { 0 => 7, 1..=3 => 4, _ => 9 }, if x > 1 && y > 1 { 3 } else { 8 });\n    let t = (if x > 9 { 1u32 } else { 2u32 }, match y { 2 => 5u32, _ => 6u32 }, x);\n    let (p, q, r) = t;\n    (((a + p + q + r) % 251u32) as i32)\n}\n',
    '\nexposed struct Cur { n: u32 }\nexposed struct Tok! { id: u32 }\nforall Env where Copy[Env].\nfn twice(f: closure[Env](u32) -> u32, x: u32) -> u32 { f(f(x)) }\nforall Env.\nfn with_cur(&c: Cur, f: closure[Env](&_: Cur) -> u32) -> u32 { f(&c) }\nforall Env.\nfn once(f: closure[Env](u32) -> u32, x: u32) -> u32 { f(x) }\nfn adder(base: u32) -> u32 {\n    let add = |x: u32| x + base;\n    add(5u32)\n}\nfn main() -> i32 {\n    let k = 10u32;\n    let a = twice(|x| x * 2 + k, 3);\n    var c = Cur.{ n: 7 };\n    let b = with_cur(&c, |&cur| -> u32 { cur.n = cur.n + 1; cur.n * 2 });\n    let t = Tok!.{ id: 4 };\n    let d = once(|x: u32| -> u32 { let { id } = t; id + x }, 3);\n    let { n } = c;\n    (((a + b + d + adder(1) + n) % 251) as i32)\n}\n',
    '\nforall T. exposed struct W { v: T }\nforall T. fn wrap(x: T) -> W[T] { W.{ v: x } }\nexposed struct S { a: u32, b: u32 }\nfn main() -> i32 {\n    let s = S.{ a: 7u32, b: 9u32 };\n    let parts = foreach f of s { wrap(f) };\n    let { a: x, b: y } = parts;\n    let { v: p } = x;\n    let { v: q } = y;\n    ((p * 10 + q) as i32)\n}\n',
    '\nfn main() -> i32 {\n    let a = 3u32;\n    let b = 7u32;\n    let outer = |x: u32| {\n        let a = 10u32;\n        let inner = |y: u32| b * 100 + a * 10 + x + y;\n        inner(2) + b\n    };\n    let mapped = foreach item of (1u32, 2u32) { (item, b, a, b) };\n    let (left, right) = mapped;\n    let (p, q, r, s) = left;\n    (((outer(a) + p + q + r + s) % 251u32) as i32)\n}\n',
    '\nextern "C" fn malloc(size: u64) -> *u8;\nextern "C" fn free(p: *u8) -> ();\nexposed struct Pair { a: u32, b: u32 }\nfn main() -> i32 {\n    let raw = unsafe malloc(8u64);\n    let p = unsafe (raw as *Pair);\n    unsafe *p = Pair.{ a: 20, b: 22 };\n    let v = unsafe *p;\n    let { a, b } = v;\n    let w = unsafe free(raw);\n    ((a + b) as i32)\n}\n',
    '\nforall Self. trait Step { fn step(&x: Self) -> u32; }\ninstance Step[u32] { fn step(&x: u32) -> u32 { x + 1 } }\nfn main() -> i32 { 0 }\n',
    'exposed enum E { A, B }\nfn main() -> i32 { match E::A { E::A => 1i32 } }',
    'exposed enum E { A, B }\nfn main() -> i32 { match E::A { E::A | E::A => 1i32, E::B => 3i32 } }',
    'fn main() -> i32 { match 0u8 { 0 => 1i32 } }',
    'fn main() -> i32 { match 0u8 { 256 => 1i32, _ => 2i32 } }',
    'fn classify(n: i8) -> i32 {\n                match n { -4..=-1 => 2i32, 0 | 1 => 3i32, _ => 5i32 }\n            }\n            fn main() -> i32 { classify(-2i8) + classify(1i8) + classify(9i8) }\n            ',
    'exposed struct Token! { n: u32 }\n            exposed enum E! { A(Token!), B }\n            fn main() -> i32 {\n                var e = E!::A.(Token!.{ n: 7u32 });\n                let n = match &e { _ => 3u32 };\n                let m = match e { E!::A(t) => { let { n } = t; n }, E!::B => 0u32 };\n                ((n + m) as i32)\n            }',
    'exposed struct Token! { n: u32 }\n                    exposed struct Pair! { n: u32, token: Token! }\n                    fn main() -> i32 { let (x, _) = (1u32, Token!.{ n: 7u32 }); 0i32 }',
    'exposed struct Token! { n: u32 }\n                    exposed enum E! { A(Token!), B }\n                    fn main() -> i32 { match E!::A.(Token!.{ n: 7u32 }) { E!::A => 0i32, E!::B => 1i32 } }',
    'exposed struct Token! { n: u32 }\n                    exposed enum E! { A(Token!), B }\n                    fn main() -> i32 { match E!::A.(Token!.{ n: 7u32 }) { _ => 0i32 } }',
    'exposed struct Token! { n: u32 }\n            exposed enum E! { A(Token!), B(Token!) }\n            fn take(e: E!) -> u32 {\n                match e {\n                    E!::A(t) => { var { n } = t; n },\n                    E!::B(t) => { var { n } = t; n + 1 }\n                }\n            }\n            fn main() -> i32 {\n                ((take(E!::A.(Token!.{ n: 7u32 })) + take(E!::B.(Token!.{ n: 9u32 }))) as i32)\n            }',
    'exposed struct Token! { n: u32 }\n            exposed enum E! { A(Token!, u32), B }\n            fn main() -> i32 {\n                match E!::A.(Token!.{ n: 7u32 }, 9u32) {\n                    E!::A(t, _) => { var { n } = t; (n as i32) },\n                    E!::B => 0i32\n                }\n            }',
    '\n            forall T. trait P {}\n            instance P[u32] {}\n            forall T where P[T]. exposed struct Box { value: T }\n            forall T where P[T]. fn id(x: Box[T]) -> Box[T] { x }\n            fn main() -> i32 { var x = id(Box.{ value: 7u32 }); x.value as i32 }\n        ',
    'forall T. trait P {} forall T where P[T]. exposed struct Box { value: T } struct Outer { ptr: *Box[u32] } fn main() -> i32 { 7i32 }',
    "\nforall 'a. exposed struct Token! { witness: core::cap::Brand['a] }\nfn branch(c: bool) -> u32 {\n    if c {\n        let exists 'a. witness = core::cap::fresh();\n        let token = Token!['a].{ witness: witness };\n        1u32\n    } else { 2u32 }\n}\n",
    '\nexposed struct Token! { n: u32 }\nfn bump(&t: Token!) -> u32 { t.n = t.n + 1u32; t.n }\nfn consume(t: Token!, n: u32) -> u32 { var { n: old } = t; old + n }\nfn use_token(t: Token!) -> u32 { var t = t; var n = bump(&t); consume(t, n) }',
    '\nfn bump(&x: u32) -> u32 { x = x + 1u32; x }\nfn combine(a: u32, b: u32) -> u32 { a + b }\nfn main() -> u32 { var n = 1u32; combine(bump(&n), n) }',
    '\nfn bump(&x: u32) -> u32 { x = x + 1u32; x }\nfn combine(a: u32, b: u32) -> u32 { a + b }\nexposed struct Pair { a: u32, b: u32 }\nfn bad(&p: Pair) -> u32 { combine(p.a, bump(&p.a)) }',
    'fn f() -> bool { ((return true)) == ((return false)) }\n            fn main() -> i32 { if f() { 7i32 } else { 1i32 } }',
    'fn same(a: *u32, b: *u32) -> bool { a == b }\n            fn main() -> i32 { 7i32 }',
    'const WRAP: u8 = 250 + 10;\n            const NEG: i8 = -128; const AGAIN: i8 = -NEG;\n            fn main() -> i32 {\n                let x = 250u8; let y = x + 10;\n                if WRAP == 4 && y == 4 && AGAIN == -128 { 7i32 } else { 1i32 }\n            }',
    'fn f() -> u8 {\n    256\n}\nfn main() -> i32 { 0i32 }',
    'fn f() -> f32 { 1000000000000000000000000000000000000000.0 } fn main() -> i32 { 0i32 }',
    'fn f() -> f64 { 1000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000.0 } fn main() -> i32 { 0i32 }',
    'fn f() -> i8 { -129 } fn main() -> i32 { 0i32 }',
    'fn f() -> u8 { -1 } fn main() -> i32 { 0i32 }',
    'forall T -> U. trait Convert { fn convert(x: T) -> U; }\n            instance Convert[u32 -> i32] { fn convert(x: u32) -> i32 { x as i32 } }\n            forall T, U where Convert[T -> U]. fn run(x: T) -> U { T::convert(x) }\n            fn main() -> i32 {\n                let a = Convert[u32]::convert(3u32);\n                let b = run(4u32);\n                a + b\n            }',
    'forall A, B. trait Pairing { fn sum(a: A, b: B) -> u32; }\n    instance Pairing[u32, u32] { fn sum(a: u32, b: u32) -> u32 { a + b } }\nfn main() -> i32 { Pairing[u32]::sum(3u32, 4u32) as i32 }',
    'forall A, B. trait Pairing { fn sum(a: A, b: B) -> u32; }\n    instance Pairing[u32, u32] { fn sum(a: u32, b: u32) -> u32 { a + b } }\nfn main() -> i32 { Pairing[_, u32]::sum(3u32, 4u32) as i32 }',
    "forall 'a. trait Read { fn read(x: core::cap::Brand['a]) -> i32; }\n            forall 'a. instance Read['a] { fn read(x: core::cap::Brand['a]) -> i32 { 7i32 } }\n            fn main() -> i32 {\n                let exists 'a. witness = core::cap::fresh();\n                Read['a]::read(witness)\n            }",
    "forall 'a. trait Read { fn read(x: core::cap::Brand['a]) -> i32; }\n            forall 'a. instance Read['a] { fn read(x: core::cap::Brand['a]) -> i32 { 7i32 } }\n            fn main() -> i32 {\n                let exists 'a. witness = core::cap::fresh();\n                Read[_]::read(witness)\n            }",
    'forall T where Copy[T]. fn twice(x: T) -> (T, T) { (x, x) }\n            fn main() -> i32 { let f = |x| twice(x); let (a, b) = f(3i32); a + b + 1i32 }',
    # A map's items are the components of the value it maps, at its own binder.
    '\nforall T. exposed struct W { v: T }\nforall T. fn wrap(x: T) -> W[T] { W.{ v: x } }\nexposed struct S { a: u32, b: u32 }\n'
    'fn main() -> i32 {\n    let s = S.{ a: 7u32, b: 9u32 };\n    let t = foreach a: A of s { wrap(a) };\n'
    '    let u = foreach b of t { var c = b; c };\n    let v = foreach b: B of u { let d = b; d };\n    0i32\n}\n',
    # Sibling `foreach`es may write the same binder, and a type written in a body names its own.
    'exposed struct S { a: u32, b: u32 }\nfn main() -> i32 {\n    let s = S.{ a: 7u32, b: 9u32 };\n'
    '    let t = foreach a: A of s { var c: A = a; c };\n    let u = foreach b: A of s { let d: A = b; d };\n'
    '    let { a: x, b: y } = u;\n    (x + y) as i32\n}\n',
    # A function value quantifies nothing: its brand is inferred from its call, here h's 'b.
    BRAND_VALUE + "forall 'b. fn h(w: core::cap::Brand['b]) -> fn(core::cap::Brand['b], u32) -> u32 {\n"
    "    let f = g;\n    let n = f(w, 1u32);\n    f\n}\n",
    # A map's binder may be named as the binder of the value it maps, which binds nothing in its body.
    "exposed struct S { a: u32, b: u32 }\nfn main() -> i32 {\n    let s = S.{ a: 7u32, b: 9u32 };\n"
    "    let t = foreach a: A of s { (a, 1u8) };\n    let u = foreach b: A of t { var (c, n) = b; let d: A = c; d };\n"
    "    let { a: x, b: y } = u;\n    (x + y) as i32\n}\n",
    # `Sized` of a concrete type of non-zero size, of a parameter and of a tuple of parameters under the
    # caller's own premise for exactly it, and of a type with a part of bytes whatever the parameter is.
    "forall T where Sized[T]. fn need(x: T) -> T { x }\nstruct P { a: (), b: u8 }\n"
    "fn concrete(p: P, q: *()) -> *() { let r = need(p); need(q) }\n"
    "forall U where Sized[U]. fn forward(x: U) -> U { need[U](x) }\n"
    "forall U, V where Sized[(U, V)]. fn pair(x: (U, V)) -> (U, V) { need(x) }\n"
    "forall U. fn leaf(x: (U, u8)) -> (U, u8) { need(x) }\n",
    # A var binds what a let binds, and each name it binds is a var.
    "exposed struct C { n: u32 }\nimpl C { fn tick(&self) -> u32 { self.n } }\n"
    "fn f(p: (C, u32)) -> u32 { var (c, k) = p; k = &c.tick(); var [x, y] = [1u32, 2u32]; y = x; k + y }\n",
)

INFER_ERRORS = (
    # `f"...".write(&cx, out)` lends a var's place, which no `{x}` of the f-string uses while it is lent.
    (CONTEXT + "forall 'k. fn f(&cx: Domain!['k], s: Span['k]) -> Buffer! {\n"
     "    f\"{s} {Span::from_static(&cx, \"t\")}\".write(&cx, Buffer!::new())\n}\n",
     "5:31: cx is lent to this f-string; it cannot be used inside it"),
    # The whole local is lent, though only a field is the place.
    (CONTEXT + "forall 'k. fn f(&p: (u32, Domain!['k]), s: Span['k]) -> Buffer! {\n"
     "    f\"{s} {p.0}\".write(&p.1, Buffer!::new())\n}\n",
     "5:12: p is lent to this f-string; it cannot be used inside it"),
    (CONTEXT + "forall 'k. fn f(cx: Domain!['k], s: Span['k]) -> (Buffer!, Domain!['k]) {\n"
     "    (f\"{s}\".write(&cx, Buffer!::new()), cx)\n}\n",
     "5:20: cx is not a var; a lent place must be a var"),
    (CONTEXT + "forall 'k. fn f(&cx: Domain!['k], s: Span['k]) -> Buffer! {\n"
     "    f\"{s}\".write(cx, Buffer!::new())\n}\n",
     "5:18: write lends its context first: .write(&cx, out)"),
    # A label's scrutinee whose type is not known yet is reported at the match; one whose type is solved is
    # named solved.
    ('forall T. fn g() -> T { g() }\nfn f() -> u32 {\n    match g() {\n        Some(x) => 0u32,\n        _ => 1u32,\n    }\n}\n',
     '3:5: the type here is not known yet'),
    ('use std::option::Option;\nfn f() -> u32 {\n    let o = Option::None;\n    let u: Option[u8] = o;\n'
     '    match o {\n        Nothing => 0u32,\n        _ => 1u32,\n    }\n}\n', '6:9: Option[u8] has no variant Nothing'),
    ('fn f() -> u32 { let g = |x| x.0; 0u32 }\n', 'a type here is not known yet'),
    ('use std::option::Option;\nfn f(o: std::buffer::Buffer!) -> u32 { let { len } = o; 0u32 }\n', 'is not exposed here'),
    ('exposed struct P { x: u32 }\nfn f(p: P) -> u32 { let { y } = p; y }\n', 'no field y'),
    ('exposed struct P { x: u32, y: u32 }\nfn f(p: P) -> u32 { var { x, ..rest } = p; let r = rest; x }\n', 'is a ..rest bundle'),
    ('exposed struct P { x: u32 }\nfn f() -> u32 { let q = P; 0u32 }\n', 'is not a value'),
    ('use core::num;\nfn f() -> u64 { f64[u8]::to_bits(1.0) }\n', 'need a named impl target'),
    ('exposed enum E { A(u32), B }\nfn f() -> E { E::A }\n', 'carries a payload'),
    ('exposed enum E { A(u32), B }\nfn f() -> E { E::A.(1u32, 2u32) }\n', 'takes one payload'),
    ('fn f() -> u32 { let a: u32 = [1u32, ..]; a }\n', 'needs its length from the expected type'),
    ('exposed struct P { x: u32, y: u32 }\nfn f(p: P) -> P { let q = p; P.{ x: 1u32, ..q } }\n', 'is not a ..rest bundle'),
    ('exposed struct P { x: u32, y: u32 }\nexposed struct Q { x: u32, z: u32 }\nfn f(p: P) -> Q { var { x, ..rest } = p; Q.{ x, ..rest } }\n', 'has no field z'),
    ('use std::iter::range;\nfn f() -> u32 { for i in range(0u32, 3u32) { } 0u32 }\n', 'a for loop lends its iterator'),
    ('fn f(b: bool) -> u32 { match b { 1 => 0u32, _ => 1u32 } }\n', 'value patterns need an integer'),
    ('exposed enum E { A(u32), B(u32) }\nfn f(e: E) -> u32 { match e { E::A(x) | E::B(x) => x } }\n', 'a grouped arm binds no payload'),
    ('exposed enum E { A(u32), B }\nfn f(e: E) -> u32 { match e { E::A(x, y) => x, _ => 0u32 } }\n', 'does not carry 2 values'),
    ("fn f() -> u32 { var x: u32 = pack 'static. 1u32; x }\n", 'pack does not match'),
    ('fn f(x: u32) -> u32 { let y = &x; 0u32 }\n', '& lends a place only'),
    # A lend is a way to pass a place: in parentheses it is no receiver, for a local or a raw pointer's target.
    ('exposed struct C { n: u32 }\nimpl C { fn get(&self) -> u32 { self.n } }\nfn f(p: *C) -> u32 { (unsafe &*p).get() }\n',
     "3:23: a lend in parentheses is no receiver: a local's or a field's is lent as `&x.m()`, and a raw pointer's target "
     "as an argument, `T::m(unsafe &*p)`"),
    ('exposed struct C { n: u32 }\nimpl C { fn get(&self) -> u32 { self.n } }\nfn f(c: C) -> u32 { var d = c; (&d).get() }\n',
     "3:33: a lend in parentheses is no receiver"),
    ('exposed struct K { env: (), code: u32 }\nfn f(k: K) -> u32 { k(1u32) }\n', 'must have a function type'),
    ('fn g(x: u32) -> u32 { x }\nfn f() -> u32 { g(1u32, 2u32) }\n', 'expected 1 argument, got 2'),
    ('trait Show { fn show(x: Self) -> u32; }\nforall T. fn f(x: T) -> u32 { T::show(x) }\n', 'needs exactly one bound'),
    ('fn f() -> u32 { core::ptr::NonNull(); 0u32 }\n', 'unknown builtin NonNull'),
    ('fn f() -> u32 { core::atomic::fence(); 0u32 }\n', 'fence takes one ordering argument'),
    ('fn f() -> u32 { let b = core::cap::static_brand(1u32); 0u32 }\n', 'takes 0 value arguments'),
    ('fn f() -> u32 { let p = core::mem[u32]::alloca[u32](); 0u32 }\n', 'type arguments belong on alloca, not a namespace'),
    ('fn f() -> u32 { let p = core::mem::alloca(); 0u32 }\n', 'alloca takes 1 type argument'),
    ('use std::buffer::Buffer!;\nfn f() -> u32 { let b = f"x".write(); 0u32 }\n', 'write takes the writer, or a lent context and the writer'),
    ('use std::buffer::Buffer!;\nfn f(x: *u8) -> Buffer! { f"x = {x}" }\n', '2:34: no instance of Format[*u8, Buffer!]'),
    ("use std::buffer::Buffer!;\nuse std::raw::Span;\nfn f(s: Span['static]) -> Buffer! { var s = s; f\"{&s}\".write(Buffer!::new()) }\n",
     "3:51: no instance of Format[Span['static], Buffer!]"),
    ('use std::buffer::Buffer!;\nforall T. fn f(x: T) -> Buffer! { f"{x}" }\n', 'no instance of Format[T, Buffer!]'),
    ('exposed struct C { n: u32 }\nimpl C { fn get(self) -> u32 { 0u32 } }\nfn f(c: C) -> u32 { var d = c; &d.get() }\n', 'takes its receiver by value'),
    ('trait A { fn go(x: Self) -> u32; }\ntrait B { fn go(x: Self) -> u32; }\nfn f(x: u8) -> u32 { x.go() }\n', 'has several methods named go'),
    ('fn f(p: *u32) -> u32 { unsafe p.store() }\n', 'store takes 1 argument'),
    ('fn f(p: u32) -> u32 { core::ptr::load_volatile(p) }\n', 'needs a pointer'),
    ('exposed struct P { x: u32 }\nimpl P { fn make() -> P { P.{ x: 1u32 } } }\nfn f(p: P) -> P { p.make() }\n', 'make takes no receiver'),
    ("fn f() -> u32 { let x: core::cap::Brand['q] = core::cap::static_brand(); 0u32 }\n", "brand 'q is not in scope"),
    # An exists type written in a body binds no brand in scope there: a parameter's or an opening's.
    ("forall 'k. fn f(w: core::cap::Brand['k]) -> u32 {\n    let p: exists 'k. core::cap::Brand['k] = pack 'k. w;\n    0u32\n}\n",
     "exists binder 'k shadows a brand in scope"),
    ("fn f() -> u32 {\n    let exists 'a. w = core::cap::fresh();\n    let p: exists 'a. core::cap::Brand['a] = pack 'a. w;\n    0u32\n}\n",
     "exists binder 'a shadows a brand in scope"),
    ('forall T. trait Show { fn show(x: T) -> u32; }\nforall T where Show[(T, T)]. instance Show[T] { fn show(x: T) -> u32 { 0u32 } }\n', 'recursive instance premise Show -> Show:'),
    ('trait Show { fn show(x: Self) -> u32; }\ninstance Show[u32] { fn show(x: u32) -> u32 { 0u32 } }\ninstance Show[u32] { fn show(x: u32) -> u32 { 1u32 } }\n', 'overlapping instances of Show: (u32) and (u32)'),
    ('trait Show { fn show(x: Self) -> u32; }\nfn f() -> u32 { let s = Show; 0u32 }\n', 'is not a value'),
    ("fn f() -> u32 { let g = |x| { let exists 'a. y = x; 0u32 }; 0u32 }\n", 'the type here is not known yet'),
    ('exposed struct P { x: u32 }\nfn f() -> u32 { let p = P.{ x: 1u32, y: 2u32 }; 0u32 }\n', 'is unknown or given twice'),
    ('fn f(o: (u32, u32)) -> u32 { o.2 }\n', 'has no component 2'),
    *VISIBILITY_ERRORS,
    ("forall T. fn bad(x: T) -> () { let exists 'a. y = x; () } fn main() -> i32 { 7i32 }", 'let exists opens'),
    ('forall T. fn bad(x: T) -> () { let y = x.field; () } fn main() -> i32 { 7i32 }', 'T has no'),
    ('forall T. fn bad(x: T) -> () { let y = x.0; () } fn main() -> i32 { 7i32 }', 'T has no'),
    ('forall T. fn bad(x: T) -> () { let (a, b) = x; () } fn main() -> i32 { 7i32 }', 'pattern does not'),
    ('forall T. fn bad(x: T) -> () { let { field } = x; () } fn main() -> i32 { 7i32 }', 'a struct pattern'),
    ('forall T. fn bad(x: T) -> T { -x } fn main() -> i32 { 7i32 }', 'operator - is'),
    ('forall T. fn bad(x: T) -> T { !x } fn main() -> i32 { 7i32 }', 'operator ! is'),
    ('exposed struct Callable { env: u32, code: fn() -> u32 }\nfn code() -> u32 { 4u32 }\n                    fn main() -> i32 {\n                        let code_value = code;\n                        let f = Callable.{ env: 4u32, code: code_value };\n                        (f() as i32)\n                    }', 'a closure code'),
    ('exposed struct Callable { code: fn(u32) -> u32 }\n                    fn code(env: u32) -> u32 { env }\n                    fn main() -> i32 {\n                        let f = Callable.{ code: code };\n                        (f() as i32)\n                    }', 'a closure must'),
    ('exposed struct Callable { env: bool, code: fn(u32) -> u32 }\n                    fn code(env: u32) -> u32 { env }\n                    fn main() -> i32 {\n                        let f = Callable.{ env: true, code: code };\n                        (f() as i32)\n                    }', 'type mismatch: bool'),
    ('fn main() -> i32 { const T: u32 = 5u8; 0i32 }', '1:35: const T has type u8, but its declared type is u32'),
    ('forall T, U. exposed struct S { first: T, second: U }\n    forall T, U. exposed tuple Pair(T, U);\n    forall T, U. exposed enum E { Item(T, U), Empty }\n    exposed struct Plain { n: u32 }\n    exposed tuple Single(u32);\n    exposed enum Flag { On }\nfn main() -> i32 { let x = E[u32, u32]::Empty[bool]; 7i32 }', 'type arguments belong'),
    ('forall T, U. exposed struct S { first: T, second: U }\n    forall T, U. exposed tuple Pair(T, U);\n    forall T, U. exposed enum E { Item(T, U), Empty }\n    exposed struct Plain { n: u32 }\n    exposed tuple Single(u32);\n    exposed enum Flag { On }\nfn main() -> i32 { let x = S[u32, u32, bool].{ first: 3u32, second: 4u32 }; 7i32 }', 'accepts at most'),
    ('forall T, U. exposed struct S { first: T, second: U }\n    forall T, U. exposed tuple Pair(T, U);\n    forall T, U. exposed enum E { Item(T, U), Empty }\n    exposed struct Plain { n: u32 }\n    exposed tuple Single(u32);\n    exposed enum Flag { On }\nfn main() -> i32 { let x = Pair[u32, u32, bool].(3u32, 4u32); 7i32 }', 'accepts at most'),
    ('forall T, U. exposed struct S { first: T, second: U }\n    forall T, U. exposed tuple Pair(T, U);\n    forall T, U. exposed enum E { Item(T, U), Empty }\n    exposed struct Plain { n: u32 }\n    exposed tuple Single(u32);\n    exposed enum Flag { On }\nfn main() -> i32 { let x = E[u32, u32, bool]::Item.(3u32, 4u32); 7i32 }', 'accepts at most'),
    ('forall T, U. exposed struct S { first: T, second: U }\n    forall T, U. exposed tuple Pair(T, U);\n    forall T, U. exposed enum E { Item(T, U), Empty }\n    exposed struct Plain { n: u32 }\n    exposed tuple Single(u32);\n    exposed enum Flag { On }\nfn main() -> i32 { let x = package[bool]::Plain.{ n: 7u32 }; 7i32 }', 'type arguments belong'),
    ('forall T, U. exposed struct S { first: T, second: U }\n    forall T, U. exposed tuple Pair(T, U);\n    forall T, U. exposed enum E { Item(T, U), Empty }\n    exposed struct Plain { n: u32 }\n    exposed tuple Single(u32);\n    exposed enum Flag { On }\nfn main() -> i32 { let x = package[bool]::Single.(7u32); 7i32 }', 'type arguments belong'),
    ('forall T, U. exposed struct S { first: T, second: U }\n    forall T, U. exposed tuple Pair(T, U);\n    forall T, U. exposed enum E { Item(T, U), Empty }\n    exposed struct Plain { n: u32 }\n    exposed tuple Single(u32);\n    exposed enum Flag { On }\nfn main() -> i32 { var x = package[bool]::Flag::On; 7i32 }', 'type arguments belong'),
    ('forall T, U. exposed struct S { first: T, second: U }\n    forall T, U. exposed tuple Pair(T, U);\n    forall T, U. exposed enum E { Item(T, U), Empty }\n    exposed struct Plain { n: u32 }\n    exposed tuple Single(u32);\n    exposed enum Flag { On }\nexport package::Flag::On; fn main() -> i32 { var x = package[bool]::On; 7i32 }', 'type arguments belong'),
    ('forall T, U. exposed struct S { first: T, second: U }\n    forall T, U. exposed tuple Pair(T, U);\n    forall T, U. exposed enum E { Item(T, U), Empty }\n    exposed struct Plain { n: u32 }\n    exposed tuple Single(u32);\n    exposed enum Flag { On }\nfn main() -> i32 { var x = Plain[u32].{ n: 7u32 }; 7i32 }', 'accepts at most'),
    ('forall T, U. exposed struct S { first: T, second: U }\n    forall T, U. exposed tuple Pair(T, U);\n    forall T, U. exposed enum E { Item(T, U), Empty }\n    exposed struct Plain { n: u32 }\n    exposed tuple Single(u32);\n    exposed enum Flag { On }\nfn main() -> i32 { var x = Single[u32].(7u32); 7i32 }', 'accepts at most'),
    ('forall T, U. exposed struct S { first: T, second: U }\n    forall T, U. exposed tuple Pair(T, U);\n    forall T, U. exposed enum E { Item(T, U), Empty }\n    exposed struct Plain { n: u32 }\n    exposed tuple Single(u32);\n    exposed enum Flag { On }\nfn main() -> i32 { var x = Flag[u32]::On; 7i32 }', 'accepts at most'),
    ('exposed struct Token! {}\n                forall T. exposed struct Box { value: T }\n                forall T. impl Box[T] {\n                    forall U where Copy[T], Copy[U]. fn drop(self: Box[T], unused: U) -> () { () }\n                }\n                fn main() -> i32 { Box::drop(Box.{ value: Token!.{} }, ()); 7i32 }', 'type argument must'),
    ('forall T. fn bad(x: T) -> () { var x = x; let result = core::mem::enum_variant(&x); () } fn main() -> i32 { 0i32 }', 'enum_variant requires an'),
    ('exposed enum E { A } fn main() -> i32 { let tag = core::mem::enum_variant(&E::A); 0i32 }', 'enum_variant(&place) requires a'),
    ("\n            fn bad(p: exists 'x, 'y. (core::cap::Brand['x], core::cap::Brand['y])) -> u32 {\n                let exists 'a, 'a. (x, y) = p;\n                0u32\n            }\n            fn main() -> i32 { 0i32 }\n            ", 'an existential opening'),
    ("fn main() -> i32 {\n                let p = pack 'missing. 7u32;\n                0i32\n            }", 'pack brand missing'),
    ('\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nforall T where Exposed[T]. fn need(x: T) -> T { x }\nforall T. fn bad(x: T) -> T { need[T](x) }', 'the shape of T is not visible here: write `where Exposed[T]`, or expose its declaration to this module'),
    ('forall T. fn id(x: T) -> T { x }\n    forall T. exposed struct Box { value: T }\n    forall T. impl Box[T] { fn get(self: Box[T]) -> T { self.value } }\nfn main() -> i32 { Box[i32, bool]::get(Box.{ value: 7i32 }) }', 'the impl type'),
    ('forall T. fn id(x: T) -> T { x }\n    forall T. exposed struct Box { value: T }\n    forall T. impl Box[T] { fn get(self: Box[T]) -> T { self.value } }\nfn main() -> i32 { let f = id[i32]; f[bool](7i32) }', 'explicit type arguments'),
    ('forall T. fn id(x: T) -> T { x }\n    forall T. exposed struct Box { value: T }\n    forall T. impl Box[T] { fn get(self: Box[T]) -> T { self.value } }\nfn main() -> i32 { id[i32, bool](7i32) }', 'id takes 1'),
    ('forall T. fn id(x: T) -> T { x }\n    forall T. exposed struct Box { value: T }\n    forall T. impl Box[T] { fn get(self: Box[T]) -> T { self.value } }\nfn main() -> i32 { package[bool]::id(7i32) }', 'function type arguments'),
    ('forall T. fn id(x: T) -> T { x }\n    forall T. exposed struct Box { value: T }\n    forall T. impl Box[T] { fn get(self: Box[T]) -> T { self.value } }\nexport package::Box::get; fn main() -> i32 { package[i32]::get(Box.{ value: 7i32 }) }', 'method type arguments'),
    ('\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nfn f(n: u32) -> u32 { let m = n; m = 1u32; m }\n', 'm is not'),
    ('\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nfn f(c: Cur) -> u32 { var d = c; let n = d.bump(1u32); n }\n', 'bump takes its'),
    ('\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nfn g(&c: Cur) -> u32 { 0u32 }\nfn f(c: Cur) -> u32 { g(c) }\n', 'an & parameter'),
    ("\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nfn f() -> u32 { let exists 'a. x = 5u32; 0u32 }\n", 'let exists opens'),
    ('\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nexposed struct Other { x: u32 }\nfn f(o: Other) -> u32 { var p = o; let n = &p.step(); 0u32 }\n', 'no instance of'),
    ('\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nfn f(n: u32) -> u32 { n(1u32) }\n', 'u32 is not'),
    ('\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nfn f() -> Cur { Cur.{ } }\n', "missing field pos"),
    ('\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nfn f() -> Cur { Cur.{ pos: 1u32, pos: 2u32 } }\n', 'field pos is'),
    ('\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nfn f(o: Opt[u32]) -> Res[u32, u32] { let v = o?; Res::Ok.(v) }\n', '? needs the'),
    ('\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nfn f() -> u32 { let x = 5; 0u32 }\n', "an integer literal's"),
    ('\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nfn f(c: Cur) -> u32 { c.nothing() }\n', 'has no method'),
    ('\nforall T. exposed enum Opt { Some(T), None }\nforall T, E. exposed enum Res { Ok(T), Err(E) }\nexposed struct Cur { pos: u32 }\nimpl Cur {\n    fn bump(&self, by: u32) -> u32 { by }\n    fn get(self) -> u32 { 0u32 }\n}\nforall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }\ninstance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }\nforall T. fn id(x: T) -> T { x }\nfn f(c: Cur) -> u32 { c.nothing }\n', 'has no component'),
    ('forall T. fn bad(x: T, y: T) -> T { x + y } fn main() -> i32 { 0i32 }', 'operator + is'),
    ('fn bad(a: bool, b: bool) -> bool { a + b } fn main() -> i32 { 0i32 }', 'operator + is'),
    ('fn bad(a: bool, b: bool) -> bool { a - b } fn main() -> i32 { 0i32 }', 'operator - is'),
    ('fn bad(a: bool, b: bool) -> bool { a * b } fn main() -> i32 { 0i32 }', 'operator * is'),
    ('fn bad(a: bool, b: bool) -> bool { a / b } fn main() -> i32 { 0i32 }', 'operator / is'),
    ('fn bad(a: bool, b: bool) -> bool { a % b } fn main() -> i32 { 0i32 }', 'operator % is'),
    ('fn bad(a: bool, b: bool) -> bool { a & b } fn main() -> i32 { 0i32 }', 'operator & is'),
    ('fn bad(a: bool, b: bool) -> bool { a | b } fn main() -> i32 { 0i32 }', 'operator | is'),
    ('fn bad(a: bool, b: bool) -> bool { a ^ b } fn main() -> i32 { 0i32 }', 'operator ^ is'),
    ('fn bad(a: bool, b: bool) -> bool { a << b } fn main() -> i32 { 0i32 }', 'operator << is'),
    ('fn bad(a: bool, b: bool) -> bool { a >> b } fn main() -> i32 { 0i32 }', 'operator >> is'),
    ('fn bad(a: bool, b: bool) -> bool { a < b } fn main() -> i32 { 0i32 }', 'operator < is'),
    ('fn bad(a: bool, b: bool) -> bool { a <= b } fn main() -> i32 { 0i32 }', 'operator <= is'),
    ('fn bad(a: bool, b: bool) -> bool { a > b } fn main() -> i32 { 0i32 }', 'operator > is'),
    ('fn bad(a: bool, b: bool) -> bool { a >= b } fn main() -> i32 { 0i32 }', 'operator >= is'),
    ('fn bad(a: f32, b: f32) -> f32 { a % b } fn main() -> i32 { 0i32 }', 'operator % is'),
    ('fn bad(a: f32, b: f32) -> f32 { a & b } fn main() -> i32 { 0i32 }', 'operator & is'),
    ('fn bad(a: f32, b: f32) -> f32 { a | b } fn main() -> i32 { 0i32 }', 'operator | is'),
    ('fn bad(a: f32, b: f32) -> f32 { a ^ b } fn main() -> i32 { 0i32 }', 'operator ^ is'),
    ('fn bad(a: f32, b: f32) -> f32 { a << b } fn main() -> i32 { 0i32 }', 'operator << is'),
    ('fn bad(a: f32, b: f32) -> f32 { a >> b } fn main() -> i32 { 0i32 }', 'operator >> is'),
    ('fn bad(x: f32) -> f32 { !x } fn main() -> i32 { 0i32 }', 'operator ! is'),
    ('fn bad(x: (u32, u32)) -> (u32, u32) { -x } fn main() -> i32 { 0i32 }', 'operator - is'),
    ('fn bad(x: (u32, u32)) -> (u32, u32) { !x } fn main() -> i32 { 0i32 }', 'operator ! is'),
    ('fn bad(a: *u32, b: *u32) -> bool { a < b }\n                fn main() -> i32 { 0i32 }', 'operator < is'),
    ('fn bad(x: (u32, u32), y: (u32, u32)) -> bool { x == y } fn main() -> i32 { 0i32 }', 'operator == is'),
    ("forall 'a. fn bad(x: core::cap::Brand['a], y: core::cap::Brand['a]) -> bool { x == y } fn main() -> i32 { 0i32 }", 'operator == is'),
    ('fn main() -> i32 { let x = 1.5; 0i32 }', "a float literal's"),
    ('forall A, B. trait Pairing { fn sum(a: A, b: B) -> u32; }\n    instance Pairing[u32, u32] { fn sum(a: u32, b: u32) -> u32 { a + b } }\nforall T where Pairing[T, T]. fn add(a: T, b: T) -> u32 { T[u32]::sum(a, b) } fn main() -> i32 { add(3u32, 4u32) as i32 }', 'an associated parameter'),
    ('forall A, B. trait Pairing { fn sum(a: A, b: B) -> u32; }\n    instance Pairing[u32, u32] { fn sum(a: u32, b: u32) -> u32 { a + b } }\nfn main() -> i32 { (Pairing[u32, u32, bool]::sum(3u32, 4u32)) as i32 }', 'accepts at most'),
    ('forall A, B. trait Pairing { fn sum(a: A, b: B) -> u32; }\n    instance Pairing[u32, u32] { fn sum(a: u32, b: u32) -> u32 { a + b } }\nuse package::Pairing::sum; fn main() -> i32 { (sum[bool](3u32, 4u32)) as i32 }', 'type arguments belong'),
    ('forall A, B. trait Pairing { fn sum(a: A, b: B) -> u32; }\n    instance Pairing[u32, u32] { fn sum(a: u32, b: u32) -> u32 { a + b } }\nexport package::Pairing::sum; fn main() -> i32 { (package[bool]::sum(3u32, 4u32)) as i32 }', 'type arguments belong'),
    ('const N: i32 = 7i32; fn main() -> i32 { N[bool] }', 'constant paths take'),
    # Types and brands as a message about source writes them: a brand as written, where two brands that
    # differ read the same each with where it was opened, and no name the compiler made.
    ("forall 'k. fn same(x: core::cap::Brand['k], y: core::cap::Brand['k]) -> u32 { 0u32 }\n"
     "fn f() -> u32 {\n    let exists 'a. x = core::cap::fresh();\n    let exists 'b. y = core::cap::fresh();\n    same(x, y)\n}\n",
     "5:5: brand mismatch: Brand['b] and Brand['a]; 'b was opened at 4:5, 'a at 3:5"),
    # An opening names a brand no brand in scope has: not a parameter's, not 'static, not an earlier opening's.
    ("forall 'k. fn same(x: core::cap::Brand['k], y: core::cap::Brand['k]) -> u32 { 0u32 }\n"
     "fn f() -> u32 {\n    let exists 'k. x = core::cap::fresh();\n    let exists 'k. y = core::cap::fresh();\n    same(x, y)\n}\n",
     "4:5: let exists 'k reuses a brand already in scope"),
    ("forall 'k, 'q. fn other(x: core::cap::Brand['k], y: core::cap::Brand['q]) -> core::cap::Brand['k] {\n    y\n}\n",
     "1:99: brand mismatch: Brand['q] and Brand['k]"),
    ("forall 'k. fn keep(x: core::cap::Brand['k]) -> core::cap::Brand['k] {\n    let exists 'k. y = core::cap::fresh();\n    y\n}\n",
     "2:5: let exists 'k reuses a brand already in scope"),
    ("fn f() -> u32 {\n    let exists 'static. y = core::cap::fresh();\n    0u32\n}\n",
     "2:5: let exists 'static reuses a brand already in scope"),
    # A foreach body runs once for each component, so what it captures is Copy.
    ("struct H! { n: u32 }\nfn take(h: H!) -> u32 { let { n } = h; n }\nfn f(h: H!) -> u32 {\n"
     "    let parts = foreach x of (1u32, 2u32) { let n = take(h); x };\n    let (a, b) = parts;\n    a + b\n}\n",
     "4:58: h is captured by a foreach body, which runs once for each component: a capture must be Copy"),
    ("forall T. fn f(t: T) -> u32 {\n    let parts = foreach x of (1u32, 2u32) { let u = t; x };\n    0u32\n}\n",
     "2:53: t is captured by a foreach body, which runs once for each component: a capture must be Copy"),
    # The message names the form written.
    ("fn f() -> u32 {\n    var exists 'a. x = core::cap::fresh();\n    var exists 'a. y = core::cap::fresh();\n    0u32\n}\n",
     "3:5: var exists 'a reuses a brand already in scope"),
    ("fn f() -> u32 {\n    let exists 'k. x = core::cap::fresh();\n    let y = {\n        let exists 'k. z = core::cap::fresh();\n        0u32\n    };\n    y\n}\n",
     "4:9: let exists 'k reuses a brand already in scope"),
    ("forall T, 'k. exposed struct Ref { ptr: *T, marker: core::cap::Brand['k] }\n"
     "fn f(p: *u32) -> u32 {\n    let exists 'a. a = core::cap::fresh();\n    let exists 'b. b = core::cap::fresh();\n"
     "    let r = Ref.{ ptr: p, marker: a };\n    let s: Ref[u32, 'b] = r;\n    0u32\n}\n",
     "6:27: brand mismatch: Ref[u32, 'a] and Ref[u32, 'b]; 'a was opened at 3:5, 'b at 4:5"),
    ("fn f() -> u32 {\n    let x = {\n        let exists 'a. w = core::cap::fresh();\n        w\n    };\n    0u32\n}\n",
     "2:13: brand 'a escapes its lexical scope"),
    # A function value quantifies nothing: its brands are fixed where it is named, so it is called at one
    # brand only, and a brand no use determines is an error.
    (BRAND_VALUE + "forall 'b, 'q. fn h(w: core::cap::Brand['b], v: core::cap::Brand['q]) -> u32 {\n"
     "    let f = g;\n    let m = f(w, 1u32);\n    f(v, 2u32)\n}\n",
     "5:5: brand mismatch: Brand['q] and Brand['b]"),
    (BRAND_VALUE + "fn h() -> u32 {\n    let f = g;\n    0u32\n}\n",
     "3:13: cannot infer the type argument 'b of g"),
    ("forall T. fn apply(x: T, f: closure(T) -> u32) -> u32 {\n    let n: u32 = f;\n    n\n}\n",
     "2:18: type mismatch: closure[Env](T) -> u32 and u32"),
    ("fn f() -> u32 {\n    let items = foreach item of (1u32, 2u32) { (item, 1u32) };\n    items + 1u32\n}\n",
     "3:5: type mismatch: u32 and foreach _ of (u32, u32) => (_, u32)"),
    ("exposed struct Token! { n: u32 }\nfn peek(&t: Token!) -> u32 { t.n }\n"
     "fn f() -> u32 {\n    let g: fn(Token!) -> u32 = peek;\n    0u32\n}\n",
     "4:32: type mismatch: fn(&_: Token!) -> u32 and fn(Token!) -> u32"),
    # A nominal type reads as its last path segment, or as its full path where another reads the same.
    ("exposed struct Error { n: u32 }\nfn f(e: std::io::Error) -> Error { e }\n",
     "2:34: type mismatch: std::io::Error and "),
    ("fn g(v: (u32, *u8)) -> [u32; 3] { v }\n",
     "1:33: type mismatch: (u32, *u8) and [u32; 3]"),
    ("forall Self. trait P { fn p(x: Self) -> u32; }\nforall 'k. instance P[u32] { fn p(x: u32) -> u32 { x } }\n",
     "2:12: instance of P: parameter 'k needs a binder-free head occurrence"),
    # A trait's name reads as a type's does: in full where a type in the message reads the same.
    (methods("use package::m::Show;\nfn f(x: package::n::Show) -> u32 { Show::show(x) }\n", m=SHOW,
             n="exposed struct Show { v: u32 }\n"),
     "no instance of a::m::Show[a::n::Show]"),
    # The type of a `foreach` item reaches a local bound outside the body.
    ("exposed struct S { a: u32, b: u32 }\nforall T. fn id(x: T) -> T { x }\nfn main() -> i32 {\n"
     "    let s = S.{ a: 7u32, b: 9u32 };\n    let h = id;\n    let parts = foreach f: F of s { h(f) };\n    0i32\n}\n",
     "6:17: the type of foreach item f escapes its lexical scope"),
    ("exposed struct S { a: u32, b: u32 }\nfn main() -> i32 {\n    let s = S.{ a: 7u32, b: 9u32 };\n"
     "    var keep = s.a;\n    let g = |x| x;\n    let parts = foreach f of s { g(f) };\n    0i32\n}\n",
     "6:17: the type of foreach item f escapes its lexical scope"),
    ("fn main() -> i32 {\n    const K: u32 = 1u8;\n    0i32\n}\n", "2:20: const K has type u8, but its declared type is u32"),
    # A type argument nothing in the body determines.
    ("forall T. fn id(x: T) -> T { x }\nfn main() -> i32 {\n    let h = id;\n    0i32\n}\n",
     "3:13: cannot infer the type argument T of id"),
    ("forall T. enum Opt { Some(T), None }\nfn main() -> i32 {\n    let x = Opt::None;\n    0i32\n}\n",
     "3:13: cannot infer the type argument T of Opt"),
    ("forall T. fn mk() -> u32 { 0u32 }\nfn main() -> i32 {\n    mk() as i32\n}\n",
     "3:5: cannot infer the type argument T of mk"),
    ("exposed struct B { x: u32 }\nimpl B { forall T. fn m(self) -> u32 { self.x } }\n"
     "fn main() -> i32 {\n    var b = B.{ x: 1u32 };\n    b.m() as i32\n}\n",
     "5:5: cannot infer the type argument T of m"),
    ("forall 'a. fn f(x: u32) -> u32 { x }\nfn main() -> i32 {\n    let g = f;\n    g(1u32) as i32\n}\n",
     "3:13: cannot infer the type argument 'a of f"),
    # Missing fields, listed in alphabetical order.
    ("exposed struct P { b: u32, a: u32 }\nfn f() -> P { P.{ } }\n", "2:15: missing fields a and b"),
    ("exposed struct P { c: u32, b: u32, d: u32, a: u32 }\nfn f() -> P { P.{ b: 1u32 } }\n", "2:15: missing fields a, c and d"),
    # A type no source writes, which nothing in the body determines.
    ("fn main() -> i32 {\n    let f = |x| 0i32;\n    0i32\n}\n", "2:13: cannot infer the type of closure parameter x"),
    ("forall T. fn id(x: T) -> T { x }\nfn main() -> i32 {\n    let f = |a: u32, &b| id(b);\n    0i32\n}\n",
     "3:13: cannot infer the type of closure parameter b"),
    ("fn main() -> i32 {\n    let f = || std::process::exit(1i32);\n    0i32\n}\n", "2:13: cannot infer the return type of the closure"),
    # An array has at least one element: no literal is empty.
    ("fn main() -> i32 {\n    let a = ([], 1u32);\n    0i32\n}\n", "2:14: an array has at least one element"),
    ("forall T. exposed struct B { v: T }\nfn main() -> i32 {\n    let b = B.{ v: [] };\n    0i32\n}\n",
     "3:20: an array has at least one element"),
    ("fn main() -> i32 {\n    let a: [u8; 1] = [];\n    0i32\n}\n", "2:22: an array has at least one element"),
    # The type of one `foreach` item reaches the body of a sibling that writes the same binder.
    ("exposed struct S { a: u32, b: u32 }\nexposed struct P { c: u8, d: u8 }\nforall T. fn same(x: T, y: T) -> (T, T) { (x, y) }\n"
     "fn main() -> i32 {\n    let s = S.{ a: 7u32, b: 9u32 };\n    let p = P.{ c: 1u8, d: 2u8 };\n"
     "    let first = foreach f: F of s { (f, core::option::Option::None) };\n"
     "    let second = foreach g: F of p {\n        var { a: x, b: y } = first;\n        let (k, o) = x;\n"
     "        (same(o, core::option::Option::Some.(g)), y)\n    };\n    0i32\n}\n",
     "8:18: the type of foreach item g escapes its lexical scope"),
    # A binder never shadows a type parameter or an enclosing binder.
    ("exposed struct S { a: u32, b: u32 }\nforall F. fn f(x: F, s: S) -> F {\n    let t = foreach f: F of s { f };\n    x\n}\n",
     "3:13: foreach binder F shadows another type named F"),
    ("exposed struct S { a: u32, b: u32 }\nfn main() -> i32 {\n    let s = S.{ a: 7u32, b: 9u32 };\n"
     "    let t = foreach f: F of s {\n        let u = foreach g: F of s { g };\n        f\n    };\n    0i32\n}\n",
     "5:17: foreach binder F shadows another type named F"),
    # `Sized` holds of a type with parameters only by the caller's own premise for exactly it or a part of
    # bytes whatever the parameters are, so not of a tuple of `Sized` parameters, and never of a
    # zero-size type.
    ("forall T where Sized[T]. fn need(x: T) -> T { x }\nforall U. fn bad(x: U) -> U { need[U](x) }\n",
     "Sized requires an exact premise"),
    ("forall T where Sized[T]. fn need(x: T) -> T { x }\n"
     "forall U, V where Sized[U], Sized[V]. fn bad(x: (U, V)) -> (U, V) { need(x) }\n", "Sized requires an exact premise"),
    ("forall T where Sized[T]. fn need(x: T) -> T { x }\nfn bad(x: ((), [(); 2])) -> ((), [(); 2]) { need(x) }\n",
     "Sized requires an exact premise"),
    # A zero-size value has no heap block, so no region to split.
    ("use std::owned::Owned!;\nfn main() -> i32 {\n    let exists 'r. (alloc, region) = Owned!::new(((), ())).open();\n"
     "    let (restore, parts) = region.split();\n    0i32\n}\n", "Sized requires an exact premise"),
    # Only a var is lent or assigned, and a closure holds a copy of what it captures.
    ("exposed struct C { n: u32 }\nimpl C { fn tick(&self) -> u32 { self.n } }\n"
     "fn f() -> u32 { let a = C.{ n: 0u32 }; &a.tick() }\n", "3:41: a is not a var; a lent place must be a var"),
    ("exposed struct C { n: u32 }\nfn g(&c: C) -> u32 { c.n }\nfn f(c: C) -> u32 { g(&c) }\n",
     "3:24: c is not a var; a lent place must be a var"),
    ("fn f() -> u32 { var a = 1u32; let g = || { a = 2u32; a }; g() }\n", "1:44: a is captured, and a body cannot assign what it captures; assign it outside, or give a closure a place parameter for it"),
    ("exposed struct C { n: u32 }\nimpl C { fn tick(&self) -> u32 { self.n } }\n"
     "fn f() -> u32 { var a = C.{ n: 0u32 }; let g = || &a.tick(); g() }\n", "3:52: a is captured, and a body cannot lend what it captures; lend it outside, or give a closure a place parameter for it"),
    # An `if` without `else` has type ().
    ("fn f(c: bool) -> u32 { if c { 1u32 } }\n", "1:24: an `if` without `else` has type (), but its block has type u32"),
    # A closure type reads as source writes it.
    ("fn apply(f: closure(u32) -> u32) -> u32 { f(1u32) }\nfn d(x: u32) -> u32 { x }\nfn g() -> u32 { apply(d) }\n",
     "type mismatch: fn(u32) -> u32 and closure(u32) -> u32"),
    # A method no visible trait provides names the traits that would.
    ({"a/kur.toml": manifest("a"), "a/lib.kur": "use package::shapes::Square;\nuse package::shapes;\nfn f(s: Square) -> u32 { s.area() }\n",
      "a/shapes.kur": "exposed struct Square { side: u32 }\ntrait Area { fn area(self: Self) -> u32; }\n"
                      "instance Area[Square] { fn area(self: Square) -> u32 { self.side } }\n"},
     "3:26: Square has no method area; the trait Area provides it, import it"),
)
