# Library contract inventory

This document classifies the functions of the `core`, `alloc` and `std`
packages by signature shape, and records the caller obligation of every
function whose signature exposes a raw pointer. Items are named by module
path within their home package (`core::raw::raw_get`,
`alloc::buffer::Buffer::push_n`); `std` re-exports most of these modules
under `std::` paths.

A signature classification is not a proof of its implementation.
Restricted constructors, unsafe bodies, FFI contracts and target behavior
remain part of the trusted boundary.

## 0. How the classification works

### 0.1 The classes

Each function is classified by **signature alone**:

- **class A**: some parameter's type mentions a `Brand` **and** the return
  type mentions a capability (`core::cap::Exclusive`/`Shared`/`SharedJoin`/
  `SharedResume`, or `core::mem::Region`/`RegionResume`);
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

So `std::io::print(fd: i32, s: Slice[u8, 'static])` is class C even though
its body reads through the slice's pointer, while
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
has. Every minting entry point (`core::cap::Exclusive::fresh`,
`alloc::owned::Allocation::adopt` and `adopt_value`, `Vec::new`, `HashMap::new`,
`Arena::new`, `Vec::reserve_n`, ...) mints its brand internally and returns
it **sealed** in an `exists`, so the brand is fresh per call and no caller
can name it in advance. Forging a second capability for a live brand is not
caught but **unspellable**: no function accepts the brand that such a forgery
would need to pass.

The functions that take a `Brand` value and return a branded object
(`Ref::from_raw`, `Slice::from_raw_parts`, `Span::from_raw_parts_branded`)
return data, not a capability, and are all `unsafe fn` (section 2).

### 1.1 Reading a brand out of a capability

`alloc::arena::Arena::new` stores the arena's brand as a value in its
`marker` field, because `Arena::alloc` brands pointers the arena computes
itself. It gets the brand from the `Ref` that `Region::share` hands back
(`base_ref.into_parts()`). Reading a brand *out* of a capability you were
just given is not a forgery; it is the direction that stays sound.
`Arena::marker` returns it to containers that live in the arena's domain:
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
| `alloc::heap::Uninit::into_raw` | Consumes the linear `Uninit!` and returns its address. `Uninit!` is restricted to `alloc::heap`, so the address has no safe way back to an owner; it grants no dereference, ownership or reconstruction authority. |
| `alloc::heap::Uninit::into_parts` | `into_raw`, plus the block's `usize` extent, which is plain data. |
| `alloc::heap::null_u8` | Returns the null `*u8`, for comparing allocator results against. Dereferencing it is a separate unsafe operation, checked at that site. |
| `core::slice::Slice::into_parts` | Returns the `(ptr, len, marker)` a `Slice` was built from. Rebuilding a `Slice` needs the `unsafe fn from_raw_parts`, so it grants no new authority. |
| `core::mem::Ref::into_parts` | Returns the `(ptr, marker)` a `Ref` was built from; rebuilding one needs the `unsafe fn Ref::from_raw`, so a stale pointer cannot be re-wrapped under a live brand in safe code. |
| `core::raw::Span::into_parts` | Returns the `(ptr, len)` a `Span` was built from, never its brand: a span must not hand out a witness for its domain. Reading through the pointer needs `unsafe`, and rebuilding a span needs the `unsafe fn Span::from_raw_parts`. |
| `core::cstr::CStr::as_ptr` | Returns the unchanged pointer of a `CStr`, which only unsafe adoption or the process-argument wrapper can construct. A raw pointer alone grants no safe dereference or ownership. |
| `std::process::Args::as_raw` | Returns the unchanged argv table of an already-adopted `Args`; dereferencing it requires `unsafe`. |
| `alloc::arena::ArenaBytes::into_parts` | Returns the exact pointer and initialized extent of a restricted arena byte run; grants no safe dereference or way to forge another checked view. |
| `core::array::Array::into_parts` | Consumes the restricted, linear `Array!` and returns its storage. The only way back is the `unsafe fn from_parts` (section 2.4). |
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
The library's own externs satisfy it: `malloc`/`realloc`/`free`, `abort`
(declared `-> !`, the permitted terminating case), `read`/`write`/`open`/
`close`/`lseek`, `clock`, and the `libc` adapters' C functions. An extern
that can `longjmp`, throw, or resume from a handler into its caller is not
made safe by this library's vocabulary or by an `unsafe` marker.

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
| `alloc::collections::hashmap::resize_hashed`, `resize_in` | `base` is a live bucket array of exactly `old_bc` slots; it is freed and replaced, so every outstanding `slot_ptr` into it dies |
| `alloc::arena::free_chain_after_root_release` | `c` heads a chunk chain this arena built, **and** `Arena::destroy` has already retired the root chunk's data buffer through `Allocation::release`. Every header and every non-root data buffer is freed, so every arena pointer dies. Calling it without the prior `release` leaks the root buffer; calling it on a chain whose root was freed some other way double-frees it |
| `alloc::heap::HeapAllocator::adopt_raw` | `p` is a live `malloc`/`realloc` result with no other owner; it becomes a linear, freeable `Uninit!`. Records extent 0, since the size did not travel with the address |
| `alloc::heap::HeapAllocator::adopt_raw_sized` | `adopt_raw`'s obligation, **plus** `extent` is the size most recently requested for that block. With `adopt_raw`, the only way into an `Uninit!` from a bare pointer |
| `alloc::buffer::Buffer::from_owned_ptr` | `adopt_raw` one layer up: `p` is a live `malloc`/`realloc` result of at least `cap` bytes with no other owner, `len <= cap`, and `p[0..len)` is initialized. The result is linear and frees `p` at `destroy` |
| `thread::thread::trampoline` | `raw` is the `SpawnPayload[Env, Ret]` box `thread::spawn` built for the same `Env` and `Ret`, owned by nothing else: it is read, freed, and its closure called once. Reached only through the pointer `spawn` hands `libc::thread::spawn` |
| `libc::thread::spawn` | `entry(arg)` is a call whose obligations hold on the new thread (`entry` is typed `unsafe fn(*u8) -> *u8` for that reason); `arg`'s pointee stays valid while the new thread may use it, and accesses to it from the two threads are safe to interleave. On failure no thread ran and `entry` was not called |

