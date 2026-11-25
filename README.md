# Kursal

Kursal is a systems programming language that compiles to C.

## Quickstart

You need:

- Linux: `scripts/bootstrap.sh` runs each compiler it builds under `ulimit -v` and `timeout`, capped at
  6 GB of address space and 10 minutes.
- [uv](https://docs.astral.sh/uv/), which provides Python 3.12 or later for the reference compiler.
- Clang, or another C compiler named by `$KURSAL_CC`, and a linker that does link-time optimization:
  `kursal build`, the bootstrap's included, compiles a file per module with `-flto=thin`, so the GNU
  linker needs LLVM's gold plugin (`LLVMgold.so`), which most distributions install with Clang.

The tested toolchain is Clang 23.1, GNU ld 2.47, uv 0.9 and Python 3.12. Build the compiler, then a
program with it:

```sh
scripts/bootstrap.sh
bin/kursal build examples/hello && ./a.out
```

`scripts/bootstrap.sh` takes a few minutes: the reference compiler, in Python, builds the compiler,
which then builds itself into `bin/kursal`. The program is
[`examples/hello/lib.kur`](examples/hello/lib.kur):

```kursal
// Prints "Hello, world!" to standard output with `std::io::print`.
use std::io::print;

fn main() {
    print("Hello, world!\n");
}
```

and it prints:

```text
Hello, world!
```

`kursal build PATH` writes `a.out` in the current directory, and its generated C in `a.out.build`,
a file per module, which Clang or GCC compiles in parallel and links with link-time optimization;
another `$KURSAL_CC` gets the C as one file, `a.out.c`. Use `-o OUTPUT` to choose another location,
`--single-c` to compile the C as one file with any compiler, which builds slower and runs a little
faster, or `--emit-c` to print the C. `kursal check` checks a KURS file, the core language the
compiler lowers to, not a source package; `bin/kursal build PATH --emit-c > /dev/null` checks a
package, a library too, without compiling its C. Run `bin/kursal --help` for the other commands.

[`examples/README.md`](examples/README.md) lists the example programs, starting with the ones to read
first.

## Packages

A package is a directory with a manifest, `kur.toml`, and its sources: every `.kur` file under the
source root is a module, named by its path (`a/lib.kur` and `a.kur` are both module `a`), and
`lib.kur` at the root is the package's root module. A single `.kur` file also builds on its own, as
a package that depends on the standard library `std`.

```toml
[package]
name = "app"
kind = "executable"
entry = "main"
source_root = "."

[dependencies.std]
path = "../lib/std"
```

| Key | |
|---|---|
| `name` | the package's name; required |
| `kind` | `"library"` (the default) or `"executable"` |
| `entry` | an executable's entry function, required for one and not allowed in a library: a path in the package, bare (`"main"`) or from `package` (`"package::main"`, `"package::server::main"`) |
| `source_root` | the directory of the sources, relative to the manifest; the default is `"src"` |
| `link_flags` | an array of nonempty strings, each passed as one argument to the C compiler after the generated C and C sources, without a shell; the flags of every dependency are passed too, so a library can declare its own `-l` |
| `c_sources` | an array of C source paths, relative to the package, compiled with the generated C into every executable that depends on the package |
| `[dependencies.NAME]` | a dependency, imported as `NAME` (`use NAME::module::Item;`); `path`, its only key, is its directory or its `kur.toml`, relative to the manifest |

A manifest is checked when it is read, before its sources or its dependencies are. Any other key
in the manifest, in `[package]` or in a dependency's table is an error (`.../kur.toml: unknown
manifest key version`), as are a `kind` other than those two (`.../kur.toml: manifest kind must be
"library" or "executable"`) and an `entry` in a library (`.../kur.toml: manifest entry is only for
kind "executable"`). The standard library is `std`, over `core`, `alloc` and `libc`, all in
[`lib/`](lib/); a package that needs `core` alone depends on it directly, as
[`examples/core_only`](examples/core_only/kur.toml) does.

## API documentation

`kursal doc` generates the content of a documentation site from a package's public modules, `//!`
module comments, `///` item comments, and declaration signatures; the site's configuration and
theme are in [`docs/site/`](docs/site/). For the standard library and its dependencies:

```sh
bin/kursal doc lib/std -o docs/generated/src
cp -R docs/site/. docs/generated
krayon build docs/generated
```

See [`docs/README.md`](docs/README.md) for serving the generated site.

## Repository

| Directory | Contents |
|---|---|
| [`reference-compiler/`](reference-compiler/) | the reference compiler, in Python: source to KURS (the core language) to C |
| [`compiler/`](compiler/) | the compiler, in Kursal, which builds itself and must agree with the reference compiler |
| [`lib/`](lib/) | the standard library packages: `core`, `alloc`, `libc` (the C runtime and its bindings), `std` (over `core`, `alloc` and `libc`), `toml` |
| [`examples/`](examples/) | example programs and libraries, and programs the compiler must reject |
| [`editors/`](editors/) | Tree-sitter grammars and highlight queries for `.kur` and readable `.kurs` |
| [`scripts/`](scripts/) | `bootstrap.sh`, which builds `compiler/` into `bin/kursal` |

`scripts/bootstrap.sh` has the reference compiler build `compiler/`, and the result build it again
into `bin/kursal`; with `--check`, `bin/kursal` builds it a third time and must write the same C.
Both compilers find the standard library in `$KURSAL_LIB`, else in `lib/`, and compile C with
`$KURSAL_CC`, else `clang`, at `-O3`.
See [`reference-compiler/README.md`](reference-compiler/README.md) to use and test the reference
compiler, and [`compiler/README.md`](compiler/README.md) for the compiler and its C backend.

## Standard library

Text goes out through writers (`std::fmt::Writer`): `print("done\n")` and `print(f"{n} files\n")`
print to standard output, `eprint` to standard error; `out.put(x)` prints a literal, a number, a
`bool` or a `Buffer!` into any writer, a `Buffer!` or `stdout()` among them, and
`out.write(&cx, bytes)` copies the bytes of a view read through the capability `cx` lends.

Running out of memory aborts a program: the library's constructors, and every operation that grows a
container (`push`, `insert`, a write to a buffer, an arena's allocations), stop it with `abort` when
`malloc` or `realloc` fails, and return no error for it. A program that must recover makes its
containers with the fallible constructors, which return an `AllocError` instead:
`Buffer!::try_with_capacity`, `Vec!::try_new`, `OwnedVec!::try_new`, `HashMap!::try_new`,
`OwnedMap!::try_new` and `Owned!::try_new` (listed in [`lib/alloc/lib.kur`](lib/alloc/lib.kur)). A
container made so still aborts if it later fails to grow.
[`lib/std/CONTRACTS.md`](lib/std/CONTRACTS.md) records the obligation of every function with a raw
pointer in its signature, and why the rest are safe.

## Contributing

Report bugs and propose changes in the repository's GitHub issues. The compiler in `compiler/` must
accept and reject the same programs as the reference compiler, with the same messages, and produce
the same output, so a change to the language changes both, with tests in
`reference-compiler/tests/`. Before sending a change, run:

```sh
cd reference-compiler
uv sync
uv run mypy                          # static types of the reference compiler
uv run python tools/test.py -j 8     # every test module, compiler/ against the reference too
cd ..
scripts/bootstrap.sh --check         # the compiler builds itself to a fixpoint
```

[`reference-compiler/README.md`](reference-compiler/README.md) has more ways to run the tests, and
[`compiler/README.md`](compiler/README.md) how the compiler is built and compared.

## License

Kursal is licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE) or <https://www.apache.org/licenses/LICENSE-2.0>)
- MIT license ([LICENSE-MIT](LICENSE-MIT) or <https://opensource.org/licenses/MIT>)

at your option.

Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in Kursal by
you, as defined in the Apache-2.0 license, shall be dual licensed as above, without any additional terms or
conditions.
