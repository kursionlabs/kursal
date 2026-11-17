# Host libc adapters

`fs.c` is the C half of the standard library's operating-system layer:
`std::io` (descriptors), `std::fs` (paths, files and directories),
`std::env` (the environment), `std::process` (running programs) and
`std::time::sleep` call it. `thread.kur` is a thin POSIX thread adapter
over `thread.c`. Both target POSIX hosts, not freestanding kernels.

Every C function retries a call a signal interrupts, opens descriptors
close-on-exec and reports a failure as an errno (negative where the result
is otherwise a count or a descriptor); the Kursal side makes it a
`std::io::Error`, which the `thread` package reports too. `fs.c` asks for
64-bit file offsets and times (`_FILE_OFFSET_BITS`, `_TIME_BITS`) on 32-bit
hosts. Platform structure layouts (`DIR`, `dirent`, `stat`, `timespec`,
`pthread_t`) never cross the boundary.

`lib.kur` is the package facade and exports the `thread` namespace. Package
discovery neither compiles nor links C sources: the build does. `kursal
build`, and the reference compiler's `tools/build.py` and tests, compile
every program with `lib/libc/fs.c` and `lib/libc/thread.c` and link it with
`-lpthread` (`kursal.cc`, `compiler/main.kur`), as in
`cc generated.c lib/libc/fs.c lib/libc/thread.c -lpthread -o tool`. glibc
2.34 and later fold the threads library into libc, so a link without
`-lpthread` may happen to work there, but that is not portable. The C ABI
is declared in `fs.h`/`thread.h`.

## `fs.c`

A path, a variable's name or an argument is passed as its bytes followed by
a NUL, with its length: the Kursal side copies any byte view into a buffer
with the NUL (`std::io::c_string`) and frees it when the call returns, and
`fs.c` fails with EINVAL when the bytes hold a NUL of their own, which C
would read as the end. Strings C allocates (a canonical path, a variable's
value) are copied into `Buffer!` storage and released through
`kur_libc_fs_release`; libc allocations never reach the buffer allocator.

Directory handles are opaque. `kur_libc_fs_next_entry` returns an entry
borrowed until the next read or close of its handle, so `std::fs` reads its
name and classification before then. Classification uses the host's entry
type hint where available and otherwise follows symbolic links; a failed
fallback observation counts as not a directory. Build with
`KUR_LIBC_FS_NO_D_TYPE` to exercise the portable `fstatat` fallback.

`kur_libc_process_run` takes a command line of `count` NUL-terminated
arguments, the program first, builds the table `posix_spawnp` reads, starts
the child with this process's environment, and waits for it: the exit
status, 256 plus the signal's number, or a negative errno. `posix_spawnp`
reports the errno of an `exec` that fails (ENOENT for a program not on the
`PATH`, EACCES for a file that is not executable), and runs no code of this
library in the child. A line whose arguments are not exactly `count` is
EINVAL.

`kur_libc_process_report_error` is called by no Kursal code: the C `main`
the compiler writes for an entry returning `Result[(), std::io::Error]`
calls it with an `Err`'s errno, and it writes `error: DESCRIPTION (error
ERRNO)` to stderr, the description `strerror`'s.

These calls describe observations, not authenticated ownership or a stable
file-system snapshot. Code that uses them to enforce path containment must
still compare canonical paths on component boundaries, check physical
identities, and reject directory aliases and cycles. None of them prevents
concurrent file-system mutation (TOCTOU).

## `thread.kur`

A thin wrapper over `pthread_create`/`pthread_join`. `Thread!` is
restricted and linear: each spawned thread is joined exactly once, as a
`std::fs::Directory!` is closed once. `join` consumes the handle even when
`pthread_join` fails. There is no detach: a thread's result always reaches
a `join`.

`spawn` is `unsafe fn`. Its `arg: *u8` becomes reachable from two
independently scheduled call stacks as soon as the new thread starts, with
no ordering between them, and nothing here tracks which stack may touch it
when. Calling `spawn` is the unproven step, not any single operation inside
it. The `thread` package builds a safe, closure-based `spawn` on top of
this adapter.

`entry: unsafe fn(*u8) -> *u8` is a function reference, passed across
the `extern "C"` boundary as a C function pointer with no cast. It is
typed `unsafe` because an entry that reads its `*u8` has an obligation
about it; `spawn`'s caller vouches that `entry(arg)` meets it
(`std/CONTRACTS.md`, section 2.5). `join` is `unsafe` because it returns
the raw `*u8` that `entry` produced, with no static knowledge of what it
points to.