### 2.2 Writes through an arbitrary pointer

| Function | Caller must guarantee |
|---|---|
| `core::raw::raw_set`, `raw_byte_set` | `base[0..len)` is a writable array; an out-of-range `i` is a silent no-op, so a wrong `len` loses writes rather than corrupting memory |
| `alloc::collections::hashmap::alloc_one` | the returned `*T` is a fresh owning allocation that must reach `free_one` exactly once |
| `alloc::collections::hashmap::alloc_entry` | `key`, `val` and `next` are taken over by the new entry; none may be freed separately afterwards |
| `alloc::collections::hashmap::put_chain` | `head` is a well-formed chain; `key_ptr` is consumed into a new entry *or* freed (`is_new` says which) |
| `alloc::collections::hashmap::link_after` | `last` is null or an entry of the chain `head` starts; `rest` replaces what followed it |
| `alloc::collections::hashmap::rehash_bucket_hashed`, `rehash_bucket_in` | `new_base[0..new_bc)` is a live bucket array; every entry on `head` is relinked into it, and `head` must not be walked afterwards |
| `alloc::arena::bump_chunk` | `cur` is the arena's live current chunk; the reserved bytes and the returned chunk belong to that arena. The padding aligns the address itself, not the offset into the chunk |
| `alloc::arena::new_chunk` | `next` is a chain head or the chain terminator; the returned header is owned by the chain |
| `alloc::arena::new_header` | `next` is a chain head or the terminator, and `data` is a live buffer of at least `cap` bytes that the returned header takes over; passing a `data` still owned elsewhere makes two owners of one block |
| `alloc::arena::copy_bytes` | `dest[0..count)` is writable, `src[0..count)` readable, and the two ranges do not overlap |
| `alloc::buffer::Buffer::commit`, `BufferWriter::commit_external` | every byte being published (`p[len..len+n)`) has been written since the last reserve/grow. Publishing past `cap` aborts |
| `alloc::arena::ArenaByteWriter::commit_external` | exactly `count` bytes at the pointer `writable_tail` returned have been initialized |
| `alloc::collections::vec::Vec::reserve_n` | before any of `[old_len, old_len + extra)` is read through the returned `Slice`, or the returned `Vec!` is observed by anything that could read them, a valid `T` is written to every one of those slots |
| `std::io::read_exact`, `std::io::File::read_exact` | `buf[0..size)` is writable; the kernel writes through `buf`. For the free `read_exact`, also `fd` is a descriptor the caller may read: stdin, or one a `File!` it holds owns |

