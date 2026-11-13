# compiler

The Kursal compiler, written in Kursal. It is built on the design of the reference compiler,
[`reference-compiler/kursal`](../reference-compiler/kursal): each module here translates the Python
module of the same name, and must accept and reject the same programs, with the same messages, and
produce the same output.

| Module | Translates | |
|---|---|---|
| `base` | | storage: text, sequences, maps, sets, cells; `fail` |
| `system` | | arguments, files, directories, environment |
| `syntax`, `parser` | `syntax.py`, `parser.py` | the surface AST and the parser |
| `dump` | | the AST as text, for comparison with the reference parser |
| `core`, `sexpr` | `core.py`, `sexpr.py` | the KURS core and S-expressions |
| `types`, `unify` | `types.py`, `unify.py` | substitution, equality, shapes; unification over `core::Meta` |
| `literals`, `formation`, `layout`, `termination`, `coherence` | the same names | checks of literals, types, declarations and instances |
| `resolve` | `resolve.py` | manifests, the package graph, and what every name names |
| `resolved` | | a resolved program as text, for comparison with the reference resolver |
| `kurs` | `kurs.py` | serialized KURS: its reader and writer |
| `main` | `__main__.py` | the command line |
| `selftest` | | `kursalc selftest-types`, the type modules on small cases, for the tests |

## Building and testing

From `reference-compiler/`:

```sh
uv run python tools/build.py ../compiler /tmp/kursalc --times   # build with the reference compiler
/tmp/kursalc parse ../examples/lisp/lib.kur                      # one command of the compiler
uv run python tools/build.py ../examples/wc /tmp/wc --kurs /tmp/wc.kurs   # also write its KURS
/tmp/kursalc kurs /tmp/wc.kurs                                   # read KURS and write it back
uv run python tools/test.py tests.test_compiler                  # compare with the reference compiler
```

`tests/test_compiler.py` builds the compiler once per change to its sources (cached in
`reference-compiler/.build/`) and compares its output with the reference compiler's.

## Storage

A compiler builds one web of values and keeps it until it exits, so nothing in `base` is freed and
every handle is a Copy pointer: a change made through one copy of a `Seq`, `Map`, `Set` or `Cell` is
seen through every copy, as with Python's lists, dicts and sets. Elements are Copy values, read and
written whole. This is what lets the rest of the compiler pass state around by value, with no `&` and
no brands, and read like the Python it translates.

| Python | Kursal |
|---|---|
| `str` | `Text` (a byte view; `text("literal")`, `t.is("literal")`, `t.eq(u)`, `slice`, `find`, `split`, `starts_with`) |
| building a `str` | `Out::new().put("a").add(t).u64(n).done()` |
| `list`, `tuple` of values | `Seq[T]` (`push`, `get`, `set`, `len`, `pop`, `last`, `copy`, `slice`, `append`, `insert`, `remove`, `sort`) |
| `dict` | `Map[K, V]` (insertion-ordered like `dict`: `set`, `get` → `Option`, `at`, `get_or`, `has`, `remove`, `keys`, `values`) |
| `set` | `Set[K]` (insertion-ordered: `add`, `has`, `remove`, `items`) |
| a mutable attribute | a field holding a `Cell[T]` (`get`, `set`), a `Seq` or a `Map` |
| a dataclass | an `exposed struct`; a `Key` instance (`key_hash`, `key_same`) if it is a map key |
| a union of classes | an `exposed enum` whose variants carry structs |
| `raise KursError(message, pos)` | `fail(message)` with the same text; see below |
| `Optional[T]` | `Option[T]` |
| a recursive field | `Cell[T]` (or a `Seq`), so the type has a finite size |

