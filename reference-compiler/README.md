# reference-compiler

The reference compiler for Kursal (Python package `kursal`). It is a direct,
spec-driven implementation of the language: source text is parsed, resolved,
typed, lowered to KURS (the core language), checked, and translated to C by
the canonical translation. There are no optimization passes.

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
| C | `cgen.py`, `mangle.py` | one C translation unit |

`kurs.py` reads serialized KURS (S-expressions, `sexpr.py`), and `readable.py`
prints it in the readable notation.

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

End-to-end tests build C with `cc` and run it; each test program's exit code
is a checksum of its behavior.
