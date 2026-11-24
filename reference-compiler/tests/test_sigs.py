"""Signatures in core terms, and constant folding."""

import unittest

from kursal import core as C
from kursal.resolve import resolve
from kursal.sigs import SigError, signatures
from tests.test_resolve import manifest, tree


def sigs_of(source):
    root = tree({"p/kur.toml": manifest("p", ("core",)), "p/lib.kur": source})
    return signatures(resolve(root / "p"))


def find(s, name):
    return next(sig for decl, sig in s.sigs.items() if decl.name == name)


def const(s, name):
    return next(v for d, v in s.consts.items() if d.name == name)


class TypeDeclTests(unittest.TestCase):
    def test_trait_headers_do_not_silently_drop_premises(self):
        source = "forall Self. trait Other {}\nforall Self where Other[Self]. trait Bad {}\n"
        with self.assertRaisesRegex(SigError, "trait declarations cannot have their own trait premises"):
            sigs_of(source)

    def test_a_declaration_linear_for_every_instantiation_carries_the_bang(self):
        lin = "struct L! { x: u32 }\nforall T. enum Opt { None, Some(T) }\n"
        s = sigs_of(lin + "struct H! { o: Opt[L!] }\nforall T. struct W { w: Opt[T] }\nforall 'k. struct Cap! {}\n"
                    "fn f(w: W[L!]) -> W[L!] { w }\n")
        self.assertTrue(s.types["p::Cap!"].linear)
        self.assertFalse(s.types["p::W"].linear)
        cases = (
            ("struct S { l: L! }\n", "3:8: struct S holds a linear type in its field l; declare it S!"),
            ("struct S { o: Opt[L!] }\n", "3:8: struct S holds a linear type in its field o; declare it S!"),
            ("forall T. struct G { t: T, l: (u8, L!) }\n", "3:18: struct G holds a linear type in its field l whatever its type arguments; declare it G!"),
            ("forall 'k. enum E { A, B(L!, core::cap::Brand['k]) }\n", "3:17: enum E holds a linear type in its variant B; declare it E!"),
            ("forall T. tuple P(T, Opt[L!]);\n", "3:17: tuple P holds a linear type in its element 1 whatever its type arguments; declare it P!"),
        )
        for source, message in cases:
            with self.subTest(source=source), self.assertRaisesRegex(SigError, message):
                sigs_of(lin + source)

    def test_a_foreach_type_is_no_component(self):
        # A foreach type is the type of a whole value, parameter or result (TY-FLAT).
        for source, what in (
            ("struct F { f: foreach F of (u32, u8) => *F }\n", "a component"),
            ("tuple P(foreach X of (u32,) => X, u8);\n", "a component"),
            ("enum E { A(foreach X of (u32,) => X) }\n", "a component"),
            ("fn f(p: *foreach X of (u32,) => X) {}\n", "a pointee"),
            ("forall T. struct B { b: *T }\nfn f(b: B[foreach X of (u32,) => X]) {}\n", "a type argument"),
            ("fn f(x: foreach X of (foreach Y of (u32,) => Y) => X) {}\n", "a foreach source"),
            ("fn f(x: foreach X of (u32,) => foreach Y of (X,) => Y) {}\n", "a foreach body"),
            ("forall T where Exposed[foreach X of T => X]. fn f(x: T) {}\n", "a type argument"),
        ):
            with self.subTest(source=source), self.assertRaisesRegex(SigError, f"{what} is a foreach type"):
                sigs_of(source)

    def test_struct_enum_tuple(self):
        s = sigs_of("forall T, 'k where Copy[T]. struct Box! { v: T, b: core::cap::Brand['k] }\n"
                    "enum E { A, B(u32), C(u32, bool) }\nexposed tuple Pair(u32, u32);\n")
        box, enum, pair = s.types["p::Box!"], s.types["p::E"], s.types["p::Pair"]
        self.assertTrue(box.linear)
        self.assertEqual(box.params, (C.Param("T", C.COPY), C.Param("k", C.BRAND)))
        self.assertEqual(box.body.fields[1][1], C.Brand(C.Skolem("k")))
        self.assertEqual(box.exposure[0], "owner")
        self.assertEqual([t for _, t in enum.body.variants], [C.UNIT, C.U32, C.Tuple((C.U32, C.BOOL))])
        self.assertIsNone(pair.exposure)

    def test_exposure_scope_must_enclose(self):
        root = tree({"p/kur.toml": manifest("p"), "p/lib.kur": "", "p/a/lib.kur": "",
                     "p/a/b.kur": "exposed (package::a) struct S { x: u32 }\n", "p/c.kur": ""})
        s = signatures(resolve(root / "p"))
        self.assertEqual(s.types["p::a::b::S"].exposure, ("within", C.ModuleId(0, ("a",))))
        bad = tree({"p/kur.toml": manifest("p"), "p/lib.kur": "", "p/a.kur": "exposed (package::c) struct S { x: u32 }\n",
                    "p/c.kur": ""})
        with self.assertRaises(SigError):
            signatures(resolve(bad / "p"))


