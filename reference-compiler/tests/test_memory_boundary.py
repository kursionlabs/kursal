"""Checked machine operations must have faithful, explicitly supported C recipes."""

import unittest

from kursal import core as C
from kursal.check import check_program
from kursal.cgen import emit_c
from kursal.sexpr import KursError


def operation(op, params, result, args, *, ty=None, order=None):
    instr = C.Instr(("%out",), op, args, ty=ty, lit=order, unsafe=True)
    fun = C.Fun("probe", (), (), result, tuple(t for _, t in params),
                (C.Block("entry", params, (instr,), C.Ret("%out")),))
    return C.Program(funs={"probe": fun})


def generate(program):
    return emit_c(program, check_program(program))


class VolatileTests(unittest.TestCase):
    def test_pointer_cell_qualifies_the_pointer_object(self):
        item = C.Ptr(C.U32)
        load = operation("ptr_load_volatile", (("%p", C.Ptr(item)),), item, ("%p",))
        self.assertIn("*(uint32_t* volatile*)entry_p", generate(load))
        store = operation("ptr_store_volatile", (("%p", C.Ptr(item)), ("%v", item)),
                          C.UNIT, ("%p", "%v"))
        self.assertIn("*(uint32_t* volatile*)entry_p = entry_v;", generate(store))

    def test_scalar_volatile_access_stays_qualified(self):
        load = operation("ptr_load_volatile", (("%p", C.Ptr(C.U32)),), C.U32, ("%p",))
        self.assertIn("*(uint32_t volatile*)entry_p", generate(load))


class CastTests(unittest.TestCase):
    def cast(self, source, target):
        return operation("cast", (("%x", source),), target, ("%x",), ty=target)

    def test_cross_integer_float_cast_requires_equal_widths(self):
        for source, target in ((C.U32, C.Prim("f64")), (C.Prim("f32"), C.Prim("u64"))):
            with self.subTest(source=source, target=target):
                program = self.cast(source, target)
                checker = check_program(program)
                with self.assertRaisesRegex(KursError, "reinterpretation requires equal widths"):
                    emit_c(program, checker)
        output = generate(self.cast(C.U32, C.Prim("f32")))
        self.assertIn('_Static_assert(sizeof(entry_out) == sizeof(entry_x)', output)
        self.assertIn('__builtin_memcpy(&entry_out, &entry_x, sizeof(entry_out));', output)

    def test_aggregate_copy_has_size_assertion_and_no_partial_copy(self):
        output = generate(self.cast(C.Tuple((C.U32,)), C.Tuple((C.U32, C.U32))))
        self.assertIn('"KURS cast requires equal representation sizes"', output)
        self.assertNotIn('sizeof(entry_out) < sizeof(entry_x)', output)
        self.assertLess(output.index('_Static_assert(sizeof(entry_out)'), output.index('__builtin_memcpy(&entry_out'))

    def test_bool_and_float_numeric_conversion_does_not_copy_representation(self):
        output = generate(self.cast(C.BOOL, C.Prim("f32")))
        self.assertIn('entry_out = (float)entry_x;', output)
        self.assertNotIn('__builtin_memcpy', output)

    def test_pointer_float_conversion_has_no_invented_integer_intermediate(self):
        with self.assertRaisesRegex(KursError, "raw pointers only with pointers or integers"):
            generate(self.cast(C.Ptr(C.U32), C.Prim("f64")))
        output = generate(self.cast(C.Ptr(C.U32), C.Ptr(C.Prim("u8"))))
        self.assertIn('entry_out = (uint8_t*)entry_x;', output)


class AtomicTests(unittest.TestCase):
    def atomic(self, kind, item=C.U32, values=0, order="relaxed"):
        params = (("%p", C.Ptr(item)),) + tuple((f"%v{k}", item) for k in range(values))
        result = C.UNIT if kind in {"store", "fence"} else C.Tuple((item, C.BOOL)) if kind == "compare_exchange" else item
        return operation("fence" if kind == "fence" else "ptr_atomic_" + kind, params, result,
                         tuple(n for n, _ in params), order=order)

    def test_unknown_operations_and_wrong_arities_fail_core_assertion(self):
        with self.assertRaisesRegex(KursError, "unknown_instruction"):
            check_program(self.atomic("typo"))
        for kind, values in (("store", 0), ("load", 1), ("swap", 0), ("compare_exchange", 1),
                             ("fetch_add", 0), ("fence", 0)):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(KursError, "atomic_operand_count"):
                    check_program(self.atomic(kind, values=values, order="seq_cst"))

    def test_atomic_pointees_are_integers_bools_or_pointers(self):
        for item in (C.Prim("f32"), C.Tuple((C.U32, C.U32))):
            with self.subTest(item=item), self.assertRaisesRegex(KursError, "atomic_pointee_type"):
                check_program(self.atomic("load", item))
        for item in (C.BOOL, C.Ptr(C.U32)):
            with self.subTest(fetch_item=item), self.assertRaisesRegex(KursError, "atomic_pointee_type"):
                check_program(self.atomic("fetch_and", item, values=1))

    def test_transfer_and_cas_preserve_orders_and_result(self):
        self.assertIn('__atomic_load_n(entry_p, __ATOMIC_ACQUIRE)',
                      generate(self.atomic("load", C.BOOL, order="acquire")))
        self.assertIn('__atomic_store_n(entry_p, entry_v0, __ATOMIC_RELEASE)',
                      generate(self.atomic("store", C.Ptr(C.U32), values=1, order="release")))
        output = generate(self.atomic("compare_exchange", values=2, order="acq_rel/acquire"))
        self.assertIn('entry_out.e0 = entry_v0;', output)
        self.assertIn('&entry_out.e0, entry_v1, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)', output)
        with self.assertRaisesRegex(KursError, "bad_compare_exchange_ordering"):
            check_program(self.atomic("compare_exchange", values=2, order="release/acquire"))

    def test_fence_has_no_operands(self):
        program = operation("fence", (), C.UNIT, (), order="seq_cst")
        self.assertIn('__atomic_thread_fence(__ATOMIC_SEQ_CST)', generate(program))
