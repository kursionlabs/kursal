# compiler

The Kursal compiler, written in Kursal. It is built on the design of the reference compiler,
[`reference-compiler/kursal`](../reference-compiler/kursal): each module here translates a Python
module, and must accept and reject the same programs, with the same messages, and produce the same
output.

The compiler has one directory per phase, in pipeline order below; `support` is the layer beneath
them all. A phase's `dump` module prints what the phase computed, as text, for comparison with the
reference in the tests.

| Module | Translates | |
|---|---|---|
| `support::base` | | storage: text, sequences, maps, sets, cells; `fail` |
| `support::system` | | arguments, files, directories, processes, the environment |
| `syntax::ast`, `syntax::parser` | `syntax.py`, `parser.py` | the surface AST and the parser |
| `syntax::dump` | | the AST as text |
| `resolve::resolver` | `resolve.py` | manifests, the package graph, and what every name names |
| `resolve::dump` | | a resolved program as text |
| `kurs::core`, `kurs::sexpr` | `core.py`, `sexpr.py` | the KURS core and S-expressions |
| `kurs::serial` | `kurs.py` | serialized KURS: its reader and writer |
| `kurs::readable` | `readable.py` | readable KURS |
| `types::ops`, `types::unify` | `types.py`, `unify.py` | substitution, equality, shapes; unification over `kurs::core::Meta` |
| `types::literals`, `formation`, `layout`, `termination`, `coherence` | the same names | checks of literals, types, declarations and instances |
| `types::selftest` | | `kursal selftest-types`, the type modules on small cases, for the tests |
| `sigs::signatures` | `sigs.py` | signatures: type declarations, function, method, extern and trait signatures, instances, folded constants |
| `sigs::dump` | | signatures as text |
| `infer::inference` | `infer.py` | type inference of function bodies: each node's type, what each call calls, local bindings and captures |
| `infer::dump` | | what inference found, as text |
| `lower::lowering`, `lower::placeflow`, `lower::promote` | `lower.py`, `placeflow.py`, `promote.py` | lowering inferred bodies to core: control flow, places and their contracts, branded locals as SSA values |
| `check::checker` | `check.py` | the KURS checker; it records every SSA value's type by `FunKey` |
| `backend::mangle`, `backend::cgen` | `mangle.py`, `cgen.py` | symbol names (with SHA-256); checked KURS to C |
| `main` | `__main__.py` | the command line |

## Building and testing

From `reference-compiler/`:

```sh
uv run python tools/build.py ../compiler /tmp/kursal --times   # build with the reference compiler
/tmp/kursal parse ../examples/lisp/lib.kur                      # one command of the compiler
uv run python tools/build.py ../examples/wc /tmp/wc --kurs /tmp/wc.kurs   # also write its KURS
/tmp/kursal kurs /tmp/wc.kurs                                   # read KURS and write it back
/tmp/kursal check /tmp/wc.kurs                                  # check it: silent when it is sound
/tmp/kursal c /tmp/wc.kurs --entry example_wc::main             # its C, as `python -m kursal --emit-c`
/tmp/kursal readable /tmp/wc.kurs                               # readable KURS
/tmp/kursal lower ../examples/wc                                 # the package lowered to KURS
/tmp/kursal build ../examples/wc -o /tmp/wc2 --times             # the whole pipeline, as tools/build.py
/tmp/kursal build ../compiler -o /tmp/kursal2                     # the compiler builds itself
uv run python tools/test.py tests.test_compiler                  # compare with the reference compiler
KURSAL_SELFHOST=1 uv run python tools/test.py tests.test_compiler   # also check the self-build's fixpoint
```

`tests/test_compiler.py` builds the compiler once per change to its sources (cached in
`reference-compiler/.build/`) and compares its output with the reference compiler's. It also caches
there every package's lowered KURS with the reference's C and readable KURS for it, and the core
programs the reference's other test modules check (gathered by running those tests with
`check_program` watched) with the reference's verdicts; the reference's lowering and whole-pipeline C
of every package, bad example and `compiler/`; and the packages the other test modules lower
(gathered with `lower` watched) with the reference's lowering of each. Each run of the compiler is
limited to 6 GB of address space and a timeout.

## Storage

A compiler builds one web of values and keeps it until it exits, so nothing in `support::base` is freed and
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

Nothing is freed, so a loop that builds throwaway values on every step costs memory for good. In a
hot loop, read a map's or set's keys in place with `positions` and `entry` rather than copying them
with `keys` or `items`, and compare text without splitting it.

