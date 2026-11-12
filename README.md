# Kursal

Kursal is a systems programming language that compiles to C.

## Repository

| Directory | Contents |
|---|---|
| [`reference-compiler/`](reference-compiler/) | the reference compiler, in Python: source to KURS (the core language) to C |
| [`lib/`](lib/) | the standard library packages: `core`, `alloc`, `std`, `libc`, `thread`, `toml` |
| [`examples/`](examples/) | example programs and libraries, and programs the compiler must reject |

See [`reference-compiler/README.md`](reference-compiler/README.md) to build and test it.

## License

Kursal is licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE) or <https://www.apache.org/licenses/LICENSE-2.0>)
- MIT license ([LICENSE-MIT](LICENSE-MIT) or <https://opensource.org/licenses/MIT>)

at your option.

Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in Kursal by
you, as defined in the Apache-2.0 license, shall be dual licensed as above, without any additional terms or
conditions.
