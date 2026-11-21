"""The place translation (`kursal.devalue`) as an oracle: every place program the checker accepts translates
to a value-only program it accepts too. The programs are the focused ones here, the place corpus, every core
program the other test modules check and accept, and every package that builds: the library, each example
and the compiler itself."""

import os
import sys
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Optional

from kursal import core as C
from kursal.check import check_program
from kursal.devalue import devalue
from kursal.kurs import parse_program
from kursal.lower import lower
from kursal.resolve import resolve
from kursal.sexpr import KursError
from kursal.sigs import signatures
from tests.place_cases import CASES, TYPES
from tests.test_compiler import COMPILER, core_cases, lowered_programs

PACK = "(exists ($h) (tuple u32 (brand $h)))"
OUTER = f"(exists ($o) (tuple u32 {PACK} (brand $o)))"

FOCUSED = {
    "load_store": """(fun f u32 (Box!) (locals (&q u32)) (entry ((&b Box!))
   (= %h (load &b.h)) (= %n (load &b.n)) (= %u (store &q %n)) (= %m (load &q)) (= %k (load &q))
   (= %u2 (store &b.h %h)) (ret %m)))""",
    "nested_lends_split_at_a_product": """(fun both u32 (H! u32) (entry ((&h H!) (&n u32)) (= %r (load &n)) (ret %r)))
 (fun f u32 (Holder! Box!) (locals (&b Box!)) (entry ((&x Holder!) (%box Box!))
   (= %u (store &b %box)) (= %r (call both &b.h &b.n)) (= %s (call touch &b.h))
   (= %whole (load &b)) (= (%h %n) (destruct %whole Box!)) (= %t (call eat %h)) (ret %r)))""",
    "place_switch_then_payload_load": """(fun f u32 (Holder!) (entry ((&x Holder!)) (switch &x.e E! (A a) (B b)))
   (a () (known &x.e A) (= %h (load &x.e.A)) (= %r (call eat %h)) (= %r2 (copy %r))
     (= %nb (construct_variant E! B %r2)) (= %u (store &x.e %nb)) (ret %r))
   (b () (known &x.e B) (= %v (load &x.e.B)) (= %t (variant_tag &x.e E!)) (jmp out %v))
   (out ((%v u32)) (known &x.e B) (= %w (load &x.e.B)) (ret %v)))""",
    # A switch on a place whose variant is known goes to that arm: what is known below it stays known.
    "switch_on_a_known_place": """(type Nest! (enum (L E!) (R u32)))
 (fun f u32 (Nest!) (entry ((&x Nest!)) (switch &x Nest! (L a) (R b)))
   (a () (known &x L) (switch &x.L E! (A c) (B d)))
   (c () (known &x L) (known &x.L A) (switch &x Nest! (L c2) (R b)))
   (c2 () (known &x L) (known &x.L A) (= %r (call touch &x.L.A)) (ret %r))
   (d () (known &x L) (known &x.L B) (= %r (load &x.L.B)) (ret %r))
   (b () (= %r (const u32 0)) (ret %r)))""",
    "open_and_pack_across_blocks": f"""(fun f u32 (Pk! bool) (entry ((&k Pk!) (%c bool))
   (= %u (open &k.pkg ($o))) (jnz %c ((l $o)) ((r $o))))
   (l ($l) () (opened &k.pkg ($l)) (= %n (load &k.pkg.0)) (jmp (j $l) %n))
   (r ($r) () (opened &k.pkg ($r)) (= %n (call (peek_t $r) &k.pkg)) (jmp (j $r) %n))
   (j ($j) ((%n u32)) (opened &k.pkg ($j)) (= %u (pack {PACK} $j &k.pkg)) (ret %n)))""",
    # `pack` closes the views open inside the package too.
    "pack_over_a_nested_view": f"""(type Pk2 (struct (pkg {OUTER})))
 (fun f u32 (Pk2) (entry ((&k Pk2)) (= %u (open &k.pkg ($a))) (= %u1 (open &k.pkg.1 ($b))) (jmp (n $a $b)))
   (n ($x $y) () (opened &k.pkg ($x)) (opened &k.pkg.1 ($y))
     (= %n (load &k.pkg.1.0)) (= %u2 (pack {OUTER} $x &k.pkg)) (ret %n)))""",
    "store_into_an_open_view": f"""(type Pk2 (struct (pkg {OUTER})))
 (fun f u32 (Pk2) (entry ((&k Pk2)) (= %u (open &k.pkg ($a))) (= %v (load &k.pkg))
   (= (%n %i %w) (destruct %v (tuple u32 {PACK} (brand $a)))) (= %m (copy %n))
   (= %v2 (construct (tuple u32 {PACK} (brand $a)) %n %i %w)) (= %s (store &k.pkg %v2))
   (= %u2 (pack {OUTER} $a &k.pkg)) (ret %m)))""",
    "raw_target_lend": """(fun f u32 ((ptr Box!)) (entry ((%p (ptr Box!)))
   (= (%h %n) (destruct %p (ptr Box!))) (unsafe (= %r (call touch &*%h))) (ret %r)))""",
    "empty_shell_drop": """(fun f u32 (Box!) (locals (&q Box!)) (entry ((%b Box!))
   (= %u (store &q %b)) (= %h (load &q.h)) (= %r (call eat %h)) (ret %r)))
 (fun g u32 (Box! H!) (entry ((&b Box!) (%h2 H!))
   (= %h (load &b.h)) (= %n (load &b.n)) (= %nb (construct Box! %h2 %n)) (= %u (store &b %nb))
   (= %r (call eat %h)) (ret %r)))""",
    "loop_header_with_empty_paths": """(fun g u32 (u32) (locals (&i u32) (&t H!)) (entry ((%n u32)) (= %u (store &i %n)) (jmp head))
   (head () (empty &t) (= %x (load &i)) (= %z (const u32 0)) (= %c (eq %x %z)) (jnz %c (out) (body)))
   (body () (empty &t) (= %one (const u32 1)) (= %h (construct H! %one)) (= %u (store &t %h))
     (= %h2 (load &t)) (= %k (call eat %h2)) (= %x (load &i)) (= %y (sub %x %k)) (= %u2 (store &i %y)) (jmp head))
   (out () (empty &t) (= %r (load &i)) (ret %r)))
 (fun h u32 (Box!) (entry ((&b Box!)) (jmp head))
   (head () (= %h (load &b.h)) (= %k (call eat %h)) (= %z (const u32 0)) (= %c (eq %k %z)) (jnz %c (back) (done)))
   (back () (empty &b.h) (= %one (const u32 1)) (= %h1 (construct H! %one)) (= %u (store &b.h %h1)) (jmp head))
   (done () (empty &b.h) (= %one (const u32 1)) (= %h1 (construct H! %one)) (= %u (store &b.h %h1))
     (= %r (const u32 0)) (ret %r)))""",
    "function_value_with_place_params": """(type Cb (struct (f (fn ((& H!)) u32))))
 (fun f u32 (Box!) (entry ((&b Box!))
   (= %f (fn touch)) (= %cb (construct Cb %f)) (= %g (destruct %cb Cb)) (= %r (call %g &b.h)) (ret %r)))""",
    "trait_method_with_place_params": """(trait Poke (T) (poke u32 ((& T) u32)))
 (instance Poke (H!) (fun poke u32 (H! u32) (entry ((&h H!) (%k u32))
   (= %n (load &h.n)) (= %r (add %n %k)) (ret %r))))
 (fun f u32 (Box!) (entry ((&b Box!)) (= %k (const u32 1)) (= %r (trait_call Poke poke (H!) &b.h %k)) (ret %r)))""",
    "never_returning_callee_with_a_place_arg": """(fun stop ! (H!) (entry ((&h H!)) (= %n (trap)) (ret %n)))
 (fun f u32 (Box!) (entry ((&b Box!)) (= %n (call stop &b.h)) (unreachable)))
 (fun g u32 (Box!) (entry ((&b Box!)) (= %n (call stop &b.h)) (ret %n)))""",
    "store_of_never": """(fun f u32 () (locals (&q u32)) (entry () (= %n (trap)) (= %u (store &q %n)) (= %r (load &q)) (ret %r)))""",
}