### 2.3 Reads through an arbitrary pointer

| Function | Caller must guarantee |
|---|---|
| `core::raw::raw_get` | `base[0..len)` is a readable array; an out-of-range `i` returns `None` |
| `core::raw::raw_byte_at` | the same; out of range returns `0u8`, deliberately, for scanners that treat NUL as matching nothing |
| `core::raw::raw_span_bytes_eq` | `a[0..len)` and `b[0..len)` are both readable; the byte loop behind the capability-checked span equality |
| `core::raw::span_byte_at`, `core::slice::slice_byte_at` | the caller has just been handed a read capability for the view's brand; these read with no capability of their own and are the shared bodies of the `ReadBytes` instances |
| `core::cstr::cstr_len`, `cstr_eq_lit` | the pointer addresses a NUL-terminated byte run; there is no length to bound the scan |
| `core::cstr::CStr::from_raw` | `ptr` names a live NUL-terminated byte run for as long as the `CStr` is used |
| `std::process::Args::from_main` | `argc`/`argv` obey the C `main` contract: `argc >= 0`, `argc` readable entries, each a NUL-terminated string live for the rest of the process |
| `core::hash::HashState::write_bytes` | `ptr[0..len)` is readable for the whole call |
| `core::fmt::Writable::write_raw` (and its instances) | `ptr[0..len)` is readable for the whole call; the writer reads the bytes before returning and keeps no pointer to them |
| `alloc::buffer::Buffer::push_n` | `src[0..count)` is readable and does not alias the buffer's own storage, which the growth on entry may reallocate |
| `std::io::write_all` | `ptr[0..len)` is readable |
| `std::io::File::write_all_raw` | `ptr[0..len)` is readable throughout the write |

### 2.4 Produces, offsets, or wraps a raw pointer

Individually mild (several are only integer arithmetic), but together they
are how a pointer gets out of a checked structure, and so how the other
groups receive reachable arguments. The safe members are the rulings in
section 2.0. Some rows run the other way, taking a pointer *in* and wrapping it in
a checked type; their obligation is the same kind of thing.

