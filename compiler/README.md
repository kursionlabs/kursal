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
| `support::base` | | storage on the standard library's arena: the context `Cx!`, text, sequences, maps, sets, cells; `fail` |
| `support::system` | | arguments, files, directories, processes, the environment, the clock, C's `strtod` |
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

The compiler keeps what it builds in an arena of the standard library, `std::alloc::arena::Arena!['k]`.
`support::base::Cx!['k]` holds the arena with the one capability for its domain, `Exclusive!['k]`:

```kursal
forall 'k.
exposed struct Cx! {
    tag: Exclusive!['k],
    arena: Arena!['k],
}
```

`main` makes one with `Cx::new()` for the command it runs, passes it down by place, `&cx`, and
destroys it when the command is done; a command that fails stops the process from inside. Every
value of the storage is a `Copy` view branded `'k`, built on the standard library's arena structures:

- `Text['k]` is a `std::raw::Span['k]`: the bytes of a literal, of a file copied into the arena, or of
  text the compiler built.
- `Seq[T, 'k]` is a `std::collections::arena_vec::ArenaVec[T, 'k]`: a header in the arena naming
  segments of elements that double in size, so elements never move and growing copies nothing. A
  `Map[K, V, 'k]` keeps its entries in insertion order in one `ArenaVec` and its hash slots in an
  arena slice (`Arena::alloc_slice`), made at the first key, under a header of its own; a
  `Set[K, 'k]` is a `Map[K, bool, 'k]`.
- `Cell[T, 'k]` is a `std::mem::Ref[T, 'k]` to one value in the arena.

Each handle names a header in the arena, so a change made through one copy of a `Seq`, `Map`, `Set`
or `Cell` is seen through every copy, as with Python's lists, dicts and sets. Elements are `Copy`
values, read and written whole, and no reference into storage is handed out, so growing a sequence
never leaves one dangling. Every operation that reads or writes takes `&cx`: a read presents
`cx.tag` (`Ref::copy`, `ArenaVec::get`), a write presents it too (`Ref::replace`, `ArenaVec::set`),
and one that may allocate also lends `cx.arena` to the library (`Arena::alloc`,
`Arena::alloc_slice`). `support::base` has no `unsafe` and no raw pointers; the brand and the
capability are what make its views safe to read.

Every type that holds storage carries the brand (`kurs::core::Type['k]`, `syntax::ast::Expr['k]`,
`lower::lowering::FnLower['k]`), and every function that touches it is `forall 'k.` and takes
`&cx: Cx!['k]`, first or right after `self`. The brand is always a parameter, never one brand for
the whole compiler, so a phase's data could live in an arena of its own.

| Python | Kursal |
|---|---|
| `str` | `Text['k]` (a byte view; `text(&cx, "literal")`, `t.is(&cx, "literal")`, `t.eq(&cx, u)`, `t.len()`, `slice`, `find`, `split`, `starts_with`) |
| building a `str` | `Out::new(&cx).put(&cx, "a").add(&cx, t).u64(&cx, n).done(&cx)` |
| `list`, `tuple` of values | `Seq[T, 'k]` (`push`, `get`, `set`, `len`, `pop`, `last`, `copy`, `slice`, `append`, `insert`, `remove`, `sort`; each takes `&cx`) |
| `dict` | `Map[K, V, 'k]` (insertion-ordered like `dict`: `set`, `get` → `Option`, `at`, `get_or`, `has`, `remove`, `keys`, `values`) |
| `set` | `Set[K, 'k]` (insertion-ordered: `add`, `has`, `remove`, `items`) |
| a mutable attribute | a field holding a `Cell[T, 'k]` (`get`, `set`), a `Seq` or a `Map` |
| a dataclass | an `exposed struct` branded `'k`; a `HashIn[X['k], Cx!['k]]` instance if it is a map key |
| a union of classes | an `exposed enum` whose variants carry structs |
| `raise KursError(message, pos)` | `fail(&cx, message)` with the same text; see below |
| `Optional[T]` | `Option[T]` |
| a recursive field | `Cell[T, 'k]` (or a `Seq`), so the type has a finite size |
| `x is y` on lists, dicts, cells | `a.address() == b.address()` |

