"""Shape authority is checked at use sites, not invented by specialization."""

import unittest

from kursal import core as C
from kursal.check import check_program
from kursal.cgen import Backend
from kursal.sexpr import KursError
from kursal.types import Types
from tests.test_infer import run


OWNER = C.ModuleId(0, ("owner",))
CLIENT = C.ModuleId(0, ("client",))
SECRET = C.Named("Secret")
T = C.Named("T")
F = C.Named("F")


def exposed(ty):
    return C.Premise("Exposed", (ty,))


def declarations():
    secret = C.TypeDecl("Secret", True, (), (), C.Struct((("n", C.U32),)), OWNER, ("owner", OWNER))
    return C.Program(types={"Secret": secret})


def identity():
    return C.Fun("identity", (C.Param("F"),), (), F, (F,),
                 (C.Block("entry", (("%x", F),), (), C.Ret("%x")),), module=CLIENT)


def traversal(source, module, params=(), premises=()):
    mapped = C.Foreach("F", False, source, F)
    return C.Fun("walk", params, premises, mapped, (source,),
                 (C.Block("entry", (("%x", source),),
                          (C.Instr(("%out",), "foreach", ("%x", "identity"), ty=mapped),),
                          C.Ret("%out")),), module=module)


class ExposureTests(unittest.TestCase):
    def test_exact_assumptions_do_not_spread_to_pointers_or_transformed_types(self):
        types = Types(declarations())
        kinds = {"T": C.ANY}
        self.assertTrue(types.exposure_available(T, CLIENT, kinds, (exposed(T),)))
        self.assertFalse(types.exposure_available(C.Ptr(T), CLIENT, kinds, (exposed(T),)))
        mapped = C.Foreach("F", True, C.Ptr(T), F)
        self.assertFalse(types.exposure_available(mapped, CLIENT, kinds, (exposed(C.Ptr(T)),)))
        self.assertTrue(types.exposure_available(mapped, CLIENT, kinds, (exposed(mapped),)))

    def test_concrete_authority_respects_owner_and_scoped_exposure(self):
        program = declarations()
        types = Types(program)
        self.assertTrue(types.exposure_available(SECRET, OWNER, {}, ()))
        self.assertFalse(types.exposure_available(SECRET, CLIENT, {}, ()))
        self.assertFalse(types.exposure_available(SECRET, None, {}, ()))
        program.types["Secret"].exposure = ("within", OWNER)
        self.assertTrue(types.exposure_available(SECRET, OWNER.child("nested"), {}, ()))
        self.assertFalse(types.exposure_available(SECRET, CLIENT, {}, ()))
        # Exposing a product does not require exposing the contents of each field.
        self.assertTrue(types.exposure_available(C.Tuple((SECRET, T)), CLIENT, {"T": C.ANY}, ()))

    def test_pointer_sum_and_hidden_outer_pointer_are_not_traversable(self):
        program = declarations()
        program.types["Alias"] = C.TypeDecl("Alias", False, (), (), C.Ptr(C.U32))
        types = Types(program)
        self.assertFalse(types.exposure_available(C.Named("Alias"), CLIENT, {}, ()))
        self.assertFalse(types.exposure_available(C.Ptr(C.Enum((("A", C.U32),))), CLIENT, {}, ()))
        self.assertTrue(types.exposure_available(C.Ptr(C.Array(C.U32, 0)), CLIENT, {}, ()))

    def test_direct_foreach_requires_source_authority(self):
        program = declarations()
        program.funs = {"identity": identity(), "walk": traversal(SECRET, CLIENT)}
        with self.assertRaisesRegex(KursError, "foreach_source_not_exposed"):
            check_program(program)
        program.funs["walk"].module = OWNER
        check_program(program)

    def test_generic_foreach_requires_exact_premise(self):
        program = declarations()
        program.funs = {"identity": identity(), "walk": traversal(T, CLIENT, (C.Param("T"),))}
        with self.assertRaisesRegex(KursError, "foreach_source_not_exposed"):
            check_program(program)
        program.funs["walk"].premises = (exposed(T),)
        check_program(program)

    def test_mapper_cannot_smuggle_component_exposure(self):
        program = declarations()
        mapper = identity()
        mapper.premises = (exposed(F),)
        program.funs = {"identity": mapper, "walk": traversal(C.Tuple((C.U32,)), CLIENT)}
        with self.assertRaisesRegex(KursError, "unsatisfied_premise"):
            check_program(program)

    def test_call_cannot_forge_exposure_and_backend_does_not_reauthorize(self):
        program = declarations()
        method = C.Method("pass", T, (T,))
        program.traits["Pass"] = C.Trait("Pass", ("T",), (method,))
        impl = C.Fun("pass", (), (), T, (T,),
                     (C.Block("entry", (("%x", T),), (), C.Ret("%x")),), module=CLIENT)
        program.instances["Pass"] = [C.Instance("Pass", (C.Param("T"),), (exposed(T),),
                                                (T,), (impl,), CLIENT)]
        caller = C.Fun("caller", (), (), SECRET, (SECRET,),
                       (C.Block("entry", (("%x", SECRET),),
                                (C.Instr(("%out",), "trait_call", ("Pass", "pass", "%x"), tys=(SECRET,)),),
                                C.Ret("%out")),), module=CLIENT)
        program.funs["caller"] = caller
        with self.assertRaisesRegex(KursError, "evidence"):
            check_program(program)
        caller.module = OWNER
        checker = check_program(program)
        # The last checked body is in CLIENT, but the caller already authorized use.
        before = checker.module, checker.premises, checker.kinds
        selected, sub = Backend(program, checker).resolve_trait("Pass", "pass", (SECRET,))
        self.assertIs(selected, impl)
        self.assertEqual(sub, {"T": SECRET})
        self.assertEqual((checker.module, checker.premises, checker.kinds), before)

    def test_inference_requires_and_forwards_exact_assumptions(self):
        requirement = "forall T where Exposed[T]. fn need(x: T) -> T { x }\n"
        with self.assertRaisesRegex(KursError, "Exposed requires"):
            run(requirement + "forall T. fn bad(x: T) -> T { need[T](x) }")
        run(requirement + "forall T where Exposed[T]. fn forward(x: T) -> T { need[T](x) }")
        run(requirement + "fn concrete(x: u32) -> u32 { need[u32](x) }")
