# Library contract inventory

This document classifies the functions of the `core`, `alloc`, `std`,
`libc` and `toml` packages by signature shape, and records the
caller obligation of every function whose signature exposes a raw pointer.
Items are named by module path within their home package
(`core::raw::raw_get`, `alloc::buffer::Buffer!::push_n`,
`libc::thread::spawn`); `std` re-exports the `core` and `alloc` modules
under `std::` paths, but not `libc`'s. `toml` declares no raw pointer, no `unsafe fn` and no
`extern "C"` function: all of it is class C.

A signature classification is not a proof of its implementation.
Restricted constructors, unsafe bodies, FFI contracts and target behavior
remain part of the trusted boundary.

## 0. How the classification works

### 0.1 The classes

Each function is classified by **signature alone**:

- **class A**: some parameter's type mentions a `Brand` **and** the return
  type mentions a capability (`core::cap::Exclusive!`/`Shared!`/`SharedJoin!`/
  `SharedResume!`, or `core::mem::Region!`/`RegionResume!`);
- **class B**: not class A, and a raw pointer type (`*T`, or the builtin
  `core::ptr::NonNull[T]`) occurs anywhere in a parameter type or the return type;
- **class C**: neither.

Classification is **never by name.** There is no function visibility: every
function a module declares can be named and called by any module that can
reach that module, so `alloc::collections::hashmap::free_one` is public API
even though it reads like an internal helper. "Make it module-private" is
not an available remedy for anything in this document.

### 0.2 Named types are not unfolded

The walk descends through the structure of a *written* type term: a
pointer's or `NonNull`'s inner type, tuple elements, an array's element type,
a named type's type **arguments**, an `exists` body, a `foreach` mapping
body, and the parameter and return types of a function type. It **never
opens a named type's declaration** to walk its fields. For `foreach`, the
source only selects the shape, so it is not traversed; an explicit pointer
in the mapping body is counted. Structural region split/merge over `*T` is
therefore not a raw-pointer API merely because its shape is selected
through indirection.

So `std::io::print(s: Slice[u8, 'static])` is class C even though its body
reads through the slice's pointer, while
`std::io::write_all(fd: i32, ptr: *u8, len: usize)` is class B: the same
pointer, spelled bare. Unfolding would put nearly every function that takes
or returns a `Slice`, `Span`, `Ref`, `Buffer!`, `Vec!`, `Arena!`, `HashMap!`
or `ArenaVec` into class B and make the class useless as a signal. The
walk under-reports in one predictable direction: **a pointer that reaches a
caller inside a checked structure is the structure's obligation, not the
function's.** Where that structure's invariant is at issue, the remedy is
making the type restricted (section 4), not this list.

### 0.3 Lowered closures

Lowering produces one function per closure literal. These are classified
like any other function, not filtered out. A closure has no declaration
site on which to write `unsafe fn`, so a closure in class B would need a
written ruling (section 2.0b).

## 1. Class A: brand-to-authority constructors (empty)

A brand value is `Copy` and can be read from any `Ref`, `Slice` or `Span`
that carries one. A function turning a brand value into a
capability for that brand would therefore make the authority copyable too.

**No library function does this.** The only ways to obtain an
`Exclusive!['k]` are to be handed one or to mint a fresh brand nobody else
has. Every minting entry point (`core::cap::Exclusive!::fresh`,
`alloc::owned::Allocation!::adopt` and `adopt_value`, `Vec!::new`, `HashMap!::new`,
`Arena!::new`, `Buffer!::open`, ...) mints its brand internally and returns
it **sealed** in an `exists`, so the brand is fresh per call and no caller
can name it in advance. Forging a second capability for a live brand is not
caught but **unspellable**: no function accepts the brand that such a forgery
would need to pass.

The functions that take a `Brand` value and return a branded object
(`Ref::from_raw`, `Slice::from_raw_parts`, `Span::from_raw_parts_branded`)
return data, not a capability, and are all `unsafe fn` (section 2).

### 1.1 Reading a brand out of a capability

`alloc::arena::Arena!::new` stores the arena's brand as a value in its
`marker` field, because `Arena!::alloc` brands pointers the arena computes
itself. It gets the brand from the `Ref` that `Region!::share` hands back
(`base_ref.into_parts()`). Reading a brand *out* of a capability you were
just given is not a forgery; it is the direction that stays sound.
`Arena!::marker` returns it to containers that live in the arena's domain:
holding the arena is what entitles a caller to place objects there.

## 2. Class B: a raw pointer in the signature

Each entry is a genuine caller obligation, normally declared with
`unsafe fn`. The checker enforces it: a call to an `extern "C"` function or
to an `unsafe fn` (directly, or through a function value whose type is
`unsafe`) is rejected unless the enclosing function is itself `unsafe fn`
or the call carries an `unsafe` marker. A safe function value cannot be
bound to an `unsafe fn`. A reader of a signature sees the obligation, a
reviewer of a diff sees where it is discharged, and a program that ignores
it does not compile.

The obligation shared by every entry below, not repeated in each row: any
`*T` passed in must be non-null, correctly aligned for `T`, point at a live
allocation of at least the implied extent, and not be aliased by any live
`Ref`/`Slice`/capability whose invariant the call would break. Any `*T`
handed back must not outlive the structure it came from.

A further obligation applies to every `extern "C"` declaration, class B or
not; see section 2.0c. Another applies to every lend of a raw pointer's
target, whatever the signature; see section 2.0d.

### 2.0 Class-B functions ruled safe

Class B is a *candidate* set: membership asks "can this function discharge
its own obligation and export a safe signature?" The functions below answer
yes. A blanket "class B means `unsafe fn`" rule would get them wrong.

