# Host libc adapters

`fs.c` is the C half of the standard library's operating-system layer:
`std::io` (descriptors), `std::fs` (paths, files and directories),
`std::env` (the environment), `std::process` (running programs and
counting processors) and `std::time` (the monotonic clock and `sleep`)
call it. `thread.kur` is a thin POSIX thread adapter
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

`kur_libc_process_spawn` takes a command line of `count` NUL-terminated
arguments, the program first, builds the table `posix_spawnp` reads, and
starts the child with SIGPIPE's default action: its process id, or a
negative errno. `posix_spawnp` reports the errno of an `exec` that fails
(ENOENT for a program not on the `PATH`, EACCES for a file that is not
executable), and runs no code of this library in the child. A line whose
arguments are not exactly `count` is EINVAL. The child's environment is
this process's, or none, changed by the `std::process::Command!`'s
`env`/`env_remove` operations in order, in a table built for the call; its
working directory changes with `posix_spawn_file_actions_addchdir_np`
(glibc 2.29, musl 1.1.24, macOS 10.15, FreeBSD 13.1 and later). Build with
`KUR_LIBC_NO_ADDCHDIR` for a C library without it: a command given a
working directory then fails with ENOSYS rather than run elsewhere. Each
standard stream is inherited, `/dev/null`, a descriptor the child gets a
copy of, or a new pipe: pipes are made close-on-exec (`pipe2`), the
child's end reaches it by `dup2`, and this process's end is handed back.
`kur_libc_process_wait` waits for the child: the exit status, 256 plus the
signal's number, or a negative errno. `kur_libc_process_kill` and `_signal`
send it SIGKILL or a signal by number, and `kur_libc_process_poll` waits
until one of two pipes can be read, so that `Child!::wait_with_output`
reads standard output and error as they come and a child that fills one
pipe never waits on this process reading the other.

`kur_libc_error_message` writes the C library's description of an errno
into a buffer through `strerror_r`, thread-safely (the GNU form under
glibc, the POSIX one elsewhere), and `kur_libc_error_kind` sorts an errno
into the kinds of `std::io::ErrorKind`, so that no host's errno numbers
are written in Kursal code.

`kur_libc_fs_size_hint` is what `File!::read_to_buffer` sizes its buffer
by: a regular file's size, 0 for a pipe, device or socket, which are read
until they end, EISDIR for a directory. `kur_libc_stdin_claim` and
`kur_libc_stdin_release` set and clear one process-wide flag, so that one
reader of standard input at a time (a `std::io::Input!`, or `read_line` or
`read_all_stdin` for the length of the call) holds it, and none skips the
bytes another has read ahead. `kur_libc_process_cpus` counts the processors
of the process's affinity mask (`sched_getaffinity`, where the C library
has it), else those online. `kur_libc_time_monotonic` reads
`CLOCK_MONOTONIC`, so that no clock's number is written in Kursal code.

`kur_libc_process_report_error` is called by no Kursal code: the C `main`
the compiler writes for an entry returning `Result[(), std::io::Error]`
calls it with an `Err`'s errno, and it writes `error: DESCRIPTION (error
ERRNO)` to stderr, the description `strerror`'s.

`kur_libc_process_start` is called by no Kursal code either: the C `main`
`kursal build` writes calls it before anything else. It opens `/dev/null`
on each of the descriptors 0, 1 and 2 the process started without (and
aborts if it cannot), so a `File!` never gets a standard stream's number
and `print`, `eprint` and `read_line` never reach a file the program owns.
It ignores SIGPIPE, so a write to a closed pipe fails with EPIPE, which the
output functions return, instead of ending the process. The children
`kur_libc_process_spawn` starts get the default action back
(`posix_spawnattr_setsigdefault`), since an ignored signal stays ignored
across `exec`.
It draws the process's hash seed, 16 bytes from `getentropy`, and aborts
when the operating system has none to give; `kur_libc_process_hash_seed`
reads it, a word at a time, for `std::process::hash_seed`, and aborts,
saying why, when the seed was never drawn. Nothing changes the seed after
start-up, so threads read it without a lock. A program whose C `main` is
not the one `kursal build` writes (`kursal c --entry`, linked with this
file) calls `kur_libc_process_start` before any Kursal code.

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
