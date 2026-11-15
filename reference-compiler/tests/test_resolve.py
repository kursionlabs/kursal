"""Packages and name resolution, on small package trees written per test."""

import tempfile
import unittest
from pathlib import Path

from kursal.core import ModuleId
from kursal.resolve import LIB as LIBRARIES, Decl, ResolveError, resolve


def tree(files):
    """Write {relative path: text} into a fresh directory; returns its path."""
    root = Path(tempfile.mkdtemp(prefix="kursal-resolve-"))
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def manifest(name, deps=(), entry=None):
    text = f'[package]\nname="{name}"\nsource_root="."\n'
    if entry:
        text += f'kind="executable"\nentry="{entry}"\n'
    for dep in deps:
        text += f'[dependencies.{dep}]\npath="{LIBRARIES / "core" if dep == "core" else "../" + dep}"\n'
    return text


LIB = {
    "lib/kur.toml": manifest("lib"),
    "lib/lib.kur": "use package::shapes;\nexport shapes;\n",
    "lib/shapes.kur": "exposed struct Point { x: u32 }\nfn make(a: u32) -> Point { Point.{ x: a } }\n"
                      "enum Shape { Dot(Point), Empty }\nimpl Point { fn get(self) -> u32 { 0 } }\n",
    "lib/hidden.kur": "fn secret() -> u32 { 0 }\n",
}


def app(source, extra=None):
    files = dict(LIB, **{"app/kur.toml": manifest("app", ["lib"]), "app/lib.kur": source}, **(extra or {}))
    return tree(files) / "app"


def body_of(resolver, name):
    return resolver.scopes[ModuleId(0)].decls[name].node.body


def kernel(core_source, source=""):
    """A package named core, in `kernel/`, whose lib.kur is `core_source`, and `app`, which depends on it."""
    return tree({"kernel/kur.toml": manifest("core"), "kernel/lib.kur": core_source,
                 "app/kur.toml": manifest("app", ["kernel"]), "app/lib.kur": source}) / "app"


class BuiltinTests(unittest.TestCase):
    """Only package core names builtins (`builtin::name`); every other package imports them from it."""

    def test_core_exports_builtins_under_their_names(self):
        r = resolve(kernel("export builtin::{Brand, NonNull, fresh};\n",
                           "use kernel::{Brand, NonNull};\n"
                           "forall 'k. fn f(b: Brand['k], p: NonNull[u32]) -> u32 { let exists 'a. w = kernel::fresh(); 0u32 }\n"))
        fn = r.scopes[ModuleId(0)].decls["f"].node
        self.assertEqual([p.type.ref for p in fn.params], [("builtin", "Brand"), ("builtin", "NonNull")])

    def test_only_core_names_builtins(self):
        for source in ("fn bad(x: builtin::Brand['static]) -> () { () }\n", "fn bad() -> u32 { builtin::fresh(); 0u32 }\n",
                       "use builtin::fresh;\nfn bad() -> u32 { fresh(); 0u32 }\n", "export builtin::Brand;\n"):
            with self.subTest(source=source), self.assertRaisesRegex(ResolveError, "only package core names builtins"):
                resolve(kernel("", source))

    def test_unknown_builtins(self):
        for type_name in ("builtin", "builtin::Brand::Extra['static]", "builtin::Phantom"):
            with self.subTest(type_name=type_name), self.assertRaisesRegex(ResolveError, "unknown builtin"):
                resolve(kernel(f"fn bad(x: {type_name}) -> () {{ () }}\n"))

    def test_a_builtin_value_is_not_a_type(self):
        with self.assertRaisesRegex(ResolveError, "fresh is not a type"):
            resolve(kernel("export builtin::fresh;\n", "use kernel::fresh;\nfn bad(x: fresh) -> () { () }\n"))


