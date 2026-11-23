"""Nominal premises are formation requirements, not constructor-only checks."""
import unittest

from kursal.check import check_program
from kursal.kurs import parse_program
from kursal.sexpr import KursError


HEADER = '(trait P (T)) (type Box (forall (T) ((P T)) (tuple T))) '


class NominalPremiseTests(unittest.TestCase):
    def check(self, source):
        return check_program(parse_program(HEADER + source))

    def test_unused_nested_type_applications_require_premises(self):
        for ty in ('(Box u32)', '(ptr (Box u32))',
                   '(array (Box u32) 0)', '(tuple (Box u32))',
                   '(fn () (Box u32))'):
            with self.subTest(ty=ty):
                with self.assertRaisesRegex(KursError, 'unsatisfied nominal premise'):
                    self.check('(type Bad ' + ty + ')')
                self.check('(instance P (u32)) (type Good ' + ty + ')')

    def test_function_assumption_forms_signature_locals_and_instructions(self):
        self.check('''(fun id (forall (T) ((P T)) (Box T) ((Box T))
          (locals (&tmp (Box T)))
          (entry ((%x (Box T))) (= %u (store &tmp %x))
            (= %r (load &tmp)) (ret %r))))''')
        with self.assertRaisesRegex(KursError, 'unsatisfied nominal premise'):
            self.check('''(fun id (forall (T) () (Box T) ((Box T))
                (entry ((%x (Box T))) (ret %x))))''')

    def test_extern_and_unreachable_block_types_require_premises(self):
        for suffix in ('(extern fun f (Box u32) ())',
            '''(fun f u32 () (entry () (= %r (const u32 0)) (ret %r))
               (dead ((%x (Box u32))) (= %r (const u32 0)) (ret %r)))'''):
            with self.subTest(suffix=suffix):
                with self.assertRaisesRegex(KursError, 'unsatisfied nominal premise'):
                    self.check(suffix)

    def test_instance_method_inherits_premises(self):
        self.check('''(trait Q (T) (run u32 ()))
            (instance Q (forall (T) ((P T)) ((Box T)))
              (fun run u32 () (entry () (= %r (const u32 0)) (ret %r))))''')

    def test_recursive_nominal_constraints_fail_without_unbounded_search(self):
        for payload in ('(Loop T)', '(Loop (tuple T T))'):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(KursError, 'non-descending nominal premise'):
                    check_program(parse_program('(trait P (T)) '
                        '(type Loop (forall (T) ((P ' + payload + ')) (tuple)))'))

    def test_nested_finite_constraints_and_pointer_recursion(self):
        self.check('''(instance P (u32))
            (instance P (forall (T) ((P T)) ((Box T))))
            (type Nested (Box (Box u32)))
            (type Node (forall (T) ((P T))
                (struct (value T) (next (ptr (Node T))))))''')


    def test_surface_signature_and_nested_type_fail_before_core_assertion(self):
        from tests import test_closure_boundary
        from kursal.lower import LowerError
        for declaration in ('fn unused(x: Box[u32]) -> Box[u32] { x }',
                            'struct Outer { ptr: *Box[u32] }'):
            with self.subTest(declaration=declaration):
                with self.assertRaisesRegex(LowerError, 'unsatisfied nominal premise'):
                    test_closure_boundary.ClosureBoundaryTests.run_source(self,
                        'forall T. trait P {} '
                        'forall T where P[T]. exposed struct Box { value: T } '
                        + declaration + ' fn main() -> i32 { 7i32 }', 7)

    def test_surface_instance_and_generic_assumption(self):
        from tests import test_closure_boundary
        test_closure_boundary.ClosureBoundaryTests.run_source(self, '''
            forall T. trait P {}
            instance P[u32] {}
            forall T where P[T]. exposed struct Box { value: T }
            forall T where P[T]. fn id(x: Box[T]) -> Box[T] { x }
            fn main() -> i32 { let x = id(Box.{ value: 7u32 }); x.value as i32 }
        ''', 7)

    def test_nominal_descent_requires_subterms_not_just_smaller_size(self):
        from kursal import core as C
        from kursal.formation import nominal_descent
        old = (C.Tuple((C.Named('X'), C.U32)),)
        self.assertTrue(nominal_descent((C.Named('X'),), old))
        self.assertFalse(nominal_descent((C.Named('Y'),), old))
        self.assertFalse(nominal_descent((C.Tuple((C.Named('Y'),)),), old))
        # A second argument cannot grow while the first descends.
        self.assertFalse(nominal_descent((C.Named('X'), C.Tuple((C.U32, C.U32))),
                                         (old[0], C.U32)))

    def test_nominal_descent_stops_at_binders(self):
        from kursal import core as C
        from kursal.formation import nominal_descent
        item = C.Named('Item')
        exists = C.Exists(('b',), C.Tuple((item, C.Brand(C.Skolem('b')))))
        self.assertFalse(nominal_descent((item,), (exists,)))

        plain = C.Fn((item,), C.UNIT)
        self.assertTrue(nominal_descent((item,), (plain,)))

        foreach = C.Foreach('T', False, C.Tuple((item,)), C.Tuple((C.Named('T'), item)))
        self.assertTrue(nominal_descent((item,), (foreach,)))  # source
        only_body = C.Foreach('T', False, C.U32, C.Tuple((C.Named('T'), item)))
        self.assertFalse(nominal_descent((item,), (only_body,)))

    def test_instance_parameter_must_be_extractable_without_crossing_a_binder(self):
        from kursal import core as C
        from kursal.coherence import check_instance_range
        from kursal.unify import match_head
        from kursal.types import subst
        x = C.Named('X')
        param = (C.Param('X'),)
        hidden = (
            C.Exists(('b',), C.Tuple((x, C.Brand(C.Skolem('b'))))),
            C.Foreach('T', False, C.U32, C.Tuple((C.Named('T'), x))),
        )
        for head in hidden:
            with self.subTest(head=head):
                with self.assertRaisesRegex(KursError, 'needs a binder-free head occurrence'):
                    check_instance_range(C.Premise('P', (head,)), param, None)
                self.assertIsNone(match_head(param, (head,), (subst(head, {'X': C.U32}),),
                                              require_extractable=True))
                check_instance_range(C.Premise('P', (head, x)), param, None)
                self.assertEqual(match_head(param, (head, x),
                                             (subst(head, {'X': C.U32}), C.U32),
                                             require_extractable=True), {'X': C.U32})

        unbound = C.Fn((x,), C.UNIT)
        check_instance_range(C.Premise('P', (unbound,)), param, None)
        self.assertEqual(match_head(param, (unbound,), (C.Fn((C.U32,), C.UNIT),)), {'X': C.U32})
        source = C.Foreach('T', False, x, C.Named('T'))
        check_instance_range(C.Premise('P', (source,)), param, None)
