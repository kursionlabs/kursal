"""The parser (kursal/parser.py): its rules one by one, the shape of the trees it builds for the delicate
constructs, and every .kur file in the repository."""

import sys
import unittest
from pathlib import Path

from kursal import syntax as S
from kursal.parser import Lexer
from kursal.sexpr import KursError

ROOT = Path(__file__).resolve().parents[2]


def body(src):
    """The block of `fn f() { src }`."""
    return S.parse("fn f() { " + src + " }")[0].body


def expr(src):
    return S.parse_expr(src)


def rejected(test, src):
    with test.assertRaises(KursError, msg=src):
        S.parse(src)


class ParameterTests(unittest.TestCase):
    """An in-place parameter marks its name."""

    def test_in_place_parameters(self):
        f = S.parse("fn f(&self, &x: T, y: u32) {}")[0]
        self.assertEqual([(p.name, p.lend) for p in f.params], [("self", True), ("x", True), ("y", False)])
        self.assertEqual(f.params[1].type.path, ("T",))

    def test_self_comes_first_and_may_be_typed(self):
        for first in ("self", "&self", "self: Self", "&self: Cell"):
            self.assertEqual(S.parse(f"fn f({first}, x: u32) {{}}")[0].params[0].name, "self")
        rejected(self, "fn f(x: u32, self) {}")
        rejected(self, "fn f(x: &T) {}")        # `&T` is not a type
        rejected(self, "fn f(x) {}")            # a non-self parameter has a type

    def test_function_and_closure_types_name_their_in_place_slots(self):
        f = S.parse("fn f(g: fn(&it: Iter, usize) -> bool, h: closure[Env](&_: T) -> (), "
                    "k: closure(&a: A, &b: B) -> R) {}")[0]
        g, h, k = (p.type for p in f.params)
        self.assertEqual([lend for lend, _ in g.params], [True, False])
        self.assertEqual((h.params[0][0], h.env.path), (True, ("Env",)))
        self.assertEqual([lend for lend, _ in k.params], [True, True])
        rejected(self, "fn f(g: fn(&Iter) -> bool) {}")   # an in-place slot has a name

    def test_closure_literals(self):
        e = expr("|&x: T, y| x")
        self.assertEqual([(lend, name) for lend, name, _ in e.params], [(True, "x"), (False, "y")])
        self.assertEqual(expr("|&g| &g.wake(now)").params[0][:2], (True, "g"))

    def test_function_types_need_an_arrow(self):
        rejected(self, "fn f(g: fn(u32)) {}")
        rejected(self, "fn f(g: closure(u32)) {}")
        self.assertIsInstance(S.parse("fn f(g: fn(u32) -> ()) {}")[0].params[0].type.ret, S.TTuple)

    def test_the_arrow_of_a_determined_parameter(self):
        i = S.parse("forall T. instance Next[Counter, fn(u32) -> u32 -> usize] { }")[0]
        self.assertEqual((len(i.type.args), i.type.determined), (3, 1))
        self.assertIsInstance(i.type.args[1], S.TFn)