class SignatureTests(unittest.TestCase):
    def test_places_and_self(self):
        s = sigs_of("struct Cur { pos: u32 }\nimpl Cur {\n  fn bump(&self, by: u32) -> u32 { by }\n"
                    "  fn get(self) -> u32 { 0u32 }\n}\n")
        bump, get = find(s, "Cur::bump"), find(s, "Cur::get")
        self.assertEqual(bump.places, frozenset({0}))
        self.assertEqual(bump.ptypes[0], C.Named("p::Cur"))
        self.assertEqual(get.places, frozenset())

    def test_closure_parameters_get_hidden_environments(self):
        s = sigs_of("forall T. fn apply(f: closure(&_: T) -> u32, g: closure[u8](T) -> T, x: T) -> u32 { 0u32 }\n")
        sig = find(s, "apply")
        env = sig.params[-1]
        self.assertEqual(len(sig.params), 2)
        f, g = sig.ptypes[0], sig.ptypes[1]
        self.assertEqual(f.fields[0], ("env", C.Named(env.name)))
        self.assertEqual(f.fields[1][1].places, frozenset({1}))
        self.assertEqual(g.fields[0], ("env", C.Prim("u8")))

    def test_traits_and_instances(self):
        s = sigs_of("forall Self -> Item. trait Next { fn step(&it: Self) -> Item; }\n"
                    "trait Show { fn show(self: Self) -> u32; }\n"
                    "struct N { n: u32 }\ninstance Next[N -> u32] { fn step(&it: N) -> u32 { 0u32 } }\n")
        self.assertEqual(s.traits["p::Next"].params, ("Self", "Item"))
        self.assertEqual(s.traits["p::Show"].params, ("Self",))
        self.assertEqual(s.traits["p::Next"].methods[0].places, frozenset({0}))
        inst = s.instances[0][0]
        self.assertEqual(inst.head, (C.Named("p::N"), C.U32))

    def test_type_arity(self):
        with self.assertRaises(SigError):
            sigs_of("forall T. struct B { v: T }\nfn f(b: B) -> u32 { 0u32 }\n")

    def test_brand_scope(self):
        with self.assertRaises(SigError):
            sigs_of("fn f(b: core::cap::Brand['k]) -> u32 { 0u32 }\n")


