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

    def test_a_number_is_not_reinterpreted_as_a_number(self):
        for source, target in ((C.U32, C.Prim("f32")), (C.Prim("f64"), C.Prim("u64")), (C.U32, C.Prim("i32")),
                               (C.Prim("f32"), C.Prim("f64"))):
            with self.subTest(source=source, target=target), \
                    self.assertRaisesRegex(KursError, "unsafe_cast_between_numbers"):
                check_program(self.cast(source, target))
        # A float's bits are reinterpreted through a struct that holds it alone.
        output = generate(self.cast(C.Tuple((C.Prim("f32"),)), C.U32))
        self.assertIn('_Static_assert(sizeof(entry_out) == sizeof(entry_x)', output)
        self.assertIn('__builtin_memcpy(&entry_out, &entry_x, sizeof(entry_out));', output)

    def float_cast(self, source, target):
        return operation("float_cast", (("%x", source),), target, ("%x",), ty=target)

    def test_float_cast_converts_by_value_and_saturates(self):
        self.assertIn("entry_out = (double)entry_x;", generate(self.float_cast(C.U32, C.Prim("f64"))))
        self.assertIn("entry_out = (float)entry_x;", generate(self.float_cast(C.Prim("f64"), C.Prim("f32"))))
        self.assertIn("entry_out = entry_x >= -0x1p63 && entry_x < 0x1p63 ? (int64_t)entry_x : "
                      "entry_x > 0 ? INT64_MAX : entry_x < 0 ? INT64_MIN : 0;",
                      generate(self.float_cast(C.Prim("f64"), C.Prim("i64"))))
        self.assertIn("entry_out = entry_x > -1 && entry_x < 0x1p8f ? (uint8_t)entry_x : entry_x > 0 ? UINT8_MAX : 0;",
                      generate(self.float_cast(C.Prim("f32"), C.Prim("u8"))))
        self.assertIn("entry_out = entry_x > -1 && entry_x < -2 * (double)INTPTR_MIN ? (uintptr_t)entry_x : "
                      "entry_x > 0 ? UINTPTR_MAX : 0;", generate(self.float_cast(C.Prim("f64"), C.USIZE)))
        for source, target in ((C.U32, C.Prim("u64")), (C.BOOL, C.Prim("f64")), (C.Prim("f64"), C.BOOL)):
            with self.subTest(source=source, target=target), \
                    self.assertRaisesRegex(KursError, "float_cast_converts_between"):
                check_program(self.float_cast(source, target))

    def test_aggregate_copy_has_size_assertion_and_no_partial_copy(self):
        output = generate(self.cast(C.Tuple((C.U32,)), C.Tuple((C.U32, C.U32))))
        self.assertIn('"KURS cast requires equal representation sizes"', output)
        self.assertNotIn('sizeof(entry_out) < sizeof(entry_x)', output)
        self.assertLess(output.index('_Static_assert(sizeof(entry_out)'), output.index('__builtin_memcpy(&entry_out'))

    def test_bool_and_float_numeric_conversion_does_not_copy_representation(self):
        output = generate(self.cast(C.BOOL, C.Prim("f32")))
        self.assertIn('entry_out = (float)entry_x;', output)
        self.assertNotIn('__builtin_memcpy', output)

    def test_a_pointer_is_cast_only_to_or_from_a_pointer_or_an_integer(self):
        # C converts a pointer only to and from those; the checker rejects what the backend cannot write.
        pointer = C.Ptr(C.U32)
        for source, target in ((pointer, C.Prim("f64")), (C.Prim("f32"), pointer), (pointer, C.BOOL),
                               (C.BOOL, pointer), (pointer, C.Tuple((pointer,))), (C.Tuple((pointer,)), pointer),
                               (pointer, C.Array(C.U32, 2))):
            with self.subTest(source=source, target=target), \
                    self.assertRaisesRegex(KursError, "pointer_cast_pointer_or_integer"):
                check_program(self.cast(source, target))
        output = generate(self.cast(pointer, C.Ptr(C.Prim("u8"))))
        self.assertIn('entry_out = (uint8_t*)entry_x;', output)
        self.assertIn('entry_out = (uint64_t)((uint64_t)((uintptr_t)entry_x));', generate(self.cast(pointer, C.Prim("u64"))))


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