class ConstructionTests(unittest.TestCase):
    """Construction goes through a dot."""

    def test_struct_literals(self):
        e = expr("Point.{ x: 1, y }")
        self.assertEqual((e.path.names, [n for n, _ in e.fields]), (("Point",), ["x", "y"]))
        self.assertIsNone(e.fields[1][1])
        self.assertEqual(expr("Point.{ x: 1, ..rest }").spread, "rest")
        self.assertEqual(expr("Point.{ ..rest }").spread, "rest")
        self.assertEqual(expr("Point.{}").fields, ())

    def test_generic_and_nested_literals(self):
        e = expr("Box[u32].{ v: 7 }")
        self.assertEqual(e.path.segments[0][1][0].path, ("u32",))
        inner = expr("Pair.{ a: Point.{ x: 1 }.x }").fields[0][1]
        self.assertIsInstance(inner, S.Field)
        self.assertIsInstance(inner.value, S.StructLit)

    def test_variants_and_tuple_declarations(self):
        some, rect = expr("Option::Some.(x)"), expr("Shape::Rect.(w, h)")
        self.assertEqual((some.callee.names, len(some.args)), (("Option", "Some"), 1))
        self.assertEqual(len(rect.args), 2)
        self.assertIsInstance(expr("Pair.(a, b)"), S.Call)
        self.assertIsInstance(expr("Option::None"), S.Path)

    def test_the_token_after_the_dot_decides(self):
        self.assertIsInstance(expr("p.f"), S.Field)
        self.assertIsInstance(expr("p.0"), S.Index)
        self.assertIsInstance(expr("p.f(x)"), S.Call)
        self.assertIsInstance(expr("P.{ x }"), S.StructLit)
        self.assertIsInstance(expr("P.(x)"), S.Call)

    def test_only_a_path_is_built(self):
        with self.assertRaises(KursError):
            expr("f(x).{ y: 1 }")

    def test_construction_is_a_syntax_error_after_anything_but_a_path(self):
        for src in ["f(x).{ y: 1 }", "p.q.{ y: 1 }", "f(x).(1, 2)", "(a).{ x }", "a.0.{ x }"]:
            with self.subTest(src=src):
                with self.assertRaisesRegex(KursError, "syntax error"):
                    expr(src)

    def test_env_less_closure_types_only_as_parameter_types(self):
        S.parse("fn f(g: closure(i32) -> i32) {}")
        S.parse("fn f(g: fn(closure(i32) -> i32) -> ()) {}")
        S.parse("fn f() { let c = |g: closure(i32) -> i32| g; }")
        for src in ["fn f() -> closure(i32) -> i32 {}", "struct S { g: closure(i32) -> i32 }",
                    "fn f(g: Option[closure(i32) -> i32]) {}", "fn f() { let g: closure(i32) -> i32 = h; }",
                    "fn f(g: *closure(i32) -> i32) {}"]:
            with self.subTest(src=src):
                rejected(self, src)
        S.parse("struct S { g: closure[E](i32) -> i32 }")

    def test_a_struct_literal_needs_its_dot(self):
        rejected(self, "fn f() { Point { x: 1 } }")
        rejected(self, "fn f() { let p = Point { x: 1 }; p }")


class WholeExpressionTests(unittest.TestCase):
    """Block-like forms, closures, `return` and `pack` are whole expressions."""

    def test_block_tails_are_distinguished_from_expression_statements(self):
        cases = (
            ("if c { 1 } else { 2 }", 0, S.If),
            ("if c { 1 } else { 2 };", 1, None),
            ("if c { g(); } h()", 1, S.Call),
            ("while c {}", 1, None),
            ("(while c {})", 0, S.While),
            ("for x in &it {}", 1, None),
            ("(for x in &it {})", 0, S.For),
            ("foreach x of xs { x }", 0, S.Foreach),
            ("foreach x of xs { x };", 1, None),
            ("{} {}", 1, S.Block),
            ("{};", 1, None),
            ("return 1;", 1, None),
        )
        for source, statements, tail in cases:
            with self.subTest(source=source):
                block = body(source)
                self.assertEqual(len(block.stmts), statements)
                if tail is None:
                    self.assertIsNone(block.tail)
                else:
                    self.assertIsInstance(block.tail, tail)

    def test_block_like_expressions_in_whole_positions(self):
        b = body("let a = if c { 1 } else { 2 }; let m = match x { A => 1, _ => 2 }; f(if c { 1 } else { 2 }); "
                 "while c { g(); } if c { h(); } match y { _ => 3 }")
        self.assertEqual(len(b.stmts), 5)
        self.assertIsInstance(b.tail, S.Match)
        self.assertIsInstance(body("if c { 1 } else { 2 }").tail, S.If)

    def test_inside_an_operator_they_need_parentheses(self):
        self.assertIsInstance(expr("1 + (if c { a } else { b })").right, S.If)
        for src in ("1 + if c { a } else { b }", "x == match y { _ => 1 }", "-if c { 1 } else { 2 }",
                    "1 + |x| x", "a && return 1", "1 + pack 'k. v"):
            with self.assertRaises(KursError, msg=src):
                expr(src)
        self.assertIsInstance(expr("1 + (|x| x)").right, S.Closure)

    def test_closures_return_and_pack_in_whole_positions(self):
        self.assertIsInstance(expr("|x: u32| x + 1"), S.Closure)
        self.assertIsInstance(body("let f = |x| x + 1; f").stmts[0].value, S.Closure)
        self.assertIsInstance(body("return 1;").stmts[0].value, S.Return)
        self.assertIsInstance(body("pack 'a. (tag, acc)").tail, S.Pack)
        self.assertIsInstance(body("g(|x| x, 2)").tail.args[0], S.Closure)

    def test_a_block_like_form_ends_a_statement(self):
        b = body("if c { f(); } -x")
        self.assertEqual((len(b.stmts), b.tail.op), (1, "-"))
        self.assertEqual(len(body("if c { f(); }; g();").stmts), 2)

    def test_conditions_do_not_swallow_the_block(self):
        self.assertIsInstance(body("if a == B { 1 } else { 2 }").tail.cond, S.Binop)
        self.assertIsInstance(body("match p { A => 1 }").tail.scrutinee, S.Path)
        self.assertIsInstance(body("while i < n { i = i + 1; }").stmts[0].value, S.While)


