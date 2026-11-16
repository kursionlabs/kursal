# Thread spawning

Depends on `libc` and on `std` directly: a package sees only its own
dependencies' names, so `libc`'s dependency on `std` does not make `std::`
paths visible here. Link with `lib/libc/thread.c` and `-pthread`, as for
`libc::thread`; this package has no C code of its own.

`spawn(f: closure[Env]() -> Ret) -> Result[Thread![Ret], (closure[Env]() -> Ret, std::io::Error)]`
moves `f`, and whatever it captured, into a new OS thread, and
`Thread![Ret]::join(self) -> Result[Ret, std::io::Error]` blocks until it
finishes and returns its `Ret` value. When no thread can be started,
`spawn` hands `f` back unrun beside the error, so a linear value it
captured is still the caller's. `Ret` may be a zero-size type such as
`()`. There is no `detach`: a spawned
thread's `Ret` must always reach a `join`, so a linear value it produces is
never stranded in an abandoned thread with nobody left to destroy it.

**Why `spawn` is safe to call.** Holding a value does not by itself grant
permission to act on it, so moving an environment into another thread is
inert by itself:

- A linear `Env` transfers exclusive ownership; the checker rejects any
  further use of what was captured in the calling function.
- A Copy `Env` with no raw pointer in it is plain data, with nothing to
  race on.
- A Copy `Env` that is or contains a raw pointer grants no access by
  existing: every dereference of it, in either thread, needs its own
  `unsafe`, as raw pointers do everywhere.

Shared mutable state should cross as `std::cap::Exclusive!`/`Shared!`
rather than a bare pointer. Both are linear, so moving or splitting one into
the closure is the same ownership transfer a linear `Env` gets.

Internally, `SpawnPayload[Env, Ret]` and `ResultBox[Ret]` box the closure
and its result on the heap so they can cross `libc::thread`'s raw
`unsafe fn(*u8) -> *u8` boundary (`ResultBox` carries a `bool` beside the
value, so it has bytes even for a zero-size `Ret`). `trampoline[Env, Ret]`,
instantiated once per `(Env, Ret)`, unboxes the payload, calls `f` exactly
once, and boxes the result. It is an `unsafe fn`: its argument must be the
payload box `spawn` built for the same `(Env, Ret)`. None of this is part
of the API.
