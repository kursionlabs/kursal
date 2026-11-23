"""Instance search follows structural descent rather than an arbitrary depth cap."""

import unittest

from kursal import core as C
from kursal.check import Checker, check_program
from kursal.cgen import emit_c
from kursal.infer import infer
from kursal.resolve import resolve
from kursal.sexpr import KursError
from kursal.sigs import signatures
from kursal.termination import check_instance_termination, strict_subterm
from tests.test_resolve import manifest, tree


T = C.Named("T")


def header(trait, ty, *premises):
    return C.Premise(trait, (ty,)), tuple(C.Premise(name, (arg,)) for name, arg in premises), (4, 2)


class InstanceTerminationTests(unittest.TestCase):
    def test_self_cycles_must_strictly_decrease(self):
        for premise in (T, C.Tuple((T,)), C.U32):
            with self.subTest(premise=premise), self.assertRaisesRegex(KursError, "4:2: recursive instance"):
                check_instance_termination([header("P", T, ("P", premise))])

    def test_mutual_cycles_check_every_edge(self):
        check_instance_termination([
            header("P", C.Tuple((T,)), ("Q", T)),
            header("Q", C.Tuple((T,)), ("P", T))])
        with self.assertRaisesRegex(KursError, "Q -> P"):
            check_instance_termination([
                header("P", C.Tuple((T,)), ("Q", T)),
                header("Q", T, ("P", C.Tuple((T,))))])

    def test_acyclic_premises_can_construct_types(self):
        check_instance_termination([header("P", T, ("Q", C.Tuple((T,)))), header("Q", T)])

    def test_empty_recursive_arguments_cannot_prove_descent(self):
        with self.assertRaisesRegex(KursError, "strict structural subterm"):
            check_instance_termination([(C.Premise("P", ()), (C.Premise("P", ()),), None)])

    def test_nominal_descent_is_syntactic_and_respects_binders(self):
        self.assertTrue(strict_subterm(T, C.Named("Box", (C.Tuple((T,)),))))
        self.assertTrue(strict_subterm(T, C.Fn((T,), C.U32)))
        self.assertFalse(strict_subterm(T, T))
        self.assertFalse(strict_subterm(T, C.Exists(("T",), T)))
        self.assertFalse(strict_subterm(T, C.Foreach("T", False, C.U32, T)))

    def test_recursive_premise_descends_through_unbound_function(self):
        check_instance_termination([
            header("P", C.Fn((T,), C.U32), ("P", T))])

    def test_kurs_rejects_unused_recursive_instances_before_search(self):
        p = C.Program()
        p.traits["P"] = C.Trait("P", ("T",), ())
        p.instances["P"] = [C.Instance("P", (C.Param("T"),), (C.Premise("P", (T,)),), (T,), ())]
        with self.assertRaisesRegex(KursError, "recursive instance"):
            Checker(p)

    def test_surface_rejects_unused_growing_instances(self):
        source = """
forall T. trait P {}
forall T. exposed struct Box { value: T }
forall T where P[Box[T]]. instance P[T] {}
"""
        root = tree({"p/kur.toml": manifest("p"), "p/lib.kur": source})
        resolver = resolve(root / "p")
        with self.assertRaisesRegex(KursError, "recursive instance"):
            infer(resolver, signatures(resolver))

    def test_deep_valid_proof_reaches_codegen(self):
        nested = C.U32
        for _ in range(64):
            nested = C.Tuple((nested,))
        p = C.Program()
        p.traits["P"] = C.Trait("P", ("T",), ())
        p.instances["P"] = [
            C.Instance("P", (), (), (C.U32,), ()),
            C.Instance("P", (C.Param("T"),), (C.Premise("P", (T,)),), (C.Tuple((T,)),), ())]
        p.funs["need"] = C.Fun("need", (), (C.Premise("P", (nested,)),), C.U32, (), (
            C.Block("entry", (), (C.Instr(("%r",), "const", ty=C.U32, lit="0"),), C.Ret("%r")),))
        p.funs["main"] = C.Fun("main", (), (), C.U32, (), (
            C.Block("entry", (), (C.Instr(("%r",), "call", ("need",)),), C.Ret("%r")),))
        self.assertIn("main", emit_c(p, check_program(p)))
