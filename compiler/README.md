# compiler

The Kursal compiler, written in Kursal. It is built on the design of the reference compiler,
[`reference-compiler/kursal`](../reference-compiler/kursal): each module here translates a Python
module, and must accept and reject the same programs, with the same messages, and produce the same
output.

The compiler has one directory per phase, in pipeline order below; `support` is the layer beneath
them all, and the standard library (`std`, `toml`) beneath that. A phase's `dump` module prints what
the phase computed, as text, for comparison with the reference in the tests.

| Module | Translates | |
|---|---|---|
| `support::base` | | `fail`, and `emit`, which writes the output or fails; its storage is the standard library's (see below) |
| `syntax::ast`, `syntax::parser` | `syntax.py`, `parser.py` | the surface AST and the parser |
| `syntax::dump` | | the AST as text |
| `resolve::resolver` | `resolve.py` | manifests (read by the standard library's `toml`), the package graph, and what every name names |
| `resolve::dump` | | a resolved program as text |
| `doc::model` | | what the documentation site shows and where: each package's modules, its own and re-exported, every type's methods, every instance, each item's URL |
| `doc::text` | | escaping, the URL spelling of names, relative links, summary lines, and Kursal highlighted with its names linked |
| `doc::generator` | | the documentation site's content (`kursal doc`): module, type, trait and source pages, and the search index; see `docs/README.md` |
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
| `lower::lowering`, `lower::placeflow`, `lower::promote` | `lower.py`, `placeflow.py`, `promote.py` | control flow, local SSA values and block arguments, places and their contracts |
| `check::checker` | `check.py` | the KURS checker; it records every SSA value's type by `FunKey` |
| `backend::mangle`, `backend::plan`, `backend::cgen` | `mangle.py`, `plan.py`, `cgen.py` | symbol names (with SHA-256); what a function's C leaves out; checked KURS to C |
| `main` | `__main__.py` | the command line, and an arena for each group of phases |

## Building and testing

From the repository root, `scripts/bootstrap.sh` builds the compiler with the reference compiler
(`bin/stage0/kursal`), then has that compiler build itself into `bin/kursal`. With `--check` it also
has `bin/kursal` build itself once more and requires the same C.

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

`build --times` reports wall-clock milliseconds for resolution, signatures, inference, lowering,
checking, C generation, and the copies and arena releases between those stages. For executable
builds it also reports writing C, preparing the C compiler commands, compiling the files of a split
build in parallel (`cc compile`), and the last C compiler run (`cc`: the link, or the whole build
with `--single-c`).
`front copy` moves the lowered program into the arena shared by the checker and C emitter.
Within C generation, `C setup`, `C functions`, `C definitions`, and `C assembly` show where the
backend spends its time; `C` remains the total for that stage.

`build` writes `a.out` unless `-o OUTPUT` names another destination. Its C goes to `a.out.build`
as a file per module: a header every file includes (`kursal-decls.h`: types, externs, data
declarations and every function's head), the data (`kursal-data.c`), C's `main` (`kursal-main.c`),
the thunks (`kursal-thunks.c`), and each module's functions, in files of about 512 KB
(`kursal.check.checker.c`, `kursal.check.checker.2.c`), a generic's instances with it. With Clang
(`-flto=thin`) or GCC (`-flto=auto`) as `$KURSAL_CC`, the files are compiled in parallel,
`$KURSAL_JOBS` at once or else one per processor, and linked with link-time optimization, which restores the inlining across files; a
build removes the files an earlier one left that it does not write. With `--single-c`, or another C
compiler, the C is one file, `a.out.c`, compiled whole: slower to build, a few percent faster to
run.
`build --emit-c` prints generated C, which also works for library packages. The compiler
passes each package's `[package] link_flags = ["-luv"]` entries as C compiler arguments
after the generated C and helper sources; flags declared by dependencies are included.
`[package] c_sources = ["native/uv.c"]` adds package-relative C sources to the same build.
The compiler prints a source line and caret below a located error; `KURSAL_DIAGNOSTICS=short`
retains the one-line form used by the reference comparison tests. `--help`, command `--help`,
and `--version` are available without building a package.

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
`std::arena::Domain!['k]`: an arena with the one capability for its domain.

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

- Text is a `std::str::Str['k]`: the bytes of a literal (`Str::from_static(&cx, "x")`), of a file
  copied into the arena, or of text the compiler built.
- A sequence is a `std::collections::arena_vec::ArenaVec[T, 'k]`: a header in the arena naming
  segments of elements that double in size, so elements never move and growing copies nothing.
- A map is a `std::collections::arena_map::ArenaMap[K, V, 'k]`, in insertion order like Python's
  `dict`: its entries in one `ArenaVec`, its hash slots in an arena slice made at the first key; a
  set is an `ArenaSet[K, 'k]` on top of one.
- One mutable value, and a recursive field, is a `std::mem::Ref[T, 'k]` (`std::arena::ref_in(&cx, value)`).
- The scopes of inference, opened and closed as a body is walked, are a persistent
  `std::collections::list::List` of levels, innermost first.

Each handle names a header in the arena, so a change made through one copy of a vec, map, set or
`Ref` is seen through every copy, as with Python's lists, dicts and sets. Elements are `Copy`
values, read and written whole, and no reference into storage is handed out, so growing a sequence
never leaves one dangling. `support::base` has no storage of its own: it keeps `fail`. Text is
joined with `ArenaVec::join`, vecs with `concat`, sets with `union` and `insert_all`.

Every type that holds storage carries the brand (`kurs::core::Type['k]`, `syntax::ast::Expr['k]`,
`lower::lowering::FnLower['k]`), and every function that touches it is `forall 'k.` and takes
`&cx: Domain!['k]`, first or right after `self`. The brand is always a parameter, never one brand
for the whole compiler, so each group of phases runs in an arena of its own.

### An arena per group of phases

`main` opens two arenas, each under a brand of its own.

| Group | Phases | Hands on |
|---|---|---|
| front end, `'f` | the command line; parsing, resolving, signatures, inference, lowering with place flow and promotion; or reading KURS | `main::Handoff['f]`: the core program, the `Fresh` counter, the entry's name, `build`'s output path |
| core, `'c` | checking, mangling, and C emission | the C, printed or written before `cc` runs |

The core program is where the pipeline is narrowest: it is self-contained (the `check`, `c` and
`readable` commands start from it, read from KURS), while the syntax trees, the resolver, the
signatures and what inference found are not needed after lowering. Lowering stays in the front end
because place flow and promotion report errors through the resolver, in their files.

The front end copies its handoff into the core arena with `kurs::transfer::copy_program`,
`std::arena::copy_str` and `types::ops::carry_fresh`, then destroys the front-end arena.
The copy preserves map order and copies shared parts separately because the lowered program
is read-only. The C emitter reads the checked program and `Checker.typed` in the core arena
and destroys that arena when it finishes. On failure, the operating system releases the
remaining arenas. `tests/test_compiler.py` checks success-path allocations under Valgrind.

Each arena is opened once in `main`. The backends specialize a generic function once for type
arguments that differ only in brands (`backend::mangle::unbranded`). Brands are erased from C
types, so the groups share the same generated code for modules such as `support`, `types` and
`kurs::core`, including the checker code that place flow uses.

### Scratch state

What a walk needs only while it runs, and that never escapes into its result, lives in the
standard library's linear collections, `OwnedVec!` and `OwnedMap!`, on the heap, and is freed when
the walk ends: the depth-first path and visited set that order place flow's walk
(`lower::placeflow::reverse_postorder`), the numbering, stack and active set
of Tarjan's algorithm (`types::termination::components`), the open lists of the KURS reader
(`kurs::sexpr::read`), the variables seen while collecting inference variables (`types::unify`),
the variants a match has covered (`FnLower::match_coverage`), and the walk of package dependencies
(`Infer::depends_on`). A scratch map with `Copy` keys that point into the arena hashes them under
`&cx` (`insert_in`, `get_in`, `remove_in`), and under the arena's seed (`OwnedMap!::new(seed(&cx))`,
`std::arena::seed`). State that lives in a struct of handles shared with
the rest of a phase stays in the arena, and so does everything that ends up in a result.

### The hash seed

`main` makes each arena with the process's hash seed, `std::process::hash_seed()`, which start-up
draws from the operating system, so every map's hash differs from one run to the next (see
`std::hash`). The compiler's output does not depend on it: an `ArenaMap` or `ArenaSet` lists its
keys in insertion order whatever their hashes, the scratch `OwnedMap!`s are only looked up and
freed, never listed, and nothing else reads a hash but a map. `scripts/bootstrap.sh --check`, whose
two builds of the compiler draw two seeds and must make the same C, and the comparisons with the
reference compiler check this. A map listed in hash order (`OwnedMap!::each` or `drain`) would make
the output change from run to run; sort what such a map lists, or keep the map in the arena.

