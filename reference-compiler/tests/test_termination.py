"""Specialization is finite before C emission starts; runtime recursion is allowed."""

import unittest

from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.core import (U32, UNIT, Block, Foreach, Fun, Instr, Instance, Method, Named, Param,
                       Program, Premise, Ret, Trait, Tuple)
from kursal.kurs import parse_program
from kursal.sexpr import KursError
from kursal.termination import check_mono_termination


def function(name, target, args, op="call", params=("T",)):
    return Fun(name, tuple(Param(p) for p in params), (), U32, (), (
        Block("entry", (), (Instr(("%r",), op, target, tys=args, pos=(7, 3)),), Ret("%r")),))


class TerminationTests(unittest.TestCase):
    def test_checked_growth_is_rejected_by_codegen_with_position(self):
        program = parse_program("""
(fun f (forall (T) () u32 ()
  (entry () (= %r (call (f (tuple T T)))) (ret %r))))
(fun main u32 () (entry () (= %r (call (f u32))) (ret %r)))
""")
        checker = check_program(program)  # Safety is independent of finiteness.
        with self.assertRaisesRegex(KursError, r"3:\d+: recursive specialization f -> f"):
            emit_c(program, checker)

    def test_ordinary_recursion_permutation_and_closed_reset_emit(self):
        for args in ("T U", "U T", "(tuple u32 u32) T"):
            with self.subTest(args=args):
                program = parse_program(f"""
(fun f (forall (T U) () u32 ()
  (entry () (= %r (call (f {args}))) (ret %r))))
(fun main u32 () (entry () (= %r (call (f u32 u32))) (ret %r)))
""")
                self.assertIn("main", emit_c(program, check_program(program)))

    def test_mutual_growth_and_function_address_are_edges(self):
        for op in ("call", "fn"):
            with self.subTest(op=op):
                program = parse_program(f"""
(fun f (forall (T) () u32 ()
  (entry () (= %r (call (g T))) (ret %r))))
(fun g (forall (U) () u32 ()
  (entry () (= %p ({op} (f (tuple U U))))
    (= %r (const u32 0)) (ret %r))))
""")
                check_program(program)
                with self.assertRaisesRegex(KursError, "recursive specialization g -> f"):
                    check_mono_termination(program)

    def test_acyclic_construction_is_allowed(self):
        program = Program()
        program.funs["f"] = function("f", ("g",), (Tuple((Named("T"),)),))
        program.funs["g"] = function("g", ("g",), (Named("T"),))
        check_program(program)
        check_mono_termination(program)

    def test_trait_dispatch_and_all_instance_bodies_are_edges(self):
        program = Program()
        program.traits["Visit"] = Trait("Visit", ("T",), (Method("run", U32, ()),))
        method = function("run", ("f",), (Tuple((Named("T"),)),))
        method.params = ()
        fixed = function("run", ("f",), (U32,), params=())
        fixed.blocks[0].instrs = (Instr(("%r",), "const", ty=U32, lit="0"),)
        program.instances["Visit"] = [Instance("Visit", (), (), (U32,), (fixed,)),
                                      Instance("Visit", (Param("T"),), (), (Tuple((Named("T"),)),), (method,))]
        program.funs["f"] = function("f", ("Visit", "run"), (Tuple((Named("T"),)),), "trait_call")
        check_program(program)
        with self.assertRaisesRegex(KursError, "recursive specialization"):
            check_mono_termination(program)

    def test_foreach_mapper_cannot_hide_a_cycle(self):
        program = Program()
        caller = function("f", ("g",), ())
        mapper = function("g", ("f",), (Named("T"),))
        program.funs.update(f=caller, g=mapper)
        traversal = Instr(("%r",), "foreach", ("%source", "g"),
                          ty=Foreach("Item", False, Named("T"), Named("Item")))
        caller.blocks[0].instrs = (traversal,)
        with self.assertRaisesRegex(KursError, "recursive foreach"):
            check_mono_termination(program)
        # Fixed sources have a finite collection of component types.
        traversal.ty = Foreach("Item", False, Tuple((U32,)), Named("Item"))
        check_mono_termination(program)
        traversal.tys = (Tuple((Named("T"),)),)
        with self.assertRaisesRegex(KursError, "unchanged parameter"):
            check_mono_termination(program)

    def test_checked_recursive_traversal_is_rejected_before_emission(self):
        program = Program()
        t = Named("T")
        program.funs["f"] = Fun("f", (Param("T"),), (Premise("builtin::Exposed", (t,)),), U32, (t,), (
            Block("entry", (("%x", t),), (
                Instr(("%mapped",), "foreach", ("%x", "mapper"), ty=Foreach("Item", False, t, UNIT)),
                Instr(("%r",), "const", ty=U32, lit="0")), Ret("%r")),))
        program.funs["mapper"] = Fun("mapper", (Param("T"),), (), UNIT, (t,), (
            Block("entry", (("%x", t),), (
                Instr(("%wrapped",), "construct", ("%x",), ty=Tuple((t,))),
                Instr(("%r",), "call", ("f", "%wrapped"), tys=(Tuple((t,)),)),
                Instr(("%unit",), "construct", ty=UNIT)), Ret("%unit")),))
        checker = check_program(program)
        with self.assertRaisesRegex(KursError, "recursive foreach"):
            emit_c(program, checker)

    def test_self_recursive_function_address_is_rejected(self):
        program = parse_program("""
(fun f (forall (T) () u32 ()
  (entry () (= %p (fn (f (tuple T)))) (= %r (const u32 0)) (ret %r))))
""")
        check_program(program)
        with self.assertRaisesRegex(KursError, "recursive specialization f -> f"):
            check_mono_termination(program)

    def test_erased_brand_parameters_do_not_count_as_growth(self):
        program = parse_program("""
(fun f (forall ($b) () u32 ()
  (entry () (= %r (call (f $b))) (ret %r))))
""")
        check_program(program)
        check_mono_termination(program)