| Function | Why it is safe |
|---|---|
| `alloc::heap::Uninit!::into_raw` | Consumes the linear `Uninit!` and returns its address. `Uninit!` is restricted to `alloc::heap`, so the address has no safe way back to an owner; it grants no dereference, ownership or reconstruction authority. |
| `alloc::heap::Uninit!::into_parts` | `into_raw`, plus the block's `usize` extent, which is plain data. |
| `core::ptr::null`, `core::ptr::is_null` | `null` returns the null `*T`, for comparing pointers against (the allocator's results, a hash chain's end, an `ArenaVec`'s missing segment); `is_null` compares with it. Dereferencing the pointer is a separate unsafe operation, checked at that site. |
| `core::slice::Slice::into_parts` | Returns the `(ptr, len, marker)` a `Slice` was built from. Rebuilding a `Slice` needs the `unsafe fn from_raw_parts`, so it grants no new authority. |
| `core::mem::Ref::into_parts` | Returns the `(ptr, marker)` a `Ref` was built from; rebuilding one needs the `unsafe fn Ref::from_raw`, so a stale pointer cannot be re-wrapped under a live brand in safe code. |
| `core::raw::Span::into_parts` | Returns the `(ptr, len)` a `Span` was built from, never its brand: a span must not hand out a witness for its domain. Reading through the pointer needs `unsafe`, and rebuilding a span needs the `unsafe fn Span::from_raw_parts`. |
| `core::cstr::CStr::as_ptr` | Returns the unchanged pointer of a `CStr`, which only unsafe adoption or `std::process::Args::get` can construct. A raw pointer alone grants no safe dereference or ownership. |
| `core::array::Array!::into_parts` | Consumes the restricted, linear `Array!` and returns its storage. The only way back is the `unsafe fn from_parts` (section 2.4). |
| `core::ptr::NonNull::new` | Checks its argument: a null pointer gives `None`, any other the unsafe cast `p as NonNull[T]`, whose one obligation (not null) the check has just discharged. |
| `core::ptr::NonNull::as_ptr` | `self as *T`, which the language makes safe: forgetting that a pointer is not null grants nothing. Using the pointer still requires `unsafe`, and turning it back into a `NonNull` goes through `new` or the `unsafe fn new_unchecked`. |

### 2.0b Members that cannot be declared

None. A lowered closure is always safe, since it has no site on which to
write `unsafe fn`. A closure that lands in class B needs a written ruling
here.

### 2.0c `extern "C"` and control flow

Every other obligation in this section concerns pointer provenance and
ownership. This one concerns **control flow** and applies to every
`extern "C"` declaration:

> **An `extern "C"` function must return normally to its caller.** It must
> not transfer control back into a frame the library is still executing by
> any other route: no `longjmp` into such a frame, no C++ exception
> propagating through one, no signal handler that resumes into it, no
> thread cancellation or `pthread_exit` unwinding through one. Terminating
> the process outright (`abort`, `_exit`) is permitted and relied on;
> resuming somewhere else is not.

Linear checking guarantees that every obligation live at a program point is
discharged on every path the checker can see, and the language has no
non-local control transfer of its own. A foreign frame that jumps back past
a library frame creates a path the checker never saw: every linear value
live in the abandoned frames is lost, along with whatever it was obliged to
free, unlock or restore.

**This is a stated rule, not a checked one.** Nothing in the compiler can
detect a violation, and an `unsafe` marker at the call site discharges the
call's pointer obligations, not this one. The obligation is on the foreign
code and can only be met by whoever chooses what to declare `extern "C"`.
The library's own externs satisfy it. They are declared in `alloc` (the
allocator's) and in `libc` (the C runtime's), each once; `std` declares
none and calls `libc`'s:

- `alloc::heap`: `malloc`, `realloc`, `free`, and `abort` and `exit`
  (declared `-> !`, the permitted terminating cases); `alloc::buffer`
  declares `abort` again for `Buffer!::new_with_cap` and `commit` (both
  below `libc`, so that a freestanding program needs only these);
- `libc::io` (`io.c`): `kur_libc_fs_read`, `_write`, `_open_read`,
  `_create`, `_close`, `_regular_size` and `_size_hint`, which retry a call
  a signal interrupts (but `close`) and report a failure as a negative
  errno, `kur_libc_stdin_claim` and `_release`, which set and clear the
  flag that makes one `std::io::Input!` at a time the reader of standard
  input, and `kur_libc_error_message` (`strerror_r`) and
  `kur_libc_error_kind`, which describe an errno;
- `libc::fs` (`fs.c`): `kur_libc_fs_error`, `_release`, `_canonicalize`,
  `_stat`, `_remove`, `_rename`, `_mkdir`, `_open_directory`,
  `_next_entry`, `_entry_name`, `_entry_is_directory` and
  `_close_directory`; `libc::env` (`env.c`): `kur_libc_env_get`;
- `libc::process` (`process.c`): `kur_libc_process_spawn`, which makes the
  pipes, the environment and the file actions a command asks for and
  starts the child with `posix_spawnp` (no library code runs in the
  child), `_wait`, which waits for it, again when a signal interrupts the
  wait, `_kill` and `_signal` (`kill(2)`), `_poll`, which waits for one of
  two pipes to be readable, again after a signal, and `_cpus`, which counts
  the processors of the affinity mask; and `start.c`'s `_hash_seed`, which
  reads the seed start-up drew (and aborts if it drew none);
- `libc::time` (`time.c`): `kur_libc_time_monotonic`, which reads the
  host's `CLOCK_MONOTONIC` (and aborts where it has none), and
  `kur_libc_time_sleep`, which sleeps on after a signal; `libc::float`:
  the C library's `strtod`;
- `libc::thread` (`thread.c`): `kur_libc_thread_error`, `_create` and
  `_join`. A thread `_create` starts runs `entry` to its return; it is
  never cancelled and never calls `pthread_exit`.

`start.c`'s `kur_libc_process_start` and `_report_error` are called by the
C `main` the compiler writes, not declared in Kursal; they return normally
or abort.

An extern that can `longjmp`, throw, or resume from a handler into its
caller is not made safe by this library's vocabulary or by an `unsafe`
marker.

