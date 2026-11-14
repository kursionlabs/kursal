"""One-way instance matching shares constructor rules and cannot leak binders."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from kursal import core as C
from kursal.cc import FLAGS, c_compiler
from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.infer import infer
from kursal.kurs import parse_program
from kursal.lower import lower
from kursal.core import ModuleId
from kursal.resolve import resolve
from kursal.sigs import signatures
from kursal.types import equal
from kursal.formation import strict_subterm
from kursal.unify import Meta, match_head, unify, zonk
from tests.test_resolve import manifest, tree


T, U = C.Named("T"), C.Named("U")


def match(pattern, query, params=(C.Param("T"),)):
    return match_head(params, (pattern,), (query,))


def brand(name):
    return C.Brand(C.Skolem(name))


class MatchingTests(unittest.TestCase):
    def test_function_result_parameter_is_inferred(self):
        self.assertEqual(match(C.Fn((), T), C.Fn((), C.U32)), {"T": C.U32})

    def test_parameters_are_not_extracted_from_under_binders(self):
        for left, right in (
            (C.Exists(("k",), C.Tuple((T, brand("k")))),
             C.Exists(("q",), C.Tuple((C.U32, brand("q"))))),
            (C.Fn((brand("k"),), T, (C.Param("k", C.BRAND),)),
             C.Fn((brand("q"),), C.U32, (C.Param("q", C.BRAND),))),
            (C.Foreach("F", False, C.Tuple((C.U32,)), C.Tuple((C.Named("F"), T))),
             C.Foreach("G", False, C.Tuple((C.U32,)), C.Tuple((C.Named("G"), C.U32)))),
        ):
            with self.subTest(left=left):
                self.assertEqual(match(left, right), {})

    def test_bound_occurrences_must_agree_with_binder_free_solution(self):
        cases = (
            (C.Exists(("k",), C.Tuple((T, brand("k")))),
             C.Exists(("q",), C.Tuple((C.U32, brand("q")))),
             C.Exists(("q",), C.Tuple((C.BOOL, brand("q"))))),
            (C.Fn((brand("k"),), T, (C.Param("k", C.BRAND),)),
             C.Fn((brand("q"),), C.U32, (C.Param("q", C.BRAND),)),
             C.Fn((brand("q"),), C.BOOL, (C.Param("q", C.BRAND),))),
            (C.Foreach("F", False, C.U32, C.Tuple((C.Named("F"), T))),
             C.Foreach("G", False, C.U32, C.Tuple((C.Named("G"), C.U32))),
             C.Foreach("G", False, C.U32, C.Tuple((C.Named("G"), C.BOOL)))),
        )
        for pattern, good, bad in cases:
            with self.subTest(pattern=pattern):
                self.assertEqual(
                    match_head((C.Param("T"),), (pattern, T), (good, C.U32),
                               require_extractable=True),
                    {"T": C.U32})
                self.assertIsNone(
                    match_head((C.Param("T"),), (pattern, T), (bad, C.U32),
                               require_extractable=True))

    def test_free_parameters_cannot_capture_bound_names(self):
        for left, right in (
            (C.Exists(("k",), T), C.Exists(("q",), brand("q"))),
            (C.Fn((), T, (C.Param("k", C.BRAND),)), C.Fn((), brand("q"), (C.Param("q", C.BRAND),))),
            (C.Foreach("F", False, C.Tuple((C.U32,)), T),
             C.Foreach("G", False, C.Tuple((C.U32,)), C.Named("G"))),
        ):
            with self.subTest(left=left):
                self.assertIsNone(match(left, right))
        self.assertIsNone(match(C.Exists(("k",), C.Skolem("outer")), C.Exists(("q",), C.Skolem("q")),
                                (C.Param("outer", C.BRAND),)))

    def test_body_inference_can_solve_a_variable_inside_its_foreach_scope(self):
        variable = Meta()
        left = C.Foreach("F", False, C.U32, C.Exists(("k",), C.Tuple((variable, brand("k")))))
        right = C.Foreach("G", False, C.U32, C.Exists(("q",), C.Tuple((C.Named("G"), brand("q")))))
        self.assertTrue(unify(left, right))
        self.assertTrue(equal(zonk(left), right))

    def test_bound_names_are_distinct_from_free_query_names(self):
        self.assertIsNone(match(C.Exists(("k",), brand("k")), C.Exists(("q",), brand("k")), ()))
        self.assertIsNone(match(C.Foreach("F", False, C.U32, C.Named("F")),
                                C.Foreach("G", False, C.U32, C.Named("F")), ()))

    def test_bound_name_permutations_preserve_distinct_identities(self):
        left = C.Exists(("a", "b"), C.Tuple((brand("a"), brand("b"))))
        right = C.Exists(("b", "a"), C.Tuple((brand("b"), brand("a"))))
        self.assertEqual(match(left, right, ()), {})
        wrong = C.Exists(("b", "a"), C.Tuple((brand("b"), brand("b"))))
        self.assertIsNone(match(left, wrong, ()))

    def test_foreach_source_is_outside_the_binder(self):
        self.assertEqual(match(C.Foreach("T", False, T, C.Named("T")),
                               C.Foreach("G", False, C.U32, C.Named("G"))), {"T": C.U32})

    def test_query_variables_are_not_solved(self):
        variable = Meta()
        self.assertIsNone(match(C.U32, variable, ()))
        self.assertIsNone(variable.ref)
        self.assertIsNone(match(T, variable))
        self.assertIsNone(variable.ref)

    def test_partial_input_matching_leaves_output_parameters_unbound(self):
        params = (C.Param("T"), C.Param("Item"))
        self.assertEqual(match_head(params, (T,), (C.U32,)), {"T": C.U32})
        self.assertIsNone(match_head(params, (T, U), (C.U32,)))

    def test_repeated_parameters_share_one_substitution(self):
        self.assertIsNone(match_head((C.Param("T"),), (C.Fn((), T), T), (C.Fn((), C.U32), C.BOOL)))
        self.assertEqual(match_head((C.Param("T"),), (C.Fn((), T), T), (C.Fn((), C.U32), C.U32)),
                         {"T": C.U32})

    def test_full_head_solutions_have_binder_free_query_paths(self):
        hidden = C.Fn((), T, forall=(C.Param("b", C.BRAND),))
        examples = (
            ((C.Tuple((T, C.U32)),), (C.Tuple((C.BOOL, C.U32)),)),
            ((C.Fn((T,), C.UNIT),), (C.Fn((C.BOOL,), C.UNIT),)),
            ((hidden, T), (C.Fn((), C.BOOL, forall=(C.Param("q", C.BRAND),)), C.BOOL)),
        )
        for head, query in examples:
            with self.subTest(head=head):
                sub = match_head((C.Param("T"),), head, query, require_extractable=True)
                self.assertIsNotNone(sub)
                self.assertTrue(any(equal(sub["T"], q) or strict_subterm(sub["T"], q)
                                    for q in query))

    def test_function_contracts_and_foreach_patterns_are_distinct(self):
        for query in (C.Fn((), C.U32, unsafe=True), C.Fn((C.U32,), C.U32),
                      C.Fn((), C.U32, places=frozenset({0}))):
            with self.subTest(query=query):
                self.assertIsNone(match(C.Fn((), T), query))
        self.assertIsNone(match(C.Foreach("F", False, C.U32, T), C.Foreach("G", True, C.U32, C.BOOL)))

    def test_copy_constraints_use_the_parameter_extracted_from_a_function_type(self):
        program = parse_program("""(type Linear! (tuple))
          (trait Test (Self) (run u32 ()))
          (instance Test (forall (T) ((Copy T)) ((fn () T)))
            (fun run u32 () (entry () (= %n (const u32 0)) (ret %n))))""")
        checker = check_program(program)
        self.assertIsNotNone(checker.find_instance("Test", (C.Fn((), C.U32),)))
        self.assertIsNone(checker.find_instance("Test", (C.Fn((), C.Named("Linear")),)))

    def test_closed_quantified_types_can_be_parameter_solutions(self):
        closed = C.Exists(("k",), brand("k"))
        self.assertEqual(match(T, closed), {"T": closed})

    def test_kurs_dispatch_compiles_and_runs_for_unbound_function_head(self):
        for pattern, query in (
            ("(fn () T)", "(fn () u32)"),
        ):
            with self.subTest(pattern=pattern), tempfile.TemporaryDirectory() as directory:
                program = parse_program(f"""(trait Test (Self) (run u32 ()))
                  (instance Test (forall (T) () ({pattern}))
                    (fun run u32 () (entry () (= %n (const u32 7)) (ret %n))))
                  (fun main i32 () (entry () (= %n (trait_call Test run ({query})))
                    (= %r (int_cast %n i32)) (ret %r)))""")
                c = emit_c(program, check_program(program), "main")
                binary = str(Path(directory) / "test")
                built = subprocess.run([c_compiler(), *FLAGS, "-x", "c", "-o", binary, "-"],
                                       input=c, text=True, capture_output=True)
                self.assertEqual(built.returncode, 0, built.stderr)
                self.assertEqual(subprocess.run([binary]).returncode, 7)

    def test_surface_inference_and_lowering_match_a_generic_function_head(self):
        source = """
forall Self. trait Test { fn run(self: Self) -> u32; }
forall T. instance Test[fn() -> T] { fn run(self: fn() -> T) -> u32 { 7u32 } }
fn value() -> u32 { 3u32 }
fn main() -> i32 { value.run() as i32 }
"""
        root = tree({"p/kur.toml": manifest("p", entry="package::main"), "p/lib.kur": source})
        resolver = resolve(root / "p")
        sigs = signatures(resolver)
        infer(resolver, sigs)
        call = resolver.scopes[ModuleId(0)].decls["main"].node.body.tail.value
        self.assertEqual(call.call.targs, (C.Fn((), C.U32),))
        program = lower(resolver, sigs)
        self.assertIn("main", emit_c(program, check_program(program), "p::main"))
