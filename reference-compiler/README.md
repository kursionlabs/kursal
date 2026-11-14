# reference-compiler

The reference compiler for Kursal (Python package `kursal`). It is a direct,
spec-driven implementation of the language: source text is parsed, resolved,
typed, lowered to KURS (the core language), checked, and translated to C. The
translation leaves out what the C would not use and turns calls in tail position into
jumps (`plan.py`, `cgen.py`); there are no passes over KURS.

It needs only Python 3.12+. [uv](https://docs.astral.sh/uv/) manages the
development tools (`pyproject.toml`, `uv.lock`); mypy checks `kursal/`.

## Pipeline

| Stage | Module | Output |
|---|---|---|
| Parse | `parser.py` | the surface AST of `syntax.py` |
| Resolve | `resolve.py` | the package graph from `kur.toml`; every path bound to what it names |
| Signatures | `sigs.py`, `formation.py`, `layout.py`, `coherence.py`, `termination.py` | declaration types in core terms; folded constants |
| Inference | `infer.py`, `unify.py`, `types.py` | the type of every expression; call targets; closure captures |
| Lowering | `lower.py`, `placeflow.py`, `promote.py` | a KURS program (`core.py`), with `&` places and block contracts |
| Checking | `check.py` | acceptance by the core rules |
| C | `plan.py`, `cgen.py`, `mangle.py` | one C translation unit |

`kurs.py` reads and writes serialized KURS (S-expressions, `sexpr.py`), and `readable.py`
prints it in the readable notation.

## A worked example: Counter

[`examples/counter`](examples/counter/lib.kur) is a complete package with no library dependencies. It
starts a counter at 40, branches on its value, and lends it to `bump`, which stores 41 and returns 41.
The result adds the updated field and the returned value, so the program exits **82**.

```bash
uv run python examples/counter/walkthrough.py            # regenerate examples/counter/generated, then verify
uv run python examples/counter/walkthrough.py /tmp/out   # write elsewhere, leaving the snapshots alone
```

The runner calls `resolve`, `signatures`, `lower`, `check_program` and `emit_c`, and writes
[the inferred annotations](examples/counter/generated/annotations.txt),
[readable KURS](examples/counter/generated/program.kurs), [C](examples/counter/generated/program.c) and
[the KURS serialization](examples/counter/generated/program.sexpr). It then reads the serialization back,
checks it, requires the same C from both routes, and builds and runs both executables.

What to look for in each output:

- **Inference.** `bump`'s parameter is a place (`&counter: Counter`): lending is a parameter mode, not a
  pointer type. `main`'s `counter` and `result` get binding IDs 1 and 2, which name their storage below.
- **Lowering.** Each source local is a whole-function place (`&counter#1`, `&result#2`). A field read is
  `load &counter#1.value`; the call lends the place itself, `call counter::bump(&counter#1)`. The `if`
  becomes `branch` to `b10` and `b11`, which meet at `join15(%r14: u32)`. An operand that precedes
  another waits in a temporary place (`&#tmp1`, `&#tmp3`) and is loaded again just before use.
- **Place contracts.** Each block states which places are empty on entry (`state { empty &result#2; ... }`).
  The checker checks every block against its own contract alone, and every jump against its target's.
- **C.** The place parameter is a pointer, and the call passes the caller's storage:
  `b10_v12 = ...bump...(&p_counter_1);`. Each block is a label; a jump with an argument assigns
  `join15_r14` and jumps to `L_join15`. Contracts leave nothing behind at run time.

## Packages

A package is a directory with a `kur.toml`:

```toml
[package]
name = "app"
kind = "executable"          # or "library" (the default)
entry = "package::main"      # executables only
source_root = "src"          # the default

[dependencies.std]
path = "../lib/std"
```

Every `.kur` file under the source root is a module, named by its path
(`a/lib.kur` and `a.kur` are both module `a`). A single `.kur` file can also be
compiled on its own; it then depends on the standard library `std` when one is
installed at `$KURSAL_LIB/std` (by default `lib/std` at the repository root).

## Usage

```bash
cd reference-compiler
uv sync
uv run python -m kursal FILE.kurs                         # check serialized KURS
uv run python -m kursal FILE.kurs --emit-c --entry main   # check, then emit C
uv run python -m kursal FILE.kurs --emit-kurs             # readable KURS
```

From source, the stages are called as a library:

```python
from kursal.resolve import resolve
from kursal.sigs import signatures
from kursal.lower import lower
from kursal.check import check_program
from kursal.cgen import emit_c

resolver = resolve("path/to/package")
sigs = signatures(resolver)
program = lower(resolver, sigs)
c = emit_c(program, check_program(program), sigs.name(resolver.packages[0].entry_decl))
```

## Tests

```bash
uv run python tools/test.py                  # every tests/test_*.py, up to four workers
uv run python tools/test.py -j 8             # choose the worker count
uv run python tools/test.py tests.test_lower # one module (class and method names also work)
uv run python tools/test.py -j 1 -v          # serial, full unittest output
uv run mypy                                  # static types
```

End-to-end tests build C with the command of `kursal/cc.py` (`$KURSAL_CC`, else
`clang`, with `-O3 -w -fno-builtin`), the one `tools/build.py` and the self-hosted compiler use, and run it; each test program's exit code
is a checksum of its behavior.
