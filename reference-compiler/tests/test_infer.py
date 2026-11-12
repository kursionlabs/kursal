"""Inference: the types it finds, and the type errors it rejects."""

import unittest

from kursal import core as C
from kursal.infer import FnCall, InferError, TraitCall, infer
from kursal.core import ModuleId
from kursal.resolve import resolve
from kursal.sigs import signatures
from tests.test_resolve import manifest, tree

PRELUDE = """
forall T. exposed enum Opt { Some(T), None }
forall T, E. exposed enum Res { Ok(T), Err(E) }
exposed struct Cur { pos: u32 }
impl Cur {
    fn bump(&self, by: u32) -> u32 { by }
    fn get(self) -> u32 { 0u32 }
}
forall Self -> Item. trait Next { fn step(&it: Self) -> Opt[Item]; }
instance Next[Cur -> u32] { fn step(&it: Cur) -> Opt[u32] { Opt::None } }
forall T. fn id(x: T) -> T { x }
"""


def run(source):
    root = tree({"p/kur.toml": manifest("p"), "p/lib.kur": PRELUDE + source})
    resolver = resolve(root / "p")
    sigs = signatures(resolver)
    infer(resolver, sigs)
    return resolver


def fn(resolver, name):
    return resolver.scopes[ModuleId(0)].decls[name].node


def stmt_types(resolver, name):
    """The types of a function's `let`/`var` values, in order."""
    body = fn(resolver, name).body
    return [s.value.ty for s in body.stmts if hasattr(s, "value") and hasattr(s.value, "ty")]


U32, U64, BOOL = C.U32, C.Prim("u64"), C.BOOL


def named(name, *args):
    return C.Named(f"p::{name}", args)


