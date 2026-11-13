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
| `support::base` | | `fail`, and hashing the compiler's own keys; its storage is the standard library's (see below) |
| `support::system` | | arguments, files, directories, processes and the environment, through the standard library |
| `syntax::ast`, `syntax::parser` | `syntax.py`, `parser.py` | the surface AST and the parser |
| `syntax::dump` | | the AST as text |
| `resolve::resolver` | `resolve.py` | manifests, the package graph, and what every name names |
| `resolve::dump` | | a resolved program as text |
| `kurs::core`, `kurs::sexpr` | `core.py`, `sexpr.py` | the KURS core and S-expressions |
| `kurs::serial` | `kurs.py` | serialized KURS: its reader and writer |
| `kurs::readable` | `readable.py` | readable KURS |
| `kurs::transfer` | | a core program copied from one arena into another |
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
| `main` | `__main__.py` | the command line, and an arena for each group of phases |

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
limited to 6 GB of address space and a timeout. When valgrind is installed, the compiler builds
`examples/lisp` under its leak check, which fails on any error and on any byte not freed; with
`KURSAL_SELFHOST=1`, the self-built compiler builds `compiler/` under it too (a long run, limited to
14 GB).

## Storage

The compiler keeps what it builds in arenas of the standard library. Each group of phases has one
`std::alloc::arena::Domain!['k]`: an arena with the one capability for its domain.

```kursal
forall 'k.
exposed struct Domain! {
    tag: Exclusive!['k],
    arena: Arena!['k],
}
```

It lends both to the standard library: every arena structure takes the context by place, `&cx`,
bounded by `core::cap::Access` where it reads or writes the domain and by `alloc::arena::Allocate`
where it allocates there, so `&cx` is all a call needs. Every value of the storage is a `Copy` view
branded `'k`:

- Text is a `std::raw::Span['k]`: the bytes of a literal (`Span::from_static(&cx, "x")`), of a file
  copied into the arena, or of text the compiler built.
- A sequence is a `std::collections::arena_vec::ArenaVec[T, 'k]`: a header in the arena naming
  segments of elements that double in size, so elements never move and growing copies nothing.
- A map is a `std::collections::arena_map::ArenaMap[K, V, 'k]`, in insertion order like Python's
  `dict`: its entries in one `ArenaVec`, its hash slots in an arena slice made at the first key; a
  set is an `ArenaSet[K, 'k]` on top of one.
- One mutable value, and a recursive field, is a `std::mem::Ref[T, 'k]` (`Ref::new(&cx, value)`).
- The scopes of inference, opened and closed as a body is walked, are a persistent
  `std::collections::list::List` of levels, innermost first.

Each handle names a header in the arena, so a change made through one copy of a vec, map, set or
`Ref` is seen through every copy, as with Python's lists, dicts and sets. Elements are `Copy`
values, read and written whole, and no reference into storage is handed out, so growing a sequence
never leaves one dangling. `support::base` has no storage of its own: it keeps `fail`, and the
hashing helpers for the compiler's own key types.

Every type that holds storage carries the brand (`kurs::core::Type['k]`, `syntax::ast::Expr['k]`,
`lower::lowering::FnLower['k]`), and every function that touches it is `forall 'k.` and takes
`&cx: Domain!['k]`, first or right after `self`. The brand is always a parameter, never one brand
for the whole compiler, so each group of phases runs in an arena of its own.

### An arena per group of phases

`main` opens three arenas, one after another, each under a brand of its own:

| Group | Phases | Hands on |
|---|---|---|
| front end, `'f` | the command line; parsing, resolving, signatures, inference, lowering with place flow and promotion; or reading KURS | `main::Handoff['f]`: the core program, the `Fresh` counter, the entry's name, `build`'s output path |
| core, `'c` | checking | the program again, and `Checker.typed`, the type of every SSA value |
| back end, `'b` | mangling and C | the C, printed or written; then `cc` runs |

