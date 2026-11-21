"""The example programs: every package in examples/ builds, every program runs as recorded, and every
program in examples/bad/ is rejected for the reason it demonstrates."""

import subprocess
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Optional

from kursal.cc import FLAGS, RUNTIME, c_compiler, cc_command
from kursal.cgen import emit_c
from kursal.check import check_program
from kursal.lower import lower
from kursal.resolve import resolve
from kursal.sexpr import KursError
from kursal.sigs import signatures

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

SAMPLE = "one two\nthree four five\n"
JSON_IN = '{"name": "Ada", "tags": ["x", 1, -2.5e3, true, null], "nested": {"empty": [], "o": {}}}'
JSON_OUT = ('{\n  "name": "Ada",\n  "tags": [\n    "x",\n    1,\n    -2.5e3,\n    true,\n    null\n  ],\n'
            '  "nested": {\n    "empty": [],\n    "o": {}\n  }\n}\n')
BLOCKALLOC_OUT = (
    'point (30, 40), segment to (6, 8), row 108, huge 864\n'
    'too big: refused, got back extra = 7\n'
    '16-byte class full after 255 more blocks\n'
    'class 16: 256 of 256 in use, peak 256, 1 failed\n'
    'class 32: 1 of 128 in use, peak 1, 0 failed\n'
    'class 64: 1 of 64 in use, peak 1, 0 failed\n'
    'class 128: 1 of 32 in use, peak 1, 0 failed\n'
    'freed the odd values (sum 16129), reused 127 blocks\n'
    'raw block: freed\n'
    'raw block again: refused, double free\n'
    'inside a block: refused, pointer into a block\n'
    "malloc'd memory: refused, foreign pointer\n"
    'freed the rest (sum 151257), last point (30, 40)\n'
    'class 16: 0 of 256 in use, peak 256, 2 failed\n'
    'class 32: 0 of 128 in use, peak 2, 0 failed\n'
    'class 64: 0 of 64 in use, peak 1, 0 failed\n'
    'class 128: 0 of 32 in use, peak 1, 0 failed\n'
)
LISP_IN = (
    "; a list library, closures and recursion\n(define (filter p xs)\n  (if (null? xs) '()\n"
    "      (if (p (car xs)) (cons (car xs) (filter p (cdr xs))) (filter p (cdr xs)))))\n"
    "(define (foldl f acc xs) (if (null? xs) acc (foldl f (f acc (car xs)) (cdr xs))))\n"
    "(define (reverse xs) (foldl (lambda (acc x) (cons x acc)) '() xs))\n"
    "(define (range a b) (if (< a b) (cons a (range (+ a 1) b)) '()))\n"
    "(define (even? n) (if (= n 0) #t (odd? (- n 1))))\n(define (odd? n) (if (= n 0) #f (even? (- n 1))))\n"
    "(filter even? (range 0 10))\n(foldl + 0 (range 1 101))\n(reverse (range 0 5))\n"
    "(define (fact n) (if (< n 2) 1 (* n (fact (- n 1)))))\n(fact 20)\n"
    "(define (make-adder n) (lambda (x) (+ x n)))\n((make-adder 5) 10)\n(let ((a 2) (b 3)) (* a b (- 1)))\n"
    "(cons 1 2)\n'(nested (list) #t #f -7)\n(null? '(1))\n"
)
LISP_OUT = "(0 2 4 6 8)\n5050\n(4 3 2 1 0)\n2432902008176640000\n15\n-6\n(1 . 2)\n(nested (list) #t #f -7)\n#f\n"

HOST_OUT = (
    "arguments: one two (3 with the name)\ngreeting: unset\nfile: written and read back\n"
    "directory: b.txt, 22 bytes, made and removed\n"
    "run: exit 0\nrun: exit 3\nrun: signal 9\nrun: error: No such file or directory (error 2)\n"
    "run: error: Invalid argument (error 22)\nclock: monotonic, slept\n"
    "float 1: 4607182418800017408 f32 1065353216\nfloat -2.5e3: 13881088010067378176 f32 3306962944\n"
    "float 0.1: 4591870180066957722 f32 1036831949\nfloat 1e400: 9218868437227405312 f32 2139095040\n"
    "float 4.9e-324: 1 f32 0\nfloat  1: none\nfloat 1x: none\nfloat : none\n"
)

CONSTS_OUT = ("6 rows, every 20 degrees:\n0 C = 32 F\n20 C = 68 F\n40 C = 104 F\n60 C = 140 F\n80 C = 176 F\n"
              "100 C = 212 F\na u8 holds 0 to 255; an i32 has 32 bits\n")
ATOMICS_OUT = ("counter: 2000 of 2000 increments\nmessage: 42, read after the flag\nclaim: won by 1 of 2 threads\n"
               "key: checksum 1800, 16 of 16 bytes wiped before free\n")
