# Kursal

Kursal is a systems programming language that compiles to C.

## Quickstart

You need Linux, [uv](https://docs.astral.sh/uv/) (it provides Python 3.12 for the reference compiler),
and Clang, or another C compiler named by `$KURSAL_CC`. Build the compiler, then a program with it:

```sh
scripts/bootstrap.sh
bin/kursal build examples/hello && ./a.out
```

`scripts/bootstrap.sh` takes a few minutes: the reference compiler, in Python, builds the compiler,
which then builds itself into `bin/kursal`. The program is
[`examples/hello/lib.kur`](examples/hello/lib.kur):

```kursal
use std::io::print;

fn main() {
    print("Hello, world!\n");
}
```

and it prints:

```text
Hello, world!
```

`kursal build PATH` writes `a.out` and its generated C companion `a.out.c` in the current
directory. Use `-o OUTPUT` to choose another location, or `--emit-c` to print the C. Run
`bin/kursal --help` for the other commands.

[`examples/README.md`](examples/README.md) lists the example programs, starting with the ones to read
first.

## API documentation

`kursal doc` generates a Krayon site from a package's public modules, `//!`
module comments, `///` item comments, and declaration signatures. For the standard library and its
dependencies:

```sh
bin/kursal doc lib/std -o docs/generated
../krayon/bin/krayon build docs/generated
```

See [`docs/README.md`](docs/README.md) for serving the generated site. Krayon
is built separately from its sibling repository.

## Repository

| Directory | Contents |
|---|---|
| [`reference-compiler/`](reference-compiler/) | the reference compiler, in Python: source to KURS (the core language) to C |
| [`compiler/`](compiler/) | the compiler, in Kursal, which builds itself and must agree with the reference compiler |
| [`lib/`](lib/) | the standard library packages: `core`, `alloc`, `std`, `libc`, `thread`, `toml` |
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

Running out of memory aborts a program: the library's constructors, and every operation that grows a
container (`push`, `insert`, a buffer's `push_*`, an arena's allocations), stop it with `abort` when
`malloc` or `realloc` fails, and return no error for it. A program that must recover makes its
containers with the fallible constructors, which return an `AllocError` instead:
`Buffer!::try_new_with_cap`, `Vec!::try_new`, `OwnedVec!::try_new`, `HashMap!::try_new`,
`OwnedMap!::try_new` and `Owned!::try_new` (listed in [`lib/alloc/lib.kur`](lib/alloc/lib.kur)). A
container made so still aborts if it later fails to grow.
[`lib/std/CONTRACTS.md`](lib/std/CONTRACTS.md) records the obligation of every function with a raw
pointer in its signature, and why the rest are safe.

## License

Kursal is licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE) or <https://www.apache.org/licenses/LICENSE-2.0>)
- MIT license ([LICENSE-MIT](LICENSE-MIT) or <https://opensource.org/licenses/MIT>)

at your option.

Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in Kursal by
you, as defined in the Apache-2.0 license, shall be dual licensed as above, without any additional terms or
conditions.