The core program is where the pipeline is narrowest: it is self-contained (the `check`, `c` and
`readable` commands start from it, read from KURS), while the syntax trees, the resolver, the
signatures and what inference found are not needed after lowering. Lowering stays in the front end
because place flow and promotion report errors through the resolver, in their files.

A group's hand-off is copied into the next arena with `kurs::transfer::copy_program` (every type,
function, block, instruction, declaration and data form, maps in the same order),
`check::checker::copy_typed`, `Span::copy_to(&from, &to)` (read under the first arena's
capability) and `types::ops::carry_fresh`; then the first arena is destroyed. A
part shared by two places in the original is copied for each: nothing changes a program once it is
lowered. `fail` still stops the process from inside, leaving whichever arenas are open to the
operating system; on success, every arena is destroyed and every byte the compiler allocated is
freed, which `tests/test_compiler.py` checks under valgrind.

Each arena is opened once, in `main`, and `front_end`, `check_program` and `back_end` take it by
place. The backends specialize a generic function once for type arguments that differ only in
brands (`backend::mangle::unbranded`), and brands are erased from C types, so code that more than
one group runs (the `support` and `types` modules, `kurs::core`, the checker that place flow uses)
is made once, whatever arena it runs in.

### Scratch state

What a walk needs only while it runs, and that never escapes into its result, lives in the
standard library's linear collections, `OwnedVec!` and `OwnedMap!`, on the heap, and is freed when
the walk ends: the worklist of place flow (`PlaceFlow::plan`), the numbering, stack and active set
of Tarjan's algorithm (`types::termination::components`), the open lists of the KURS reader
(`kurs::sexpr::read`), the variables seen while collecting inference variables (`types::unify`),
the variants a match has covered (`FnLower::match_coverage`), and the walk of package dependencies
(`Infer::depends_on`). A scratch map with `Copy` keys that point into the arena hashes them under
`&cx` (`insert_in`, `get_in`, `remove_in`). State that lives in a struct of handles shared with
the rest of a phase stays in the arena, and so does everything that ends up in a result.

### Text being built

