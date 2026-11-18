# reference-compiler

The reference compiler for Kursal (Python package `kursal`). It is a direct,
spec-driven implementation of the language: source text is parsed, resolved,
typed, lowered to KURS (the core language), checked, and translated to C. The
translation leaves out what the C would not use, reads values where they lie instead of
copying them, passes large aggregates by pointer, and turns calls in tail position into jumps
(`plan.py`, `cgen.py`; [`compiler/README.md`](../compiler/README.md), "The C backend"); there
are no passes over KURS.

It needs only Python 3.12+. [uv](https://docs.astral.sh/uv/) manages the
development tools (`pyproject.toml`, `uv.lock`); mypy checks `kursal/`.

## Pipeline

| Stage | Module | Output |
|---|---|---|
| Parse | `parser.py` | the surface AST of `syntax.py` |
| Resolve | `resolve.py` | the package graph from `kur.toml`; every path bound to what it names |
| Signatures | `sigs.py`, `formation.py`, `layout.py`, `coherence.py`, `termination.py` | declaration types in core terms; folded constants, but for arithmetic on `usize` and `isize`, which the C compiler folds |
| Inference | `infer.py`, `unify.py`, `types.py` | the type of every expression; call targets; closure captures |
| Lowering | `lower.py`, `placeflow.py`, `promote.py` | a KURS program (`core.py`), with `&` places and block contracts |
| Checking | `check.py` | acceptance by the core rules |
| C | `plan.py`, `cgen.py`, `mangle.py` | one C translation unit |

`kurs.py` reads and writes serialized KURS (S-expressions, `sexpr.py`), and `readable.py`
prints it in the readable notation. `literals.py` holds the rules for scalar literals that
inference, lowering and the checker share, and `cc.py` the C compiler command.

`usize` and `isize` are pointer-sized (`uintptr_t` and `intptr_t` in C), and one C file serves 32-bit
and 64-bit targets. Their literals range over 64 bits. Constant arithmetic on them is not evaluated
here: `sigs.py` keeps it as an `OpConst`, lowering emits its instructions, and the C compiler folds
them at the target's width. An array length, which a type must know, cannot use such arithmetic.
[`compiler/README.md`](../compiler/README.md), "Pointer-sized integers", has the details.

## Builtins

The primitive types, `Never` (also written `!`) and the traits `Copy` and `Exposed` are always in
scope. Everything else the compiler provides is a builtin, named `builtin::name`, and only package
`core` writes such a path (anywhere else it is an error, `only package core names builtins`). `core`
exports each builtin from the module it belongs to, as it would any other name (`export
builtin::NonNull;`, the `export` form of `use`), and every other package imports it from there:
`use core::ptr::NonNull;`, `use std::mem::sizeof;` through the standard library, or a path such as
`core::cap::fresh()`.

| Module | Builtin | |
|---|---|---|
| `core::cap` | `Brand['k]` | the witness type of brand `'k`: Copy and zero-size |
| | `fresh()` | a new brand's witness, `exists 'k. Brand['k]` |
| | `unsafe static_brand()` | the witness of `'static` |
| `core::ptr` | `NonNull[T]` | a pointer that is never null; `core::ptr` gives it its methods |
| | `unsafe load_volatile(p)`, `unsafe store_volatile(p, v)` | volatile access, as `p.load_volatile()` and `p.store_volatile(v)` |
| `core::mem` | `sizeof[T]()` | the size of `T` in bytes, 0 for a zero-size type |
| | `unsafe alloca[T]()` | room for a `T` in the calling function's frame |
| | `enum_variant(&place)`, `enum_variant(value)` | the index of an enum value's variant, beside the value when it is taken by value |
| `core::atomic` | `unsafe fence(ordering)` | a memory fence |
| `core::process` | `trap()` | stops the program at once (C's `__builtin_trap`); its type is `fn() -> !` |

`sizeof` is a function of one type argument, like `alloca`, not syntax. The operations of a raw
pointer (`p.load()`, `p.store(v)`, `p.atomic_load(o)`, `p.compare_exchange(...)`, `p.fetch_add(...)`)
are methods of `*T`, and need no import.

## KURS names

In KURS a bare atom is the IR's own vocabulary, a primitive type or a type parameter; a name with
`::` is always the program's, fully qualified (`core::option::Option`). A linear type's name ends
with `!`, as in the source, in its declaration and in every reference: `(type core::mem::Region!
...)`, `(core::mem::Region! $k)`, `(fun alloc::buffer::Buffer!::new ...)`; a type is linear exactly
when its name ends with `!`. The checker holds a declaration to the source's rule: a type without the
`!` is `Copy` when its type parameters are (`type formation: type S holds a linear type; declare it
S!`). In C a symbol escapes the `!` as `_21`
(`_ZN4kurs5alloc6buffer9Buffer_213new17h...E`, `mangle.path_segments`), and a type's readable C name
leaves it out (`K_alloc_buffer_Buffer`).

- **Types**: `struct`, `enum`, `tuple`, `array`, `exists` and `foreach` (whose pattern `(ptr F)`
  binds the pointee), and `(ptr T)`, `(non_null T)`, `(brand $k)`, `(fn (PTYPE*) RET)` (an
  unsafe function's type `(fn (unsafe) (PTYPE*) RET)`).
- **Premises**: `(Copy T)` and `(Exposed T)`.
- **Builtin operations**: `int_cast`, `to_ptr` (a `non_null` as its `ptr`), `cast`, `sizeof`,
  `alloca`, `fresh`, `static_brand` and `trap` (of type `!`, so the block ends with `unreachable`);
  through a raw pointer `ptr_load`, `ptr_store`,
  `ptr_load_volatile` and `ptr_store_volatile`; and the atomics, each with its ordering: one
  `ptr_atomic_KIND` per atomic method of `*T`, whatever the method's own spelling (`ptr_atomic_load`,
  `ptr_atomic_store`, `ptr_atomic_swap`, `ptr_atomic_compare_exchange`, `ptr_atomic_fetch_add`,
  `_sub`, `_and`, `_or`, `_xor`), and `fence`, which takes no pointer.
- **Unsafety** is marked only by the `(unsafe (= ...))` wrapper of one instruction, or by an unsafe
  function (the `(unsafe)` clause of its declaration, and of its type). The operations that need it are exactly `UNSAFE_OPS` (`core.py`; `kurs::core::is_unsafe_op`):
  `cast`, `alloca`, `static_brand`, the `ptr_` loads and stores, the atomics and `fence`; a call needs
  it when its callee is unsafe or when it lends a raw pointer's target (`&*%p`).

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
  `join15_r14 = ...bump...(&p_counter_1);`. Each block is a label; a jump with an argument assigns
  `join15_r14` and jumps to `L_join15`, and a value only that jump reads is assigned to it where it
  is made, as the call's result is. Contracts leave nothing behind at run time.

## Packages

A package is a directory with a `kur.toml`:

```toml
[package]
name = "app"
kind = "executable"          # or "library" (the default)
entry = "package::main"      # executables only
source_root = "src"          # the default
link_flags = ["-luv"]        # optional linker arguments, inherited by dependents
c_sources = ["native/uv.c"] # optional C sources, relative to this package

[dependencies.std]
path = "../lib/std"
```

A `kind` other than those two is an error that names the manifest (`.../kur.toml: manifest 'kind' must
be "library" or "executable"`).

`link_flags` is an array of nonempty strings without NUL. Each element is passed as one argument
to the C compiler after the generated C and helper sources. Flags from the executable and every dependency
are included in package traversal order, so a library can declare its own `-l` requirement.
The compiler passes arguments directly, without a shell.

`c_sources` is an array of package-relative C source paths. Both build commands resolve each path
against the declaring package's directory and compile the sources of all dependencies alongside
the generated C, before passing `link_flags` to the linker. Native dependencies such as libuv
remain optional: only packages that declare these sources and flags require them.

An executable's entry is a safe, non-generic function without premises, of
one of three shapes, each with or without a parameter `args: Args` of type
`std::process::Args`:

| Entry | Exit status |
|---|---|
| `fn main()` | 0 |
| `fn main() -> i32` | the result |
| `fn main() -> Result[(), std::io::Error]` | 0 for `Ok`; for `Err`, 1, once `error: DESCRIPTION (error ERRNO)` is written to stderr |

Any other entry is an error:

```text
backend: the entry app::main must be `fn()`, `fn() -> i32` or `fn() -> Result[(), std::io::Error]`, each with or without a parameter `args: std::process::Args`, and safe, not generic and without premises
```

The backend writes C's `main` for the entry: `main(int argc, char **argv)`,
which fills `Args` from `argv` and `argc`, for one taking them, else
`main(void)`. For an `Err` it calls `kur_libc_process_report_error` in
`lib/libc/fs.c`, which writes the errno with the C library's description of
it (`strerror`), as `error: No such file or directory (error 2)`. The
backend knows `Args` and the result by their shapes alone, so no name is
special: `Args` is a struct of exactly a `*(*u8)` and a `usize`, and the
result an enum of `Ok` of `()` and `Err` of a struct of exactly one `i32`.
`Args` is restricted to `std::process`, so no safe code makes one otherwise,
and its accessors may rely on what C's `main` received.

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

End-to-end tests build C with the command of `kursal/cc.py` (`$KURSAL_CC`, else `clang`, with
`-O3 -w -fno-builtin`), the one `tools/build.py` and the self-hosted compiler use, and run it; each
test program's exit code is a checksum of its behavior. The C's requirements of its C compiler and
target are in [`compiler/README.md`](../compiler/README.md), "The C compiler".

## Fuzzing

`tools/fuzz` generates and mutates programs and runs each through both compilers, looking for, in
order: a program the front end accepts whose lowered KURS the checker rejects (the front end must
reject it first, with a message about the source); a crash, trap or timeout of either compiler; the
two compilers disagreeing on a verdict, a message, the KURS or the C; and C that does not compile,
or that behaves differently at `-O3` and at `-O0` under `-fsanitize=undefined,address`.

```bash
uv run python -m tools.fuzz corpus --out CORPUS.json      # the packages the tests lower, to mutate
uv run python -m tools.fuzz run --seed 1 --minutes 25 --count 100000 --out OUT \
    --selfhost ../bin/kursal --execute --corpus CORPUS.json --minimize -j 8
uv run python -m tools.fuzz show --seed 1000003           # the program one seed makes
uv run python -m tools.fuzz one FILE.kur --prelude --selfhost ../bin/kursal   # one program's outcome
uv run python -m tools.fuzz minimize OUT/findings/CLASS-HASH/seedN --out MIN  # delta debugging
```

`gen.py` writes well-typed programs over a prelude of linear and `Copy` structs and enums, a branded
tag and its consumers and lenders, tracking which locals hold values, so that a linear value is
consumed once on every path, a place parameter is full at every return, branches end alike, a loop
body leaves the state it found and an early return consumes what is live; it also writes probes the
front end must reject, such as one call lending overlapping places. `mutate.py` drops, duplicates,
swaps and moves statements, inserts returns and traps, wraps statements in branches and loops, and
turns lends into moves and back, in generated programs, examples and the corpus. `reduce.py`
minimizes a finding by ddmin over statements, then words, keeping its bug class and normalized
message. A campaign is deterministic for its seed and count, and runs a snapshot of this compiler
and a copy of the self-hosted binary taken when it starts. Every compiler run has a timeout and a
memory limit; run a campaign as one job in a memory-capped scope, like the repository's other heavy
jobs.
