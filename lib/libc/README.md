# Host libc adapters

`fs.kur` provides filesystem operations for hosted tools, `process.kur`
this program's arguments and running other programs, and `thread.kur` a
thin POSIX thread adapter. The C halves are `fs.c` and `thread.c`
(`process.kur` calls libc directly and reports errno through `fs.c`); they
target POSIX hosts, not freestanding kernels. `std::io` reads, writes,
opens, closes and sizes files through `fs.c` as well (`kur_libc_fs_read`,
`_write`, `_open_read`, `_create`, `_close`, `_regular_size`), which retry
a call a signal interrupts, open descriptors close-on-exec and return a
negative errno, so every program links `fs.c`. `fs.c` asks for 64-bit file
offsets and times (`_FILE_OFFSET_BITS`, `_TIME_BITS`) on 32-bit hosts. Platform
structure layouts (`DIR`, `dirent`, `stat`, `pthread_t`) never cross the
boundary.

`lib.kur` is the package facade and exports the `fs`, `process` and `thread`
namespaces. Every module in the package is part of the build, so a program
that depends on `libc` links both `fs.c` and `thread.c`, even if it uses
only one adapter, for example
`cc generated.c lib/libc/fs.c lib/libc/thread.c -pthread -o tool`. Package
discovery neither compiles nor links C sources. The C ABI is declared in
`fs.h`/`thread.h`; library code uses `libc::fs`/`libc::thread`.
**`-pthread` is required for `thread.c`.** glibc 2.34 and later fold
pthread support into libc, so a link without it may happen to work there,
but that is not portable.

## `fs.kur`

Paths are length-delimited `Buffer!` values, borrowed (`&path`) and handed
back unchanged. Embedded NUL bytes are rejected. `canonicalize` returns an
independently owned buffer without a trailing NUL in its logical length.
`metadata` follows links and reports device/inode identity and file kind.
`environment_variable` returns an owned copy of a variable's value, or
nothing when it is unset. These values describe an observation, not
authenticated ownership or a stable filesystem snapshot. `open_read`
returns a `std::io::File!` over a fresh, close-on-exec descriptor; its
usual read and close operations apply. `create` makes the file, or empties
an existing one, and returns a `File!` open for writing (mode 0644 less the
umask for a new file). Opening errors are positive errno
values; later `File!` operations report errors as `std::io` does.

`DirectoryStream!` is restricted and linear. `next` returns an owned entry
name, `Ok(None)` at EOF, or a positive errno on failure. Names include `.`
and `..`, and iteration order is unspecified. Callers must close the
directory even after a read error; `close` consumes the handle regardless
of its result. `next_entry` also returns an owned `DirectoryEntry!` holding
the name and an `is_directory` observation. Classification uses the host's
entry type hint where available and otherwise follows symbolic links;
a failed fallback observation counts as not a directory. This matches
directory-walk classification and does not replace strict validation when
a file or directory is actually opened. Build with `KUR_LIBC_FS_NO_D_TYPE`
to exercise the portable `fstatat` fallback.

The unsafe C ABI requires valid live input buffers, a 24-byte, u64-aligned
output buffer for `stat`, and valid exclusive directory handles. Opaque
entry pointers are borrowed until the next read or close on their
originating handle, so their accessors must run before then. `fs.kur`
discharges these obligations internally. Strings returned by libc
(canonical paths, environment values) are copied into `Buffer!` storage
and released through the adapter; libc allocations never reach the buffer
allocator. Allocation failure follows the buffer library's abort policy,
except where libc reports it through errno.

Code that uses these operations to enforce path containment must still
compare canonical paths on component boundaries, check physical identities,
and reject directory aliases and cycles. None of these operations prevents
concurrent filesystem mutation (TOCTOU).

## `process.kur`

A command line is one `Buffer!` holding each argument followed by a NUL,
the program's name first. `arguments` reads this program's from
`/proc/self/cmdline`, so it is Linux-specific, and no `argv` pointer has to
be trusted. `run(&command)` forks, runs the program with `execvp` (searching
the `PATH`) and this program's environment, waits for it, and reports
`Exit::Code(status)` or `Exit::Signal(number)`; a child that cannot run the
program exits with 127. It fails with a positive errno: EINVAL for a
command line that is empty or does not end in a NUL, otherwise what `fork`
or `waitpid` reports. The command line is handed back unchanged.

## `thread.kur`

A thin wrapper over `pthread_create`/`pthread_join`/`pthread_detach`.
`Thread!` is restricted and linear: each spawned thread is joined or
detached exactly once, never neither and never both, as with
`DirectoryStream!`. `join` and `detach` consume the handle even when the
pthread call fails.

`spawn` is `unsafe fn`. Its `arg: *u8` becomes reachable from two
independently scheduled call stacks as soon as the new thread starts, with
no ordering between them, and nothing here tracks which stack may touch it
when. Calling `spawn` is the unproven step, not any single operation inside
it. The `thread` package builds a safe, closure-based `spawn` on top of
this adapter.

`entry: unsafe fn(*u8) -> *u8` is a function reference, passed across
the `extern "C"` boundary as a C function pointer with no cast. It is
typed `unsafe` because an entry that reads its `*u8` has an obligation
about it; `spawn`'s caller vouches that `entry(arg)` meets it. `join` is
`unsafe` because it returns the raw `*u8` that `entry` produced, with no
static knowledge of what it points to. `detach` is safe: it returns no
pointer.
