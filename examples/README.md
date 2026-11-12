# Examples

Each directory is a package: a `kur.toml` manifest and its `.kur` sources. Packages with
`kind="executable"` are programs; the others are libraries that demonstrate a feature and only need to
build. `bad/` holds single-file programs that the compiler must reject.

`tests/test_examples.py` in the reference compiler builds every package, runs every program with the
arguments and input it expects, and checks that every program in `bad/` is rejected for its reason.

## Building and running one

From `reference-compiler/`, with the standard library's C helpers linked in:

```sh
uv run python - <<'PY'
from kursal.resolve import resolve
from kursal.sigs import signatures
from kursal.lower import lower
from kursal.check import check_program
from kursal.cgen import emit_c
resolver = resolve("../examples/wordfreq")
sigs = signatures(resolver)
program = lower(resolver, sigs)
c = emit_c(program, check_program(program), sigs.name(resolver.packages[0].entry_decl))
open("/tmp/wordfreq.c", "w").write(c)
PY
cc -O1 -fno-builtin /tmp/wordfreq.c ../lib/libc/fs.c ../lib/libc/thread.c -lpthread -o /tmp/wordfreq
printf 'the cat and the hat\n' | /tmp/wordfreq
```

## Programs

| Example | What it shows |
|---|---|
| `calc` | an integer calculator over stdin, with error reporting |
| `json_check` | a JSON validator and pretty-printer |
| `lisp` | a small Lisp interpreter: closures, recursion, a list library |
| `kvstore` | a key-value store kept in a file |
| `routes` | shortest routes over a weighted, directed graph |
| `wc`, `wordfreq` | line, word and byte counts; word frequencies |
| `greet`, `sayhi` | reading stdin and formatting a reply |
| `election_tally` | a vote tally on `std::collections::hashmap` |
| `ring` | a fixed-capacity ring buffer over `OwnedVec!` |
| `dlist`, `graph` | a doubly linked list and a cyclic graph in one capability domain |
| `blockalloc` | a fixed-size block allocator under a scripted workload |
| `thread_sum` | two OS threads, each owning its own buffer |
| `core_only` | a program that needs only `core`: no allocator, no operating system |
| `modules` | a package whose public facade exports a namespace from an internal module |
| `mem_counter`, `mem_shared_sum`, `mem_shared_view`, `mem_split_fields` | sole ownership, shared access, read trees and field projection through `core::mem` |
| `generic_capabilities` | a user-defined resource protected by `core::cap` |
| `borrow_trait`, `convert_trait`, `format_trait`, `traits` | traits, including multi-parameter traits and the one behind `{expr}` interpolation |
| `format_strings` | `f"..."` strings |
| `iter_basics`, `iter_combinators`, `iter_custom`, `iter_for_loops`, `iter_collections` | iterators, adapters, custom iterators, `for` loops and collecting |
| `at_sugar` | `&` in-out parameters |
| `functions` | function values, indirect calls and closures |
| `tuple_decl` | `tuple Name(T, ...);` declarations |
| `box_store_liveness` | storing a linear value through a raw pointer |

## Libraries

| Example | What it shows |
|---|---|
| `basic`, `arrays`, `tuples`, `shapes` | structs, fixed-size arrays, tuples, enums |
| `generics`, `generic_result`, `option_result` | generic types and functions, `Result`, `Option` and `?` |
| `recursion` | recursion and early `return` |
| `strings` | string literals as `Slice[u8, 'static]` |
| `linear` | linear types and the control flow that consumes them |
| `skolems`, `mint_in_arm`, `mint_join`, `mint_loop` | brands: minting them and carrying them through branches, joins and loops |
| `closure_env_copy`, `closure_method` | closure parameter types |
| `heap` | C-heap interop through externs and unsafe casts |

## Rejected programs (`bad/`)

| File | Rejected because |
|---|---|
| `leaked_handle.kur` | a linear value is never consumed |
| `double_use_handle.kur` | a linear value is used twice |
| `branch_inconsistent.kur` | a linear value is consumed on one branch only |
| `while_consume_without_replacement.kur` | a loop consumes a linear value without replacing it |
| `mismatched_skolem.kur` | values of two different brands are mixed |
| `unbound_result_brand.kur` | a result type names a brand nothing binds |
| `implicit_package_open.kur`, `implicit_package_open_plain_let.kur`, `implicit_package_open_assignment.kur` | an existential package is used without `let exists` opening it |
