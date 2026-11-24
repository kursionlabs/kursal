"""Abstract values need ownership diagnostics, not nominal unfolding."""
import unittest

from kursal import core as C
from kursal.check import CheckError, check_program
from kursal.placeflow import OwnershipError
from tests import test_closure_boundary


class GenericOwnershipTests(unittest.TestCase):
    run_source = test_closure_boundary.ClosureBoundaryTests.run_source

    def reject(self, source, message):
        with self.assertRaisesRegex(OwnershipError, message):
            self.run_source(source + ' fn main() -> i32 { 7i32 }', 7)

    def test_unused_generic_parameter(self):
        self.reject('forall T. fn bad(x: T) -> () { () }', 'not consumed')

    def test_unused_generic_local(self):
        self.reject('forall T. fn bad(x: T) -> () { let y = x; () }', 'not consumed')

    def test_overwritten_generic_value(self):
        self.reject('forall T. fn bad(x: T, y: T) -> T { var value = x; value = y; value }', 'overwrite')

    def test_generic_value_live_on_only_one_branch(self):
        self.reject('forall T. fn bad(x: T, y: T, flag: bool) -> T { if flag { x } else { y } }', 'linear value')

    def test_partially_consumed_product_of_abstract_values(self):
        self.reject('forall T. fn bad(pair: (T, T)) -> T { let (a, b) = pair; a }', 'not consumed')

    def test_unused_generic_value_inside_lifted_closure(self):
        self.reject('forall T. fn bad(x: T) -> () { let f = || { let y = x; () }; f() }', 'not consumed')

    def test_generic_identity_and_copy_discard_remain_valid(self):
        self.run_source('''forall T. fn identity(x: T) -> T { x }
            forall T where Copy[T]. fn discard(x: T) -> () { () }
            fn main() -> i32 { discard(9u32); identity(7i32) }''', 7)

    def test_lent_abstract_value_may_remain_full(self):
        self.run_source('''forall T. fn observe(&x: T) -> () { () }
            fn main() -> i32 { var x = 7i32; observe(&x); x }''', 7)

    def test_core_reports_unconsumed_generic_local(self):
        ty = C.Named('T')
        fun = C.Fun('bad', (C.Param('T'),), (), C.UNIT, (ty,),
                    (C.Block('entry', (('%x', ty),), (
                        C.Instr(('%stored',), 'store', ('&x', '%x')),
                        C.Instr(('%unit',), 'construct', (), ty=C.UNIT),
                    ), C.Ret('%unit')),), locals=(('&x', ty),))
        with self.assertRaises(CheckError):
            check_program(C.Program(funs={'bad': fun}))