SHOP_OUT = ("a shelf for 10\ndeliver 8: 8 in stock\nsell 3: 5 in stock\ncannot sell 7: only 5 in stock\n"
            "cannot deliver 6: room for 5\n")

# Each program's runs, in order and sharing one directory: (arguments, stdin, stdout, exit status), and the
# stderr expected after them, where it is checked. `{file}` in the arguments is SAMPLE written beside the
# build; `{store}` is a path there, missing at first.
RUNS: dict[str, tuple[tuple, ...]] = {
    "wordfreq": (((), "the cat and the hat\nThe cat sat.\n", "cat 2\nthe 2\nThe 1\nand 1\nhat 1\nsat 1\n", 0),),
    "calc": (((), "1 + 2 * 3\n(1 + 2) * 3\n-4 * -(2 - 5)\n10 / (5 - 5)\n2 +\n",
              "7\n9\n-12\ndivision by zero at column 4\nexpected a number or '(' at column 4\n", 2),),
    "wc": ((("{file}",), "", "2 5 24 {file}\n", 0),),
    "ring": (((), "", "", 211),),
    "host": ((("one", "two"), "", HOST_OUT, 0),),
    "kvstore": (
        (("{store}", "list"), "", "", 0),
        (("{store}", "set", "name", "Ada"), "", "", 0),
        (("{store}", "set", "lang", "Kursal"), "", "", 0),
        (("{store}", "set", "name", "Grace"), "", "", 0),
        (("{store}", "get", "name"), "", "Grace\n", 0),
        (("{store}", "get", "missing"), "", "", 1),
        (("{store}", "list"), "", "lang\tKursal\nname\tGrace\n", 0),
        (("{store}", "del", "lang"), "", "", 0),
        (("{store}", "del", "lang"), "", "", 1),
        (("{store}", "list"), "", "name\tGrace\n", 0),
        (("{store}", "frobnicate"), "", "", 2),
    ),
    "json_check": (
        ((), JSON_IN, JSON_OUT, 0),
        ((), "[1, 2", "", 1),
        ((), '{"a": [1, {"b": tru}]}', "", 1),
        ((), "[01]", "", 1),
        ((), "[]", "[]\n", 0),
    ),
    "routes": (
        ((), "edge A B 7\nedge A C 2\nedge C B 3\nedge B D 1\nedge C D 9\n# comment\n\n"
             "route A D\nroute D A\nroute A A\nroute A Z\nedge D E 4\nroute C E\n",
         "A -> C -> B -> D (6)\nD -> A unreachable\nA (0)\nA -> Z unreachable\nC -> B -> D -> E (8)\n", 0),
        ((), "edge A B 7\nedge A B x\nroute A B\n", "", 1),
        ((), "route A\n", "", 1),
        ((), "walk A B\n", "", 1),
        ((), "", "", 0),
    ),
    "blockalloc": (((), "", BLOCKALLOC_OUT, 0),),
    "config": (
        ((), 'name = "demo"\n[server]\nhost = "example.org"\nport = 8080\ndebug = true\n',
         "name: demo\nserver: example.org:8080\ndebug: on\n", 0, ""),
        ((), "", "name: unnamed\nserver: localhost:80\ndebug: off\n", 0, ""),
        ((), 'name = "x"\n[server]\nport = 80 80\n', "", 1,
         "config: line 3, column 11: expected a newline or the end after a statement\n"),
        ((), '[server]\nport = "high"\n', "", 1, "config: server.port must be an integer (found string)\n"),
    ),
    "shop": (((), "", SHOP_OUT, 0),),
    "consts": (((), "", CONSTS_OUT, 0),),
    "atomics": (((), "", ATOMICS_OUT, 0),),
    "iter_for_loops": (((), "", "0 2 4 6 8 \n1\t2\t3\t\n2\t4\t6\t\n3\t6\t9\t\n"
                                "first square over 50: 64\nweighted total: 320\n", 0),),
    "iter_custom": (((), "", "0 1 1 2 3 5 8 13 21 34 55 89 \n"
                             "the even Fibonacci numbers below 4000000 sum to 4613732\n"
                             "the seventh Fibonacci number is 8\nthe second square from 3 is 16\n", 0),),
    "iter_collections": (((), "", "product of the primes: 210\n4 primes\n0: 1\n1: 8\n2: 27\n3: 64\n4: 125\n"
                                  "first cube over 20: 27\nabcdex\na square is 49: true\n", 0),),
    "lisp": (
        ((), LISP_IN, LISP_OUT, 0),
        ((), "(+ 1 2)\n(foo 3)\n", "3\n", 1),
        ((), "(define (f x) x)\n(f 1 2)\n", "", 1),
        ((), "(car 5)", "", 1),
        ((), "(+ 1 (* 2 3)", "", 1),
    ),
    # Programs whose result is their exit status, or that print without input.
    "arena_text": (((), "", "the 2\ncat 1\nand 1\nhat 1\n", 3),),
    "arena_tables": (((), "", "to 2\nbe 2\nor 1\nnot 1\nbe 2\nor 1\nnot 1\nto 7\n", 0),),
    "at_sugar": (((), "", "", 0),),
    "borrow_trait": (((), "", "", 0),),
    "box_store_liveness": (((), "", "", 2),),
    "convert_trait": (((), "", "", 0),),
    "core_only": (((), "", "", 55),),
    "dlist": (((), "", "", 0),),
    "election_tally": (((), "", "Candidate 1, re-fetched after the write-ins: 3 votes\n"
                                "Candidate 3 disqualified, had 2 votes\n"
                                "Candidate 3 confirmed removed -- 0 candidates found (expected 0): 0\n"
                                "Total distinct candidates on file: 33\n", 0),),
    "foreach_sum_pointers": (((), "", "", 0),),
    "format_strings": (((), "", "", 0),),
    "format_trait": (((), "", "point: (3, 4)\ncolor: rgb(255, 128, 0)\n", 0),),
    "functions": (((), "", "", 0),),
    "generic_capabilities": (((), "", "", 20),),
    "graph": (((), "", "from node 0, around the cycle 0 -> 1 -> 2 -> 0 and on to 3: 4 nodes\nfrom node 3: 1 node\n"
                       "from node 0, once 3 -> 4 is added: 5 nodes\n", 0),),
    "greet": (((), "Ada\n", "What's your name?\nHello, Ada!\n", 0, ""),),
    "hello": (((), "", "Hello, world!\n", 0, ""),),
    "iter_basics": (((), "", "", 0),),
    "iter_combinators": (((), "", "", 0),),
    "match_labels": (((), "", "", 0),),
    "mem_counter": (((), "", "", 0),),
    "mem_shared_sum": (((), "", "", 0),),
    "mem_shared_view": (((), "", "", 0),),
    "mem_split_fields": (((), "", "", 0),),
    "modules": (((), "", "", 0),),
    "non_null": (((), "", "", 0),),
    "sayhi": (((), "", "Hello, !\n", 0),),
    "thread_sum": (((), "", 'first thread, "hello": 532\nsecond thread, "world!!": 618\ntotal: 1150\n', 0),),
    "traits": (((), "", "", 0),),
    "tuple_decl": (((), "", "", 0),),
    "zero_size": (((), "", "", 0),),
}

