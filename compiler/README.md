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
| `support::base` | | `fail`; its storage is the standard library's (see below) |
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
never leaves one dangling. `support::base` has no storage of its own: it keeps `fail`. Text is
joined with `ArenaVec::join`, vecs with `concat`, sets with `union` and `insert_all`.

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
`std::hash::key_hash(&cx, key)` and `key_eq(&cx, a, b)`. The standard library has the instances for the
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

## The C backend

`backend::cgen` translates checked KURS to one C file, as `cgen.py` does, byte for byte. The
translation gives a C variable to each SSA value the C reads, a C object to each place, and a
label to each block; on top of that it applies the steps below, all of them in both compilers. They
read the checked program and change only what C is written: the KURS is never rewritten, so the
checker's verdict covers the program the C translates.

What a function leaves out is decided first, from its KURS alone (`backend::plan`, `plan.py`):

- **Dead values.** A value is live when the C reads it: a kept instruction, a terminator, or an edge
  into a live block parameter (a fixpoint over the blocks). A pure instruction (constants, copies,
  loads, arithmetic other than division, construction and destruction, tags, casts between integers,
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
while the call runs (by induction on the calls, from the locals). Where the argument does not hold,
`restrict` is not written:

- **Raw targets.** `f(&*p)` lends the memory behind a raw pointer, which any copy of `p` (another
  argument, a field of something reachable, another thread) may reach. Its exclusivity is the
  unsafe caller's obligation, which nothing checks, so the callee's parameter is not `restrict`, nor
  is any parameter that place is lent on to.
- **Functions whose address is taken.** A call through a function value may lend anything, and a
  function pointer's type has no qualifiers: such a function's place parameters are neither
  `restrict` nor `const`, and a parameter lent through a function value is not `const`.
- **A tail call to itself that moves a place parameter** (`f(&b, &a)` from `f(&a, &b)`): the jump
  to the entry block reuses the parameters, so one C pointer would reach two places within one
  execution of the function.

`const` needs no such argument: it only records that the function's C does not write through the
pointer, which the checker's place rules let the backend see.

### Zero-size types are erased

A type is zero-size when `zero_size` (`types::ops`, `types.py`) says so: `()`, a brand witness
(`Brand['k]`), `!`, a struct or tuple whose components are all zero-size (an empty struct, a
capability such as `Exclusive!['k]`, `(Brand['k], ())`), an array of length 0 or of zero-size
elements, an `exists` whose body is, and a `foreach` whose expansion is. An enum never is: its tag
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
- the one C object a zero-size type gets is a `data` of it (an empty string's bytes, `[u8; 0]`),
  whose address the program can observe and compare with null: a `static char`.

There is no `KurUnit` and no adapter for foreign functions returning `()`: a foreign `void`
function is its own function pointer. A function that takes and returns only zero-size values is
`static void f(void)`, and a call of it still runs it.

The language keeps its zero-size types out of memory, so erasing them never changes what a
program reads or writes. `alloca`, the unsafe loads and stores (volatile or not), unsafe casts from
or to one, extern parameters and extern values reject a zero-size type
(`zero_size_type_has_no_memory_form`), and so does the backend where a specialization chooses one
(`a zero-size type has no memory form`). So `Ref::new(&cx, ())`, `ArenaVec[(), 'k]` and a
`Vec![Marker]` do not build: they read and write their elements through pointers; an element that
holds a zero-size part beside bytes (`ArenaVec[(Marker, u32), 'k]`) does. `sizeof` is the one
operation that observes a zero-size type, and it is 0; the arithmetic the standard library does
with sizes is defined at 0 (`checked_mul` does not divide by it, `bump_chunk` reserves no bytes and
returns the current address, `HeapAllocator::alloc(0)` asks `malloc` for one byte so that it is
never null).

Two zero-size objects may share an address, with each other and with what holds them. Nothing relies on distinct addresses for zero-size data: the identities the
compiler and the standard library compare (`Ref::address`, `ArenaVec::address` and
`ArenaMap::address`, which `Meta::id`, `fun_key` and the checker's copy cache use, and `Span`'s
equal-address shortcut) are addresses of headers and states that have bytes, and a `Ref` to a
zero-size value cannot be read, so it cannot be built by `Ref::new`.

### Enum layout: tags and niches

An enum is a `uint32_t tag` and a union of its payloads, except where its types give it a cheaper
form. `NonNull[T]` (`builtin::NonNull T`), a pointer that is never null, has a **niche**: its
all-zero value is none of its values. `niche` (`types::ops`, `types.py`) finds a type's niche from
its structure alone:

- a `NonNull` has one, at offset 0, and offers one invalid value, the null pointer;
- a struct or tuple has the niche of its first component, in declaration order, that has one, and a
  non-empty array that of its element 0;
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
  cast from a pointer, whose obligation is that it is not null, or by `NonNull::new`, which checks.
  `variant_tag` and `switch` of a place need the place full, so its niche holds a `NonNull` it was
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
- **Places, lends, `const` and `restrict`** are as before: a place under its known variant is the
  enum's object (see "Why `restrict` is sound"); packing and opening a niche-layout enum copy its
  value like any other.

### The C compiler

`kursal build` compiles the C with `$KURSAL_CC`, else `clang`, with `-O3 -w -fno-builtin`, and so do
`tools/build.py` and every test (the reference's `kursal/cc.py`). `-w` silences warnings about
generated code (unused labels, the standard library's own prototypes of C functions);
`-fno-builtin` keeps the C compiler from giving a function the meaning of the C library function of
the same name, which an extern or a Kursal function may declare with another type.

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
- **`NonNull[T]`** is a builtin type, like `*T` but never null: `NonNull::new(p)` checks and returns
  an `Option`, `unsafe (p as NonNull[T])` asserts it, `n as *T` (or `n.as_ptr()`) is safe, and `==`
  compares two. Its niche makes `Option[NonNull[T]]` and `Option[Ref[T, 'k]]` one word (see "Enum
  layout").
- **Zero-size types** (`()`, empty structs, brands, capabilities) take no bytes (`sizeof` is 0, see
  "Zero-size types are erased") and have no memory form: no `Ref`, vec or map stores one as a whole
  value. Use `bool` as a placeholder value (`ArenaSet` is an `ArenaMap[K, bool]`).
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