class StatementTests(unittest.TestCase):
    """A store is a statement."""

    def test_stores(self):
        s = body("unsafe *p = v; x").stmts[0]
        self.assertIsInstance(s, S.ExprStmt)
        self.assertEqual((s.value.op, type(s.value.operand).__name__), ("unsafe", "UnsafeStore"))
        self.assertIsInstance(body("unsafe *(unsafe at(b, i)) = val;").stmts[0].value.operand, S.UnsafeStore)
        rejected(self, "fn f() { let u = unsafe *p = v; }")
        rejected(self, "fn f() { g(unsafe *p = v); }")

    def test_assignments_name_places(self):
        self.assertIsInstance(body("x = 1; x.f = 2; self.n.m = 3; 0").stmts[1], S.Assign)
        rejected(self, "fn f() { 1 + x = 1; }")
        rejected(self, "fn f() { -x = 1; }")
        self.assertIsInstance(body("(p.0).1 = 4; 0").stmts[0], S.Assign)
        for target in ("f(x)", "x?", "(*p)", "*p", "(1 + x)", "Point.{ x: 1 }", "(a, b)", "T::x", "x[u32]", "1"):
            with self.subTest(target=target):
                rejected(self, "fn f() { " + target + " = 1; }")

    def test_a_bare_store_says_how_a_store_is_written(self):
        for target in ("*p", "(*p)", "*p.f", "**p"):
            with self.subTest(target=target), self.assertRaisesRegex(
                    KursError, "1:10: a store through a raw pointer is written `unsafe \\*p = v;`$"):
                S.parse("fn f() { " + target + " = 1; }")

    def test_unsafe_reads_and_calls_are_expressions(self):
        self.assertEqual(body("unsafe *p").tail.operand.op, "*")
        self.assertEqual(body("let v = unsafe f(x); v").stmts[0].value.op, "unsafe")

    def test_a_statement_may_start_with_an_unsafe_operand(self):
        read = body("unsafe *p + 1").tail
        self.assertEqual((read.op, read.left.op, read.left.operand.op), ("+", "unsafe", "*"))
        cast = body("unsafe *p as u64").tail
        self.assertIsInstance(cast, S.Cast)
        self.assertEqual(cast.value.operand.op, "*")
        call = body("unsafe f(x) == 0; 1").stmts[0].value
        self.assertEqual((call.op, call.left.op), ("==", "unsafe"))
        self.assertIsInstance(call.left.operand, S.Call)
        rejected(self, "fn f() { unsafe (*p).f = 1; }")   # an unsafe operand is not a place