**What an extern hands back is checked like a cast's result.** C's bytes
become the result of an `extern "C"` function, or the value of a KURS
`extern value`, with no Kursal code making them, as an unsafe cast's bytes
become its result. So the checker rejects such a result or value that holds,
anywhere in its value (named types' fields included, not behind a pointer),
what only checked code makes: a package, a brand witness, a safe function
value, a transform, or `Never` (`extern_result_cannot_produce`; a result of
`!` itself declares a function that does not return, as `abort` and `exit`
are declared). It also rejects one that holds an enum or a `NonNull`
(`extern_result_layout_chosen`): their layout is the compiler's choice, which
C cannot know, and a `NonNull` from C may be null. An `unsafe fn` value may
come back, since calling it needs `unsafe`. Parameters are not restricted
this way: what Kursal hands C was made by checked code. The library's own
externs return integers, floats, raw pointers, `()` and `!`.

### 2.0d Lending a raw pointer's target

`f(unsafe &*p)` lends the memory behind a raw pointer `p: *T` to a place
parameter, as `core::mem::lend` and `lend_in` do. The `unsafe` marker
discharges this obligation, which nothing checks:

> **`p` points at a live, initialized `T`, and from the call until it
> returns nothing reaches that memory except through the lent place:** no
> other copy of `p` or pointer derived from it, no `Ref`, `Slice` or
> capability over it, no other thread.

The callee treats the place as any other: it may move the value out and
must leave a `T` there when it returns. A compiler may rely on the
exclusivity as it relies on the escape rule for a local place, for example
to mark the parameter `restrict` or to keep reading a value where it lies
across a call (compiler/README.md, "Why `restrict` is sound where it is
written"). A program that breaks it has undefined behavior.

### 2.1 Frees, or authorizes freeing, an arbitrary pointer

All are `unsafe fn`. The obligation cannot be established from the
parameter types and nothing in the body can discharge it. Reaching any of
these with a pointer obtained from a live checked structure would be a
double free.

| Function | Caller must guarantee |
|---|---|
| `alloc::collections::hashmap::free_one` | `p` is a live, uniquely owned `alloc_one` result; no `Ref`, `Slice` or other pointer to it is still live |
| `alloc::collections::hashmap::destroy_chain` | `head` is a well-formed, uniquely owned entry chain; every entry, key box and value box along it is freed |
| `alloc::collections::hashmap::resize_hashed`, `resize_in` | `base` is a live bucket array of exactly `old_bc` slots; it is freed and replaced, so every pointer into it dies |
| `alloc::arena::free_chain_after_root_release` | `c` heads a chunk chain this arena built, **and** `Arena!::destroy` has already retired the root chunk's data buffer through `Allocation!::release`. Every header and every non-root data buffer is freed, so every arena pointer dies. Calling it without the prior `release` leaks the root buffer; calling it on a chain whose root was freed some other way double-frees it |
| `alloc::heap::HeapAllocator::adopt_raw` | `p` is a live `malloc`/`realloc` result with no other owner; it becomes a linear, freeable `Uninit!`. Records extent 0, since the size did not travel with the address |
| `alloc::heap::HeapAllocator::adopt_raw_sized` | `adopt_raw`'s obligation, **plus** `extent` is the size most recently requested for that block. With `adopt_raw`, the only way into an `Uninit!` from a bare pointer |
| `std::thread::trampoline` | `raw` is the `SpawnPayload[Env, Ret]` box `std::thread::spawn` built for the same `Env` and `Ret`, owned by nothing else: it is read, freed, and its closure called once. Reached only through the pointer `spawn` hands `libc::thread::spawn` |
| `libc::thread::spawn` | `entry(arg)` is a call whose obligations hold on the new thread (`entry` is typed `unsafe fn(*u8) -> *u8` for that reason; section 2.5); `arg`'s pointee stays valid while the new thread may use it, and accesses to it from the two threads are safe to interleave. On failure (the errno) no thread ran and `entry` was not called |
| `libc::thread::Thread!::join` | the `*u8` it returns is whatever `entry` returned, with no static knowledge of what it points to; the caller treats it as `entry`'s own contract says. The handle is consumed whether `pthread_join` succeeds or not |

### 2.2 Writes through an arbitrary pointer

| Function | Caller must guarantee |
|---|---|
| `alloc::collections::hashmap::alloc_one` | the returned `*T` is a fresh owning allocation that must reach `free_one` exactly once |
| `alloc::collections::hashmap::alloc_entry` | `key`, `val` and `next` are taken over by the new entry; none may be freed separately afterwards |
| `alloc::collections::hashmap::put_chain` | `head` is a well-formed chain; `key_ptr` is consumed into a new entry *or* freed (`is_new` says which) |
| `alloc::collections::hashmap::link_after` | `last` is null or an entry of the chain `head` starts; `rest` replaces what followed it |
| `alloc::collections::hashmap::rehash_bucket_hashed`, `rehash_bucket_in` | `new_base[0..new_bc)` is a live bucket array; every entry on `head` is relinked into it, and `head` must not be walked afterwards |
| `alloc::arena::bump_chunk` | `cur` is the arena's live current chunk; the reserved bytes and the returned chunk belong to that arena. The padding aligns the address itself, not the offset into the chunk |
| `alloc::arena::new_chunk` | `next` is a chain head or the chain terminator; the returned header is owned by the chain |
| `alloc::arena::new_header` | `next` is a chain head or the terminator, and `data` is a live buffer of at least `cap` bytes that the returned header takes over; passing a `data` still owned elsewhere makes two owners of one block |
| `alloc::arena::copy_bytes` | `dest[0..count)` is writable, `src[0..count)` readable, and the two ranges do not overlap |
| `alloc::buffer::Buffer!::commit` | every byte being published (`p[len..len+n)`) has been written since the last reserve/grow. Publishing past `cap` aborts |
| `alloc::buffer::Buffer!::from_initialized_allocation` | the consumed `Uninit!` records its capacity and the first `len` bytes are initialized. A `len` past the recorded capacity aborts |
| `alloc::collections::arena_vec::merge_runs` | `src` and `dst` each address `n` elements and do not overlap; one bottom-up merge pass of `ArenaVec::sort_by` and `Vec!::sort_by` |

Every write through a raw pointer also has this obligation: it never addresses the static
image, the bytes of a string constant (a `Slice[u8, 'static]` the compiler made). The C
backend emits them `static const`, so a write there is undefined and may fault.


### 2.3 Reads through an arbitrary pointer

| Function | Caller must guarantee |
|---|---|
| `core::raw::raw_get` | `base[0..len)` is a readable array; an out-of-range `i` returns `None` |
| `core::raw::raw_byte_at` | the same; out of range returns `0u8`, deliberately, for scanners that treat NUL as matching nothing |

| `core::raw::span_byte_at`, `core::slice::slice_byte_at` | the caller has just been handed a read capability for the view's brand; these read with no capability of their own and are the shared bodies of the `ReadBytes` instances |
| `core::cstr::cstr_len` | the pointer addresses a NUL-terminated byte run; there is no length to bound the scan |
| `core::cstr::CStr::from_raw` | `ptr` names a NUL-terminated byte run that stays readable and unchanged for the rest of the process, as `argv` entries do: a `CStr` is `Copy` and carries no brand, so nothing ends its use earlier |
| `std::fs::is_dot` | `name` addresses a live NUL-terminated string (a directory entry's name, read before the next read or close of its directory) |
| `core::fmt::Writable::write_raw` (and its instances) | `ptr[0..len)` is readable for the whole call; the writer reads the bytes before returning and keeps no pointer to them |
| `alloc::buffer::Buffer!::push_n` | `src[0..count)` is readable and does not alias the buffer's own storage, which the growth on entry may reallocate |
| `std::io::write_all` | `ptr[0..len)` is readable, and `fd` is a descriptor the caller may write: standard output or error, or one a `File!` it holds owns |
| `std::io::File!::write_all_raw` | `ptr[0..len)` is readable throughout the write |

Every read through a raw pointer (`unsafe *p`, and the functions above) owes the same thing for
brands: a value of a type that holds a brand witness (`Brand['r]`, `Exclusive!`, `Shared!`, a
receipt, anything holding or capturing one), zero-size or not, may be read only from memory that a
value of that type was moved into and not since moved out of. The checker does not track this; a
read that conjures a witness some other way breaks the obligation, as a read of any type from
memory that does not hold one does. An unsafe cast never produces a witness: the checker rejects a
cast whose result holds one in its value. At a zero-size type the checker rejects the read itself, and
the write: a load or store of one reads and writes nothing, so it is formed only at a type with an
erased value, which holds no brand witness, package or linear value (compiler/README.md, "Zero-size
types are erased"). Generic code is checked at its type parameter, which is not zero-size there; a
read or write it makes at a zero-size instantiation owes the same, and this obligation covers it.

A read through a pointer never produces `Never`. No memory holds a value of `!`, nor of a type that
holds one in its value (a struct, tuple or array of length above 0 with a component that does, an enum
with a payload that does, named types' fields included; not what a pointer in it points at), so such a
read has undefined behavior. The checker rejects a read whose pointee is such a type
(`unsafe_read_cannot_produce_never`), and so a lend of such a pointer's target
(`f(unsafe &*p)`, section 2.0d), from which the callee could read one. It checks a generic read, `unsafe *p` with `p: *T`, at `T`
itself, so it cannot see a `T` instantiated with such a type (`rd[!](p)` for
`forall T. unsafe fn rd(p: *T) -> T { unsafe *p }`): there the obligation is the caller's, as it is
for every other type the memory must hold.

### 2.4 Produces, offsets, or wraps a raw pointer

Individually mild (several are only integer arithmetic), but together they
are how a pointer gets out of a checked structure, and so how the other
groups receive reachable arguments. The safe members are the rulings in
section 2.0. Some rows run the other way, taking a pointer *in* and wrapping it in
a checked type; their obligation is the same kind of thing.

| Function | Caller must guarantee |
|---|---|

| `core::slice::Slice::from_raw_parts` | `ptr[0..len)` is live, initialized and accessible under `marker`'s brand, and `ptr` is not null, even when `len` is 0 or `T` is zero-size: `get_ref` makes a `Ref` of it, which is never null |
| `core::mem::Ref::from_raw` | `ptr` points at a live `T` inside the domain `marker` names, so presenting that domain's capability really authorizes the access |
| `core::raw::Span::from_raw_parts` | `ptr[0..len)` is initialized and stays readable while the domain of the context `&cx` lends has its capability. Literal-backed spans use the safe `Span::from_static` |
| `core::raw::Span::from_raw_parts_branded` | as `from_raw_parts`, for a module whose own object holds the domain's brand (an arena's `marker`); `marker` must be a witness that object really holds, since a span planted in another domain reads that domain's bytes |
| `alloc::buffer::Buffer!::as_ptr` | the pointer is invalidated by any later growth of the same buffer (which may reallocate) and by `destroy` |
| `alloc::buffer::Buffer!::tail_ptr` | the same; the bytes it addresses are uninitialized until written externally and committed |

| `alloc::arena::Arena!::alloc_bytes` | the returned run is reserved but **uninitialized** and unaligned; it must not outlive the arena |
| `core::mem::alloca` | the returned `*T` is used only until the calling function returns, when its storage is freed; and a linear value written there is moved out (or otherwise accounted for) before then. The checker tracks values and places, not raw storage: a linear `T` left in an `alloca` slot at return is dropped unseen |
| `core::ptr::offset` | `count` elements of `T` past `base` stay inside the block `base` points into; no bound is passed or checked. The one address computation of the library: every module that steps through a block calls it |
| `alloc::collections::arena_vec::place` | `i` is below the capacity of `h`'s segments, which are live arena blocks of the sizes `locate` computes |
| `alloc::collections::hashmap::alloc_buckets` | the returned array is owned: exactly one `resize`/`destroy` must free it |
| `core::mem::make_split_part` | `field` addresses initialized storage of `Field` that no other live capability covers, as for `Region!::from_raw`: the returned `Region!` is fresh authority over it. It returns only the part's fresh brand, never a `Link!`; `Link!` is restricted to `core::mem` and built only by `Splitting!::parts`, so a part over unrelated storage cannot be merged into a parent |
| `core::mem::Region!::from_raw` | `ptr` is not null, also for a zero-size `T`, and points at an initialized `T`, aligned for `T`, in at least `sizeof[T]()` bytes, that nothing else can reach while `'r` lives, and `cap` is the only capability over `'r` (in practice fresh from `Exclusive!::fresh`): the region's `replace` and `share` read and write the `T` unchecked. `Allocation!::adopt` passes its own obligation on, with a fresh capability over a block its `Uninit!` owned outright |
| `core::mem::lend`, `lend_in` | `p` points at a live `T` that nothing else reads or writes while `f` runs. `Ref::update`/`update_in` discharge it with the brand's `Exclusive!`; `Owned!::update`/`update_in` with the `Owned!` they hold. `lend_in`'s `cx` carries no obligation |
| `core::array::Array!::from_parts` | `base` addresses `len` initialized `T`s that nothing else reads or writes while the array exists; every later access is checked against `len` |
| `alloc::collections::vec::before`, `swap_at`, `sift_down` | `p` addresses the vector's own initialized elements and every index is below its length |
| `core::ptr::NonNull::new_unchecked` | `p` is not null: it is `unsafe (p as NonNull[T])`, after which the C compiler is told so. A null one is undefined behavior; at best it reads as the dataless variant of any enum in the niche layout that holds it (`Option[NonNull[T]]`, `Option[Ref[T,'k]]`, a `List` link; section 2.6) |

### 2.5 Hands a raw pointer to caller-supplied code

These take a function parameter that receives raw pointers the library
produces, so the obligation runs outward: the **callee** the caller
supplies is what must behave.

They are `unsafe fn` **on the function**, never `unsafe fn` on the
callback's type, and the difference matters:

- `unsafe fn visit(self, f: fn(*T) -> *E)` says "calling `visit` is
  unsafe": the caller writes `unsafe visit(f)` and thereby asserts the
  property `visit` cannot check.
- `fn visit(self, f: unsafe fn(*T) -> *E)` would say "the callback may do
  unsafe things": a safe `visit` that accepts an unsafe callback, callable
  with no marker anywhere. That is a different and, here, wrong statement.

| Function | Caller must guarantee |
|---|---|
| `alloc::collections::hashmap::hash_key`, `keys_equal` | the pointers address live keys of type `K`; each key is moved out and put back |
| `alloc::collections::hashmap::find_chain`, `find_chain_by`, `key_matches`, `put_chain`, `take_chain`, `rehash_bucket_hashed`, `resize_hashed` | the map's own chain and bucket invariants; reached from the safe `get`/`find`/`insert`/`remove` |
| `alloc::collections::hashmap::find_chain_in`, `put_chain_in`, `take_chain_in`, `rehash_bucket_in`, `resize_in` | the same invariants, for keys read through a domain's capability; reached from the safe `get_in`/`insert_in`/`remove_in` |

| `libc::thread::spawn` | see section 2.1 |

`libc::thread::spawn` is the one function whose callback type is itself
`unsafe fn(*u8) -> *u8`, and the rule above still holds. The callback is
not something `spawn` lets the caller make unsafe; it is an existing
`unsafe fn` (in practice `std::thread::trampoline`), whose own
obligation is about the `*u8` it is handed. A safe function value cannot be
bound to an `unsafe fn` parameter's type in reverse, and `spawn` is itself
`unsafe fn`: its caller writes `unsafe libc::thread::spawn(entry, arg)` and
thereby vouches that `entry(arg)` meets `entry`'s obligation on the new
thread. `std::thread::spawn` discharges it once, for the payload it
builds, and is safe.

`HashMap!::get`/`insert`/`remove` are
class C: they dispatch through the `Hash[K]`/`Eq[K]` traits instead of raw
function pointers, and they hand back every key and value they do not keep,
so a linear one is never lost.

### 2.6 `NonNull` and unsafe casts

`NonNull[T]` is a builtin type of the language, not a library struct:
a pointer like `*T` whose all-zero value is no value of the type. It is
Copy, as wide as `*T`, and compares with `==`/`!=`; it is not ordered and
has no fields. `core::ptr` exports it (`export builtin::NonNull;`) and
declares its methods (`new`, `new_unchecked`, `as_ptr`). The ways to make one are:

- `NonNull::new(p)`, which checks;
- an unsafe cast `unsafe (p as NonNull[T])` from a pointer or another
  `NonNull`, whose obligation is that `p` is not null (`new_unchecked`).
  The C written for the cast tells the C compiler that the result is not
  null (`if (p == 0) __builtin_unreachable();`), so it can fold the niche
  tests of the enums built from it; a null one is undefined behavior.

`n as *T` needs no `unsafe`. Nothing else casts to or from a `NonNull`:
not an integer, and not a struct, tuple or array that holds one, which
would make the same claim without saying so. A raw pointer is cast only to
or from a pointer, a `NonNull` or an integer, which C converts through
`uintptr_t` (`pointer_cast_pointer_or_integer`): not a `bool` or a float,
and not a struct, tuple or array, even one that holds only a pointer.

The niche of a `NonNull` lets the compiler represent an enum of a
dataless variant and a variant whose payload holds a `NonNull` as that
payload alone, the dataless variant written as the null (compiler/README.md,
"Enum layout"). So an enum's layout is the compiler's choice, and **no
unsafe cast takes or makes a value whose representation holds an enum or a
`NonNull`** (`unsafe_cast_layout_chosen`): reinterpreting an
`Option[NonNull[T]]` as a `*T` is rejected, not defined. Code that needs
the pointer matches the `Option`.

`as` between two number types is not a cast of this kind: it converts the
value (a float to an integer saturates, NaN to 0), is defined for every
value, and needs no `unsafe`. No unsafe cast takes a number to a number
(`unsafe_cast_between_numbers`). A float's bits are `f64::to_bits` and
`f64::from_bits` (and `f32`'s), class C: every bit pattern is a float and
every float a bit pattern. Inside, they cast a struct holding the float
alone to the integer of its width, or back.

### 2.7 What class B does not claim

Class B is an inventory of trusted obligations, not a deprecation list.
`unsafe fn` changes nothing about what is callable: it records who owes
what, and the checker requires each call to discharge it. It is not a
visibility mechanism.

## 3. Class C: safe as is

Everything else: `core::option`, `core::iter`, the list, vector, map and
arena facades, formatting and string functions, `core::hash`'s arithmetic
and traits, and the capability operations that consume or present
authority rather than manufacturing it (`Exclusive!::share`, `Shared!::split`,
`SharedJoin!::join`, `SharedResume!::resume`, `Ref::with`/`copy`/`update`,
`Allocation!::adopt_value`/`close`, `Allocation!::release` (for a `Copy`
`T` only: it gives up the `T` in the block), `Region!::share`/`split`,
`Splitting!::parts`, `Restore!::merge`, `RegionResume!::resume`,
`Owned!::new`/`update`).

Some class-C functions are still `unsafe fn` for an obligation that is not
about a pointer in the signature:

| Function | Caller must guarantee |
|---|---|
| `alloc::owned::Allocation!::adopt` | the block `u` owns holds an initialized `T`, aligned for `T`, in at least `sizeof[T]()` bytes: the `Region!` it returns reads and writes it as one (`Region!::from_raw`'s obligation). A `malloc` result is aligned for every type, so in practice the obligation is the size and the contents. `adopt_value` is the checked form: it aborts on a block smaller than `sizeof[T]()` and writes the `T` itself. The library's own callers (`Vec!`, `HashMap!`, `Buffer!::open`, `Owned!::open`) state at each call why the block qualifies, or why a region over uninitialized slots never leaves the module and is never read as a `T` |
| `alloc::owned::Allocation!::release_storage` | every `T` the block holds is accounted for: moved out, never written, or still tracked by the caller, which keeps the returned storage and its contents. The block is given up without reading it, so a linear `T` left in it unaccounted would be dropped. `release` is the safe form, for a `Copy` `T`; the library's one caller is `Vec!` (`push`/`pop` re-adopt the same storage at once and keep tracking the elements below `len`; `destroy` moves every element out first) |
| `std::io::File!::adopt` | the descriptor is live and open, with no other owner |
| `std::io::write_static_to`, `write_span_to`, `write_buffer_to` | `fd` is a descriptor the caller may write: standard output or error, or one a `File!` it holds owns. A number taken from anywhere else may name a descriptor a `File!` owns, or one closed and since reused, and the write would go around its owner. The safe output functions name standard output and error themselves (`print*`, `eprint*`, `Output!`), or write through a `File!` |
| `std::io::read_into`, `read_to_end_from` | `fd` is a descriptor the caller may read: stdin, or one a `File!` it holds owns. Reading advances the descriptor's offset, which is that owner's state. The safe reads (`File!::read`, `read_exact`, `read_to_end`, `Input!`) name their own descriptor |
| `std::process::pipe_end` | `fd` is -1 or a descriptor nothing else owns, which the `File!` made of it closes: the parent's end of a pipe `kur_libc_process_spawn` made for the child |

**Class C means "no brand-to-authority conversion and no raw pointer in the
signature". It does not mean "verified correct".**

### 3.1 Allocation failure, growth arithmetic, bounds

**`abort` where a request is not representable.**

- `alloc::heap::HeapAllocator::alloc`/`grow` abort when `malloc`/`realloc`
  returns null, and so does everything built on them: the constructors
  (`Buffer!::new`, `Vec!::new`, `HashMap!::new`, `Owned!::new`, the
  arenas, ...) and every operation that grows a container (`push`,
  `insert`, `Buffer!::reserve`, ...). These have no error channel: a
  `Result` from every growing operation would cascade into every caller.
  Recovery is offered where a program can plan for it, when a container is
  made: `Storage::try_alloc`/`try_grow`, `Owned!::try_new`,
  `Buffer!::try_new_with_cap`, `Vec!::try_new`, `OwnedVec!::try_new`,
  `HashMap!::try_new` and `OwnedMap!::try_new` return the `AllocError`
  instead (`try_grow` and `Owned!::try_new` with what they were given,
  unconsumed). A container they make still aborts if it later fails to
  grow. Each aborting constructor is its `try_` form, aborting on the
  error.
- Growth sizing goes through `alloc::heap::checked_add`/`checked_mul`, which
  abort instead of wrapping. Both guard before computing: the addition
  checks `a > usize::MAX - b`, never a post-hoc test on a wrapped value.
  `usize` is pointer-sized, and `usize::MAX` (`!0usize`) is folded by the C
  compiler at the target's width, so the guards hold on 32-bit and 64-bit
  targets alike.
- `alloc::buffer::Buffer!::commit` aborts when the new length would pass
  the capacity. Without the check, `open` would hand out a `Slice` longer
  than the allocation, and the slice's own bounds check would admit
  out-of-bounds indices.

**Zero sizes.** A zero-size type (`()`, a brand, a capability, an empty
struct, `[T; 0]`) has size 0: `sizeof[T]()` is `0`, `alignof[T]()` is 1, and
a load or store of one through a pointer reads and writes nothing (an unsafe
cast from or to one, an extern parameter or value of one, and a load or store
of one with no erased value, which holds a brand witness, a package or a
linear value, are rejected; compiler/README.md, "Zero-size types are
erased"). So every
container and allocation function builds at a zero-size element type:
`OwnedVec![()]`, `OwnedMap![K, ()]` (a set of owned keys), `Owned![()]`,
`Vec!`, `HashMap!`, `Arena!::alloc` and `alloc_slice`, `Ref::new`,
`ArenaVec` and `ArenaMap`. The size arithmetic is defined at 0, and no
pointer a zero-size value is reached through is null, so none lands in a
`NonNull` or a `Ref`:

- `alloc::heap::Storage::try_alloc`/`try_grow` (and so
  `HeapAllocator::alloc`/`grow`) ask `malloc`/`realloc` for one byte where
  the size is 0, so a block is never null and `realloc` never frees; the
  `Uninit!` records the requested 0. `Owned!::new` of a zero-size value and
  a `Vec!` of zero-size elements hold such a block;
- `checked_mul(n, 0usize)` is 0 without dividing by 0;
- a `Vec!` of a zero-size `T` starts with capacity `usize::MAX`, so `push`
  never grows its block of no bytes, and only a length past `usize::MAX`
  aborts (`checked_mul`);
- `HashMap!` allocates no box for a zero-size key or value: `alloc_one`
  gives the address `alignof[T]()` (1), which addresses no block and is not
  null, and `free_one` frees nothing for it;
- `alloc::arena`'s `bump_chunk` reserves no bytes for a 0-byte request and
  returns the current address, so zero-size requests may share an address
  with each other and with the next allocation;
- `alloca` of a zero-size type gives the address 1.

Identities taken from addresses (`Ref::address`, `ArenaVec::address`,
`ArenaMap::address`) are of headers that have bytes; two `Ref`s to
zero-size values may share an address.

**`Option` where a request is merely out of range.** An index past the end
is an ordinary, recoverable condition:

- `core::slice::Slice::get_ref` returns `Option[Ref[T,'k]]`, checked
  against the slice's `len`. It takes no capability: it computes an
  address and reads nothing, like `sub`, and every read or write through
  the `Ref` presents the brand's capability (`Ref::read` a `Shared!` node,
  `copy`/`with`/`set` the `Exclusive!`);
- `core::slice::Slice::sub` returns `Option[Slice[T,'k]]`, checked by
  subtraction (`start > len`, then `count > len - start`) so a `count` near
  `usize::MAX` cannot wrap into range;
- `alloc::collections::vec::Vec!::get_ref` returns `Option[Ref[T,'k]]`,
  checked against `len`, **not** `cap`: the slots in between are
  uninitialized;
- `core::slice::byte_or_zero` returns `0u8` out of range, matching
  `core::raw::raw_byte_at`.

**`trap` where a miss is a bug.** The accessors whose result has no room
for "none" (`Option::unwrap`, `Result::unwrap`, `Slice::at`, `Span::at`,
`Array!::at`, `ArenaVec::at`, `ArenaMap::at`, `OwnedVec!::at`,
`OwnedMap!::at`) stop the program with the builtin `core::process::trap`.
Each has a `get` beside it that returns an `Option`.
It needs no operating system, so `core` can offer them; nothing after it
runs, so it breaks no invariant and is safe to call.

### 3.2 Open in class C

- **No initialization evidence.** `Buffer!::commit` publishes storage on
  the caller's word that it has been written. The obligation is declared and enforced at the call
  site (section 2.2), but it is an attestation, not evidence. A linear "filled"
  counter would prove that `n` writes happened, not that these `n` slots
  were the ones written. The remaining callers have the kernel write the
  bytes through a raw pointer (`std::io::read_into`, behind `File!::read`
  and every read to the end, `read_line`, and `Error::message`, which the C
  library fills), where no per-slot operation exists to attach a witness to.
  Callers that only need a fully addressable scratch buffer use
  `Buffer!::new_zeroed`, which makes the claim true and carries no
  obligation.

## 4. Type transparency

A bare `struct`/`enum` declaration is restricted: only the declaring module
may construct or destructure it, at the surface and in the core IR alike.
`exposed` reveals the whole representation. There is no per-field privacy;
surface-only field privacy would not protect hand-written core IR or
another frontend.

An unsafe cast is not construction, and the checker does not restrict it by
exposure: `unsafe (x as T)` may make a value of a restricted `T` anywhere,
and `unsafe (t as X)` may take one apart into its representation. The cast's
obligation then includes every invariant of `T` that its module's own
constructors would establish (the rows of section 4.1): `unsafe (fd as
File!)` must name a descriptor nothing else owns, a `CStr` made from a
pointer must address a NUL-terminated string readable for the rest of the
process, an `Uninit!` must own the block it names. This is the obligation
the module's own unsafe constructors state (`File!::adopt`,
`CStr::from_raw`, `HeapAllocator::adopt_raw_sized`). A type that holds a
brand witness (a capability, a `Span`, a `Slice`, a `Ref`) is never made
this way: the checker rejects a cast whose result holds one (section 2.3). The
restriction protects what safe code can reach; an unsafe cast that breaks
the type's invariant has undefined behavior.

### 4.1 Restricted

Types whose hidden representation protects authority or bounds:

| Type | Module | Invariant |
|---|---|---|
| `Uninit!` | `alloc::heap` | owns a `malloc` result; forging one is an arbitrary `free` |
| `Owned![T]` | `alloc::owned` | sole owner of one heap `T` |
| `Allocation![T,'r]` | `alloc::owned` | the right to free one block |
| `Region![T,'r]` | `core::mem` | the right to access that block, and what `close`/`release` demand back |
| `RegionResume![T,'r]` | `core::mem` | the receipt `share` keeps when it trades a `Region!` for an `Exclusive!` and a `Ref`; the only route back to the `Region!` |
| `Link!['f,'r]` | `core::mem` | names both a split part's fresh brand and the parent brand it was split from, so a forged part cannot be merged into an unrelated region |
| `Restore![T,'r]` | `core::mem` | `split`'s receipt; holds the parent's `Exclusive!` while the parts are out, and rebuilds `Region![T,'r]` from its own stored pointer, never from a part |
| `Splitting![T,'r]` | `core::mem` | what `split` hands on to make the parts: the base pointer and the parent brand read from the region's own `Exclusive!`, consumed once by `parts` |
| `Ref[T,'k]` | `core::mem` | pairs a pointer with a brand; only the `unsafe fn from_raw` rebuilds one from raw parts |
| `Exclusive!['r]` | `core::cap` | *the* exclusive witness for a brand; only fresh-brand minting creates one |
| `Shared!['r,'q]` | `core::cap` | one node of a read tree over `'r`, identified by its lineage `'q` |
| `SharedJoin!['r,'q,'a,'b]` | `core::cap` | the only route from two specific child nodes back to their parent |
| `SharedResume!['r,'q]` | `core::cap` | the inverse of `share`; demands the exact root node back |
| `Slice[T,'k]` | `core::slice` | pairs a pointer with an independent `len`; a forged `len` reads and writes past the allocation through the bounds-checked accessors |
| `StaticRead` | `core::slice` | the read capability for `'static` byte slices; harmless to duplicate, since it grants no write, resume or deallocation |
| `Span['k]` | `core::raw` | bytes in domain `'k`, readable in safe code only through a capability that may read the domain (`ReadBytes`: its `Exclusive!`, a node of its shared-read tree, a context lending one); built only by `from_static` or the unsafe constructors |
| `CStr` | `core::cstr` | a live NUL-terminated string, built only by unsafe adoption or `Args::get`; so `len` and `bytes`, which read it as a `'static` view, are safe |
| `Array![T]` | `core::array` | storage with exactly `len` initialized elements |
| `Vec![T,'k]` | `alloc::collections::vec` | a pointer with a `len`/`cap` the module maintains; a forged one is indexed and freed through |
| `HashMap![K,V,'k]`, `Entry[K,V]` | `alloc::collections::hashmap` | `buckets` matches `bucket_count`; entries are the chain nodes `free_one`/`destroy_chain` free |
| `Drain!` | `alloc::collections::hashmap` | a map being taken apart; holds the map's capability so nothing else touches the entries meanwhile |
| `ArenaVec[T,'k]`, `Header` | `alloc::collections::arena_vec` | a header naming segments of the sizes `locate` computes, with `len` within them; a forged one is read and written through |
| `ArenaMap[K,V,'k]`, `ArenaSet[K,'k]`, `MapState`, `Entry` | `alloc::collections::arena_map` | slots that index live entries; a forged map probes out of range, which `trap` stops |
| `List[T,'k]`, `Cons[T,'k]`, `ListBuilder![T,'k]` | `alloc::collections::list` | a list is persistent: no code outside the module reaches a cell's `Ref`, so none can rewrite a cell a list holds (and make it cyclic). `ListBuilder!` writes only the cells of the list it is building, and is linear so that `finish` leaves no copy to link more cells onto the list it hands out |
| `Arena!['k]`, `Chunk` | `alloc::arena` | one long-lived brand tied to a chunk chain this module built. It holds only `Copy` values (`alloc`, `alloc_slice`, `Ref::new` and `List::cons` require `Copy`), so freeing its chunks unread loses nothing linear |
| `Buffer!`, `BufferRestore!['k]` | `alloc::buffer` | `Buffer!` owns a heap byte run that `destroy` frees with no capability; `BufferRestore!` plus the matching `Exclusive!` yields the owning `Buffer!` back |
| `FixedWriter!` | `alloc::fmt` | a buffer that never grows past `cap`; its contents are always a prefix of what was written |
| `HeapAllocator`, `Storage` | `alloc::heap` | zero-field receivers for the allocator's static methods; they own no memory |
| `AlignProbe[T]` | `core::mem` | never built; `alignof` takes only its size |
| `Stamp['r]` | `core::cap` | exposed to `core` alone (`exposed (package)`): `core` modules read a domain's brand out of it, code outside `core` can hold one but not open it |
| `File!`, `Output!`, `Input!` | `std::io` | `File!` owns one open descriptor; forging one closes an arbitrary descriptor. `descriptor` hands out its number, which no safe function takes. `Output!` writes to a descriptor it does not own (stdout, stderr), which a forged one could point anywhere. `Input!` is made only by `stdin`, once it holds standard input, so that one reader at a time reads ahead from it; a forged one would skip what another has read ahead, and its `finish` would give standard input back from under the one that holds it. No `File!` owns a standard stream's number: the C `main` of an executable (`kursal build`) opens `/dev/null` on each of descriptors 0, 1 and 2 the process started without, before the program runs |
| `Args` | `std::process` | `argv` and `argc` as C's `main` received them: made only by the C `main` the compiler writes for an entry function taking one (its `argv` has `count` readable entries, each a NUL-terminated string live for the rest of the process) |
| `Command!` | `std::process` | a command line of `count` NUL-terminated arguments, which `process.c` splits into the table `posix_spawnp` reads, and `env_ops` environment changes in the form `process.c` parses; a forged one would hand `process.c` a mismatched count. Its streams are `Stdio!` values, whose `File` is a `File!` the command closes after `spawn` |
| `Child!` | `std::process` | a child this process started and has not waited for, so its process id names it and no other: `wait`, `kill` and `signal` reach it. A forged one would `waitpid` or signal an arbitrary process, and reap a child another component owns. Its pipe ends are `File!`s |
| `Instant` | `std::time` | a monotonic clock reading; nothing outside makes one |
| `Directory!` | `std::fs` | owns one open `DIR *`; forging one hands `readdir`/`closedir` an arbitrary pointer |
| `Thread!` | `libc::thread` | owns one `pthread_t` handle that `join` frees; forging one joins and frees an arbitrary pointer |
| `Thread![Ret]`, `SpawnPayload`, `ResultBox` | `std::thread` | a spawned thread whose `join` reads back a `ResultBox[Ret]`; a forged one would read any thread's result as a `Ret`. The boxes never leave the module |
| `Parser`, `FlagNode`, `KeyPath` | `toml::parser` | the parser's own state and its record of what a document has defined; nothing outside reads or makes them |

A survey that finds no outside construction sites is evidence, not proof,
that restricting a type costs nothing: it only sees the code it is run on.

### 4.2 Deliberately exposed

- **`alloc::arena::Domain!`.** An arena with its capability, and nothing
  else: building one needs both, and taking one apart gives back only what
  went in.
- **`std::io::Error`.** An errno value, the one error type of `std::io`,
  `std::fs`, `std::process` and `std::thread` (`libc` reports the errno
  itself, an `i32`). Any number is a valid one, so building one grants
  nothing.
- **Values that hold only what their builder already had:**
  `alloc::collections::hashmap::OwnedMap!` and
  `alloc::collections::vec::OwnedVec!` (a map or vector packed with its
  capability), the iterators `core::slice::SliceIter!`,
  `alloc::collections::list::ListIter!`, `alloc::collections::vec::VecIter!`
  and `OwnedVecIter!` (a view or collection with the capability that reads
  it), `core::fmt::ReadingWriter!` (a writer and a context),
  `std::process::Captured!` (an `Exit` and two `Buffer!`s), `core::cursor::Cursor` (a
  `Slice` and a position; every read is checked whatever the position),
  `core::slice::Readable` (a capability and a view), `core::borrow::
  Borrowed!` and `NoCap`, `std::fs::DirEntry!` (an owned name and a
  flag), `std::fs::Metadata`, `std::time::Duration`, `core::fmt::CountingWriter` and `alloc::fmt::Full`.
  Taking one apart gives back only parts the builder held.
- **`core::iter`'s `Range`, `Pair`, `Map`, `Filter`, `Zip`, `Enumerate`,
  `Take`, `Skip`, `TakeWhile` and `Chain`.** Plain adapter state over an
  inner iterator.
- **`toml::value::TomlValue`/`TomlDatetime`/`DatetimeKind`/`TomlError`.**
  The parser's results, to be matched on; their spans, vecs and maps are
  read through the arena's capability like any other.
- **`core::option::Option`/`Result`, `core::cmp::Order`,
  `core::atomic::Ordering`, `alloc::heap::AllocError`,
  `std::process::Exit`, `Polled!` (a `Child!` or an `Exit`) and `Stdio!`
  (a `File!` or nothing), `std::io::ErrorKind`, `std::fs::FileKind`.** These exist to be matched on, and there is no per-variant privacy, so an enum
  meant to be matched on cannot be restricted. The general rule: **a type
  can be restricted when its outside users rely on methods rather than
  destructuring.**

## 5. Maintenance

Adding a function with a raw pointer in its signature means adding it here,
with its stated obligation, and either declaring it `unsafe fn` or giving a
written ruling in section 2.0. Membership is a fact about a signature; the contract
is the answer to the question membership asks. Keeping them separate is
what lets section 2.0's rulings exist without weakening the class.