class GraphTests(unittest.TestCase):

    def test_manifest_values_are_validated_before_building_typed_packages(self):
        manifests = (
            '[package]\nname=12\nsource_root="."\n',
            '[package]\nname="p"\nsource_root=false\n',
            '[package]\nname="p"\nsyntax=12\n',
            'dependencies=[]\n[package]\nname="p"\nsource_root="."\n',
            manifest("p") + '[dependencies.x]\npath=3\n',
        )
        for source in manifests:
            with self.subTest(source=source), self.assertRaises(ResolveError):
                root = tree({"p/kur.toml": source, "p/lib.kur": ""})
                resolve(root / "p")

    def test_modules_follow_paths(self):
        root = tree({"p/kur.toml": manifest("p"), "p/lib.kur": "", "p/a.kur": "", "p/b/lib.kur": "", "p/b/c.kur": ""})
        r = resolve(root / "p")
        self.assertEqual({m.path for m in r.scopes}, {(), ("a",), ("b",), ("b", "c")})

    def test_two_files_for_one_module(self):
        root = tree({"p/kur.toml": manifest("p"), "p/m.kur": "", "p/m/lib.kur": ""})
        with self.assertRaises(ResolveError):
            resolve(root / "p")

    def test_dependency_cycle(self):
        root = tree({"a/kur.toml": manifest("a", ["b"]), "a/lib.kur": "",
                     "b/kur.toml": manifest("b", ["a"]), "b/lib.kur": ""})
        with self.assertRaises(ResolveError):
            resolve(root / "a")

    def test_module_directive_rejected(self):
        with self.assertRaises(ResolveError):
            resolve(tree({"p/kur.toml": manifest("p"), "p/lib.kur": "module x;\n"}) / "p")

    def test_entry(self):
        root = tree({"p/kur.toml": manifest("p", entry="package::run::main"), "p/lib.kur": "",
                     "p/run.kur": "fn main() -> i32 { 0i32 }\n"})
        self.assertEqual(resolve(root / "p").packages[0].entry_decl.name, "main")


class NameTests(unittest.TestCase):
    def test_references_resolve_across_packages(self):
        r = resolve(app("use lib::shapes::{Point, make as mk};\n"
                        "fn f(a: u32) -> Point { let p = mk(a); let n = p.x; Point::get(p) ; mk(n) }\n"))
        body = body_of(r, "f")
        call = body.stmts[0].value
        self.assertEqual((call.callee.ref[0], call.callee.ref[1].name), ("decl", "make"))
        self.assertEqual(call.args[0].ref, ("local", "a"))
        method = body.stmts[2].value.callee.ref[1]
        self.assertEqual((method.kind, method.name), ("method", "Point::get"))

    def test_variants_and_match_binders(self):
        r = resolve(app("use lib::shapes;\nfn f(s: shapes::Shape) -> u32 {\n"
                        "  match s { shapes::Shape::Dot(p) => 1u32, shapes::Shape::Empty => 0u32 } }\n"))
        arm = body_of(r, "f").tail.arms[0]
        kind, enum, variant = arm.cases[0].path.ref
        self.assertEqual((kind, enum.name, variant), ("variant", "Shape", "Dot"))

    def test_type_parameters_and_brands(self):
        r = resolve(tree({"app/kur.toml": manifest("app", ["core"]),
                          "app/lib.kur": "forall T, 'k. fn f(x: T, b: core::cap::Brand['k], n: u32) -> T { x }\n"}) / "app")
        fn = r.scopes[ModuleId(0)].decls["f"].node
        self.assertEqual([p.type.ref for p in fn.params], [("param", "T"), ("builtin", "Brand"), ("prim", "u32")])

    def test_shadowing_locals(self):
        r = resolve(app("fn g() -> u32 { 0u32 }\nfn f() -> u32 { let g = 1u32; g }\n"))
        self.assertEqual(body_of(r, "f").tail.ref, ("local", "g"))

    def test_glob_import(self):
        r = resolve(app("use lib::shapes::*;\nfn f(a: u32) -> Point { make(a) }\n"))
        self.assertIsInstance(body_of(r, "f").tail.callee.ref[1], Decl)

    def test_unknown_name(self):
        with self.assertRaises(ResolveError):
            resolve(app("fn f() -> u32 { nothing() }\n"))

    def test_private_module_of_a_dependency(self):
        with self.assertRaises(ResolveError):
            resolve(app("fn f() -> u32 { lib::hidden::secret() }\n"))

    def test_duplicate_alias(self):
        with self.assertRaises(ResolveError):
            resolve(app("use lib::shapes::Point;\nuse lib::shapes::Point;\n"))

    def test_sealed_alias(self):
        with self.assertRaises(ResolveError):
            resolve(app("use lib::shapes::Point as Copy;\n"))

    def test_alias_cycle(self):
        with self.assertRaises(ResolveError):
            resolve(app("use package::a::X;\nfn f() -> u32 { X.() }\n",
                        {"app/a.kur": "use package::X;\n"}))

    def test_struct_literal_needs_a_struct(self):
        with self.assertRaises(ResolveError):
            resolve(app("use lib::shapes::make;\nfn f() -> u32 { make.{ x: 1u32 } }\n"))


if __name__ == "__main__":
    unittest.main()