class UnsafeItemTests(unittest.TestCase):
    """`unsafe` opens a function, and nothing else."""

    def test_unsafe_functions(self):
        f, g, j = S.parse("unsafe fn f() {} fn g() {} instance T[u8] {}")
        self.assertEqual((type(f), f.unsafe, type(g), g.unsafe, type(j)), (S.Fn, True, S.Fn, False, S.InstanceDecl))
        with self.assertRaisesRegex(KursError, "1:8: `unsafe` marks a function, not an instance$"):
            S.parse("unsafe instance T[u32] {}")
        rejected(self, "unsafe struct S { x: u32 }")
        rejected(self, "unsafe;")

    def test_a_trait_method_is_a_signature(self):
        (trait,) = S.parse("forall T. trait Tr { fn m(x: T) -> u32; unsafe fn n(x: T); }")
        self.assertEqual([(m.name, m.body, m.unsafe) for m in trait.methods], [("m", None, False), ("n", None, True)])
        with self.assertRaisesRegex(KursError, "1:40: a trait method is a signature; its body belongs in each instance$"):
            S.parse("forall T. trait Tr { fn m(x: T) -> u32 { 1u32 } }")

    def test_every_other_function_has_a_body(self):
        for src, at in (("fn f() -> u32;", 14), ("impl P { fn m(self); }", 20),
                        ("instance Tr[u8] { fn m(x: u8); }", 30)):
            with self.subTest(src=src), self.assertRaisesRegex(
                    KursError, f"1:{at}: a function needs a body; only a trait method or an extern is a signature$"):
                S.parse(src)

    def test_what_forall_comes_before(self):
        for src in ("forall T. use a::b;", "forall T. export a::b;", "forall T. const N: u32 = 1u32;"):
            with self.subTest(src=src), self.assertRaisesRegex(
                    KursError, "1:11: `forall` comes before a struct, tuple, enum, type alias, function, impl, trait or instance$"):
                S.parse(src)
        with self.assertRaisesRegex(KursError, "1:20: in an impl, `forall` comes before a method$"):
            S.parse("impl P { forall T. const N: u32 = 1u32; }")
        (impl,) = S.parse("impl P { const N: u32 = 1u32; forall T. fn m(x: T) {} }")
        self.assertEqual(impl.items[1].forall.params, ("T",))
        rejected(self, "fn f() { forall T. const N: u32 = 1u32; }")
        rejected(self, "forall T. trait Tr { forall U. fn m(x: T, u: U); }")
        rejected(self, "forall T. instance Tr[T] { forall U. fn m(x: T) {} }")

    def test_type_aliases(self):
        """`type Name = Type;`, after a `forall` when generic, and `type` is a keyword. An `exposed` one parses,
        for signatures to reject."""
        plain, generic, linear, exposed = S.parse(
            "type Count = u32; forall T, 'k. type Pair = (T, Slice[T, 'k]); type H! = L!; exposed type P = u32;")
        self.assertEqual((type(plain), plain.name, plain.body, plain.forall), (S.TypeAlias, "Count", S.TName(("u32",)), None))
        self.assertEqual((generic.name, generic.forall.params, generic.body),
                         ("Pair", ("T", "'k"), S.TTuple((S.TName(("T",)), S.TName(("Slice",), (S.TName(("T",)), S.TBrand("'k")))))))
        self.assertEqual((linear.name, linear.body), ("H!", S.TName(("L!",))))
        self.assertEqual((exposed.exposed, exposed.pos), (S.Exposed(), (1, 78)))
        rejected(self, "type P: u32;")
        rejected(self, "type P = u32")
        rejected(self, "fn f() { let type = 1u32; }")
        rejected(self, "struct S { type: u32 }")

    def test_module_is_reserved(self):
        with self.assertRaisesRegex(KursError, "1:1: `module` is a reserved keyword$"):
            S.parse("module x;")
        rejected(self, "fn f() { let module = 1u32; }")