class TypeTests(unittest.TestCase):
    def test_literals_take_their_type_from_context(self):
        r = run("fn f(a: u64) -> u64 { let x: u64 = 1; let y = a + 2; let z = id(3u8); y }\n")
        self.assertEqual(stmt_types(r, "f"), [U64, U64, C.Prim("u8")])

    def test_explicit_type_arguments(self):
        r = run("fn f() -> u32 { let x = id[u32](7); x }\n")
        self.assertEqual(stmt_types(r, "f"), [U32])

    def test_methods_and_lent_receivers(self):
        r = run("fn f(c: Cur) -> u32 { var d = c; let a = &d.bump(1u32); let b = d.get(); a + b }\n")
        body = fn(r, "f").body
        call = body.stmts[1].value.operand
        self.assertIsInstance(call.call, FnCall)
        self.assertEqual(call.call.sig.name, "p::Cur::bump")
        self.assertIs(call.call.receiver.value, call.callee.value)
        self.assertTrue(call.call.receiver.lend)
        self.assertEqual((call.call.name, call.call.targs), ("p::Cur::bump", ()))
        self.assertEqual(body.stmts[2].value.ty, U32)

    def test_trait_dispatch_solves_determined_parameters(self):
        r = run("fn f(c: Cur) -> u32 { var d = c; let o = &d.step(); match o { Opt::Some(n) => n, Opt::None => 0u32 } }\n"
                "forall S, I where Next[S -> I]. fn g(s: S) -> Opt[I] { var t = s; &t.step() }\n")
        call = fn(r, "f").body.stmts[1].value.operand
        self.assertIsInstance(call.call, TraitCall)
        self.assertEqual(call.call.trait, "p::Next")
        self.assertEqual(call.call.targs, (named("Cur"), U32))
        generic = fn(r, "g").body.tail.operand.call
        self.assertIsInstance(generic, TraitCall)
        self.assertEqual(generic.trait, "p::Next")
        self.assertEqual(generic.targs, (C.Named("S"), C.Named("I")))

    def test_closures_take_types_from_the_callee_and_capture(self):
        r = run("forall Env. fn apply(f: closure[Env](u32) -> u32, x: u32) -> u32 { f(x) }\n"
                "fn f(k: u64) -> u32 { let bias = 1u32; apply(|x| x + bias, 2u32) }\n")
        closure = fn(r, "f").body.tail.args[0]
        self.assertEqual(tuple(c.name for c in closure.captures), ("bias",))
        self.assertEqual(closure.captures[0].binder_id, fn(r, "f").body.stmts[0].pattern.binder)
        self.assertEqual(dict(closure.ty.fields)["env"], C.Tuple((U32,)))
        self.assertEqual(closure.ty.fields[1][1].params, (C.Tuple((U32,)), U32))

    def test_nested_instance_premises_solve_outputs_and_reject_missing_instances(self):
        wrapper = """
forall T. exposed struct Wrap { value: T }
forall T, I where Next[T -> I].
instance Next[Wrap[T] -> I] {
    fn step(&it: Wrap[T]) -> Opt[I] { Opt::None }
}
"""
        r = run(wrapper + "fn f(c: Wrap[Cur]) -> Opt[u32] { var d = c; &d.step() }")
        call = fn(r, "f").body.tail.operand.call
        self.assertEqual(call.targs, (C.Named("p::Wrap", (named("Cur"),)), U32))
        with self.assertRaisesRegex(InferError, "no instance of p::Next"):
            run(wrapper + "exposed struct Other {}\n"
                "fn f(c: Wrap[Other]) -> Opt[u32] { var d = c; &d.step() }")

    def test_try_unwraps_direct_option(self):
        r = run("fn f(o: Opt[u32]) -> Opt[bool] { let n = o?; Opt::Some.(n == 0) }\n")
        t = fn(r, "f").body.stmts[0].value
        self.assertEqual((t.kind, t.ty), ("option", U32))

    def test_try_preserves_tuple_payloads_and_allows_different_success_types(self):
        r = run("fn f(o: Opt[(u32, bool)]) -> Opt[u32] { let (n, b) = o?; Opt::Some.(n) }\n"
                "fn g(r: Res[u32, u32]) -> Res[bool, u32] { let n = r?; Res::Ok.(n == 0) }\n")
        self.assertEqual(fn(r, "f").body.stmts[0].value.ty, C.Tuple((U32, BOOL)))
        self.assertEqual(fn(r, "g").body.stmts[0].value.ty, U32)

    def test_capture_records_preserve_first_use_identity_and_shadowing(self):
        r = run("fn f() -> u32 {\n"
                "  let a = 3u32; let b = 7u32;\n"
                "  let outer = |x: u32| { let a = 10u32;\n"
                "    let inner = |y: u32| b * 100 + a * 10 + x + y; inner(2) + b\n"
                "  };\n"
                "  let mapped = foreach item of (1u32, 2u32) { (item, b, a, b) }; outer(a)\n"
                "}\n")
        body = fn(r, "f").body
        a, b = (s.pattern.binder for s in body.stmts[:2])
        outer = body.stmts[2].value
        inner = outer.body.stmts[1].value
        identity = lambda e: [(c.name, c.binder_id) for c in e.captures]
        self.assertEqual(identity(outer), [("b", b)])
        self.assertEqual(identity(inner), [("b", b), ("a", outer.body.stmts[0].pattern.binder),
                                           ("x", outer.param_ids[0])])
        self.assertEqual(identity(body.stmts[3].value), [("b", b), ("a", a)])

    def test_packages(self):
        r = run("forall 'k. struct Tag! { b: Brand['k] }\n"
                "fn mint() -> exists 'r. Tag!['r] { let exists 'a. w = builtin::fresh(); pack 'a. Tag.{ b: w } }\n"
                "fn f() -> u32 { let exists 'x. t = mint(); let { b } = t; 0u32 }\n")
        let = fn(r, "f").body.stmts[0]
        self.assertEqual(let.pattern.ty, named("Tag", C.Skolem(let.opened_brands[0])))
        self.assertEqual(fn(r, "mint").body.tail.ty, C.Exists(("r",), named("Tag", C.Skolem("r"))))

    def test_function_values(self):
        r = run("fn bump(&c: Cur) -> u32 { 0u32 }\nforall 'k. fn take(b: Brand['k]) -> u32 { 0u32 }\n"
                "fn f() -> u32 { let g = bump; let h = take; 0u32 }\n")
        g, h = stmt_types(r, "f")
        self.assertEqual(g, C.Fn((named("Cur"),), U32, places=frozenset({0})))
        self.assertEqual(len(h.forall), 1)
        body = fn(r, "f").body
        self.assertEqual(body.stmts[0].value.call.name, "p::bump")
        self.assertEqual(body.stmts[1].value.call.targs, (C.Ignored(),))

    def test_call_metadata_orders_arguments_and_preserves_foreign_symbols(self):
        r = run("extern \"C\" fn foreign(x: u32) -> u32;\n"
                "forall A, B. fn pick(a: A, b: B) -> A { a }\n"
                "forall 'k. fn take(b: Brand['k]) -> u32 { 0u32 }\n"
                "forall 'r. fn f(b: Brand['r]) -> u32 {\n"
                "  let x = pick[u32, bool](1, true); let y = foreign(x);\n"
                "  let g = take; g(b)\n}\n")
        body = fn(r, "f").body
        pick, foreign = (s.value.call for s in body.stmts[:2])
        self.assertEqual((pick.name, pick.targs), ("p::pick", (U32, BOOL)))
        self.assertEqual((foreign.name, foreign.targs), ("foreign", ()))
        self.assertEqual(body.tail.call.targs, (C.Skolem("r"),))

    def test_lent_match_and_tuple_constructors(self):
        r = run("exposed tuple Pair(u32, bool);\n"
                "fn f(o: Opt[u32]) -> Pair { var p = o; let n = match &p { Opt::Some(v) => v, Opt::None => 0u32 }; "
                "Pair(n, true) }\n")
        body = fn(r, "f").body
        self.assertTrue(body.stmts[1].value.lent)
        self.assertEqual(body.tail.ty, named("Pair"))