Map keys need an instance of `base::Key`; `Text`, `u32`, `usize`, `bool`, 2- and 3-tuples and
`Seq`s of keys have one, and `core` adds `Type`, `Param`, `Premise` and `ModuleId`. `core::same` is
structural type equality (Python's `==` on frozen dataclasses), `core::hash` its hash.

A class whose methods mutate `self` becomes a struct of handles (cells, sequences, maps) whose
methods take `self` by value: the copies share the same storage. Its constructor allocates them.

## Errors

The compiler stops at the first error: `base::fail(message)` prints the message and exits with
status 1. A message is exactly what the reference compiler prints for the same error: `KursError`'s
`str()`, which is `line:col: message` when it carries a position and the message alone otherwise,
and the file prefix the front end adds (`resolve.located`). Tests compare messages.
`types::kurs_error(message, pos)` stops with `KursError(message, pos)`'s text; a `Pos` with line 0
stands for no position.

## Fresh names

`types.fresh` numbers renamed binders (`a#3`) from one counter for the whole compilation. Here that
counter is a `types::Fresh`, made once and passed to everything that may rename a binder: `subst`,
`unify`, `match_head`, `Types`, `Formation`, `check_type_expansion`, `check_coherence`. Passing the
same one everywhere numbers names as the reference compiler does.

Where the reference wraps a phase in `with resolver.located(module, pos):`, call
`r.locate(module, pos)` before it and `r.unlocate()` after, and report errors inside it with
`r.fail(message, pos)` (a `pos` on line 0 is none): it adds the file, and the item's position when
the error has none of its own. Lookups that the reference may catch (`ResolveError`) return a
`Result[Target, Problem]` instead of stopping (`Resolver.lookup`); `Resolver.need` stops.

The standard library is found as the reference finds it, in `$KURSAL_LIB`; without it, in `lib`
beside the directory holding the compiler's binary (`PREFIX/bin/kursalc` reads `PREFIX/lib`).

## The language, for a compiler author

These are the places where Kursal differs from what one might expect.

- **Names.** Keywords cannot be field, method or variable names: `use export module struct tuple
  enum fn extern impl trait instance const forall where as exposed unsafe closure exists foreach of in
  for while if else match let var return pack sizeof true false self _`. Fields here are named
  `is_unsafe`, `home`, `quantifier`, `trait_name` and the like instead.
- **Method lookup is global.** `x.name()` looks for `name` among the methods of `x`'s type, and
  otherwise among every single-parameter trait in the whole program. A trait method that shares a
  name with another trait's (`hash`, `eq`, `cmp`, `len`, ...) makes such calls ambiguous everywhere,
  including in the standard library. Give trait methods distinctive names (`key_hash`), or call them
  as `Trait::method(x)`.
- **No indexing operator.** `a[i]` is not an expression; `[` after a name starts type arguments. Use
  a `Seq` and `get`.
- **No nested patterns.** A match case binds names: `Option::Some(x) =>`, never
  `Result::Ok(Option::Some(x))`. Match the outer value, then the inner one. The `_` arm comes last.
- **Struct literals** are `Point.{ x: 1u32, y }`; tuple structs and variants with a payload are built
  with `.(`: `Option::Some.(x)`. A spread `..rest` names a bundle made by a pattern
  (`let { x, ..rest } = p; Point.{ x: 2u32, ..rest }`), not an arbitrary value.
- **Literals** carry their type when nothing else fixes it: `0u32`, `1usize`, `b'a'`. A bare `0`
  needs a type from context.
- **`unsafe`** marks one operation: `unsafe f(x)` calls an unsafe or extern function, `unsafe *p`
  reads through a pointer, `unsafe (x as *T)` casts. `unsafe (unsafe malloc(n) as *T)` is two
  operations. A safe function may contain unsafe operations.
- **Generic code over linear types.** A type parameter may stand for a linear type unless a premise
  says `Copy[T]`. Using a value twice, or dropping it on one path (`a && b` drops `b` when `a` is
  false), needs `Copy[T]`. Every `base` collection requires it.
- **Zero-size types** (`()`, empty structs) have no memory form: no `sizeof`, no storage. Use `bool`
  as a placeholder value (`Set` is a `Map[K, bool]`).
- **Expressions as statements.** `f(x);` discards a Copy result. A block's value is its last
  expression, without a `;`. `return` is allowed anywhere; a function whose type is `!` (such as
  `fail`) can stand in any expression position.
- **`if` and `match` are expressions** and need an `else`/every case when their value is used.
- **Closures** `|x| x + 1u32` have types `closure(u32) -> u32` as parameters; a closure may read Copy
  locals of its function. Plain function values have types `fn(u32) -> u32`.
- **Modules.** Each `.kur` file is a module named by its path; `use package::name` imports one, and
  `lib.kur` lists them. `use package::syntax as S;` then `S::Expr`.
- **Statics.** There are no global variables. State lives in values passed in, usually a struct of
  handles.