class LexicalTests(unittest.TestCase):
    """Numeric steps, floats, f-strings."""

    def test_a_chained_numeric_index_is_an_error(self):
        e = expr("(p.0).1")
        self.assertEqual((e.index, e.value.index), (1, 0))
        self.assertEqual(expr("p.0.f").value.index, 0)
        self.assertEqual(expr("p.f.0").index, 0)
        for src in ("p.0.1", "p.f.0.1", "p.0.1.2"):
            with self.assertRaises(KursError, msg=src):
                expr(src)

    def test_floats_have_digits_on_both_sides(self):
        self.assertEqual((expr("0.5").kind, expr("1_0.2_5f64").suffix), ("float", "f64"))
        for src in (".5", "0."):
            with self.assertRaises(KursError, msg=src):
                expr(src)

    def test_floats_take_an_exponent(self):
        for src, text, suffix in (("1e5", "1e5", None), ("2.5E-3", "2.5E-3", None), ("1_0e+1_0f32", "10e+10", "f32"),
                                  ("7.0e300f64", "7.0e300", "f64")):
            lit = expr(src)
            self.assertEqual((lit.kind, lit.value, lit.suffix), ("float", text, suffix), src)
        self.assertEqual(expr("0x1e5").kind, "int")
        for src in ("1e", "1e+", "2.5e"):
            with self.assertRaises(KursError, msg=src):
                expr(src)

    def test_a_name_followed_by_bang_is_one_name(self):
        def kinds(text):
            return [(t.kind, t.text) for t in Lexer(text, "<t>").run()[:-1]]
        self.assertEqual(kinds("Foo!"), [("NAME", "Foo!")])
        self.assertEqual(kinds("Foo! x"), [("NAME", "Foo!"), ("NAME", "x")])
        self.assertEqual(kinds("Foo!::new"), [("NAME", "Foo!"), ("::", "::"), ("NAME", "new")])
        self.assertEqual(kinds("Foo ! x"), [("NAME", "Foo"), ("!", "!"), ("NAME", "x")])
        self.assertEqual(kinds("a!=b"), [("NAME", "a"), ("!=", "!="), ("NAME", "b")])
        self.assertEqual(kinds("a != b"), [("NAME", "a"), ("!=", "!="), ("NAME", "b")])
        self.assertEqual(kinds("!x"), [("!", "!"), ("NAME", "x")])
        self.assertEqual(kinds("!!x"), [("!", "!"), ("!", "!"), ("NAME", "x")])
        self.assertEqual(kinds("return!x"), [("return", "return"), ("!", "!"), ("NAME", "x")])
        self.assertEqual(kinds("-> !"), [("->", "->"), ("!", "!")])
        self.assertEqual(kinds("a!!"), [("NAME", "a!"), ("!", "!")])
        self.assertEqual(expr("a!=b").op, "!=")
        self.assertEqual(expr("Foo![u32]::new()").callee.segments[0][0], "Foo!")
        self.assertEqual(expr("Foo!.{ x }").path.names, ("Foo!",))
        self.assertEqual(S.parse("struct Foo! { x: u32 }")[0].name, "Foo!")
        self.assertEqual(S.parse("fn f() -> ! { g() }")[0].ret, S.TNever())

    def test_only_a_type_name_ends_with_bang(self):
        for src in ("fn f!() {}", "fn f() { let x! = 1u32; }", "forall T!. fn f() {}", "struct S { a!: u32 }",
                    "enum E { A! }", "fn f(&x!: u32) {}", "fn f() -> u32 { p.x! }", "trait T! {}",
                    "const C!: u32 = 0u32;", "fn f() { match x { E::A(y!) => () } }"):
            with self.assertRaisesRegex(KursError, "only a linear type's name ends with !", msg=src):
                S.parse(src)

    def test_f_strings(self):
        e = expr('f"a {x} b {y.z}!"')
        self.assertEqual([p if isinstance(p, str) else type(p).__name__ for p in e.parts],
                         ["a ", "Path", " b ", "Field", "!"])
        self.assertEqual(expr('f""').parts, ("",))
        self.assertEqual(expr('f"{x}"').parts[0::2], ("", ""))

    def test_f_strings_nest(self):
        e = expr('f"{Point.{ x: 1 }.x} and {f"inner {y}"} {{braces}}\\n"')
        field, inner = e.parts[1], e.parts[3]
        self.assertIsInstance(field.value, S.StructLit)
        self.assertEqual(inner.parts, ("inner ", S.Path((("y", ()),), inner.parts[1].pos), ""))
        self.assertEqual(e.parts[4], " {braces}\n")

    def test_interpolations_take_a_lend_and_strings(self):
        e = expr('f"{&name} {g("}")}"')
        self.assertEqual(e.parts[1].op, "&")
        self.assertIsInstance(e.parts[3], S.Call)

    def test_f_string_errors(self):
        for src in ('f"abc', 'f"{x', 'f"a } b"'):
            with self.assertRaises(KursError, msg=src):
                expr(src)

    def test_f_strings_in_whole_files(self):
        items = S.parse('fn f(n: u32) -> Buffer! { f"n = {n}, {n + 1}" }')
        self.assertEqual(len(items[0].body.tail.parts), 5)