Arena state that a walk refills again and again is kept and refilled in place rather than made
anew: `ArenaMap::clear` and `assign` (and `ArenaSet`'s) keep the room a map has, so it takes new
room only to hold more than it has held before. Place flow simulates each block in one scratch
`Places` (`PlaceFlow.working`, loaded from the state the block starts in) and copies it only when
the block changed it; its replay of the finished headers runs in the scratch too. The checker
checks each block in one scratch state (`Checker.working`). Blocks that declare no empty paths
share one read-only set. For other blocks, `Checker::empty_contract` draws a set from a pool
refilled for each function. `PathFacts` records a path's stored type and whether it is Copy when
these are independent of place state. It also tracks which block contracts still need checking.
The checker clears these facts when the roots or kinds maps change.

### Text being built

Text is built in the standard library's linear `Buffer!`, on the heap, threaded by value through
a chain of writes, the methods every writer has (`std::fmt::Writer`, which a module imports to
find them): `Buffer!::new().put("(").write(&cx, t).put(n)`. `put` prints a literal, a number, a
`bool` or another buffer, which it frees; `write` copies text or a byte slice, read under the
domain's capability (`ReadBytes[Domain!['k], Str['k]]`), and `write_byte` one byte. Where the
pieces read better as a sentence, an f-string writes them:
`f"{line}:{col}: {message}".write(&cx, Buffer!::new())`. Text that must outlive the builder ends
in the arena through `Buffer!::save`,
`Buffer!::new().put("a").write(&cx, t).save(&cx)`, which copies the finished bytes into
one block of exactly their length and frees the buffer; a builder that is dropped on a path is
destroyed there (`out.destroy()`), and the leak check under valgrind would see one that is not.

A helper that adds to text being built either takes the buffer and returns it
(`fn str(&cx, out: Buffer!, t: Str['k]) -> Buffer!`, called `out = str(&cx, out, t)`), or, when
it writes many lines from deep in a walk, takes it by place: `resolve::dump::Walk` and
`infer::dump::Walk` pass `&out: Buffer!` to every method, `sigs::dump::sig` takes `&out`, and the
C of a function's body is written into `&lines: Buffer!`, which `backend::cgen::line` appends to
and which every method that emits an instruction or a terminator takes. A function's body is
copied into the arena once, when it is finished (its declarations come first, and emitting the
lines may add to them), and `emit_c` returns the whole C as a `Buffer!`, written to the file or
printed with `support::base::emit_buffer`, which stops the compiler with status 1 when the
write fails.

| Python | Kursal |
|---|---|
| `str` | `Str['k]` (a byte view; `Str::from_static(&cx, "literal")`, `t.is(&cx, "literal")`, `t.eq(&cx, u)`, `t.cmp(&cx, u)`, `t.len()`, `slice`, `skip`, `find`, `split`, `starts_with`, `byte_or_zero`) |
| building a `str` | `Buffer!::new().put("a").write(&cx, t).put(n).save(&cx)`; in a loop, `var out = Buffer!::new();` and `out = out.put(", ");` |
| `list`, `tuple` of values | `ArenaVec[T, 'k]` (`push`, `at`, `get` → `Option`, `set`, `len`, `pop` → `Option`, `clone`, `slice`, `skip`, `append`, `insert`, `remove`, `sort_by`, `contains`, `each`, `any`, `fold`, ...; each takes `&cx`) |
| `dict` | `ArenaMap[K, V, 'k]` (insertion-ordered like `dict`: `insert`, `get` → `Option`, `at`, `get_or`, `contains`, `remove`, `keys`, `values`, `each`, `any`) |
| `set` | `ArenaSet[K, 'k]` (insertion-ordered: `insert`, `contains`, `remove`, `keys`, `each`, `any`, `all`) |
| scopes pushed and popped by a walk | `List[T, 'k]` in a `Ref` (`cons`, `tail`, `first`, `find_map`, `reverse`, `each`) |
| a worklist or visited set local to a walk | `OwnedVec![T]` (`push`, `pop` → `Option`, `get`, `len`, `destroy`), `OwnedMap![K, V]` (`insert`, `get`; `insert_in(&cx, ...)` for keys in the arena; `destroy`) |
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
`Buffer!` costs the arena only its final length, in `save`; one buffer put into another
(`out.put(b)`) costs it nothing. Where a pure computation is repeated on the same input, cache it
in a map (`Backend::ctype`, `Types::unfold`, the layout walk's finished keys). In a hot
loop, read a map's or set's keys in place with `positions` and `key_at`, or `each` and `any`,
rather than copying them with `keys`, and compare text without splitting it.

Map keys are hashed and compared through the standard library's `HashIn[K, Domain!['k]]`, which
borrows the context by place: `HashIn[K, Domain!['k]]::hash_in(&cx, key, state)`, or
`std::hash::key_hash(&cx, key, seed)` and `key_eq(&cx, a, b)`. The standard library has the instances for the
integers, `bool`, tuples, text and `ArenaVec`s of keys, and `kurs::core` adds `Type`, `Param`,
`Premise` and `ModuleId`. `kurs::core::same` is structural type equality (Python's `==` on frozen
dataclasses), `kurs::core::hash` its hash.

Types are values, not interned: two equal types may be built apart, so a type-keyed map hashes and
compares the structure. Both are kept cheap instead. A map entry keeps its key's hash bits, so a
probe compares keys only where the bits agree, and growing or cloning a map hashes nothing again.
`hash` includes each complete name, so names from different modules can land in different buckets
even when their lengths and suffixes agree. When two types share a vec of types or members, or a
`Ref`, `same` skips that part. Copies of a type commonly share these parts. Where a backend query is asked per
value, it is kept by the value's (block label, name) (`Backend::value_erased_in`), which hashes two
short names instead of a type. `types::ops::alpha_hashes` is a hash, under the arena's seed, that
alpha-equivalent types share; `Backend::request` compares a specialization's arguments only with earlier ones whose hash
is the same.

A class whose methods mutate `self` becomes a struct of handles (refs, vecs, maps) whose methods
take `self` by value and `&cx`: the copies share the same storage. Its constructor allocates them.

### The operating system

The compiler calls the standard library for everything it asks of the operating system, and hands
it paths as text in the arena, which it reads in place:

- **Arguments.** `main` is `fn main(args: std::process::Args) -> i32`, one of the entry shapes the
  backend accepts (`fn main()`, `fn main() -> i32` and `fn main() -> Result[(), std::io::Error]`,
  each with or without `args`); the C `main` the backend writes for it fills `Args` from `argv` and
  `argc` and returns the result as the exit status (see the reference compiler's README,
  "Packages"). `front_end` views each argument as text of the arena (`Str::from_static`), copying nothing.
- **Files.** `std::fs::read_to_arena` reads a source, a manifest or a KURS file into the arena;
  `std::fs::write` writes the C. `resolve::resolver` lists source directories with
  `std::fs::read_dir`, asks `std::fs::path_exists` for a `kur.toml`, and resolves paths as
  Python's `Path.resolve()` does with `std::fs::canonicalize`. A directory it cannot list, or a
  `kur.toml` it cannot tell is there or not (any error but ENOENT and ENOTDIR), stops it with
  `cannot read PATH (error ERRNO)`, as the reference's `os.walk` and `os.stat` errors do.
- **Manifests** are read by the standard library's `toml` package (`toml::parser::parse`) into an
  `ArenaMap` of `TomlValue`s. Its reading and its errors are `tomllib`'s, so a manifest that is not
  TOML fails on the line the reference names, `FILE:LINE: invalid manifest`; the resolver then
  checks the tables and strings it reads with the reference's messages.
- **The environment.** `std::env::variable` reads `KURSAL_LIB` and `KURSAL_CC`.
- **Running `cc`.** `back_end` builds a `std::process::Command!`, which copies each argument out
  of the arena, destroys the arena, and runs it (`posix_spawnp`); `Exit::Code.(0)` is success.

The standard library's directory, without `KURSAL_LIB`, is found from `/proc/self/exe` (Linux),
the one path the compiler names. Times come from `std::time::Instant`, and float literals are read
with `std::float::parse_f64` and taken apart with `f64::to_bits` and `f64::to_f32`. The compiler
has no `unsafe` operation and no `extern` declaration of its own.

## The C backend

`backend::cgen` translates checked KURS to one C file, as `cgen.py` does, byte for byte. The
translation gives a C variable to each SSA value the C reads, a C object to each place, and a
label to each block; on top of that it applies the steps below, all of them in both compilers. They
read the checked program and change only what C is written: the KURS is never rewritten, so the
checker's verdict covers the program the C translates.

What a function leaves out is decided first, from its KURS alone (`backend::plan`, `plan.py`):

- **Dead values.** A value is live when the C reads it: a kept instruction, a terminator, or an edge
  into a live block parameter (a fixpoint over the blocks). A pure instruction (constants, copies,
  loads, arithmetic other than division, construction and destruction, tags, casts between numbers,
  `pack` and `open`) whose results are all dead is not written. A store's unit result and a call's
  result that nothing reads are not assigned, and a dead block parameter is assigned by no edge.
- **Copies.** `copy` gets no variable: it reads its source's. Within one execution of a block every C
  variable is assigned once before it is read, and a block's parameters only on the edges into it.
- **Forwarded places.** A local place that exactly two instructions of the function name, a store
  of the whole place and, later in the same block, a load of the whole place, has no C object: the
  load reads the stored value's variable. Lowering makes one for each operand that waits while the
  next one is evaluated.
- **Return joins.** A block with no instructions that returns one of its parameters, or passes its
  parameters on to another such block, is not written: an edge into it returns the value it passes.
- **Residences.** A value is read where it lies (`Plan::residences`): a value loaded from a place in
  the place; a component taken apart from a place, or from a value lying somewhere, in its member
  there; any other value in its own C variable (the path `%v`, and `%v.f` for a component). Values
  that copy, open, pack or forward one lie where it does. The C reads the place or member instead of
  a copy of it, from where the value came out of it to its last read in its block, as long as nothing
  writes there (see "Why reading values where they lie is sound"). Then:
  - a store of a value into the place it lies in writes nothing, and a construction stored at once
    into a place writes only the components that do not lie at their member of it already;
  - a call of a **lend adapter** lends its callee the paths the borrowed values lie at, and the
    values it hands back lie there. Lowering makes an adapter (`f#lend#contentsN`, `promote.py`)
    where a value whose type holds a brand the function opened is lent: such a value cannot be a
    local place, so the adapter stores it into a place of its own, lends that, and returns it with
    the call's result. The backend knows an adapter by its shape alone (`Backend::adapter`) and
    writes its call as the call it makes, on the caller's storage, so the adapter itself is not
    written, and neither are the copies of the borrowed values into its places and out again;
  - a construction whose components each lie at their own member of one path when it is made, where
    a destruct took apart a value of the construction's type, lies at that path and writes nothing
    (lowering takes a branded value apart and builds it again around a lend).
- **Homed locals** (`Backend::homes`, before the plan). A local place L whose every whole store
  stores a value lying at one place path P of L's type, and that is never holding a value while P
  is, shares P's storage: the function is planned and written with L's paths rewritten under P, so
  `let { members, ..rest } = self; ...; self = Group.{ members, ..rest }` copies nothing out of
  `self` or back. A local whose contents the C would read as a value (a load of it used otherwise
  than to move it back to P, a load of a part of it, a switch on it) keeps its own variable, which
  the C compiler can keep in registers.
- **Shared variables** (`Plan::share`). A value assigned in a block and read by one edge alone, as
  the argument of a live parameter of another block, is assigned to that parameter's variable, and
  the edge assigns nothing: an enum result of a call or of a construction is made in the join's
  variable, unless the block reads its payload in that variable (below). The payload parameter of a
  block that only one arm of a switch on a value enters reads the payload where the scrutinee lies,
  when that is a C variable (not a place, which the arm's block may write).
- **Homed parameters** (`Plan::home_params`). A block parameter of a struct or tuple type that its
  block takes apart, and whose component the block only stores into a place path its header declares
  empty, gets that component written at the path by every edge into the block: where the argument is
  made, when a construction or a call makes it into the parameter's variable, else at the edge after
  its other assignments, unless an earlier write there would change it. The store writes nothing,
  and the component lies at the path. A join `let (next, hot) = match state { ... }; state = next;`
  so writes each arm's state straight into `state`.

Then, while writing the C (`backend::cgen`, `cgen.py`):

- **Edges** assign the target's live parameters one after another, and go through scratch
  variables (`edge_N`) only when an assignment would overwrite a variable a later one reads (a
  block jumping to itself with its parameters swapped).
- **Tail calls.** A call that is its block's last instruction, whose result the block returns
  directly or through return joins:
  - to the function being written, with the same specialization, assigns the arguments to the
    parameters and jumps to the entry block, unless it lends a local place (whose object the next
    round would reuse);
  - to another Kursal function, is `KUR_MUSTTAIL return f(...);` when Clang's `musttail` holds
    without question: the callee's C parameter and result types are exactly the caller's, each a C
    integer, floating or pointer type; the call lends no local place; and the caller has no
    `alloca`, so no argument can point into the frame the tail call frees. `KUR_MUSTTAIL` is
    `__attribute__((musttail))` under Clang and nothing elsewhere.
- **Aggregates by pointer.** A value parameter whose C representation is more than four 8-byte words
  (`Backend::by_pointer`, from `Backend::words`, an estimate from the type's structure) is a
  `T const*`: the caller passes the address of where the value lies, or of a copy of it (see "Why
  passing aggregates by pointer is sound"), and the callee reads it through the pointer, or copies it
  into its own variable first where it writes that variable (a call of itself in tail position, a
  lend in place of a path in it, or of a value read there: a payload read where the switch's
  scrutinee lies, through a component or a copy of the parameter, `Plan::variable`). A function whose
  address is taken keeps the plain convention for
  function values: they call its by-value entry (`thunk`), which passes the addresses on. A call
  through a function value or to a foreign function passes every value by value. Up to four words
  the C compiler copies a value in as many moves, and a pointer would cost the callee a load per read
  and raise the inliner's estimate of it: `examples/lisp`'s `Interp!::cons`, taking two four-word
  values, is not inlined into the interpreter's loop when it takes them by pointer.
- **Out-results.** A Kursal function whose result is a tuple with elements of more than four words
  (`Backend::outs`, decided in `Backend::request` beside `by_ref`) takes, after its other parameters, a
  pointer for each such element (`T* out_k`), writes the element there, and returns a tuple of the
  other elements (`void` when none has bytes). A construction it returns and reads nowhere else
  writes those elements through the pointers at once. A caller passes the place path a homed
  component ends up at, its own out pointer for the same element when it returns the result as it
  is, else the member of the result's variable; the rest of the C result goes to the other members.
  A function value, and a `foreach` mapper, calls such a function through its by-value entry
  (`thunk`), which returns the whole tuple.
- **`const` and `restrict` place parameters.** A place parameter `&x: T` is a `T*`. When the whole
  program has been written, `qualifiers` makes it `T const*` when the function stores into no path
  under it and lends it only to `const` place parameters, and `T* restrict` when nothing reaches the
  place it designates during the call except through it, as the greatest sets closed under every
  lend in the program.

### Why `restrict` is sound where it is written

`restrict` on a parameter promises C that, while the function runs, an object reached through the
pointer is reached through no other pointer that is not derived from it. The core language makes
that so for places lent by path:

1. A place is not a value (the escape rule): no instruction gives a place's address, it is never
   stored, returned, captured or passed except by a lend, and the callee can only read it, store
   into it and lend it on. So the C object of a local place has no address but the ones lends pass,
   and each lend's pointer is derived from the lender's.
2. The places one call lends are disjoint: no lent path is a prefix of another. An enum's payload
   is lent only under its known variant, so two payloads of one enum, which share storage, are never
   lent together; two fields of one struct are separate objects. In the niche layout (below) the
   payload is the whole enum's object, and the other variant is zero-size, so nothing is lent beside
   it either.
3. While a callee runs its caller is suspended, so the caller's own pointer to the place is not
   used.

So a place parameter lent, at every call, a path under a local place of the caller, or under a
place parameter of the caller that is itself `restrict`, is reached only through the parameter
while the call runs (by induction on the calls, from the locals). `restrict` is not written in
these cases:

- **Raw targets.** `f(unsafe &*p)` lends the memory behind a raw pointer, which a copy of `p`
  (another argument, a field of something reachable, another thread) could reach. That nothing does
  while it is lent is the obligation of the unsafe lend (`lib/std/CONTRACTS.md`, section 2.0d),
  which nothing checks. A backend may rely on it, as it relies on the escape rule for a local place;
  this one does not: the callee's parameter is not `restrict`, nor is any parameter that place is
  lent on to.
- **Functions whose address is taken.** A call through a function value may lend anything, and a
  function pointer's type has no qualifiers: such a function's place parameters are neither
  `restrict` nor `const`, and a parameter lent through a function value is not `const`.
- **A tail call to itself that moves a place parameter** (`f(&b, &a)` from `f(&a, &b)`): the jump
  to the entry block reuses the parameters, so one C pointer would reach two places within one
  execution of the function.

`const` needs no such argument: it only records that the function's C does not write through the
pointer, which the checker's place rules let the backend see.

### Why passing aggregates by pointer is sound

A value parameter is a value: the callee must see what the caller passed, unchanged, for the whole
call, and the caller must see its own values unchanged after it. The callee never writes through the
pointer (`const`); where it would write the parameter, it copies the value into its own variable at
entry and writes that. The caller passes the address of where the value lies only when nothing can
write there while the callee runs:

- a C variable of the caller (its own, or one a value reads in place) is written only by the
  caller's own code, which is suspended, and lends in place of a path in it are made by the caller
  alone;
- a local place of the caller is written only by the caller or through a lend of it, so it is
  passed when the same call lends no path overlapping it (the escape rule: no other address of it
  exists); a value lying under a place parameter is copied into a temporary first, since this
  backend does not rely on a raw target's exclusivity (see "Raw targets" above);
- the pointee of the caller's own by-pointer parameter is, by the same argument one call up,
  written by nobody while the caller runs, and so while its callee runs.

A function value's C type keeps every value parameter by value, which a call through it, a foreign
call and a `foreach` mapper's capture assume of their callee only through `request`'s decision; the
by-value entry of a function whose address is taken copies each argument into its own parameter and
passes the parameter's address, which nothing else can reach. The tail call `musttail` rule needs
scalar parameters only, so a callee taking an aggregate is never one.

### Why reading values where they lie is sound

A value read where it lies must find there, at each read, what it was when it came out of it. The
plan (`Plan::residences`) allows a residence only where that holds:

1. **Who writes a C variable.** The C variable of an SSA value is assigned once per execution of
   its block, before any read, and a block parameter's on the edges into its block. The only other
   write is a lend in place of a path in it, which the plan allows only when the value whose
   variable it is is read no more after the call; a residence that a lend in place for another
   value may overlap (whether or not that lend happens) ends there.
2. **Who writes a place.** Under the escape rule a local place's C object has no address but the
   ones its lends pass, so only a store or a lend of an overlapping path, in this function, writes
   it. So is memory under a place parameter, unless a caller lent it from a raw target (`&*p`) and
   broke that lend's obligation; this backend does not rely on the obligation (see "Raw targets"
   above), so for a residence there, every call, `foreach`, raw store and atomic write before the
   last read ends it. A store of the value into the very place it lies in, as the last read, writes
   nothing.
3. **Calls read first.** A call's arguments are evaluated before its body runs, so the call that
   reads a value last may write where it lay; any other write before the last read ends the
   residence, and the value is copied out of the place where it came out.
4. **No overlapping assignment.** C requires the two sides of an assignment to be the same object
   or not to overlap. A store into a path overlapping, but not equal to, where its value lies ends
   the residence; a construction is stored in place only when every component that lies in the
   target lies at its own member there, and then that member is not written.
5. **A lend in place.** The callee is given the path the borrowed value lies at instead of the
   adapter's place holding a copy of it: it sees the same value, and what it leaves there is what
   the adapter would have handed back, which then lies there. The paths lent are disjoint from each
   other and from every place the same call lends (checked), and recorded as lends for `const` and
   `restrict` like any other (a C variable as a local place: its address goes to this call alone).
   The adapter's place was its own, so nothing else could see the lent value change; on the
   caller's storage, the plan makes sure nothing else does: a C variable is read no more after the
   call (1), and a place has the value handed back stored into it again, in the same block, before
   any other instruction names an overlapping path. A place a value of a `Copy` type was loaded
   from still holds it, and may be read again in another block; it holds the value handed back
   by then, as KURS says it does.
6. **Edges.** An assignment of an edge or a self tail call from a value lying under a place
   parameter, or in another value's variable, reads that parameter's pointer or that variable:
   the parallel moves account for it like any other variable read (`Move.base`).
7. **A construction where its parts lie.** Each component lies at its own member of the path when
   the construction is made (nothing wrote it from where the component came out of it, whatever
   reads the component later), every member has its component, and the path held a value of the
   construction's type, so the object at the path is the constructed value; from there the
   construction is a value lying at the path like any other (2–4).
8. **Homed locals.** L and P may share memory when at no point both hold a value: then a write to
   one while it is empty touches only memory the other does not hold a value in, and nothing reads
   an empty place. The backend follows the checker's own account of the places: each block's header
   lists the paths empty where it starts (every local at the function's start), a load or place
   destruct of a value that is not `Copy` empties its path, and a store fills its path; the paths
   around a store are counted full, which can only refuse a homing. L counts as holding nothing
   once nothing in its block, or in a block reachable from it, names it again (a `Copy` value
   loaded out of L leaves it full but dead). A store into L of a value lying at P writes nothing.
   The rewritten function is planned like any other, so every residence is checked against writes
   to P that were writes to L.
9. **Shared variables.** A block parameter's variable is assigned only on the edges into its block
   and read only in that block. A value sharing it is assigned in a predecessor and read by nothing
   but the edge, which leaves the predecessor and enters the block, so its life and the parameter's
   do not overlap, and the predecessor is never the block itself. A payload parameter read from its
   scrutinee's variable: the switch's block assigns that variable (1), or, when the scrutinee lies in
   a parameter's variable, the edges into the switch's block and the values shared into the
   parameter; the arm's block is entered from that switch alone and is not the switch's block, so the
   switch's block runs again only after the arm's block has left, and the arm's block (and a block
   whose payload it passes on, read in the same variable) shares no value into the variable it reads
   its payload in, which it may read after the value is made. That variable is found through what
   reads another value's variable (`Plan::variable`): a residence in a value, a copy, a load of a
   forwarded place, and a payload read where its scrutinee lies; a switch on a copy of a parameter
   reads the payload in the parameter's variable. The moves of an edge out of the arm's block read
   the scrutinee's variable, which `Move.base` accounts for. A lend in place of such a payload writes
   the scrutinee's variable, so a parameter taken by pointer that it lies in is copied first.

### Why homed parameters and out-results are sound

A homed component is written at its place path P earlier than the store that KURS has write it, and
an out-result is written through a pointer by the callee instead of returned:

1. **P is empty from the write to the store.** The block's header says P is empty where the block is
   entered, so at the end of every edge into it (the place states of an edge are those of its
   target), and nothing in the edge's block after the write, nor in the block before the store, names
   a path overlapping P: no store fills it and no lend reaches it. An empty place holds no value: its
   bytes belong to nothing the program may read, whatever pointer it reads through. A `Copy` value
   loaded out of P leaves it full, so such a P is never empty in a header.
2. **Nothing reads P's new contents but the store.** No value lying at a path overlapping P is read
   after the write (the plan checks the rest of the block, its terminator and its edges), and the
   component's value is read only by the store, which writes nothing: what is at P when the store
   runs is that value.
3. **No write under a later read.** An edge, or a construction making the argument, writes its homed
   components one after another, in the order of the parameters and their components, so a component
   is not homed when it lies at a path overlapping the path of one homed before it in that order: the
   earlier write would change it (`(b, a)` stored back into `(&s.0, &s.1)` from `(&s.0, &s.1)` homes
   one of the two). A component lying at a path that overlaps its own P without being P is not homed
   either (C forbids the overlapping assignment); one lying at P itself is not written. At an edge the
   homed components are written after every other assignment, so the moves read what they read
   before, and a component is not homed when it lies in the variable of a parameter of the edge's
   target (a block jumping to itself, or a payload read in the target's variable), which the moves
   may have assigned by then.
4. **Out pointers reach nothing else.** A callee writes an out pointer's pointee and never reads it.
   The pointee is a homed path P, which the call lends to nobody (the plan refuses a call lending a
   path overlapping P) and which no argument passed by pointer lies in (`by_address` copies one
   that does); the caller's own out pointer, exclusive by the same argument one call up; or a member
   of the variable of the call's result, which the call defines, so no argument lies in it: a value
   of another block lies in it only as a payload read where its scrutinee lies, and a block reading
   its payload in a parameter's variable shares no value into that parameter (item 9 of "Why
   reading values where they lie is sound"). The callee writes it at a return, or at a construction
   that is returned and read nowhere else, after which nothing in the callee reads anything there.
5. **Calls through function values** keep the plain convention: they reach a function writing out
   pointers through its by-value entry, whose pointees are its own locals. A `foreach` mapper does
   the same. A self tail call keeps the out pointers it was given; `musttail` needs a scalar result,
   which a reduced tuple is not.

### Zero-size types are erased

A type is zero-size when `zero_size` (`types::ops`, `types.py`) says so: `()`, a brand witness
(`Brand['k]`), `!`, a struct or tuple whose components are all zero-size (an empty struct, a
capability such as `Exclusive!['k]`, `(Brand['k], ())`), an array of zero-size elements, an
`exists` whose body is, and a `foreach` whose expansion is. An enum never is: its tag
has bytes. A type parameter is answered at each specialization, after brands are erased.

Such a type has no C representation. Its C type is `void` (`erased` asks), and:

- no struct or tuple member has it, an enum's payload union has no member for it (an enum whose
  payloads are all zero-size is `struct { uint32_t tag; }`), and no array has it as elements (the
  array is zero-size itself);
- no C variable has it: an SSA value, a block parameter or a local place of one has none, a pure
  instruction whose results are all of one is not written (constructing, destructuring, copying,
  loading, `pack`, `open`, `fresh`, `static_brand`), a store of one writes nothing and does not make
  the place parameter it stores under non-`const`, and an edge or a self tail call assigns nothing
  for it;
- a function returning one returns `void` (`return;`), and a parameter of one, by value or by place,
  is not in its C parameters nor in the arguments of any call, direct, through a function value,
  foreign, a tail call or a `foreach` mapper's; a call whose result is one is a statement;
- a pointer to one is a `void*`; destructuring a pointer gives a pointer to a zero-size component
  the address of the whole (`(void*)p`), since zero-size objects may share an address;
- `sizeof` of one is `UINT64_C(0)`, and the sizes C gives the types holding one leave it out, so
  `sizeof[(u64, Exclusive!['k])]()` is `sizeof[u64]()`;
- the one C object a zero-size type gets is a `data` of it, whose address the program can observe
  and compare with null: a `static char`. A string literal's `data` is never one: it holds the
  string's bytes and a zero byte after them, so the empty string's is `[u8; 1]`.

There is no `KurUnit` and no adapter for foreign functions returning `()`: a foreign `void`
function is its own function pointer. A function that takes and returns only zero-size values is
`static void f(void)`, and a call of it still runs it.

Erasing them never changes what a program reads or writes: a zero-size value has no bytes to read or
write, and memory never holds one on its own. A raw load or store (`ptr_load`, `ptr_store`, volatile
or not), an `alloca`, or a lend of a raw pointer's target (`f(unsafe &*p)`, which reads and writes
it) is formed only at a type with `Sized`: at a type without parameters, one that is not zero-size,
so `unsafe *p` at `()`, a `Brand['k]` or a capability is rejected, `Copy` or not
(`raw_access_at_zero_size`, and at the source `a raw read, write or alloca at a zero-size type`); at
a type that mentions a parameter, one with an exact `Sized` premise or a part with bytes whatever
the parameters are, so a generic function's `unsafe *p` at `T` needs `where Sized[T]`
(`raw_access_needs_sized`, and at the source `a raw read, write or alloca at T needs Sized[T]`).
Nothing is checked again at an instantiation: a caller discharges `Sized` where it calls, so no
specialization reads, writes or reserves a zero-size type, and the backend writes every load, store
and `alloca` it meets. The containers and allocation functions take `Sized` of their elements
(`lib/std/CONTRACTS.md`, "Zero sizes"): `OwnedVec![()]`, `Owned!::new(())` and a map to `()` are not
formed. Two more operations reject a zero-size type (`zero_size_type_has_no_memory_form`), as the
front end does first, at the source (`a zero-size type has no memory form`): an unsafe cast from or
to one, which reinterprets a representation it does not have; and an extern parameter of one, which
C has no form for. Neither can take a type parameter (a cast cannot take or produce a value of one,
and an extern is not generic). An atomic operation works on an integer, a bool or a pointer, never a
zero-size type. `sizeof` of a zero-size type is 0, and the arithmetic the standard library does with
sizes is defined at 0 for what asks for no bytes (`checked_mul` does not divide by it, `bump_chunk`
reserves no bytes and returns the current address, `HeapAllocator::alloc(0)` asks `malloc` for one
byte so that it is never null).

Two zero-size objects may share an address, with each other and with what holds them. Nothing relies on distinct addresses for zero-size data: the identities the
compiler and the standard library compare (`Ref::address`, `ArenaVec::address` and
`ArenaMap::address`, which `Meta::id`, `fun_key` and the checker's copy cache use, and `Str`'s
equal-address shortcut) are addresses of headers and states that have bytes.

### Enum layout: tags and niches

An enum is a `uint32_t tag` and a union of its payloads, except where its types give it a cheaper
form. `NonNull[T]` (`non_null T` in KURS), a pointer that is never null, has a **niche**: its
all-zero value is none of its values. `niche` (`types::ops`, `types.py`) finds a type's niche from
its structure alone:

- a `NonNull` has one, at offset 0, and offers one invalid value, the null pointer;
- a struct or tuple has the niche of its first component, in declaration order, that has one, and an
  array that of its element 0;
- an `exists` has its body's;
- nothing else has one: a raw pointer, a primitive (`bool` included), a function value, a zero-size
  type, and an enum, whatever its layout (a tagged enum's unused tags are not used; a niche-layout
  enum has spent its one invalid value);
- a type parameter is answered at each specialization, after brands are erased.

An enum is in the **niche layout** when `niche_variant` says so: it has exactly two variants, one of
a zero-size type and one whose payload has a niche (a `NonNull` offers one invalid value, so one
dataless variant; a source that offered more would admit as many). Its C type is the payload's C
type: `Option[NonNull[T]]`, `Option[Ref[T, 'k]]`, a `List` link and a user's own enum of that shape
are one pointer (or the payload's struct), and `sizeof` is the payload's. The dataless variant is the
null at the niche. In C:

- `construct_variant` of the payload variant assigns the payload; of the other, assigns `0` to the
  niche's member path (`dst.f_ptr = 0;`) and writes nothing else;
- `variant_tag`, `switch` and `foreach` over the enum read the variant as
  `(x.path == 0 ? dataless : payload)`, the variants' indices in declaration order;
- the payload, as a value of a switch arm, a place path `x.Some` or a `foreach` component, is the
  enum's own object.

Every specialization is laid out by its own arguments: `Option[T]` at `NonNull[u8]` is a pointer and
at `u32` a tag. Names play no part; nothing in the compiler knows `Option` or `NonNull` by name
beyond the surface spelling of the builtin type.

The layout is sound because no program can observe or break it:

- **The niche holds a valid value whenever it is read.** A `NonNull` value is made only by an unsafe
  cast from a pointer (or another `NonNull`), whose obligation is that it is not null, or by
  `NonNull::new`, which checks. The cast's C says so to the C compiler
  (`if (n == 0) __builtin_unreachable();`), which then folds the niche tests of what is built from
  it, as it folds a constant tag: without it, a `Slice::get_ref` inlined into a byte loop keeps its
  test for null. `variant_tag` and `switch` of a place need the place full, so its niche holds a `NonNull` it was
  given; a store under a known variant stores a whole `NonNull`, never a null, and a payload that is
  moved out keeps its bytes. The dataless variant's null is written only by `construct_variant`.
- **Nothing reads the bytes the dataless variant leaves unwritten.** Only the niche is read to tell
  the variants apart, and a payload is read only under the payload variant, so valgrind sees no use
  of an undefined byte; copying the whole object copies them as C copies any partly written struct.
- **No cast sees the layout.** An unsafe cast converts a `NonNull` only from or to a pointer, and
  rejects a type that holds an enum or a `NonNull` within its value representation
  (`unsafe_cast_layout_chosen`, `non_null_cast_pointer`): the layout of an enum, tagged or not, is the
  compiler's choice, and a cast between, say, `Option[NonNull[u8]]` and `*u8` would make it a
  contract. A cast of a type parameter is already rejected, so no specialization can bring an enum
  into one.
- **Places, lends, `const` and `restrict`** are as for a tagged enum: a place under its known
  variant is the enum's object (see "Why `restrict` is sound where it is written"); packing and
  opening a niche-layout enum copy its value like any other.

### Pointer-sized integers

`usize` and `isize` are pointer-sized: their C types are `uintptr_t` and `intptr_t`, and one C file
is right on a 32-bit and on a 64-bit target. The compiler never assumes their width:

- **Literals** of `usize` and `isize` range over 64 bits, the same on every target: `0xffff_ffff_ffff_ffffusize`
  is accepted, and on a 32-bit target C converts it to the type, keeping its low 32 bits. Match
  ranges are checked against the same range.
- **Constant arithmetic on them is the C compiler's.** Folding (`sigs::signatures`, `sigs.py`) does
  not evaluate an operation on `usize` or `isize`, nor one on the value of such an operation (a
  cast to `u32` of a `usize` constant, a comparison of two): it keeps it as an operation
  (`ConstValue::Op`; `OpConst`), and lowering emits its instructions wherever the constant is used,
  for the C compiler to fold at the target's width. `usize::MAX` is `!0usize`, and `usize::BITS`
  is computed from it. A division by a literal 0 is still an error, since 0 is 0 at every width.
- **An array length** is part of a type, so the compiler must know it: it may not use arithmetic on
  `usize` or `isize` (`an array length cannot use arithmetic on usize or isize`); a literal, a
  constant that is one, or arithmetic in `u64` is accepted. **An array has at least one element**:
  a length that folds to 0, `[u8; 0]` or `[u8; N]` with `const N: usize = 0usize`, is rejected where
  it is written, and so is the empty literal `[]` (`an array has at least one element`;
  `examples/bad/empty_array.kur`), and a KURS `(array T 0)` in formation. Lengths are folded where
  a signature is read and there are no constant parameters, so no instantiation makes one 0.
- **Run-time arithmetic** is done unsigned in `uintptr_t` and decoded to `intptr_t` for `isize`
  (`kur_dec_isize`); a shift amount is taken modulo `KUR_PTR_BITS`, the width C gives
  `uintptr_t`. Conversions to and from the fixed-width types truncate or extend at the target's
  width, and pointer casts go through `uintptr_t`.
- **Foreign functions** that take or return `usize` or `isize` are declared with `uintptr_t` and
  `intptr_t`, so their C side (`lib/libc/libc.h`) uses those types too.

The size estimate that passes aggregates by pointer (`words`) counts every scalar, `usize`
included, as one word on every target.

**The size limit is the one place a width is assumed.** A type is formed only if its size is below
2^64 bytes, the address space of a 64-bit target, so `sizeof` of any formed type is a `usize`. The
size is the one the backend's C has (`Types.layout`, `Types::layout`): a primitive its width, a
pointer, a function value, `usize` and `isize` 8 bytes, zero-size parts erased, a product a C struct
of the rest with C's padding, an array its element's size times its length, and an enum a `uint32_t`
tag beside a union of its payloads, or its payload in the niche layout. Formation checks every
closed type where it is written, declarations' closed parts once all declarations are formed (`type
formation: (array (array u8 9223372036854775808) 2) is too large: its size is not below 2^64
bytes`), and the backend checks every type of a specialization again at its type arguments
(`backend: ... is too large: its size is not below 2^64 bytes, in a specialization of g`), as a type
that mentions a parameter may be small at one instantiation and too large at another. A 32-bit
target would need the limit at 2^32; the compiler checks the 64-bit one only.

### Number conversions

`x as T` between two number types converts the value, and is safe: it is defined for every value.

| From | To | Instruction | Result |
|---|---|---|---|
| integer | integer | `int_cast` | the value modulo 2^N for an N-bit `T`: truncated, or sign- or zero-extended |
| integer | float | `float_cast` | the nearest float, ties to even (`u64::MAX as f32` is 2^64) |
| float | integer | `float_cast` | toward zero (`-2.9 as i32` is -2); below `T::MIN` it is `T::MIN`, above `T::MAX` it is `T::MAX`, NaN is 0 |
| float | float | `float_cast` | `f32` to `f64` exactly; `f64` to `f32` the nearest, ties to even, past `f32::MAX` an infinity |

C converts a float to an integer only when the integral part fits (anything else is undefined), so
the C for a float to an integer tests the range first, against powers of two that both float types
hold exactly, and converts only inside it:

```c
v3 = v2 >= -0x1p63 && v2 < 0x1p63 ? (int64_t)v2 : v2 > 0 ? INT64_MAX : v2 < 0 ? INT64_MIN : 0;
```

For an unsigned `T` the range is `x > -1 && x < 0x1pN`, and for `usize` and `isize` the bounds are
`(double)INTPTR_MIN` and its negation (doubled for `usize`), at the target's width. On x86-64,
Clang at -O2 compiles the in-range path to two comparisons and the conversion instruction
(`cvttsd2si`), the saturating cases to a branch beside it.

`cast`, the unsafe reinterpretation, never takes a number to a number (`unsafe_cast_between_numbers`);
the front end never asks it to, since `as` between numbers is `int_cast` or `float_cast`. A float's
IEEE bits are `core::num`'s `f64::to_bits` and `f64::from_bits` (and `f32`'s), safe functions that
reinterpret a struct holding the float alone.

A constant conversion with a float on either side, and constant arithmetic or comparison on floats
(`const INFINITY: f64 = 1.0 / 0.0;`), is not evaluated by the compiler: like an operation on `usize`
(see "Pointer-sized integers") it is kept as an operation (`OpConst`, `ConstValue::Op`), and the C
compiler folds it.

### Integer arithmetic

An operation on integers is defined for every pair of operands, signed or unsigned: no operand
makes the C undefined, and the C compiler needs no flag such as `-fwrapv` for it. For an N-bit type:

| Operation | Result |
|---|---|
| `+`, `-`, `*` | the exact result modulo 2^N, as a value of the type: it wraps (`127i8 + 1i8` is -128, `0u8 - 1u8` is 255) |
| `-x` | `0 - x`, so the negation of a signed type's minimum is the minimum |
| `/` | the quotient rounded toward zero (`-7i32 / 2i32` is -3); a signed minimum divided by -1 wraps to the minimum |
| `%` | `x - (x / y) * y`, with the sign of `x` (`-7i32 % 2i32` is -1); a signed minimum modulo -1 is 0 |
| `/`, `%` by 0 | the program traps (`KUR_TRAP`, `__builtin_trap()`) |
| `<<`, `>>` | the amount is taken modulo N, as a two's complement value: an amount at or above N, or a negative one, shifts by its low log2(N) bits (`1u64 << 65u64` is 2, `1i8 << -1i8` is -128) |
| `>>` on a signed type | arithmetic: the sign bit fills the vacated bits (`-128i8 >> 1i8` is -64) |

The operands of an operation have one type, a shift's amount included. The C computes in
`uint64_t` (`uintptr_t` for `usize` and `isize`), where C defines every result modulo 2^64, and
reduces the result to the type: a cast for an unsigned type, and for a signed one `kur_dec_i8` to
`kur_dec_i64` (`kur_dec_isize`), which take the low N bits as a two's complement value without a C
conversion of an out-of-range value, which C leaves to the implementation. A shift widens its
operand from its unsigned N-bit type, masks the amount with `N - 1` (`KUR_PTR_BITS - 1`), and for a
signed `>>` fills the high bits itself, since C's `>>` of a negative value is the implementation's.
Division tests its divisor first and computes the quotient in the signed type only when the
divisor is not -1, the one case where C's signed division overflows:

```c
v3 = kur_dec_i8((uint64_t)(v1) + (uint64_t)(v2));
if (v2 == 0) KUR_TRAP();
v3 = (v2 == -1) ? kur_dec_i8((uint64_t)0 - (uint64_t)(v1)) : (int8_t)(v1 / v2);
v3 = (v2 == -1) ? 0 : (int8_t)(v1 % v2);
v3 = kur_dec_i8(((uint64_t)(uint8_t)(v1) >> ((uint64_t)(v2) & 7)) | (v1 < 0 && ((uint64_t)(v2) & 7) ? (UINT64_MAX << (8 - ((uint64_t)(v2) & 7))) : 0));
```

A division or remainder is never left out as dead (see "The C backend"): its trap happens wherever
the operation is evaluated, whether or not its result is read.

**Constant folding agrees with run time.** Folding (`sigs::signatures`, `sigs.py`) computes the
same results: it wraps to the type's width, rounds a quotient toward zero, takes a shift amount
modulo N and shifts a negative signed value arithmetically, so a constant has the value the same
operation has at run time. A division or remainder by a constant 0 is an error (`constant division
by zero`) rather than a trap, in a constant operand that is evaluated (not the right of a `&&` or
`||` the left decides). An operation on `usize` or `isize` is not folded (see "Pointer-sized
integers"): it is lowered to the instructions above, so only a divisor that is a literal 0, or a
constant that is one, is an error there; a divisor that is 0 only once computed
(`1usize / (1usize - 1usize)`) traps where the constant is used.

### The C compiler

`kursal build` compiles the C with `$KURSAL_CC`, else `clang`, with `-O3 -w -fno-builtin`, and so do
`tools/build.py` and every test (the reference's `kursal/cc.py`). `-w` silences warnings about
generated code (unused labels, the standard library's own prototypes of C functions);
`-fno-builtin` keeps the C compiler from giving a function the meaning of the C library function of
the same name, which an extern or a Kursal function may declare with another type.

What the C compiler writes goes to stderr as it writes it. When it fails, `kursal build` and
`tools/build.py` add the same line and exit with status 1: `cannot run the C compiler CC (error
ERRNO)` when it cannot be started (ERRNO 2 when there is no such program), `the C compiler CC exited
with status N`, or `the C compiler CC was killed by signal N`.

The C is GNU C (`__typeof__`, `__builtin_*`, `__atomic_*`), as Clang and GCC accept it, and includes
only `<stddef.h>` and `<stdint.h>`; `usize` and `isize` are pointer-sized (see "Pointer-sized
integers"). It calls no C library function
but those a program declares `extern`, yet the C compiler may: like any C compiler for a
freestanding target, it emits calls to `memcpy`, `memmove`, `memset` and `memcmp` to copy, fill
and compare memory. A target, hosted or freestanding, must provide those four.

## Errors

Resolution reports every error it finds before it stops: `Resolver::report` keeps an error with
its file and position, and `Resolver::stop`, after the last name is resolved, prints the first 20
in source order (by module, then position), each on its own line, then `and N more errors`. An
error reported twice is printed once. Every other phase stops at the first error:
`support::base::fail(&cx, message)` prints the message and exits
with status 1, leaving the open arenas to the operating system; `fail_static(literal)` needs no context. A message is exactly what the reference compiler prints for the same error: `KursError`'s
`str()`, which is `line:col: message` when it carries a position and the message alone otherwise,
and the file prefix the front end adds (`resolve.located`). Tests compare messages.
`types::ops::kurs_error(&cx, message, pos)` stops with `KursError(message, pos)`'s text; a `Pos` with line 0
stands for no position.

## Fresh names

`types.fresh` numbers renamed binders (`a#3`) from one counter for the whole compilation. Here that
counter is a `types::ops::Fresh['k]`, made once and passed to everything that may rename a binder: `subst`,
`unify`, `match_head`, `Types`, `Formation`, `check_type_expansion`, `check_coherence`, `Signatures`,
`Infer`, `lower`, `check_program`, `emit_c`. Passing the
same one everywhere numbers names as the reference compiler does; `carry_fresh` carries it into the
next group's arena, counting on from where it stopped. `types::ops::apart` names no binder from the
counter: lowering renames a package binder of a type it writes that is spelled as a brand of the
function (a type from another declaration may be) to the first free `b#0`, `b#1`, ..., so KURS never
shadows a brand in scope.

Where the reference wraps a phase in `with resolver.located(module, pos):`, call
`r.locate(&cx, module, pos)` before it and `r.unlocate(&cx)` after, and report errors inside it with
`r.fail(&cx, message, pos)` (a `pos` on line 0 is none): it adds the file, and the item's position when
the error has none of its own. Lookups that the reference may catch (`ResolveError`) return a
`Result[Target, Problem]` instead of stopping (`Resolver.lookup`); `Resolver.need` stops. Where
the reference reports an error and goes on (`with resolver.reporting(...)`, `resolver.report`),
resolution calls `r.report(&cx, message, pos)`, located the same way, and goes on; `Names::run`
ends with `r.stop(&cx)`. Signatures do the same where the reference reports a `SigError`
(`with self.reporting(...)`): each step returns a `Result` whose error is a `SigError`,
`Signatures::reported` reports it, and `r.stop(&cx)` ends each group of steps. The checks of
whole programs that the front end also runs (`check_type_expansion`, `check_instance_termination`,
`check_coherence`) take the files of the declarations they check, and no files when they check
KURS.

The standard library is found as the reference finds it, in `$KURSAL_LIB`; without it, in `lib`
beside the directory holding the compiler's binary (`PREFIX/bin/kursal` reads `PREFIX/lib`, so
the repository's `bin/kursal` reads its `lib`). `scripts/bootstrap.sh` sets `KURSAL_LIB` for every
stage.

## The language, for a compiler author

These are the places where Kursal differs from what one might expect.

- **Names.** Keywords cannot be field, method or variable names: `use export module struct tuple
  enum type fn extern impl trait instance const forall where as exposed unsafe closure exists foreach
  of in for while if else match let var return pack true false self _`. Fields here are named `ty`,
  `is_unsafe`, `home`, `quantifier`, `trait_name` and the like instead.
- **A linear type's name ends with `!`.** `struct Buffer! { ... }` declares the name `Buffer!`, and
  every reference spells it: `Buffer!`, `Buffer!::new()`, `Buffer!.{ ... }`, `Polled!::Running(x)`,
  `use std::buffer::Buffer!;`, `impl Buffer!`, `Option[Buffer!]`; an alias of one ends with
  `!` too (`use libc::thread::Thread! as RawThread!;`). The lexer reads a name followed at once by
  `!` as one name, unless the `!` begins `!=`: `a!=b` is `a != b`, `!x` is a negation, a keyword never
  takes the `!` (`return!x` is `return !x`), and `!` alone is still the type `Never`. A name without
  its `!` names nothing (`unknown name Buffer; the linear type is Buffer!`), nor does one with a `!`
  its declaration lacks (`unknown name Point!; Point is not a linear type`), and only a type's name
  may end with `!` (`only a linear type's name ends with !`). A struct, enum or tuple declaration
  carries the `!` exactly when it is linear whatever its type arguments: one that holds a linear type
  must (`struct S holds a linear type in its field l; declare it S!`, or `... whatever its type
  arguments ...` for a generic one), one linear only through its type arguments has none
  (`Option[T]`; `Option[Buffer!]` shows its linearity through its argument), and one with the `!` is
  linear even when every field is `Copy` (`Exclusive!['k]`). A `foreach` whose source has a fixed
  shape counts as its expansion (`foreach F of (u32, u8) => *F` is `Copy`); over a type parameter
  it is `Copy` only when its body does not mention the binder and is `Copy`, since a parameter of
  kind Any may have linear components, and under `*F` the rows are components of the pointee, which
  need not be `Copy` even when the pointer is. Type parameters never carry it: `forall T` may stand for
  a linear type unless a premise says `Copy[T]`.

  Resolution reports every name it cannot resolve in one run, each at the name itself and with the
  spelling that exists. When the name came through a `use` that imports the other spelling, the hint
  names that line too (`unknown name Buffer!; the linear type is Buffer!, imported as Buffer at
  src/lib.kur:1:5`), so the fix goes in the `use`.
- **Builtins are imported.** Only the primitive types, `Never`, `Copy`, `Exposed` and `Sized` are
  always in scope. What else the compiler provides, `core` exports from the module it belongs to, and
  code imports it like any other name (see the reference compiler's README, "Builtins"):
  `use std::ptr::NonNull;`, `use std::mem::sizeof;`, `use std::cap::Brand;`.
- **Declarations.** A function, an impl's method and an instance's method each have a body (`a
  function needs a body; only a trait method or an extern is a signature`). A trait method is a
  signature ending in `;`, and every instance gives it a body: there are no default methods (`a
  trait method is a signature; its body belongs in each instance`). `forall` comes before a
  struct, tuple, enum, type alias, function, impl, trait or instance, and before an impl's method;
  never before a `use` or a `const` (`` `forall` comes before a struct, tuple, enum, type alias,
  function, impl, trait or instance ``, `` in an impl, `forall` comes before a method ``).
- **Type aliases.** `forall 'k. type Bytes = Slice[u8, 'k];` names a type. The alias is transparent:
  wherever a type is written, signatures put the body in its place, its parameters replaced by the
  arguments (`Bytes['k]` is `Slice[u8, 'k]`, and the two mix freely), so inference, KURS and the C
  never see it, and messages show the type it stands for. Its `forall` lists only parameters, each
  of which the body uses (`type alias Pair takes no premises or determined parameters`, `type alias
  Pair does not use its parameter U`); a use gives every argument (`Bytes takes 1 type argument, got
  0`); no alias refers to itself, directly or through others (`type alias A refers to itself through
  B`); and its name ends with `!` exactly when the type it stands for is linear whatever its
  arguments are (`type alias L stands for a linear type; name it L!`, `type alias M! stands for a
  type that is not linear; name it M`). It has no representation of its own, so it is never
  `exposed` (`a type alias is never exposed: it has no representation of its own`), and it names a
  type only: not a value, a constructor or the namespace of members (`Bytes is a type alias; it
  stands only where a type is written`). An `impl` or `instance` written through an alias means the
  head it stands for, so `impl Bytes['k]` gives methods to `Slice`, and must be in `Slice`'s package.
  An alias is imported, exported and kept private like any item.
- **`Sized[T]`** says that `T` has a finite by-value layout of non-zero size. It is a premise of a
  function or an instance (so of each of its methods), never of a type declaration, and no program
  declares an instance of it. It holds of a type that mentions no type parameter when that type is
  not zero-size, the same on every target (`Sized[(u8, ())]` holds, `Sized[()]` does not). Of a type
  that mentions one it holds by the caller's own premise for exactly that type, or by a part held by
  value that has bytes whatever the parameters are, a primitive, a pointer, a sum or a function
  value, but not a foreach type: `Sized[(T, u32)]` holds with no premise, while `Sized[(T, U)]`
  needs exactly that premise, `Sized[T]` and `Sized[U]` not giving it (`Sized requires an exact
  premise or a part whose size is not zero whatever the type parameters are`). It implies neither
  `Copy` nor `Exposed`. A raw load, store, `alloca` or lend of a raw pointer's target needs it of
  its type ("Zero-size types are erased").
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
- **A case may name a variant by its label alone**: `Some(x) =>`, `None =>`, looked up in the sum
  the scrutinee has once its type is known (`the type here is not known yet; annotate it` before
  then). `Option::Some(x)` stays valid, and one match may mix both. A bare label is never a binding:
  a case binds only inside its parentheses, so a misspelled one is an error (`Option[u8] has no
  variant Nothing`), not a catch-all. A sum with no declared name, such as a `foreach` over an
  `Option`, is matched by its labels the same way.
- **An enum has at least one variant.** `enum E {}` is rejected (`enum E has no variant; an enum
  has at least one`), in KURS too: a sum of no variant would be a type with no value.
- **Struct literals** are `Point.{ x: 1u32, y }`; tuple structs and variants with a payload are built
  with `.(`, and only those: `Option::Some.(x)`. Resolution rejects a call of a constructor
  (`` `Option::Some` is a constructor; write `Option::Some.(...)` ``) and a `.(` after anything
  else (`` `f` is not a tuple or an enum variant; write `f(...)` ``). A spread `..rest` names a bundle made by a pattern
  (`let { x, ..rest } = p; Point.{ x: 2u32, ..rest }`), not an arbitrary value.
- **Literals** carry their type when nothing else fixes it: `0u32`, `1usize`, `b'a'`. A bare `0`
  needs a type from context. A float literal has a point (`2.5`), an exponent (`1e-7`, `1.5E+300`)
  or a float suffix (`7f64`); a point needs digits on both sides.
- **`unsafe`** marks one operation: `unsafe f(x)` calls an unsafe or extern function, `unsafe *p`
  reads through a pointer, `unsafe (x as *T)` casts, `f(unsafe &*p)` lends a pointer's target.
  `unsafe (unsafe malloc(n) as *T)` is two operations. A safe function may contain unsafe operations,
  and an `unsafe fn`'s body is no unsafe context: each unsafe operation in it is marked too, and
  `unsafe fn` says only that its callers take on an obligation. A store through a raw pointer is the
  statement `unsafe *p = v;`.
  `as` between two number types is a conversion by value and needs no `unsafe` (see "Number
  conversions"); a float's bits are `x.to_bits()` and `f64::from_bits(b)`. An unsafe cast neither
  makes nor takes apart a value that holds a package, a brand witness, a function value, a
  transform, a value of a type parameter or `Never` (`unsafe_cast_cannot_produce`), named types'
  fields included; behind a pointer, reading one is a separate unsafe operation. A raw pointer is cast
  only to or from a pointer, a `NonNull` or an integer (`pointer_cast_pointer_or_integer`), named types
  unfolded, as C converts one. No read through a pointer, nor lend of its target, produces a value
  that holds `Never`, named types' fields included (`unsafe_read_cannot_produce_never`); a generic read at a `T` instantiated
  with one is the caller's obligation (`lib/std/CONTRACTS.md`, section 2.3). What an extern function returns, like an unsafe
  cast's result, holds no package, brand witness, safe function value, transform or `Never` (a result
  of `!` itself is a function that does not return), and no enum or `NonNull`, whose layout C cannot
  know (`extern_result_cannot_produce`, `extern_result_layout_chosen`), and is zero-size only if it is
  `Copy`, since a zero-size linear result would be a resource made from nothing
  (`extern_zero_size_linear_result`; `extern f returns a zero-size linear type`); an `unsafe fn` value may come
  back (`lib/std/CONTRACTS.md`, section 2.0c). The walk is the by-value one: named types unfolded,
  not through a pointer or a function value. An extern takes no type or brand
  parameter (`extern f takes no type or brand parameters`) and no zero-size one, and a function type
  anywhere in its signature, behind a pointer or in a named type's fields too, is an `unsafe fn` type
  (`extern_safe_function_type`; `extern f has a safe function type in its signature`): foreign code
  neither makes nor calls a safe function value. Foreign code makes the arguments of such a value, and
  takes its result or hands the value back, so each of its parameters and its result is held to both
  rules, an extern parameter's and its result's, to any depth; its result may be zero-size only if it
  is `Copy`, as `()` is C's `void`, where a linear value would vanish. Foreign code takes and passes values and lends nothing back, so
  an extern takes no place parameter (`extern f takes a place parameter; foreign code takes values`)
  and no function type in its signature takes one (`extern_place_parameter`), where KURS can write one.
  Two externs of the standard library (`lib/alloc/heap.kur`, `lib/libc/thread.kur`):

  ```kursal
  extern "C" fn malloc(size: usize) -> *u8;
  extern "C" fn kur_libc_thread_create(entry: unsafe fn(*u8) -> *u8, arg: *u8) -> *u8;
  ```

  and a call of one is unsafe: `let p = unsafe malloc(16usize);`.
- **Generic code over linear types.** A type parameter may stand for a linear type unless a premise
  says `Copy[T]`. Using a value twice, or dropping it on one path (`a && b` drops `b` when `a` is
  false), needs `Copy[T]`. The arena collections (`ArenaVec`, `ArenaMap`, `ArenaSet`, `List`) require
  it of their elements.
- **Places.** A linear value is lent by place: `fn f(&cx: Domain!['k])` is called `f(&cx)`, and the
  callee's changes to `cx` are the caller's. Fields of a lent struct can be lent in one call
  (`&cx.arena.alloc(&cx.tag, value)`), and a call may lend `cx` again in its arguments
  (`f(&cx, g(&cx, x))`). A function may move a place parameter out if it puts a value back before it
  returns: `let { tag, arena } = cx; ...; cx = Domain!.{ tag, arena };`. A
  `match`, `&&` or `if` may lend it in every branch.
- **Only a var is lent or assigned.** The root of a place that is lent (`&x`, `&x.f`, `&x.m()`,
  `f(&x)`, `match &x`, `for v in &it`, `{&x}` in an f-string, `f"...".write(&x, out)`) or assigned
  is a `var`, a place parameter (`&x: T`, `&self`), or a binder of a case of a lent match. A `let`
  never changes after it is bound (`a is not a var; a lent place must be a var`), nor does a parameter taken by value,
  `self` included, nor the binder of a case of a match on a value: such a value is lent after
  `var x = x;` (`var self = self;`). A closure or `foreach` body holds its own value of what it captures, a copy or the
  value moved in, so it neither assigns nor lends a captured name, a `var` included (`a is captured,
  and a body cannot lend what it captures; ...`); it takes a place parameter instead. `var` binds what `let` binds, and every name it binds is a var:
  `var (tag, arena) = ...;`, `var { x, ..rest } = p;`, `var [a, b] = xs;`,
  `var exists 'k. (tag, arena) = Arena!::new(seed);`.
- **Brands.** `forall 'k.` binds a brand like a type parameter, and a type that holds branded storage
  takes it as its last argument (`ArenaVec[Str['k], 'k]`). A brand is minted by a constructor that
  returns `exists 'k. ...` and is opened by `let exists 'k. ...`, or `var exists 'k. cx = Domain!::new(hash_seed());` to lend what it binds. An opening's
  names and an `exists` type's binders are distinct and new where they are written: never `'static`,
  a brand parameter, an earlier opening still in scope or an enclosing `exists`'s binder. A trait may
  take a brand as a parameter (`forall D, 'k. trait Discharge`, `instance Discharge[Checker['k], 'k]`). Only a
  declaration quantifies brands: a function value's type `fn(T1, ..., Tn) -> R` quantifies nothing,
  and KURS's `fn` names every brand of its function, each in scope where the value is made. A function
  named bare as a value (`let f = g;`) has its brands inferred from how the value is used, like its
  type parameters; one no use determines is an error (`cannot infer the type argument 'k of g`), and
  a value called at two brands is a brand mismatch.
- **`NonNull[T]`** (`std::ptr::NonNull`) is a builtin type, like `*T` but never null:
  `NonNull::new(p)` checks and returns an `Option`, `unsafe (p as NonNull[T])` asserts it, `n as *T`
  (or `n.as_ptr()`) is safe, and `==` compares two. Its niche makes `Option[NonNull[T]]` and
  `Option[Ref[T, 'k]]` one word (see "Enum layout").
- **Zero-size types** (`()`, empty structs, brands, capabilities) take no bytes (`sizeof` is 0, see
  "Zero-size types are erased"); a `Ref`, vec or map of one stores nothing, so `OwnedMap![K, ()]` is
  a set.
- **Types are finite** (`check_type_expansion`, `types::layout`, run before anything else asks a
  question of a declared type). A type that holds itself by value is rejected (`type T: infinite
  by-value layout`); a pointer or a function between breaks the cycle. Every
  declaration is walked, one whose body is a `foreach` or holds one like any other. A `foreach`
  whose source's components are known counts as its expansion, and is itself active while its
  expansion is walked: `foreach *F of *L => F` over `struct L { next: *L, v: u32 }` is `(*L, u32)`
  as a struct, finite. The walk of a `foreach` also covers its source and, where a type parameter
  decides its components, the pointee of a pointer source under the pointer pattern, the origin of
  its rows: with `forall X. struct Wrap! { f: foreach *F of *(foreach G of X => Wrap![X]) => F }`
  the rows bind F to `Wrap![X]` itself, so `Wrap!` holds itself by value and is rejected where it is
  declared (`examples/bad/foreach_holds_itself.kur`). There the components are unknown, and the body
  is walked with the binder a leaf; the walk of the source and its origin lays out what the binder
  is bound to at every instantiation, so no instance is walked again. Where the components are known
  the expansion is walked instead, and not the pointee: `struct T { f: foreach *F of *T => u8 }` is
  `{ f: u8 }` and finite, while `struct T { f: foreach *F of *T => F }` holds the `foreach` again in
  its expansion. A type written in a signature or a body is walked the same way where it is written.
  C names a type by its expanded structure, a declared name staying a name, so a `foreach` that
  reaches itself again before any name, even behind a function, is rejected too (`struct T { f:
  fn(foreach *F of *T => *F) -> u32 }`: `recursive foreach needs a named type between`). Errors name
  the declaration whose walk found them. The size of every closed type the program writes, each part
  of it included (a pointee too; a closed `foreach` through its expansion), is below 2^64 bytes,
  checked where it is written; the
  backend's check of each specialization's types is the one check made at an instantiation.
- **A `foreach` type stands in every type position**: a field or element, a tuple's or an enum's
  component, a pointee, a package body, a `foreach`'s source or body, and a type argument, a
  premise's included, besides a value's, a parameter's or a result's type
  (`examples/foreach_types`). A `foreach` over a type parameter, as `foreach F of T => (F, u8)`, or
  over a source that a parameter decides (a declaration whose body is one, another such `foreach`,
  and under `*F` a pointer to one, as `foreach *F of *T => F`), is *stuck*: a type of its own, equal only to a `foreach` with the same pattern and
  equal source and body, the binders renamed, never to what it expands to. It has no components in
  generic code (`pattern does not match foreach F of T => (F, u8)`), and is expanded where the
  parameter is known. Over a parameter, a parameter of kind Any may have linear components, and
  under `*F` the rows are components of the pointee, which need not be `Copy` even when the pointer
  is, so a stuck `foreach` whose body mentions the binder is linear, and a declaration
  that holds one carries the `!` (`struct Tagged holds a linear type in its field tags whatever its
  type arguments; declare it Tagged!`).
- **A `foreach` type has an expansion at every instantiation.** A source that is no product (nor a
  sum) is one component: `foreach F of u32 => *F` is `(*u32,)`. Under the value pattern a pointer
  source, written or a declared alias of one, is such a leaf too, whatever it points to: `foreach F
  of *E => B` is `(B,)` with F the pointer type itself (the alias, for an alias), and asks nothing
  of the pointee, so a pointer to a sum or to a hidden type is fine there, and `type A = foreach F
  of *A => u32` is `(u32,)`. A pointer that is a component of a larger source is one component, as
  any is. The pointer pattern `*F` looks through a pointer source: each component is a pointer to
  it, to a field, an element or a variant's payload, and F its pointee; a pointer to a leaf is the
  one component. So `foreach *F of *Option[u8] => F` is `Option[u8]`'s own shape, `enum { None(()),
  Some(u8) }`. Such a type is formed, laid out and passed, but no traversal makes one: a pointer to
  a sum is copied, passed and read whole (`unsafe *p`, then a `match`), and is no traversal's
  source, as its components would be those of the variant held there, which only a read finds (`a
  foreach source cannot be a pointer to a sum`, through a name too). `Exposed[*T]`, which a generic
  traversal needs, never holds for an enum `T`, so no generic code traverses one either. `sizeof`
  asks the same authority a traversal does: of the source of a `foreach` type, or of a declaration
  whose body is one, whatever that source is (`sizeof[foreach F of T => u8]()` needs `where
  Exposed[T]`; over a type whose shape another module keeps, `the shape of File! is not visible
  here`; over a `foreach` type or a pointer to a sum it never holds), and of each pointer source
  that mentions a parameter of a `foreach` anywhere in the type. A type that holds a `foreach` is
  sized like any other: `struct { f: foreach *F of *Option[u8] => F }` has a size where the
  `foreach` type alone has none. `*F` binds F to each component's pointee, so its source must give
  pointers whatever the type arguments are. A source whose shape is decided shows them as written:
  an explicit pointer, or a tuple, struct, enum or array whose components are written as pointers; a
  name is not looked through, even one whose shape is a visible product of pointers (`foreach *F
  needs a source written with pointer components`). Where a type parameter decides the shape, only
  an explicit pointer will do, which the walk of the type checks: `foreach *F of T => F` is
  rejected, as `T` may be `bool` (`foreach *F needs a source written as a pointer *P, as a type
  parameter decides its shape`). Through a pointer a `foreach` only computes addresses, and reads
  nothing.

  Exposure is authority over a source's rows and components, not over sizes and `Copy`, but for the
  `sizeof` of a `foreach` type itself. `sizeof`, `Copy` and the layout of a container over a type
  whose shape is hidden, a stuck `foreach` or a type restricted to its owner module, may reveal that
  shape: the size of `struct { a: T, b: T }` is that of two `T`, and of a struct holding `foreach F
  of T => (F, u8)` it counts the components of `T`. This is accepted, and not gated by `Exposed`.
- **A `foreach` over a value of a `foreach` type maps it.** With `v: foreach F of S => B`, `foreach
  x of v { .. }` gives the body each component of `v`, of type `B` at the component's row, and makes
  `foreach F of S => R`, `R` the body's type: the same source and pattern, so it needs `Exposed[S]`
  as a traversal of `S` does. Each component is given to the body exactly once, so a linear one is
  consumed once, and of a sum only the variant `v` holds. The body is checked for any `F`, so it hands on a part of type `F` rather
  than dropping it (`examples/foreach_map`). No `foreach` type is a traversal's or a map's source:
  `Exposed` of one never holds, by its shape or by a premise, so a value whose type is a `foreach`
  over a `foreach` is passed and kept whole but not mapped (`a foreach source cannot be a foreach
  type`).
- **Expressions as statements.** `f(x);` discards a Copy result. A block's value is its last
  expression, without a `;`. `return` is allowed anywhere, and ends with `;` at the end of a block
  (``a `return` at the end of a block ends with `;` ``); a function whose type is `!` (such as `fail`)
  can stand in any expression position.
- **`if` and `match` are expressions** and need an `else`/every case when their value is used: an
  `if` without `else` has type `()`. A `match` names each variant it misses (`a match must cover
  every enum variant; it misses Amber`).
- **`?`** takes apart `core::option`'s `Option` and `Result` only, of the type the function returns:
  `x?` on an `Option` returns `None` from an `Option` function, and on a `Result` its `Err` from a
  `Result` function with the same error type. An enum of one's own with the same variants is
  another type.
- **Methods belong to their type's package.** An inherent impl, `impl T { ... }`, is in the package
  that declares `T`, and one of a primitive or builtin type is in `core` (`an inherent impl of
  Buffer! belongs in the package that declares it`), so no package adds a method that could clash
  with the type's own. Another package writes a function, or a trait and its instance. A blanket
  impl, `forall M where Tr[M]. impl M`, is no inherent impl: its methods are found through its
  bounding traits, as every writer's `put` and `write` are found where `Writer` is imported.
- **F-strings.** `{x}` formats `x` by `Format::format`, which hands the value back; `{x}` drops it,
  so a linear `x` is written `{&x}`, which lends it (`{x} in an f-string drops a linear value; lend
  it with {&x}`). An `x` of type `Str['static]` is written as the text around it is, by
  `Writer::write_static`, with no `Format` instance. `f"...".write(out)` writes into the writer `out` and hands it back.
  `f"...".write(&cx, out)` also lends the f-string the capability in the place `cx`, a var as for any
  lend: the capability is moved out of `cx`, `out` is wrapped with it as `ReadingWriter![W,
  C]::new(out, cx)`, each segment is written through the wrapper, and `finish` hands back the writer,
  the result, and the capability, which goes back into `cx`. So a `{text}` of a domain reads through
  the domain's context: `f"{line}:{col}: {message}".write(&cx, Buffer!::new())` with `message:
  Str['k]` and `&cx: Domain!['k]`. No `{x}` uses `cx` while it is lent (`cx is lent to this
  f-string; it cannot be used inside it`).
- **Closures** `|x| x + 1u32` have types `closure(u32) -> u32` as parameters. A closure captures
  the locals of its function it mentions, a linear one by moving it in, which makes the closure
  linear: it is then called once or handed on, as `std::thread::spawn` takes one that owns a `Buffer!`
  (`examples/thread_sum`). A parameter that is called many times or may go uncalled requires
  `Copy[Env]`, so what such a closure captures is `Copy`, and one that touches storage takes the
  context as a place parameter instead: `closure[Env](&_: Domain!['k], Type['k]) -> Type['k]`,
  written `|&cx, child: Type['k]| zonk(&cx, child)` and called `transform(&cx, ty)`. Plain function values
  have types `fn(u32) -> u32`, and a closure parameter does not take one: a comparator is passed
  as a closure that calls it, `items.sort_by(&cx, |&cx, a, b| a.cmp(&cx, b))`.
- **Modules.** Each `.kur` file is a module named by its path; `use package::name` imports one, and
  `lib.kur` lists them. `use package::syntax::ast as S;` then `S::Expr`. A directory is a module whose
  `lib.kur` lists its children, as `compiler/lib.kur` lists the phases.
- **Statics.** There are no global variables. State lives in values passed in, usually a struct of
  handles, and in the context `&cx` beside them.