| Function | Caller must guarantee |
|---|---|
| `alloc::owned::Allocation::base` | the returned `*T` addresses memory the caller **still owns**, since the `Allocation!` stays with it (`Buffer::as_ptr`'s situation, not `into_raw`'s). It is invalidated by the `release`/`grow` that retires or moves the block, and it cannot be turned back into an `Allocation!` |
| `core::slice::Slice::from_raw_parts` | `ptr[0..len)` is live, initialized and accessible under `marker`'s brand |
| `core::mem::Ref::from_raw` | `ptr` points at a live `T` inside the domain `marker` names, so presenting that domain's capability really authorizes the access |
| `core::raw::Span::from_raw_parts` | `ptr[0..len)` is initialized and stays readable while the domain of the context `&cx` lends has its capability. Literal-backed spans use the safe `Span::from_static` |
| `core::raw::Span::from_raw_parts_branded` | as `from_raw_parts`, for a module whose own object holds the domain's brand (an arena's `marker`); `marker` must be a witness that object really holds, since a span planted in another domain reads that domain's bytes |
| `core::raw::Span::with_len` | the `len` bytes from the span's start are initialized and lie in the buffer the span points into |
| `alloc::arena::ArenaByteWriter::writable_tail` | the destination is valid only while the writer owns the arena; external code must commit exactly the bytes it initialized |
| `alloc::buffer::Buffer::as_ptr` | the pointer is invalidated by any later growth of the same buffer (which may reallocate) and by `destroy` |
| `alloc::buffer::Buffer::tail_ptr` | the same; the bytes it addresses are uninitialized until written externally and committed |
| `alloc::buffer::BufferWriter::writable_tail` | the same, within a `BufferWriter!` session: valid until the next `push`/`append`/grow, and `commit_external` must be called with the true count |
| `alloc::arena::Arena::alloc_bytes` | the returned run is reserved but **uninitialized** and unaligned; it must not outlive the arena |
| `core::raw::elem_ptr`, `core::slice::elem_ptr`, `alloc::collections::vec::elem_ptr`, `alloc::buffer::buffer_elem_ptr` | `base` is an array with more than `i` elements; no bound is passed or checked |
| `alloc::arena::byte_ptr_at` | `base + off` lies within one live chunk's data buffer |
| `alloc::collections::hashmap::slot_ptr` | `i` is below the bucket count `base` was allocated with |
| `alloc::collections::hashmap::alloc_buckets` | the returned array is owned: exactly one `resize`/`destroy` must free it |
| `alloc::collections::hashmap::null_entry` | the null entry pointer must only be compared, never dereferenced |
| `alloc::collections::hashmap::is_null_entry` | accepts any pointer; `false` means only non-null, not valid |
| `core::mem::make_split_part` | `field` addresses initialized storage of `Field` that no other live capability covers, as for `Region::from_raw`: the returned `Region!` is fresh authority over it. It returns only the part's fresh brand, never a `Link!`; `Link!` is restricted to `core::mem` and built only by `Region::split`, so a part over unrelated storage cannot be merged into a parent |
| `core::mem::Region::from_raw` | `ptr` points at an initialized `T`, aligned for `T`, in at least `sizeof[T]()` bytes, that nothing else can reach while `'r` lives, and `cap` is the only capability over `'r` (in practice fresh from `Exclusive::fresh`): the region's `replace` and `share` read and write the `T` unchecked. `Allocation::adopt` passes its own obligation on, with a fresh capability over a block its `Uninit!` owned outright |
| `core::mem::lend`, `lend_in` | `p` points at a live `T` that nothing else reads or writes while `f` runs. `Ref::update`/`update_in` discharge it with the brand's `Exclusive!`; `Owned::update`/`update_in` with the `Owned!` they hold. `lend_in`'s `cx` carries no obligation |
| `core::array::Array::from_parts` | `base` addresses `len` initialized `T`s that nothing else reads or writes while the array exists; every later access is checked against `len` |
| `alloc::collections::vec::before`, `swap_at`, `sift_down` | `p` addresses the vector's own initialized elements and every index is below its length |
| `alloc::collections::vec::merge_pass` | `src` and `dst` each address `n` elements and do not overlap |
| `core::ptr::NonNull::new_unchecked` | `p` is not null: it is `unsafe (p as NonNull[T])`, after which the C compiler is told so. A null one is undefined behavior; at best it reads as the dataless variant of any enum in the niche layout that holds it (`Option[NonNull[T]]`, `Option[Ref[T,'k]]`, a `List` link; section 2.6) |

### 2.5 Hands a raw pointer to caller-supplied code

These take a function parameter that receives raw pointers the library
produces, so the obligation runs outward: the **callee** the caller
supplies is what must behave.

They are `unsafe fn` **on the function**, never `unsafe fn` on the
callback's type, and the difference matters:

- `unsafe fn map(self, f: fn(*T) -> *E) -> Ref[E,'k]` says "calling `map`
  is unsafe": the caller writes `unsafe r.map(f)` and thereby asserts the
  property `map` cannot check.
- `fn map(self, f: unsafe fn(*T) -> *E) -> Ref[E,'k]` would say "the
  callback may do unsafe things": a safe `map` that accepts an unsafe
  callback, callable with no marker anywhere. That is a different and, here,
  wrong statement.

| Function | Caller must guarantee |
|---|---|
| `alloc::collections::hashmap::hash_key`, `keys_equal` | the pointers address live keys of type `K`; each key is moved out and put back |
| `alloc::collections::hashmap::find_chain`, `find_chain_by`, `key_matches`, `put_chain`, `take_chain`, `rehash_bucket_hashed`, `resize_hashed` | the map's own chain and bucket invariants; reached from the safe `get`/`find`/`insert`/`remove` |
| `alloc::collections::hashmap::find_chain_in`, `put_chain_in`, `take_chain_in`, `rehash_bucket_in`, `resize_in` | the same invariants, for keys read through a domain's capability; reached from the safe `get_in`/`insert_in`/`remove_in` |
| `core::mem::Ref::map` | `f` returns a pointer *into the same allocation* as its argument; the result is stamped with the original brand `'k` with no check that the new address belongs to it |

