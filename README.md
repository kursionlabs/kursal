# Kursal

Kursal is a systems programming language that compiles to C.

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

## License

Kursal is licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE) or <https://www.apache.org/licenses/LICENSE-2.0>)
- MIT license ([LICENSE-MIT](LICENSE-MIT) or <https://opensource.org/licenses/MIT>)

at your option.

Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in Kursal by
you, as defined in the Apache-2.0 license, shall be dual licensed as above, without any additional terms or
conditions.