Map keys need an instance of `support::base::Key`; `Text`, `u32`, `usize`, `bool`, 2- and 3-tuples and
`Seq`s of keys have one, and `kurs::core` adds `Type`, `Param`, `Premise` and `ModuleId`. `kurs::core::same` is
structural type equality (Python's `==` on frozen dataclasses), `kurs::core::hash` its hash.

A class whose methods mutate `self` becomes a struct of handles (cells, sequences, maps) whose
methods take `self` by value: the copies share the same storage. Its constructor allocates them.

## Errors

The compiler stops at the first error: `support::base::fail(message)` prints the message and exits with
status 1. A message is exactly what the reference compiler prints for the same error: `KursError`'s
`str()`, which is `line:col: message` when it carries a position and the message alone otherwise,
and the file prefix the front end adds (`resolve.located`). Tests compare messages.
`types::ops::kurs_error(message, pos)` stops with `KursError(message, pos)`'s text; a `Pos` with line 0
stands for no position.

## Fresh names

`types.fresh` numbers renamed binders (`a#3`) from one counter for the whole compilation. Here that
counter is a `types::ops::Fresh`, made once and passed to everything that may rename a binder: `subst`,
`unify`, `match_head`, `Types`, `Formation`, `check_type_expansion`, `check_coherence`, `Signatures`,
`Infer`, `lower`, `check_program`, `emit_c`, `emit_kurs`. Passing the
same one everywhere numbers names as the reference compiler does.

Where the reference wraps a phase in `with resolver.located(module, pos):`, call
`r.locate(module, pos)` before it and `r.unlocate()` after, and report errors inside it with
`r.fail(message, pos)` (a `pos` on line 0 is none): it adds the file, and the item's position when
the error has none of its own. Lookups that the reference may catch (`ResolveError`) return a
`Result[Target, Problem]` instead of stopping (`Resolver.lookup`); `Resolver.need` stops.

The standard library is found as the reference finds it, in `$KURSAL_LIB`; without it, in `lib`
beside the directory holding the compiler's binary (`PREFIX/bin/kursal` reads `PREFIX/lib`).

## The language, for a compiler author

These are the places where Kursal differs from what one might expect.

- **Names.** Keywords cannot be field, method or variable names: `use export module struct tuple
  enum fn extern impl trait instance const forall where as exposed unsafe closure exists foreach of in
  for while if else match let var return pack sizeof true false self _`. Fields here are named
  `is_unsafe`, `home`, `quantifier`, `trait_name` and the like instead.
- **Method lookup sees what the caller sees.** `x.name()` looks for `name` among the methods of
  `x`'s type, and otherwise among the single-parameter traits visible in the calling module
  (declared there, imported by name or glob, or re-exported to it), and the methods of blanket impls
  (`forall I where Iterator[I -> T]. impl I`) whose bounding traits are visible there and whose
  package is the caller's or one it depends on, directly or not. Import the trait (`use package::support::base::Key;`)
  to call its methods with a dot; two visible traits sharing a method name make such calls ambiguous,
  and are called as `Trait::method(x)`, which works whether or not the trait is imported. Calling
  the standard library's iterator adapters (`range(0, n).map(...)`) needs `core` as a direct
  dependency and `Iterator` imported.
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
  false), needs `Copy[T]`. Every `support::base` collection requires it.
- **Zero-size types** (`()`, empty structs) have no memory form: no `sizeof`, no storage. Use `bool`
  as a placeholder value (`Set` is a `Map[K, bool]`).
- **Expressions as statements.** `f(x);` discards a Copy result. A block's value is its last
  expression, without a `;`. `return` is allowed anywhere; a function whose type is `!` (such as
  `fail`) can stand in any expression position.
- **`if` and `match` are expressions** and need an `else`/every case when their value is used.
- **Closures** `|x| x + 1u32` have types `closure(u32) -> u32` as parameters; a closure may read Copy
  locals of its function. Plain function values have types `fn(u32) -> u32`.
- **Modules.** Each `.kur` file is a module named by its path; `use package::name` imports one, and
  `lib.kur` lists them. `use package::syntax::ast as S;` then `S::Expr`. A directory is a module whose
  `lib.kur` lists its children, as `compiler/lib.kur` lists the phases.
- **Statics.** There are no global variables. State lives in values passed in, usually a struct of
  handles.
