# Examples

Each directory is a package: a `kur.toml` manifest and its `.kur` sources. Packages with
`kind="executable"` are programs; the others are libraries that demonstrate a feature by compiling.
`bad/` holds single-file programs that the compiler must reject.

`tests/test_examples.py` in the reference compiler builds the packages, runs programs with the
arguments and input they expect, and checks that every program in `bad/` is rejected for its reason.
Packages that exist only to test the compiler, one feature or one past bug each, are in
[`reference-compiler/tests/fixtures/`](../reference-compiler/tests/fixtures/), and the same test builds
and runs them.
`libuv_echo` has a separate build check when libuv is installed and a loopback script for its socket behavior.

## Building and running one

From the repository root, build the compiler once (it needs `uv` and `clang`; see the root
[`README.md`](../README.md)), then build a program with it and run it:

```sh
scripts/bootstrap.sh
bin/kursal build examples/wordfreq -o /tmp/wordfreq
printf 'the cat and the hat\n' | /tmp/wordfreq
```

`bin/kursal build PACKAGE -o OUTPUT` writes the C into `OUTPUT.build`, a file per module, and
compiles it, with the C runtime (`lib/libc`), into `OUTPUT`; with `--single-c` it writes one
file, `OUTPUT.c`. Without `-o` it writes `a.out` and `a.out.build` in the current directory.
A library has no executable to build, so `build` of one needs `--emit-c`, which prints its C:
`bin/kursal build examples/linear --emit-c > /dev/null` checks it.
`bin/kursal build examples/bad/type_mismatch.kur` shows a rejected program's diagnostic.

The reference compiler builds a program the same way, from `reference-compiler/`:

```sh
uv run python tools/build.py ../examples/wordfreq /tmp/wordfreq
```

[`reference-compiler/README.md`](../reference-compiler/README.md) shows how to call its stages one by one,
and what each produces.

## Start here

Small programs that do something visible, each showing what is particular to Kursal. Read `hello`,
then `greet`, then `linear`, which declares a linear type of its own and consumes it.

| Example | What it shows |
|---|---|
| `hello` | the smallest program: `fn main()` printing a line with `std::io::print` |
| `greet` | reading a line from stdin into an owned `Buffer!`, and formatting a reply with `f"..."`; `main` returns `Result[(), std::io::Error]`, so `?` ends it on an I/O error |
| `linear` (library), with `bad/leaked_handle.kur`, `bad/double_use_handle.kur`, `bad/branch_inconsistent.kur`, `bad/while_consume_without_replacement.kur` | a linear type, `struct Handle! { ... }`, the control flow that consumes it, and the programs that get it wrong |
| `calc` | an integer calculator over stdin: parsing, and errors reported with their column |
| `wc`, `wordfreq` | command-line tools: line, word and byte counts of files; word frequencies of stdin |
| `graph` | a cyclic graph in one arena, read and written through one capability, with no `unsafe` |
| `thread_sum` | two OS threads, each given ownership of its own buffer |
| `config` | a TOML configuration read with the `toml` package, with defaults and its errors reported |
| `shop`, `stock` | a program and the library it depends on, which keeps its type's representation to itself |
| `consts` | module constants and associated constants |

## Advanced showcases

Larger programs: complete applications, or a feature used where it is the right tool.

| Example | What it shows |
|---|---|
| `kvstore` | a key-value store kept in a file |
| `json_check` | a JSON validator and pretty-printer |
| `routes` | shortest routes over a weighted, directed graph |
| `lisp` | a small Lisp interpreter: closures, recursion, a list library |
| `atomics` | atomic operations, memory orderings, a fence and volatile access, each with its reason and the `unsafe` contract it relies on |
| `blockalloc` | a fixed-size block allocator under a scripted workload |
| `host` | the host without `unsafe`: arguments (`fn main(args: Args)`), the environment, files and directories, running programs, a monotonic clock and a sleep, floats from text and their bits |
| [`libuv_echo`](libuv_echo/README.md) | a one-client TCP server: safe application code chooses to write each input buffer back, while a C shim owns libuv callbacks and buffers; building needs libuv headers and `-luv` |

## Reference examples

Each teaches one concept through its contracts and signatures; read the source. Most check themselves
and report through their exit status, 0 when every check holds.