def places_left(program: C.Program) -> list[str]:
    """What of the place model a program still uses."""
    found = []

    def fun(f: C.Fun) -> None:
        if f.locals:
            found.append(f"{f.name}: locals")
        for block in f.blocks:
            if block.empty or block.known or block.opened:
                found.append(f"{f.name}/{block.label}: contract")
            if any(name.startswith("&") for name, _ in block.params):
                found.append(f"{f.name}/{block.label}: place parameter")
            for instr in block.instrs:
                if instr.op in ("load", "store") or any(a.startswith("&") for a in instr.args):
                    found.append(f"{f.name}/{block.label}: {instr.op} on a place")
            if isinstance(block.term, C.Switch) and block.term.scrutinee.startswith("&"):
                found.append(f"{f.name}/{block.label}: switch on a place")

    for f in program.funs.values():
        fun(f)
    for instances in program.instances.values():
        for inst in instances:
            for f in inst.methods:
                fun(f)
    found += [f"trait {t.name}: place method" for t in program.traits.values() for m in t.methods if m.places]
    return found


def translates(test: unittest.TestCase, program: C.Program) -> None:
    value = devalue(program)
    test.assertEqual(places_left(value), [])
    check_program(value)


def oracle(source: str | Path) -> Optional[str]:
    """Why a program's translation is not a value-only program the checker accepts, or None when it is.
    `source` is KURS text, or a package, which is lowered first."""
    sys.setrecursionlimit(20000)
    if isinstance(source, Path):
        resolver = resolve(source)
        program = lower(resolver, signatures(resolver))
    else:
        program = parse_program(source)
    try:
        value = devalue(program)
        left = places_left(value)
        if left:
            return f"places left: {left[:5]}"
        check_program(value)
    except KursError as error:
        return str(error)
    return None


class FocusedTests(unittest.TestCase):
    def test_each_place_feature_translates(self):
        for name, source in FOCUSED.items():
            with self.subTest(name):
                program = parse_program(TYPES + source)
                check_program(program)
                translates(self, program)

    def test_the_place_corpus_translates(self):
        for name, (accepted, _, source) in CASES.items():
            if accepted:
                with self.subTest(name):
                    translates(self, parse_program(TYPES + source))


class CorpusTests(unittest.TestCase):
    """Every core program another test module checks and accepts, every package that builds, and the
    compiler, through the oracle in parallel. Packages arrive lowered and serialized (`lowered_programs`);
    the compiler, which `packages` leaves out, is lowered here."""

    def test_every_accepted_program_translates(self):
        # The compiler, the longest by far, starts first.
        sources: dict[str, str | Path] = {"the compiler": COMPILER}
        sources |= {f"core case {i}": case["kurs"] for i, case in enumerate(core_cases()) if case["check"] is None}
        sources |= {f"package {package['name']}": package["kurs"] for package in lowered_programs()}
        with ProcessPoolExecutor(max_workers=min(4, os.cpu_count() or 1)) as pool:
            for name, failure in zip(sources, pool.map(oracle, sources.values())):
                with self.subTest(name):
                    self.assertIsNone(failure)


if __name__ == "__main__":
    unittest.main()
