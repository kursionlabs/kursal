"""Instance parameters come from the full head, never just premises or methods."""

import unittest

from kursal import core as C
from kursal.check import check_program
from kursal.coherence import check_instance_range
from kursal.infer import infer
from kursal.kurs import parse_program
from kursal.resolve import resolve
from kursal.sexpr import KursError
from kursal.sigs import signatures
from tests.test_resolve import manifest, tree


class InstanceRangeTests(unittest.TestCase):
    def test_unused_type_and_brand_parameters_are_rejected(self):
        for param, spelling in ((C.Param("T"), "T"), (C.Param("k", C.BRAND), r"\$k")):
            with self.subTest(param=param), self.assertRaisesRegex(
                    KursError, f"7:3: instance of P: parameter {spelling} needs a binder-free head occurrence"):
                check_instance_range(C.Premise("P", (C.U32,)), (param,), (7, 3))

    def test_nested_head_and_output_positions_count(self):
        check_instance_range(C.Premise("P", (C.U32, C.Named("Box", (C.Named("Item"), C.Skolem("k"))))),
                             (C.Param("Item"), C.Param("k", C.BRAND)), None)

    def test_bound_occurrences_do_not_count(self):
        for ty, param in (
            (C.Exists(("k",), C.Brand(C.Skolem("k"))), C.Param("k", C.BRAND)),
            (C.Fn((), C.Brand(C.Skolem("k")), (C.Param("k", C.BRAND),)), C.Param("k", C.BRAND)),
            (C.Foreach("T", False, C.Tuple((C.U32,)), C.Named("T")), C.Param("T")),
        ):
            with self.subTest(ty=ty), self.assertRaisesRegex(KursError, "needs a binder-free head occurrence"):
                check_instance_range(C.Premise("P", (ty,)), (param,), None)

    def test_foreach_binder_does_not_scope_over_its_source(self):
        check_instance_range(C.Premise("P", (C.Foreach("T", False, C.Named("T"), C.Named("T")),)),
                             (C.Param("T"),), None)

    def test_kurs_rejects_unused_instances_and_premise_only_parameters(self):
        for source in (
            "(trait P (Self)) (instance P (forall (T) () (u32)))",
            "(trait P (Self)) (trait Q (T)) (instance P (forall (T) ((Q T)) (u32)))",
            """(trait P (Self) (run usize ()))
            (instance P (forall (T) () (u32))
              (fun run usize () (entry () (= %r (builtin::sizeof T)) (ret %r))))""",
        ):
            with self.subTest(source=source), self.assertRaisesRegex(KursError, "parameter T needs a binder-free head occurrence"):
                check_program(parse_program(source))

    def test_kurs_accepts_parameters_only_in_output_positions(self):
        checker = check_program(parse_program("""(trait P (Self Item))
          (instance P (forall (Item) () (u32 Item)))"""))
        self.assertEqual(checker.find_instance("P", (C.U32, C.BOOL))[1], {"Item": C.BOOL})

    def test_kurs_checks_brand_range_and_binds_brand_outputs(self):
        with self.assertRaisesRegex(KursError, r"parameter \$k needs a binder-free head occurrence"):
            check_program(parse_program("(trait P (Self)) (instance P (forall ($k) () (u32)))"))
        checker = check_program(parse_program("""(trait P (Self $k))
          (instance P (forall ($q) () (u32 $q)))"""))
        self.assertEqual(checker.find_instance("P", (C.U32, C.Skolem("static")))[1],
                         {"q": C.Skolem("static")})

    def test_surface_rejects_parameters_only_in_premises(self):
        source = """
forall Self. trait P {}
forall T. trait Q {}
forall T where Q[T]. instance P[u32] {}
"""
        root = tree({"p/kur.toml": manifest("p"), "p/lib.kur": source})
        resolver = resolve(root / "p")
        with self.assertRaisesRegex(KursError, "parameter T needs a binder-free head occurrence"):
            infer(resolver, signatures(resolver))

    def test_surface_accepts_parameters_only_in_outputs(self):
        source = """
forall Self -> Item. trait P {}
forall Item. instance P[u32 -> Item] {}
"""
        root = tree({"p/kur.toml": manifest("p"), "p/lib.kur": source})
        resolver = resolve(root / "p")
        infer(resolver, signatures(resolver))