class MatchTests(unittest.TestCase):
    """Position rules: `self` only first, a `_` arm only last, an env-less closure type only as a parameter type."""

    def test_the_wildcard_arm_comes_last(self):
        m = body("match x { A => 1, B(v) => v, _ => 0 }").tail
        self.assertEqual((len(m.arms), m.arms[-1].cases[0].kind), (3, "wild"))
        self.assertEqual(body("match x { A => 1, _ => { 0 } }").tail.arms[-1].cases[0].kind, "wild")
        rejected(self, "fn f() { match x { _ => 0, A => 1 } }")
        rejected(self, "fn f() { match x { A | _ => 0 } }")

    def test_arm_bodies_are_whole_expressions(self):
        m = body("match x { A => if c { 1 } else { 2 }, B => return 3, C => |y| y, D => 4 }").tail
        self.assertEqual([type(a.body).__name__ for a in m.arms], ["If", "Return", "Closure", "Lit"])

    def test_integer_arms(self):
        m = body("match n { 0 => 1, 1..=9 | 20 => 2, -5..=-1 => 3, _ => 4 }").tail
        self.assertEqual([c.kind for a in m.arms for c in a.cases], ["value"] * 4 + ["wild"])


class RepositoryTests(unittest.TestCase):
    def test_every_source_parses(self):
        # Hidden directories (caches, worktrees) are not sources.
        files = sorted(p for p in ROOT.rglob("*.kur")
                       if not any(part.startswith(".") for part in p.relative_to(ROOT).parts))
        for path in files:
            with self.subTest(file=str(path.relative_to(ROOT))):
                S.parse(path.read_text(), str(path))