class ErrorTests(unittest.TestCase):
    def test_lending_function_name_does_not_implicitly_return_its_argument(self):
        self.rejects("fn bump(&c: Cur) -> u32 { 0u32 }\n"
                     "fn take(f: fn(Cur) -> (Cur, u32)) -> u32 { 0u32 }\n"
                     "fn f() -> u32 { take(bump) }\n")

    def rejects(self, source):
        with self.assertRaises(InferError):
            run(source)

    def test_mismatch(self):
        self.rejects("fn f() -> u32 { true }\n")

    def test_undetermined_literal(self):
        self.rejects("fn f() -> u32 { let x = 5; 0u32 }\n")

    def test_lend_must_match_parameter_mode(self):
        self.rejects("fn f(c: Cur) -> u32 { var d = c; let n = d.bump(1u32); n }\n")
        self.rejects("fn g(&c: Cur) -> u32 { 0u32 }\nfn f(c: Cur) -> u32 { g(c) }\n")
        self.rejects("fn f(c: Cur) -> Cur { var d = c; id(&d) }\n")

    def test_no_instance(self):
        self.rejects("exposed struct Other { x: u32 }\nfn f(o: Other) -> u32 { var p = o; let n = &p.step(); 0u32 }\n")
        self.rejects("exposed struct Other { x: u32 }\nforall S, I where Next[S -> I]. fn g(s: S) -> u32 { 0u32 }\n"
                     "fn f(o: Other) -> u32 { g(o) }\n")

    def test_try_rejects_passenger_tuples(self):
        for source in (
            "fn f(o: (Opt[u32], bool)) -> (Opt[u32], bool) { let (n, b) = o?; (Opt::Some.(n), b) }",
            "fn f(o: (bool, Opt[u32])) -> Opt[u32] { let n = o?; Opt::Some.(1) }",
            "fn f(o: Opt[u32]) -> (Opt[u32], bool) { let n = o?; (Opt::Some.(n), true) }",
            "fn f(o: (Res[u32, u32], bool)) -> (Res[u32, u32], bool) { let (n, b) = o?; (Res::Ok.(n), b) }",
        ):
            with self.subTest(source=source):
                self.rejects(source)

    def test_try_needs_matching_result(self):
        self.rejects("fn f(o: Opt[u32]) -> Res[u32, u32] { let v = o?; Res::Ok.(v) }\n")
        self.rejects("fn f(o: Opt[u32]) -> u32 { o? }\n")
        self.rejects("fn f(r: Res[u32, bool]) -> Res[u32, u32] { let n = r?; Res::Ok.(n) }\n")

    def test_unknown_method_and_field(self):
        self.rejects("fn f(c: Cur) -> u32 { c.nothing() }\n")
        self.rejects("fn f(c: Cur) -> u32 { c.nothing }\n")

    def test_struct_literal_fields(self):
        self.rejects("fn f() -> Cur { Cur.{ } }\n")
        self.rejects("fn f() -> Cur { Cur.{ pos: 1u32, pos: 2u32 } }\n")

    def test_let_exists_needs_a_package(self):
        self.rejects("fn f() -> u32 { let exists 'a. x = 5u32; 0u32 }\n")

    def test_not_callable(self):
        self.rejects("fn f(n: u32) -> u32 { n(1u32) }\n")

    def test_assignment_needs_a_var(self):
        self.rejects("fn f(n: u32) -> u32 { let m = n; m = 1u32; m }\n")


if __name__ == "__main__":
    unittest.main()
