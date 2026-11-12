"""Signatures in core terms, and constant folding."""

import unittest

from kursal import core as C
from kursal.resolve import resolve
from kursal.sigs import SigError, signatures
from tests.test_resolve import manifest, tree


def sigs_of(source):
    root = tree({"p/kur.toml": manifest("p"), "p/lib.kur": source})
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

    def test_struct_enum_tuple(self):
        s = sigs_of("forall T, 'k where Copy[T]. struct Box! { v: T, b: Brand['k] }\n"
                    "enum E { A, B(u32), C(u32, bool) }\nexposed tuple Pair(u32, u32);\n")
        box, enum, pair = s.types["p::Box"], s.types["p::E"], s.types["p::Pair"]
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
            sigs_of("fn f(b: Brand['k]) -> u32 { 0u32 }\n")


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


if __name__ == "__main__":
    unittest.main()