Nothing is freed until the arena is, so a loop that builds throwaway values on every step costs
memory for the rest of the command. A sequence takes up to twice its length in room; a map's slots
are made again, twice as many, as it grows, and the old ones stay; `Out::done` copies text longer
than 32 bytes into one block. Where a pure computation is repeated on the same input, cache it in
a map (`Backend::ctype`, `Types::unfold`, the layout walk's finished keys). In a hot loop, read a
map's or set's keys in place with `positions` and `entry` rather than copying them with `keys` or
`items`, and compare text without splitting it.

Map keys are hashed and compared through the standard library's `HashIn[K, Cx!['k]]`, which takes
the context by value and hands it back; `key_hash(&cx, key)` and `key_same(&cx, a, b)` call it
with the context by place. The standard library has the instances for the integers, `bool` and
tuples of keys, `support::base` those for `Text` and `Seq`s of keys, and `kurs::core` adds `Type`,
`Param`, `Premise` and `ModuleId`. `kurs::core::same` is structural type equality (Python's `==` on
frozen dataclasses), `kurs::core::hash` its hash.

A class whose methods mutate `self` becomes a struct of handles (cells, sequences, maps) whose
methods take `self` by value and `&cx`: the copies share the same storage. Its constructor allocates
them.

## Errors

The compiler stops at the first error: `support::base::fail(&cx, message)` prints the message and exits
with status 1, leaving the arena to the operating system; `fail_static(literal)` needs no context. A message is exactly what the reference compiler prints for the same error: `KursError`'s
`str()`, which is `line:col: message` when it carries a position and the message alone otherwise,
and the file prefix the front end adds (`resolve.located`). Tests compare messages.
`types::ops::kurs_error(&cx, message, pos)` stops with `KursError(message, pos)`'s text; a `Pos` with line 0
stands for no position.

## Fresh names

`types.fresh` numbers renamed binders (`a#3`) from one counter for the whole compilation. Here that
counter is a `types::ops::Fresh['k]`, made once and passed to everything that may rename a binder: `subst`,
`unify`, `match_head`, `Types`, `Formation`, `check_type_expansion`, `check_coherence`, `Signatures`,
`Infer`, `lower`, `check_program`, `emit_c`, `emit_kurs`. Passing the
same one everywhere numbers names as the reference compiler does.

Where the reference wraps a phase in `with resolver.located(module, pos):`, call
`r.locate(&cx, module, pos)` before it and `r.unlocate(&cx)` after, and report errors inside it with
`r.fail(&cx, message, pos)` (a `pos` on line 0 is none): it adds the file, and the item's position when
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
  package is the caller's or one it depends on, directly or not. Import the trait (`use std::hash::HashIn;`)
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
- **Places.** A linear value is lent by place: `fn f(&cx: Cx!['k])` is called `f(&cx)`, and the
  callee's changes to `cx` are the caller's. Fields of a lent struct can be lent in one call
  (`&cx.arena.alloc(&cx.tag, value)`), and a call may lend `cx` again in its arguments
  (`f(&cx, g(&cx, x))`). A function may move a place parameter out if it puts a value back before it
  returns: `let (back, state) = HashIn[K, Cx!['k]]::hash_in(cx, key, state); cx = back;`. A
  `match`, `&&` or `if` may lend it in every branch.
- **Brands.** `forall 'k.` binds a brand like a type parameter, and a type that holds branded storage
  takes it as its last argument (`Seq[Text['k], 'k]`). A brand is minted by a constructor that
  returns `exists 'k. ...` and is opened by `let exists 'k. cx = Cx::new();`; a trait may take one
  as a parameter (`forall D, 'k. trait Discharge`, `instance Discharge[Checker['k], 'k]`).
- **Zero-size types** (`()`, empty structs) have no memory form: no `sizeof`, no storage. Use `bool`
  as a placeholder value (`Set` is a `Map[K, bool]`).
- **Expressions as statements.** `f(x);` discards a Copy result. A block's value is its last
  expression, without a `;`. `return` is allowed anywhere; a function whose type is `!` (such as
  `fail`) can stand in any expression position.
- **`if` and `match` are expressions** and need an `else`/every case when their value is used.
- **Closures** `|x| x + 1u32` have types `closure(u32) -> u32` as parameters; a closure may read Copy
  locals of its function, but captures nothing linear, so one that touches storage takes the
  context as a place parameter: `closure[Env](&_: Cx!['k], Type['k]) -> Type['k]`, written
  `|&cx, child: Type['k]| zonk(&cx, child)` and called `transform(&cx, ty)`. Plain function values
  have types `fn(u32) -> u32`; a brand-generic function passes as one (`items.sort(&cx, text_before)`
  for `fn(&_: Cx!['k], Text['k], Text['k]) -> bool`).
- **Modules.** Each `.kur` file is a module named by its path; `use package::name` imports one, and
  `lib.kur` lists them. `use package::syntax::ast as S;` then `S::Expr`. A directory is a module whose
  `lib.kur` lists its children, as `compiler/lib.kur` lists the phases.
- **Statics.** There are no global variables. State lives in values passed in, usually a struct of
  handles, and in the context `&cx` beside them.