# examples/bad/: each program, and the diagnostic that rejects it.
REJECTED = {
    "branch_inconsistent.kur": "linear value 'h' remains live on some paths",
    "close_by_number.kur": "calling an unsafe function requires unsafe authorization",
    "double_use_handle.kur": "value 'h' has been moved",
    "foreach_holds_itself.kur": "7:8: type foreach_holds_itself::Tree: infinite by-value layout$",
    "foreach_pointer_over_parameter.kur": "10:5: type formation: foreach \\*F needs a source written with pointer components$",
    "foreach_through_pointer_to_sum.kur": "10:17: a foreach through a pointer to a sum reads its variant there and requires unsafe authorization$",
    "forged_file.kur": "the shape of std::io::File! is not exposed here",
    "implicit_package_open.kur": "pattern does not match exists '1\\. \\(Tag!\\['1\\], u32\\)",
    "implicit_package_open_assignment.kur": "type mismatch: exists '1\\. Tag!\\['1\\] and Tag!\\['a\\]",
    "implicit_package_open_plain_let.kur": "a struct pattern needs a struct, not exists '1\\. Tag!\\['1\\]",
    "leaked_handle.kur": "linear value 'h' is not consumed before return",
    "linear_in_arena.kur": "type argument must satisfy Copy",
    "mismatched_skolem.kur": "15:41: brand mismatch: Ref\\[u32, '2\\] and Ref\\[u32, 'k\\]; '2 was opened at 16:5$",
    "match_unknown_label.kur": "9:9: Option\\[u8\\] has no variant Nothing$",
    "non_exhaustive_match.kur": "a match must cover every enum variant",
    "overlapping_instances.kur": "overlapping instances of Describe: \\(u32\\) and \\(T\\)",
    "private_import.kur": "unknown or private member File! of fs",
    "push_after_finish.kur": "15:6: .*value 'builder' has been moved and cannot be lent$",
    "ref_after_remove_in.kur": "17:28: brand mismatch: 'b and 'a; 'b was opened at 15:5, 'a at 13:5$",
    "release_linear.kur": "14:25: type argument must satisfy Copy$",
    "safe_adopt.kur": "calling an unsafe function requires unsafe authorization",
    "type_mismatch.kur": "type mismatch: bool and u32",
    "unbound_result_brand.kur": "brand '1 is not in scope",
    "while_consume_without_replacement.kur": "linear value 'h' remains live on some paths to while",
    "write_by_number.kur": "13:19: calling an unsafe function requires unsafe authorization$",
}