`HashMap::get`/`insert`/`remove` are
class C: they dispatch through the `Hash[K]`/`Eq[K]` traits instead of raw
function pointers, and they hand back every key they do not keep, so a
linear key is never lost.

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
would make the same claim without saying so.

The niche of a `NonNull` lets the compiler represent an enum of a
dataless variant and a variant whose payload holds a `NonNull` as that
payload alone, the dataless variant written as the null (compiler/README.md,
"Enum layout"). So an enum's layout is the compiler's choice, and **no
unsafe cast takes or makes a value whose representation holds an enum or a
`NonNull`** (`unsafe_cast_layout_chosen`): reinterpreting an
`Option[NonNull[T]]` as a `*T` is rejected, not defined. Code that needs
the pointer matches the `Option`.

### 2.7 What class B does not claim

Class B is an inventory of trusted obligations, not a deprecation list.
`unsafe fn` changes nothing about what is callable: it records who owes
what, and the checker requires each call to discharge it. It is not a
visibility mechanism.

## 3. Class C: safe as is

Everything else: `core::option`, `core::iter`, the list, vector, map and
arena facades, formatting and string functions, `core::hash`'s arithmetic
and traits, and the capability operations that consume or present
authority rather than manufacturing it (`Exclusive::share`, `Shared::split`,
`SharedJoin::join`, `SharedResume::resume`, `Ref::with`/`copy`/`update`,
`Allocation::adopt_value`/`close`/`release`, `Region::share`/`split`/`merge`,
`RegionResume::resume`, `Owned::new`/`update`).

Some class-C functions are still `unsafe fn` for an obligation that is not
about a pointer in the signature:

| Function | Caller must guarantee |
|---|---|
| `alloc::owned::Allocation::adopt` | the block `u` owns holds an initialized `T`, aligned for `T`, in at least `sizeof[T]()` bytes: the `Region!` it returns reads and writes it as one (`Region::from_raw`'s obligation). A `malloc` result is aligned for every type, so in practice the obligation is the size and the contents. `adopt_value` is the checked form: it aborts on a block smaller than `sizeof[T]()` and writes the `T` itself. The library's own callers (`Vec!`, `HashMap!`, `Buffer::open`, `Owned::open`) state at each call why the block qualifies, or why a region over uninitialized slots never leaves the module and is never read as a `T` |
| `std::io::File::adopt` | the descriptor is live and open, with no other owner |
| `std::io::read_to_end_from` | `fd` is a descriptor the caller may read: stdin, or one a `File!` it holds owns. Reading advances the descriptor's offset, which is that owner's state |

**Class C means "no brand-to-authority conversion and no raw pointer in the
signature". It does not mean "verified correct".**

### 3.1 Allocation failure, growth arithmetic, bounds

**`abort` where a request is not representable.**

- `alloc::heap::HeapAllocator::alloc`/`grow` abort when `malloc`/`realloc`
  returns null. The constructors built on them have no error channel, and a
  `Result` would cascade into every constructor in the library and every
  caller of those. `grow` has a further reason: a failed `realloc` leaves
  the old block live while its owning `Uninit!` has already been consumed,
  so there is nothing to hand an error back in.
- Growth sizing goes through `alloc::heap::checked_add`/`checked_mul`, which
  abort instead of wrapping. Both guard before computing: the addition
  checks `a > usize::MAX - b`, never a post-hoc test on a wrapped value.
  `usize` is pointer-sized, and `usize::MAX` (`!0usize`) is folded by the C
  compiler at the target's width, so the guards hold on 32-bit and 64-bit
  targets alike.
- `alloc::buffer::Buffer::commit` aborts when the new length would pass
  the capacity. Without the check, `open` would hand out a `Slice` longer
  than the allocation, and the slice's own bounds check would admit
  out-of-bounds indices.

**Zero sizes.** A zero-size type (`()`, a brand, a capability, an empty
struct, `[T; 0]`) has size 0 and no memory form: `sizeof[T]()` is `0`, and
the compiler rejects every load or store of one through a pointer, also
where a specialization chooses it. So `Arena::alloc`, `alloc_slice`,
`Ref::new`, `Ref::copy` and the vectors do not build at a zero-size element
type (one holding a zero-size part beside bytes is ordinary). The size
arithmetic is still defined at 0, for raw code and for `T`s that only
compute with their size:

- `alloc::heap::Storage::try_alloc`/`try_grow` (and so
  `HeapAllocator::alloc`/`grow`) ask `malloc`/`realloc` for one byte where the size is 0, so a block is never
  null and `realloc` never frees; the `Uninit!` records the requested 0;
- `checked_mul(n, 0usize)` is 0 without dividing by 0;
- `alloc::arena`'s `bump_chunk` reserves no bytes for a 0-byte request and
  returns the current address, so zero-size requests may share an address
  with each other and with the next allocation.

Identities taken from addresses (`Ref::address`, `ArenaVec::address`,
`ArenaMap::address`) are of headers that have bytes; two `Ref`s to
zero-size values, which only `Ref::from_raw` can make, may share an
address.

**`Option` where a request is merely out of range.** An index past the end
is an ordinary, recoverable condition:

- `core::slice::Slice::get_ref` returns `Option[Ref[T,'k]]`, checked
  against the slice's `len`;
- `core::slice::Slice::sub` returns `Option[Slice[T,'k]]`, checked by
  subtraction (`start > len`, then `count > len - start`) so a `count` near
  `usize::MAX` cannot wrap into range;
- `alloc::collections::vec::Vec::get_ref` returns `Option[Ref[T,'k]]`,
  checked against `len`, **not** `cap`: the slots in between are
  uninitialized;
- `core::slice::byte_or_zero` returns `0u8` out of range, matching
  `core::raw::raw_byte_at`.

**`trap` where a miss is a bug.** The accessors whose result has no room
for "none" (`Option::unwrap`, `Result::unwrap`, `Slice::at`, `Span::at`,
`ArenaVec::at`, `ArenaMap::at`) stop the program with the builtin
`core::process::trap` (through `alloc::heap::out_of_range` in `alloc`).
It needs no operating system, so `core` can offer them; nothing after it
runs, so it breaks no invariant and is safe to call.

### 3.2 Open in class C

- **No initialization evidence.** `Buffer::commit`, the `commit_external`
  operations and `Vec::reserve_n` publish storage on the caller's word that
  it has been written. The obligation is declared and enforced at the call
  site (section 2.2), but it is an attestation, not evidence. A linear "filled"
  counter would prove that `n` writes happened, not that these `n` slots
  were the ones written. The remaining callers have the kernel write the
  bytes through a raw pointer (`std::io::File::read_to_buffer`, `read_all_stdin`,
  `read_line`), where no per-slot operation exists to attach a witness to.
  Callers that only need a fully addressable scratch buffer use
  `Buffer::new_zeroed`, which makes the claim true and carries no
  obligation.

  `Vec::get_ref`'s bound narrows the consequence without closing it: a
  `reserve_n` caller can still read the uninitialized region through the
  `Slice` it is handed. For `u8` that is an uninitialized byte; for a
  general `Copy` `T` it is a value synthesized from allocator residue.

## 4. Type transparency

A bare `struct`/`enum` declaration is restricted: only the declaring module
may construct or destructure it, at the surface and in the core IR alike.
`exposed` reveals the whole representation. There is no per-field privacy;
surface-only field privacy would not protect hand-written core IR or
another frontend.

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
| `Restore![T,'r]` | `core::mem` | `split`'s receipt; rebuilds `Region![T,'r]` from its own stored pointer, never from a part |
| `Ref[T,'k]` | `core::mem` | pairs a pointer with a brand; only the `unsafe fn from_raw` rebuilds one from raw parts |
| `Exclusive!['r]` | `core::cap` | *the* exclusive witness for a brand; only fresh-brand minting creates one |
| `Shared!['r,'q]` | `core::cap` | one node of a read tree over `'r`, identified by its lineage `'q` |
| `SharedJoin!['r,'q,'a,'b]` | `core::cap` | the only route from two specific child nodes back to their parent |
| `SharedResume!['r,'q]` | `core::cap` | the inverse of `share`; demands the exact root node back |
| `Slice[T,'k]` | `core::slice` | pairs a pointer with an independent `len`; a forged `len` reads and writes past the allocation through the bounds-checked accessors |
| `StaticRead` | `core::slice` | the read capability for `'static` byte slices; harmless to duplicate, since it grants no write, resume or deallocation |
| `Span['k]` | `core::raw` | bytes in domain `'k`, readable in safe code only through that domain's capability; built only by `from_static` or the unsafe constructors |
| `CStr` | `core::cstr` | a live NUL-terminated string, built only by unsafe adoption or `Args` |
| `Array![T]` | `core::array` | storage with exactly `len` initialized elements |
| `Vec![T,'k]` | `alloc::collections::vec` | a pointer with a `len`/`cap` the module maintains; a forged one is indexed and freed through |
| `HashMap![K,V,'k]`, `Entry[K,V]` | `alloc::collections::hashmap` | `buckets` matches `bucket_count`; entries are the chain nodes `free_one`/`destroy_chain` free |
| `Drain!` | `alloc::collections::hashmap` | a map being taken apart; holds the map's capability so nothing else touches the entries meanwhile |
| `ArenaVec[T,'k]`, `Header` | `alloc::collections::arena_vec` | a header naming segments of the sizes `locate` computes, with `len` within them; a forged one is read and written through |
| `ArenaMap[K,V,'k]`, `ArenaSet[K,'k]`, `MapState`, `Entry` | `alloc::collections::arena_map` | slots that index live entries; a forged map probes out of range, which `out_of_range` stops |
| `Arena!['k]`, `Chunk` | `alloc::arena` | one long-lived brand tied to a chunk chain this module built. It holds only `Copy` values (`alloc`, `alloc_slice`, `Ref::new` and `List::cons` require `Copy`), so freeing its chunks unread loses nothing linear |
| `ArenaByteWriter!`, `ArenaBytes` | `alloc::arena` | arena byte runs whose public length is exactly the initialized prefix |
| `Buffer!`, `BufferRestore!['k]` | `alloc::buffer` | `Buffer!` owns a heap byte run that `destroy` frees with no capability; `BufferRestore!` plus the matching `Exclusive!` yields the owning `Buffer!` back |
| `FixedWriter!` | `alloc::fmt` | a buffer that never grows past `cap`; its contents are always a prefix of what was written |
| `HeapAllocator`, `Storage` | `alloc::heap` | zero-field receivers for the allocator's static methods; they own no memory |
| `File!`, `Output!` | `std::io` | `File!` owns one open descriptor; forging one closes an arbitrary descriptor |
| `Args` | `std::process` | an argv table adopted under the C `main` contract |
| `CpuInstant` | `std::time` | a `clock()` reading |

A survey that finds no outside construction sites is evidence, not proof,
that restricting a type costs nothing: it only sees the code it is run on.

### 4.2 Deliberately exposed

- **`alloc::arena::Domain!`.** An arena with its capability, and nothing
  else: building one needs both, and taking one apart gives back only what
  went in.
- **`core::raw::Pos`.** A plain source position; no invariant to protect.
- **`std::io::Error`.** An errno value. Any number is a valid one, so
  building one grants nothing.
- **`alloc::collections::list::ListBuilder`.** The first and last cells of
  a list being built; it holds only `List` and `Ref` values, which any
  holder of the domain could make.
- **`core::option::Option`/`Result`, `alloc::collections::list::List`/
  `Cons`, `alloc::collections::vec::Popped!`, the `Found` enums.** These
  exist to be matched on, and there is no per-variant privacy, so an enum
  meant to be matched on cannot be restricted. The general rule: **a type
  can be restricted when its outside users rely on methods rather than
  destructuring.**

## 5. Maintenance

Adding a function with a raw pointer in its signature means adding it here,
with its stated obligation, and either declaring it `unsafe fn` or giving a
written ruling in section 2.0. Membership is a fact about a signature; the contract
is the answer to the question membership asks. Keeping them separate is
what lets section 2.0's rulings exist without weakening the class.