class ExpressionShapeTests(unittest.TestCase):
    def test_precedence(self):
        e = expr("a + b * c as u64 << 1 == d || e && f")
        self.assertEqual(e.op, "||")
        self.assertEqual(e.left.op, "==")
        self.assertEqual(e.left.left.op, "<<")
        add = e.left.left.left
        self.assertEqual((add.op, add.right.op), ("+", "*"))
        self.assertIsInstance(add.right.right, S.Cast)

    def test_prefix_operators(self):
        e = expr("-x.f(y)?")
        self.assertEqual(e.op, "-")
        self.assertIsInstance(e.operand, S.Try)
        lend = expr("&ctx.cur.next()")
        self.assertEqual(lend.op, "&")
        self.assertIsInstance(lend.operand, S.Call)

    def test_unsafe_store_is_one_statement(self):
        e = body("unsafe *p = Chunk.{ data, used: zero };").stmts[0].value
        self.assertEqual(e.op, "unsafe")
        self.assertIsInstance(e.operand, S.UnsafeStore)
        self.assertIsInstance(e.operand.value, S.StructLit)

    def test_struct_literal_shorthand_and_spread(self):
        e = expr("Token.{ count: count + 1u32, kind, ..rest }")
        self.assertEqual([n for n, _ in e.fields], ["count", "kind"])
        self.assertIsNone(e.fields[1][1])
        self.assertEqual(e.spread, "rest")

    def test_type_arguments_in_paths(self):
        e = expr("Result[TomlValue['k], TomlError]::Err.(e)")
        name, args = e.callee.segments[0]
        self.assertEqual(name, "Result")
        self.assertEqual(args[0].args, (S.TBrand("'k"),))
        self.assertEqual(e.callee.names, ("Result", "Err"))
        self.assertTrue(e.construct)
        self.assertEqual(len(args), 2)

    def test_closures_extend_right(self):
        e = expr("|x, &tag| x + 1")
        self.assertIsInstance(e, S.Closure)
        self.assertEqual(e.params[1][:2], (True, "tag"))
        self.assertEqual(e.body.op, "+")
        self.assertIsInstance(expr("|| 0"), S.Closure)

    def test_literals(self):
        self.assertEqual((expr("0xFF_00u32").value, expr("0xFF_00u32").suffix), (0xFF00, "u32"))
        self.assertEqual(expr("b'\\n'").value, 10)
        self.assertEqual(expr("2.5f64").suffix, "f64")
        self.assertEqual(expr('"a\\tb"').value, "a\tb")

    def test_an_integer_literal_fits_64_bits(self):
        self.assertEqual(expr("18446744073709551615").value, (1 << 64) - 1)
        self.assertEqual(expr("0xFFFF_FFFF_FFFF_FFFF").value, (1 << 64) - 1)
        for src in ("18446744073709551616", "0x1_0000_0000_0000_0000u64", "fn f() -> u32 { match 1u32 { 18446744073709551616 => 0u32, _ => 1u32 } }"):
            with self.subTest(src=src), self.assertRaisesRegex(KursError, "integer literal out of range"):
                S.parse(src) if src.startswith("fn") else expr(src)

    def test_escapes(self):
        self.assertEqual(expr('"\\x41\\x7f\\0"').value, "A\x7f\0")
        self.assertEqual(expr("b'\\xff'").value, 255)
        self.assertEqual(expr('f"\\x41{x}"').parts[0], "A")

    def test_an_escape_error_is_at_its_literal(self):
        for src, message in (('"A\\xZZB"', "1:25: invalid \\\\x escape: \\\\x takes two hex digits$"),
                             ('"\\x1"', "1:25: invalid \\\\x escape: \\\\x takes two hex digits$"),
                             ('f"a{x}\\x"', "1:31: invalid \\\\x escape: \\\\x takes two hex digits$"),
                             ("b'\\x'", "1:25: invalid \\\\x escape: \\\\x takes two hex digits$"),
                             ('"\\x80"', "1:25: \\\\x80 is not ASCII; a string's \\\\x escape is at most \\\\x7f$"),
                             ("b'é'", "1:25: a byte literal's character is ASCII; write any other byte as \\\\xHH$"),
                             ('"\\q"', "1:25: unknown escape \\\\q$")):
            with self.subTest(src=src), self.assertRaisesRegex(KursError, message):
                S.parse(f"fn f() -> u32 {{ let s = {src}; 0u32 }}")

    def test_nesting_is_limited(self):
        sys.setrecursionlimit(20000)
        S.parse("fn f() -> i32 { " + "(" * 125 + "0i32" + ")" * 125 + " }")
        S.parse("fn f() -> i32 { " + " + ".join(["1i32"] * 2000) + " }")
        for src in ("(" * 200 + "0i32" + ")" * 200, "{ " * 200 + "0i32" + " }" * 200, "-" * 200 + "1i32",
                    "x" + ".f()" * 100, "0i32" + " as i32" * 200,
                    " else ".join(["if c { 0i32 }"] * 200) + " else { 1i32 }"):
            with self.subTest(src=src[:20]), self.assertRaisesRegex(KursError, "nested too deeply \\(the limit is 128 levels\\)$"):
                S.parse("fn f() -> i32 { " + src + " }")
        with self.assertRaisesRegex(KursError, "1:137: nested too deeply"):
            S.parse("fn f(p: " + "*" * 200 + "u8) {}")

    def test_format_string(self):
        e = expr('f"{dst}.tag = {index(&t, "}")};"')
        self.assertEqual(e.parts[0], "")
        self.assertIsInstance(e.parts[1], S.Path)
        self.assertEqual(e.parts[2], ".tag = ")
        self.assertIsInstance(e.parts[3], S.Call)

    def test_pack(self):
        e = expr("pack 'a, 'b. (tag, acc)")
        self.assertEqual(e.brands, ("'a", "'b"))