| Example | What it shows |
|---|---|
| `generic_capabilities` | a user-defined resource protected by `core::cap` |
| `mem_counter`, `mem_shared_sum`, `mem_shared_view`, `mem_split_fields` | sole ownership, shared access, read trees and field projection through `core::mem` |
| `dlist` | a doubly linked list in one capability domain |
| `arena_text` | text and tables in one arena, through a context of the program's own that lends the arena and its capability (`Access`, `Allocate`, `ReadBytes`): `ArenaVec` and its combinators, span search and comparison, keys compared through `HashIn`, text built in a `Buffer!` (with decimal numbers) and saved into the arena, text copied to another arena |
| `arena_tables` | an `ArenaMap` and an `ArenaSet` in insertion order, with removal and growth, and scopes as a persistent `List`, in one `Domain!` |
| `election_tally` | a vote tally on `std::collections::hashmap` |
| `borrow_trait`, `convert_trait`, `format_trait`, `traits` | traits, including multi-parameter traits and the one behind `{expr}` interpolation |
| `format_strings` | `f"..."` strings |
| `iter_basics`, `iter_combinators`, `iter_custom`, `iter_for_loops`, `iter_collections` | iterators, adapters, custom iterators, `for` loops and collecting |
| `functions` (with `bad/function_value_at_two_brands.kur`) | function values, indirect calls and closures |
| `match_labels`, with `bad/match_unknown_label.kur` | match cases that name a variant by its label alone, `Some(x)` or `None`, looked up in the scrutinee's sum, also a sum with no name |
| `foreach_map`, with `bad/foreach_of_foreach.kur` | a `foreach` over a value of a `foreach` type, mapping each component once, over a source that is no `foreach` type |
| `foreach_types`, with `bad/foreach_holds_itself.kur`, `bad/foreach_pointer_over_parameter.kur`, `bad/foreach_pointer_over_leaf.kur`, `bad/foreach_through_pointer_to_sum.kur` | a `foreach` type in every type position: a field over a type parameter, a tuple's, an enum's and an array's component, a pointee, a package body, a type argument, a `foreach`'s source and body, through a pointer to a sum; a region split into its parts in one step |
| `modules` | a package whose public facade exports a namespace from an internal module |
| `core_only` | a program that needs only `core`: no allocator, no operating system |
| `heap` (library) | C-heap interop: `malloc` checked for null, a raw pointer cast, and `free` |
| `skolems`, `mint_join` (libraries), with `bad/mismatched_skolem.kur`, `bad/unbound_result_brand.kur`, `bad/reopened_brand.kur`, `bad/shadowing_exists_binder.kur` | brands: minting them, carrying them through branches and joins, and naming each apart from those in scope |
| `basic`, `arrays`, `tuples`, `shapes` (libraries) | structs, fixed-size arrays, tuples, enums |
| `generics`, `generic_result`, `option_result` (libraries) | generic types and functions, `Result`, `Option` and `?` |
| `recursion`, `strings` (libraries) | recursion and early `return`; string literals as `Str['static]`, text built with `put`, and an f-string reading a branded `Str['k]` |
| `closure_method` (library) | closure parameter types |

## Feature tests

Programs that each check one feature through its exit status.

| Example | Tests |
|---|---|
| `tuple_decl` | `tuple Name(T, ...);` declarations |
| `non_null` | `NonNull[T]`, the pointer that is never null, and the enums whose layout it fills: `Option[NonNull[T]]`, `Option[Ref[T, 'k]]`, a `List` link and a user's own enum are one word |
| `zero_size` | zero-size types (`()`, brands, capabilities, markers, arrays of them): no bytes in `sizeof`, in the structs that hold them, or in calls and matches; no raw read, write or `alloca` at one, so a container holds one only beside bytes |

## Rejected programs (`bad/`)

Each file states why it is rejected; the test checks the diagnostic.

Ordinary errors:

| File | Rejected because |
|---|---|
| `type_mismatch.kur` | a `bool` is where a `u32` is declared: no implicit conversions |
| `non_exhaustive_match.kur` | a `match` leaves out a variant of the enum |
| `match_unknown_label.kur` | a match case names a label the scrutinee's sum does not have |
| `lend_a_let.kur` | a `let` binding is lent, which only a `var` is |
| `lend_a_capture.kur` | a closure lends a `var` it captures, of which it holds a copy |
| `try_on_own_enum.kur` | `?` is applied to an enum of the program's own, which has the variants of `Option` but is not core's |
| `constructor_call.kur` | a variant is called like a function, `Option::Some(n)`, where it is built with `Option::Some.(n)` |
| `overlapping_instances.kur` | two instances of one trait apply to the same type |
| `private_import.kur` | a name is reached through a module that only imports it, without `export` |
| `forged_file.kur` | a `File!` is built outside its module, whose representation is restricted |
| `too_large_type.kur` | a type is not smaller than the address space, 2^64 bytes |
| `empty_array.kur` | an array type's length folds to 0, and an array has at least one element |
| `foreach_holds_itself.kur` | a struct holds itself by value through the pointee of a `foreach`'s pointer source |
| `foreach_of_foreach.kur` | a `foreach` maps a value whose `foreach` type has a `foreach` type as its source |
| `foreach_pointer_over_parameter.kur` | `foreach *F of T` matches pointer components, and a type parameter `T` may have none; only an explicit `*P` will do |
| `foreach_pointer_over_leaf.kur` | `foreach *F of (*u8, u32)` matches pointer components, and `u32` is none; the type is rejected before the body that misuses the parameter |
| `foreach_through_pointer_to_sum.kur` | a `foreach` traverses a pointer to a sum, whose components depend on the variant it holds; the type over it is formed |
| `extern_abi.kur` | an extern names a calling convention other than `"C"` |
| `inferred_type_in_annotation.kur` | `_` stands for a type argument in an annotation, where only an expression's is inferred |
| `type_alias_unused_parameter.kur` | a type alias's body leaves out one of its parameters |
| `type_alias_cycle.kur` | a type alias refers to itself through another |
| `type_alias_linear_unmarked.kur`, `type_alias_marked_not_linear.kur` | a type alias's name has the `!` exactly when the type it stands for is linear, and these do not |
| `type_alias_arity.kur` | a type alias is given more arguments than it takes |
| `type_alias_premises.kur` | a type alias's `forall` states a premise, which nothing would check |
| `exposed_type_alias.kur` | a type alias is `exposed`, and it has no representation of its own |
| `type_alias_as_namespace.kur` | a type alias names a member, where only the type it stands for does |
| `impl_through_alias_elsewhere.kur` | an `impl` through a type alias adds methods to `Slice` outside its package |
| `fstring_context_used_inside.kur` | an f-string uses the context `.write(&cx, out)` lends it, inside |

Ownership, brands and authority:

| File | Rejected because |
|---|---|
| `leaked_handle.kur` | a linear value is never consumed |
| `double_use_handle.kur` | a linear value is used twice |
| `branch_inconsistent.kur` | a linear value is consumed on one branch only |
| `while_consume_without_replacement.kur` | a loop consumes a linear value without replacing it |
| `linear_in_arena.kur` | a linear value is put in an arena, which never consumes what it holds |
| `mismatched_skolem.kur` | values of two different brands are mixed |
| `unbound_result_brand.kur` | a result type names a brand nothing binds |
| `function_value_at_two_brands.kur` | a function value, whose type quantifies no brand, is called at two brands |
| `reopened_brand.kur` | `let exists` opens a package under a brand name already in scope |
| `shadowing_exists_binder.kur` | an `exists` type's binder is spelled as a brand in scope |
| `push_after_finish.kur` | a list builder is used after `finish` consumed it and handed the persistent list out |
| `release_linear.kur` | `Allocation!::release`, which gives up the value in the block, is used at a linear type |
| `ref_after_remove_in.kur` | a `Ref` into a map is used after `remove_in` gave the map a new brand |
| `implicit_package_open.kur`, `implicit_package_open_plain_let.kur`, `implicit_package_open_assignment.kur` | an existential package is used without `let exists` opening it |
| `close_by_number.kur`, `write_by_number.kur`, `safe_adopt.kur` | an `unsafe fn` is called without `unsafe` |
| `unmarked_in_unsafe_fn.kur` | an `unsafe fn` reads through a raw pointer without `unsafe`: its body is no unsafe context |
