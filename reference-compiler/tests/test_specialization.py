"""Logical specialization and ABI preservation (no external C compiler needed)."""

import unittest

from kursal import core as C
from kursal.cgen import Backend, emit_c
from kursal.check import check_program
from kursal.sexpr import KursError


def constant(name, value):
    return C.Fun(name, (), (), C.U32, (),
                 (C.Block("entry", (), (C.Instr(("%n",), "const", ty=C.U32, lit=str(value)),),
                          C.Ret("%n")),))


def pick_program():
    trait = C.Trait("Pick", ("T",), (C.Method("code", C.U32, ()),))
    instances = [C.Instance("Pick", (), (), (C.Brand(C.Skolem("static")),), (constant("code", 1),)),
                 C.Instance("Pick", (), (), (C.UNIT,), (constant("code", 2),))]
    t = C.Named("T")
    choose = C.Fun("choose", (C.Param("T"),), (C.Premise("Pick", (t,)),), C.U32, (),
                   (C.Block("entry", (),
                            (C.Instr(("%n",), "trait_call", ("Pick", "code"), tys=(t,)),), C.Ret("%n")),))
    main = C.Fun("main", (), (), C.U32, (),
                 (C.Block("entry", (),
                          (C.Instr(("%a",), "call", ("choose",), tys=(C.Brand(C.Skolem("static")),)),
                           C.Instr(("%b",), "call", ("choose",), tys=(C.UNIT,)),
                           C.Instr(("%n",), "sub", ("%a", "%b"))), C.Ret("%n")),))
    return C.Program(funs={"choose": choose, "main": main}, traits={"Pick": trait}, instances={"Pick": instances})


class SpecializationTests(unittest.TestCase):
    def test_equal_layouts_do_not_merge_different_dispatch(self):
        program = pick_program()
        backend = Backend(program, check_program(program))
        backend.emit_program()
        choose = program.funs["choose"]
        keys = [args for args, _symbol in backend.symbols[id(choose)]]
        self.assertCountEqual(keys, [(C.Brand(C.Skolem("_")),), (C.UNIT,)])
        methods = {id(i.methods[0]) for i in program.instances["Pick"]}
        self.assertEqual(set(backend.symbols) & methods, methods)

    def test_logical_keys_preserve_contracts_and_share_alpha_renamings(self):
        fun = constant("f", 0)
        fun.params = (C.Param("T"),)
        program = C.Program(funs={"f": fun})
        backend = Backend(program, check_program(program))
        first = backend.request(fun, {"T": C.Exists(("a",), C.Brand(C.Skolem("a")))})
        renamed = backend.request(fun, {"T": C.Exists(("b",), C.Brand(C.Skolem("b")))})
        self.assertEqual(first, renamed)
        self.assertNotEqual(first, backend.request(fun, {"T": C.UNIT}))
        safe = backend.request(fun, {"T": C.Fn((), C.U32)})
        unsafe = backend.request(fun, {"T": C.Fn((), C.U32, unsafe=True)})
        self.assertNotEqual(safe, unsafe)

    def test_brand_arguments_share_one_specialization(self):
        fun = constant("f", 0)
        fun.params = (C.Param("a", C.BRAND), C.Param("b", C.BRAND))
        program = C.Program(funs={"f": fun})
        backend = Backend(program, check_program(program))
        first = backend.request(fun, {"a": C.Skolem("static"), "b": C.Skolem("x")})
        self.assertEqual(first, backend.request(fun, {"a": C.Skolem("x"), "b": C.Skolem("static")}))
        self.assertEqual(first, backend.request(fun, {"a": C.Skolem("y"), "b": C.Skolem("y")}))
        self.assertEqual(len(backend.queue), 1)

    def test_a_generic_traversal_is_never_specialized_at_a_foreach_source(self):
        # A type parameter never stands for a foreach type (TY-FLAT), so specialization never makes a
        # traversal whose source is one.
        f, s = C.Named("F"), C.Named("S")
        mapped = C.Foreach("F", False, s, f)
        mapper = C.Fun("mapper", (C.Param("F"), C.Param("S")), (), f, (f,),
                       (C.Block("entry", (("%x", f),), (), C.Ret("%x")),))
        walk = C.Fun("walk", (C.Param("S"),), (C.Premise("Exposed", (s,)),), mapped, (s,),
                     (C.Block("entry", (("%x", s),),
                              (C.Instr(("%y",), "foreach", ("%x", "mapper"), ty=mapped, tys=(s,)),),
                              C.Ret("%y")),))
        source = C.Foreach("G", False, C.U32, C.Named("G"))
        caller = C.Fun("caller", (), (), source, (source,),
                       (C.Block("entry", (("%x", source),),
                                (C.Instr(("%y",), "call", ("walk", "%x"), tys=(source,)),),
                                C.Ret("%x")),))
        program = C.Program(funs={"mapper": mapper, "walk": walk, "caller": caller})
        with self.assertRaisesRegex(KursError, "a type argument expands to a foreach type"):
            check_program(program)


class FunctionAddressTests(unittest.TestCase):
    def address_program(self):
        program = pick_program()
        program.funs.clear()
        a = C.Brand(C.Skolem("a"))
        chosen = C.Fun("chosen", (C.Param("a", C.BRAND),), (C.Premise("Pick", (a,)),), C.U32, (),
                       (C.Block("entry", (),
                                (C.Instr(("%n",), "trait_call", ("Pick", "code"), tys=(a,)),), C.Ret("%n")),))
        program.instances["Pick"] = [C.Instance("Pick", (C.Param("b", C.BRAND),), (),
                                                  (C.Brand(C.Skolem("b")),), (constant("code", 1),))]
        main = C.Fun("main", (), (), C.U32, (),
                     (C.Block("entry", (),
                              (C.Instr(("%f",), "fn", ("chosen",), tys=(C.Skolem("static"),)),
                               C.Instr(("%n",), "call", ("%f",), tys=())), C.Ret("%n")),))
        program.funs = {"chosen": chosen, "main": main}
        return program

    def test_a_function_value_quantifies_no_brand(self):
        # Taking the address binds every brand, so the premise is discharged where the value is made.
        program = self.address_program()
        self.assertIn("chosen", emit_c(program, check_program(program)))
        main = program.funs["main"]
        # `fn` takes no `_`: a brand left unnamed is no type.
        main.blocks[0].instrs[0].tys = (C.Named("_"),)
        with self.assertRaisesRegex(KursError, "unknown type _"):
            check_program(program)
        # A call through a function value takes no brand arguments.
        main.blocks[0].instrs[0].tys = (C.Skolem("static"),)
        main.blocks[0].instrs[1].tys = (C.Skolem("static"),)
        with self.assertRaisesRegex(KursError, "wrong number of type arguments"):
            check_program(program)

    def test_a_foreign_unit_function_is_its_own_address(self):
        # A unit result is `void` in C, for Kursal and foreign functions alike: no adapter.
        main = C.Fun("main", (), (), C.UNIT, (),
                     (C.Block("entry", (),
                              (C.Instr(("%f",), "fn", ("foreign",)),
                               C.Instr(("%u",), "call", ("%f",), unsafe=True)), C.Ret("%u")),))
        program = C.Program(funs={"main": main}, externs={"foreign": C.Extern("foreign", C.UNIT, ())})
        output = Backend(program, check_program(program)).emit_program()
        self.assertIn("void foreign(void);", output)
        self.assertIn("entry_f = foreign;", output)
        self.assertIn("entry_f();", output)
        self.assertIn("return;", output)