def build(package: Path, out: Path) -> tuple[str, Optional[Path], str]:
    """(name, executable or None for a library, error or "")."""
    sys.setrecursionlimit(20000)
    try:
        resolver = resolve(package)
        sigs = signatures(resolver)
        program = lower(resolver, sigs)
        entry = resolver.packages[0].entry_decl
        c = emit_c(program, check_program(program), sigs.name(entry) if entry is not None else None, hosted=True)
    except KursError as e:
        return package.name, None, f"{type(e).__name__}: {e}"
    c_file = out / f"{package.name}.c"
    c_file.write_text(c)
    if entry is None:
        cc = subprocess.run([c_compiler(), *FLAGS, "-c", str(c_file), "-o", str(out / f"{package.name}.o")],
                            capture_output=True, text=True)
        return package.name, None, cc.stderr[:500] if cc.returncode else ""
    executable = out / package.name
    link_flags = [flag for dependency in resolver.packages for flag in dependency.link_flags]
    c_sources = [str(source) for dependency in resolver.packages for source in dependency.c_sources]
    cc = subprocess.run(cc_command([str(c_file), *map(str, RUNTIME), *c_sources, *map(str, package.glob("*.c"))], str(executable), *link_flags),
                        capture_output=True, text=True)
    return package.name, executable, cc.stderr[:500] if cc.returncode else ""


class ExampleTests(unittest.TestCase):
    def test_every_example_builds_and_runs_as_recorded(self):
        packages = sorted(p.parent for p in EXAMPLES.glob("*/kur.toml") if p.parent.name != "libuv_echo")
        with tempfile.TemporaryDirectory(prefix="kursal-examples-") as base:
            out = Path(base)
            with ProcessPoolExecutor(max_workers=4) as pool:
                built = list(pool.map(build, packages, [out] * len(packages)))
            programs = {name for name, executable, _ in built if executable is not None}
            self.assertEqual(programs, set(RUNS))
            sample, store = out / "sample.txt", out / "store.txt"
            sample.write_text(SAMPLE)
            for name, executable, error in built:
                with self.subTest(example=name):
                    self.assertEqual(error, "")
                    for args, stdin, stdout, status, *stderr in RUNS.get(name, ()):
                        args = [a.replace("{file}", str(sample)).replace("{store}", str(store)) for a in args]
                        assert executable is not None
                        result = subprocess.run([str(executable), *args], input=stdin, capture_output=True,
                                                text=True, timeout=60, cwd=out)
                        self.assertEqual((result.stdout, result.returncode),
                                         (stdout.replace("{file}", str(sample)), status), args)
                        if stderr:
                            self.assertEqual(result.stderr, stderr[0], args)

    def test_libuv_echo_builds(self):
        if shutil.which("pkg-config") is None or subprocess.run(["pkg-config", "--exists", "libuv"]).returncode != 0:
            self.skipTest("libuv headers and library are not installed")
        with tempfile.TemporaryDirectory(prefix="kursal-libuv-echo-") as base:
            name, executable, error = build(EXAMPLES / "libuv_echo", Path(base))
            self.assertEqual(name, "libuv_echo")
            self.assertEqual(error, "")
            self.assertIsNotNone(executable)

    def test_bad_examples_are_rejected_for_their_reason(self):
        sys.setrecursionlimit(20000)
        files = sorted((EXAMPLES / "bad").glob("*.kur"))
        self.assertEqual({f.name for f in files}, set(REJECTED))
        for f in files:
            with self.subTest(example=f.name), self.assertRaisesRegex(KursError, REJECTED[f.name]):
                resolver = resolve(f)
                check_program(lower(resolver, signatures(resolver)))


class WalkthroughTests(unittest.TestCase):
    def test_the_counter_walkthrough_runs_and_its_snapshots_are_current(self):
        counter = Path(__file__).resolve().parents[1] / "examples/counter"
        with tempfile.TemporaryDirectory(prefix="kursal-counter-") as out:
            result = subprocess.run([sys.executable, str(counter / "walkthrough.py"), out],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr[-500:])
            for snapshot in sorted((counter / "generated").iterdir()):
                with self.subTest(file=snapshot.name):
                    self.assertEqual((Path(out) / snapshot.name).read_text(), snapshot.read_text())


if __name__ == "__main__":
    unittest.main()