class ConstTests(unittest.TestCase):
    def test_folding(self):
        s = sigs_of("const BITS: u32 = 5;\nconst MASK: u32 = (1u32 << BITS) - 1;\n"
                    "const WRAP: u8 = 250 + 10;\nconst NEG: i8 = -128;\nconst DIV: i32 = -7 / 2;\n"
                    "const REM: i32 = -7 % 2;\nconst SHIFT: u32 = 1 << 33;\nconst CAST: u8 = (300u32 as u8);\n"
                    "const MINDIV: i8 = NEG / -1;\nconst OK: bool = MASK == 31 && !false;\n")
        values = {n: const(s, n)[1] for n in ("MASK", "WRAP", "NEG", "DIV", "REM", "SHIFT", "CAST", "MINDIV", "OK")}
        self.assertEqual(values, {"MASK": 31, "WRAP": 4, "NEG": -128, "DIV": -3, "REM": -1, "SHIFT": 2,
                                  "CAST": 44, "MINDIV": -128, "OK": True})

    def test_associated_and_array_length(self):
        s = sigs_of("struct G { w: u32 }\nimpl G { const CELLS: usize = 4; }\n"
                    "fn f(a: [u8; G::CELLS]) -> u32 { 0u32 }\n")
        self.assertEqual(find(s, "f").ptypes[0], C.Array(C.Prim("u8"), 4))

    def test_errors(self):
        for source in ("const A: u32 = B;\nconst B: u32 = A;\n", "const A: u32 = 1 / 0;\n",
                       "const A: u8 = 256;\n", "const A: u32 = true;\n",
                       "const A: bool = 1u32 && 2u32;\n"):
            with self.subTest(source=source), self.assertRaises(SigError):
                sigs_of(source)

    def test_a_value_of_another_type_names_both_types(self):
        cases = (
            ("const A: u32 = true;\n", "1:16: const A has type bool, but its declared type is u32"),
            ("const A: u32 = 5u8;\n", "1:16: const A has type u8, but its declared type is u32"),
            ('const A: (u32, *u8) = "x";\n', "1:23: const A has type Slice[u8, 'static], but its declared type is (u32, *u8)"),
        )
        for source, message in cases:
            with self.subTest(source=source), self.assertRaises(SigError) as caught:
                sigs_of(source)
            self.assertIn(f"lib.kur:{message}", str(caught.exception))

    def test_errors_are_reported_together_in_source_order(self):
        cases = (
            ("fn f(x: u32[u8]) -> u32 { 0u32 }\nstruct S { a: u32 }\n"
             "fn g(y: core::cap::Brand[u32]) -> u32 { 0u32 }\nforall T. fn h(x: T[u32]) -> u32 { 0u32 }\n",
             ["1:9: primitive u32 takes no type arguments", "3:9: Brand requires exactly one brand argument",
              "4:19: type parameter T takes no type arguments"]),
            ("trait Show { fn show(x: Self) -> u32; }\n"
             "instance Show[u32] { fn show(x: u32) -> u32 { x } fn extra(x: u32) -> u32 { x } }\n"
             "instance Show[u8] { fn show(x: u8) -> u8 { x } }\n",
             ["2:51: extra is not a method of trait Show", "3:21: method show does not match its declaration in trait Show"]),
            # The functions are not looked at while a type declaration is wrong.
            ("fn f(x: u32[u8]) -> u32 { 0u32 }\nstruct V { d: Never[u32] }\nstruct U { c: u32[u8] }\n",
             ["2:15: Never takes no type arguments", "3:15: primitive u32 takes no type arguments"]),
            # An error in a constant would recur wherever the constant is used: it stops at once.
            ("struct U { c: u32[u8] }\nconst A: u32 = true;\nstruct S { a: [u8; A] }\nstruct T { b: [u8; A] }\n",
             ["1:15: primitive u32 takes no type arguments", "2:16: const A has type bool, but its declared type is u32"]),
        )
        for source, messages in cases:
            with self.subTest(source=source), self.assertRaises(SigError) as caught:
                sigs_of(source)
            lines = str(caught.exception).split("\n")
            self.assertEqual(len(lines), len(messages), lines)
            for line, message in zip(lines, messages):
                self.assertIn(f"lib.kur:{message}", line)


if __name__ == "__main__":
    unittest.main()