class StatementShapeTests(unittest.TestCase):
    def test_condition_has_no_struct_literal(self):
        b = body("if a == B { x } else { y }")
        self.assertIsInstance(b.tail, S.If)
        self.assertEqual(b.tail.cond.op, "==")

    def test_block_like_statement_ends_the_statement(self):
        b = body("if c { f(); } y = 1; -x")
        self.assertEqual(len(b.stmts), 2)
        self.assertIsInstance(b.stmts[1], S.Assign)
        self.assertEqual(b.tail.op, "-")

    def test_trailing_if_is_the_tail_and_while_is_not(self):
        self.assertIsInstance(body("let x = 1; if c { 1 } else { 2 }").tail, S.If)
        b = body("while c { f(); }")
        self.assertIsNone(b.tail)
        self.assertIsInstance(b.stmts[0].value, S.While)

    def test_match_arms(self):
        m = body("""match &j {
            Json::Number(text) => &text.len(),
            Json::Pair(a, _) => { a }
            b'0'..=b'9' | -1 => 1,
            _ => 0usize
        }""").tail
        self.assertEqual(m.scrutinee.op, "&")
        self.assertEqual(len(m.arms), 4)
        self.assertEqual(m.arms[1].cases[0].binders, ("a", "_"))
        low, high = m.arms[2].cases[0].low, m.arms[2].cases[0].high
        self.assertEqual((low.value, high.value, m.arms[2].cases[1].low.value), (48, 57, -1))
        self.assertEqual(m.arms[3].cases[0].kind, "wild")

    def test_patterns(self):
        b = body("let exists 'k. (tag, s): (T, U) = buf.open(); let { a, b: (c, _), ..rest } = t; let [x, y] = arr; 0")
        exists = b.stmts[0]
        self.assertEqual(exists.brands, ("'k",))
        self.assertIsInstance(exists.pattern, S.PTuple)
        fields = b.stmts[1].pattern
        self.assertEqual([n for n, _ in fields.fields], ["a", "b"])
        self.assertEqual(fields.rest, "rest")
        self.assertIsInstance(b.stmts[2].pattern, S.PArray)

    def test_a_var_binds_what_a_let_binds(self):
        b = body("var exists 'k. (tag, s) = buf.open(); var { a, ..rest } = t; var [x, y] = arr; var n: u32 = 0; "
                 "let m = 1; 0")
        self.assertEqual([s.mutable for s in b.stmts], [True, True, True, True, False])
        self.assertEqual(b.stmts[0].brands, ("'k",))
        self.assertEqual([type(s.pattern) for s in b.stmts[:4]], [S.PTuple, S.PStruct, S.PArray, S.PBind])

    def test_a_tail_return_ends_with_a_semicolon(self):
        with self.assertRaisesRegex(KursError, "1:29: a `return` at the end of a block ends with `;`$"):
            S.parse("fn f() -> u32 { return 1u32 }")
        self.assertIsInstance(body("return 1u32;").stmts[0].value, S.Return)


class ItemShapeTests(unittest.TestCase):
    def test_generic_declarations(self):
        f = S.parse("forall T, 'k where Copy[T], Eq[T, T]. fn f(&tag: Exclusive!['k], x: T) -> T { x }")[0]
        self.assertEqual(f.forall.params, ("T", "'k"))
        self.assertEqual(len(f.forall.premises), 2)
        self.assertTrue(f.params[0].lend)
        self.assertEqual(f.params[0].type.path, ("Exclusive!",))

    def test_determined_parameters(self):
        t = S.parse("forall Self -> Item. trait Iterator { fn next(&it: Self) -> Option[Item]; }")[0]
        self.assertEqual((t.forall.params, t.forall.determined), (("Self",), ("Item",)))
        i = S.parse("forall T. instance ListAccess[Ctx, List[T] -> T] { }")[0]
        self.assertEqual(i.type.determined, 1)

    def test_use_trees(self):
        u = S.parse("use package::shapes::{Point as P, make_point};")[0]
        path, star, children, alias, start = u.tree
        self.assertEqual((path, start), (("package", "shapes"), (1, 5)))
        self.assertEqual(children[0], (("Point",), False, (), "P", (1, 23)))
        self.assertTrue(S.parse("use std::io::*;")[0].tree[1])

    def test_types(self):
        f = S.parse("fn f(g: closure[Env](&_: C, T) -> bool, p: foreach *F of *T => Region![F, 'f], "
                    "q: exists 'a. (Tag!['a], u32), r: fn(*u8) -> !) {}")[0]
        g, p, q, r = (x.type for x in f.params)
        self.assertEqual((g.env.path, g.params[0][0]), (("Env",), True))
        self.assertTrue(p.indirect)
        self.assertIsInstance(p.source, S.TPtr)
        self.assertEqual(q.brands, ("'a",))
        self.assertIsInstance(r.ret, S.TNever)

    def test_exposure(self):
        s = S.parse("exposed(package::resolve) struct Stamp! { marker: Brand['r] }")[0]
        self.assertEqual(s.exposed.scope, ("package", "resolve"))
        self.assertEqual(len(s.fields), 1)
        self.assertIsNone(S.parse("struct Hidden { x: u32 }")[0].exposed)


if __name__ == "__main__":
    unittest.main()