Text is built in the standard library's linear `Buffer!`, on the heap, threaded by value through
the chain of pushes: `Buffer::new().push_static("(").push_bytes(&cx, t).push_u64(n)`. A span is
read under the domain's capability (`ReadBytes[Domain!['k], Span['k]]`), so `push_bytes(&cx, t)`
takes it whole. Text that must outlive the builder ends in the arena through `Buffer::save`,
`Buffer::new().push_static("a").push_bytes(&cx, t).save(&cx)`, which copies the finished bytes into
one block of exactly their length and frees the buffer; a builder that is dropped on a path is
destroyed there (`out.destroy()`), and the leak check under valgrind would see one that is not.

A helper that adds to text being built either takes the buffer and returns it
(`fn str(&cx, out: Buffer!, t: Span['k]) -> Buffer!`, called `out = str(&cx, out, t)`), or, when
it writes many lines from deep in a walk, takes it by place: `resolve::dump::Walk` and
`infer::dump::Walk` pass `&out: Buffer!` to every method, `sigs::dump::sig` takes `&out`, and the
C of a function's body is written into `&lines: Buffer!`, which `backend::cgen::line` appends to
and which every method that emits an instruction or a terminator takes. A function's body is
copied into the arena once, when it is finished (its declarations come first, and emitting the
lines may add to them), and `emit_c` returns the whole C as a `Buffer!`, written to the file or
printed with `std::io::print_buffer`.

| Python | Kursal |
|---|---|
| `str` | `Span['k]` (a byte view; `Span::from_static(&cx, "literal")`, `t.is(&cx, "literal")`, `t.eq(&cx, u)`, `t.cmp(&cx, u)`, `t.len()`, `slice`, `skip`, `find`, `split`, `starts_with`, `byte_or_zero`) |
| building a `str` | `Buffer::new().push_static("a").push_bytes(&cx, t).push_u64(n).save(&cx)`; in a loop, `var out = Buffer::new();` and `out = out.push_static(", ");` |
| `list`, `tuple` of values | `ArenaVec[T, 'k]` (`push`, `at`, `get` → `Option`, `set`, `len`, `pop` → `Option`, `clone`, `slice`, `skip`, `append`, `insert`, `remove`, `sort_by`, `contains`, `each`, `any`, `fold`, ...; each takes `&cx`) |
| `dict` | `ArenaMap[K, V, 'k]` (insertion-ordered like `dict`: `insert`, `get` → `Option`, `at`, `get_or`, `contains`, `remove`, `keys`, `values`, `each`, `any`) |
| `set` | `ArenaSet[K, 'k]` (insertion-ordered: `insert`, `contains`, `remove`, `keys`, `each`, `any`, `all`) |
| scopes pushed and popped by a walk | `List[T, 'k]` in a `Ref` (`cons`, `tail`, `first`, `find_map`, `reverse`, `each`) |
| a worklist or visited set local to a walk | `OwnedVec![T]` (`push`, `pop` → `Popped`, `get`, `len`, `free`), `OwnedMap![K, V]` (`insert`, `get`; `insert_in(&cx, ...)` for keys in the arena; `free`) |
| a mutable attribute | a field holding a `Ref[T, 'k]` (`copy`, `set`), an `ArenaVec` or an `ArenaMap` |
| a dataclass | an `exposed struct` branded `'k`; a `HashIn[X['k], Domain!['k]]` instance if it is a map key |
| a union of classes | an `exposed enum` whose variants carry structs |
| `raise KursError(message, pos)` | `fail(&cx, message)` with the same text; see below |
| `Optional[T]` | `Option[T]` |
| a recursive field | `Ref[T, 'k]` (or an `ArenaVec`), so the type has a finite size |
| `x is y` on lists, dicts, refs | `a.address() == b.address()` |

Nothing in an arena is freed until the arena is, so a loop that builds throwaway values on every
step costs memory for the rest of its group. A sequence takes up to twice its length in room; a
map's slots are made again, twice as many, as it grows, and the old ones stay. Text built in a
`Buffer!` costs the arena only its final length, in `save`; one buffer pushed into another
(`push_buffer`) costs it nothing. Where a pure computation is repeated on the same input, cache it
in a map (`Backend::ctype`, `Types::unfold`, the layout walk's finished keys). In a hot
loop, read a map's or set's keys in place with `positions` and `key_at`, or `each` and `any`,
rather than copying them with `keys`, and compare text without splitting it.

Map keys are hashed and compared through the standard library's `HashIn[K, Domain!['k]]`, which
borrows the context by place: `HashIn[K, Domain!['k]]::hash_in(&cx, key, state)`, or
`key_hash(&cx, key)` and `key_same(&cx, a, b)`. The standard library has the instances for the
integers, `bool`, tuples, spans and `ArenaVec`s of keys, and `kurs::core` adds `Type`, `Param`,
`Premise` and `ModuleId`. `kurs::core::same` is structural type equality (Python's `==` on frozen
dataclasses), `kurs::core::hash` its hash.

A class whose methods mutate `self` becomes a struct of handles (refs, vecs, maps) whose methods
take `self` by value and `&cx`: the copies share the same storage. Its constructor allocates them.

### The operating system

`support::system` copies paths and command lines out of the arena into `Buffer!`s, calls the
standard library and its `libc` package, and copies what comes back into the arena: arguments
(`libc::process::arguments`), whole files (`libc::fs::open_read`, `std::io::File::read_to_buffer`),
writing a file (`libc::fs::create`, `File::write_span`), directories, real paths and the
environment (`libc::fs`), and running `cc` (`libc::process::run`). Times come from
`std::time::Instant`, and float literals are read with `std::float::parse_f64` and taken apart with
`f64::to_bits` and `f64::to_f32`. The compiler has no `unsafe` operation and no `extern`
declaration of its own.

## Errors

The compiler stops at the first error: `support::base::fail(&cx, message)` prints the message and exits
with status 1, leaving the open arenas to the operating system; `fail_static(literal)` needs no context. A message is exactly what the reference compiler prints for the same error: `KursError`'s
`str()`, which is `line:col: message` when it carries a position and the message alone otherwise,
and the file prefix the front end adds (`resolve.located`). Tests compare messages.
`types::ops::kurs_error(&cx, message, pos)` stops with `KursError(message, pos)`'s text; a `Pos` with line 0
stands for no position.

## Fresh names

`types.fresh` numbers renamed binders (`a#3`) from one counter for the whole compilation. Here that
counter is a `types::ops::Fresh['k]`, made once and passed to everything that may rename a binder: `subst`,
`unify`, `match_head`, `Types`, `Formation`, `check_type_expansion`, `check_coherence`, `Signatures`,
`Infer`, `lower`, `check_program`, `emit_c`, `emit_kurs`. Passing the
same one everywhere numbers names as the reference compiler does; `carry_fresh` carries it into the
next group's arena, counting on from where it stopped.

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
  an `ArenaVec` and `at`.
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
- **Places.** A linear value is lent by place: `fn f(&cx: Domain!['k])` is called `f(&cx)`, and the
  callee's changes to `cx` are the caller's. Fields of a lent struct can be lent in one call
  (`&cx.arena.alloc(&cx.tag, value)`), and a call may lend `cx` again in its arguments
  (`f(&cx, g(&cx, x))`). A function may move a place parameter out if it puts a value back before it
  returns: `let { tag, arena } = cx; ...; cx = Domain.{ tag, arena };`. A
  `match`, `&&` or `if` may lend it in every branch.
- **Brands.** `forall 'k.` binds a brand like a type parameter, and a type that holds branded storage
  takes it as its last argument (`ArenaVec[Span['k], 'k]`). A brand is minted by a constructor that
  returns `exists 'k. ...` and is opened by `let exists 'k. cx = Domain::new();`; a trait may take one
  as a parameter (`forall D, 'k. trait Discharge`, `instance Discharge[Checker['k], 'k]`).
- **Zero-size types** (`()`, empty structs) have no memory form: no `sizeof`, no storage. Use `bool`
  as a placeholder value (`ArenaSet` is an `ArenaMap[K, bool]`).
- **Expressions as statements.** `f(x);` discards a Copy result. A block's value is its last
  expression, without a `;`. `return` is allowed anywhere; a function whose type is `!` (such as
  `fail`) can stand in any expression position.
- **`if` and `match` are expressions** and need an `else`/every case when their value is used.
- **Closures** `|x| x + 1u32` have types `closure(u32) -> u32` as parameters; a closure may read Copy
  locals of its function, but captures nothing linear, so one that touches storage takes the
  context as a place parameter: `closure[Env](&_: Domain!['k], Type['k]) -> Type['k]`, written
  `|&cx, child: Type['k]| zonk(&cx, child)` and called `transform(&cx, ty)`. Plain function values
  have types `fn(u32) -> u32`, and a closure parameter does not take one: a comparator is passed
  as a closure that calls it, `items.sort_by(&cx, |&cx, a, b| a.cmp(&cx, b))`.
- **Modules.** Each `.kur` file is a module named by its path; `use package::name` imports one, and
  `lib.kur` lists them. `use package::syntax::ast as S;` then `S::Expr`. A directory is a module whose
  `lib.kur` lists its children, as `compiler/lib.kur` lists the phases.
- **Statics.** There are no global variables. State lives in values passed in, usually a struct of
  handles, and in the context `&cx` beside them.
