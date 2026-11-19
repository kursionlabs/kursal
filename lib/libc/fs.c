/* 64-bit file offsets, inode numbers and times on 32-bit glibc too, before
 * any header reads them. */
#define _FILE_OFFSET_BITS 64
#define _TIME_BITS 64
/* `sched_getaffinity` and `CPU_COUNT`, where the C library has them. */
#define _GNU_SOURCE
#define _DEFAULT_SOURCE
#define _XOPEN_SOURCE 700
#include "fs.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <sched.h>
#include <signal.h>
#include <spawn.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

extern char **environ;

/* A path or name is `len` bytes followed by a NUL the caller wrote: 0 when
 * none of the `len` is a NUL, else EINVAL, since C would read the path as
 * ending there. */
static int text_ok(const uint8_t *bytes, uintptr_t len) {
    return memchr(bytes, 0, len) ? EINVAL : 0;
}

/* Read immediately after a failed operation, before allocation/cleanup. */
int32_t kur_libc_fs_error(void) { return errno; }

void kur_libc_fs_release(uint8_t *bytes) { free(bytes); }

uint8_t *kur_libc_fs_canonicalize(const uint8_t *path, uintptr_t len) {
    int bad = text_ok(path, len);
    if (bad) { errno = bad; return NULL; }
    return (uint8_t *)realpath((const char *)path, NULL);
}

/* An environment variable's value as an independently owned string, or NULL
 * when it is unset (or its name holds a NUL or an '='). */
uint8_t *kur_libc_fs_getenv(const uint8_t *name, uintptr_t len) {
    if (text_ok(name, len) || memchr(name, '=', len)) return NULL;
    const char *value = getenv((const char *)name);
    return value ? (uint8_t *)strdup(value) : NULL;
}

uint8_t *kur_libc_fs_open_directory(const uint8_t *path, uintptr_t len) {
    int bad = text_ok(path, len);
    if (bad) { errno = bad; return NULL; }
    return (uint8_t *)opendir((const char *)path);
}

/* A fresh descriptor on success; negative errno on failure. */
int32_t kur_libc_fs_open_read(const uint8_t *path, uintptr_t len) {
    int bad = text_ok(path, len);
    if (bad) return -bad;
    int fd = open((const char *)path, O_RDONLY | O_CLOEXEC);
    return fd < 0 ? -errno : fd;
}

/* Creates the file or empties it, for writing: a fresh descriptor on
 * success, negative errno on failure. A new file gets mode 0644, less the
 * umask. */
int32_t kur_libc_fs_create(const uint8_t *path, uintptr_t len) {
    int bad = text_ok(path, len);
    if (bad) return -bad;
    int fd = open((const char *)path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    return fd < 0 ? -errno : fd;
}

/* What `stat(2)` says of a path, following symbolic links: 1 for a regular
 * file, 2 for a directory, 3 for anything else, with its size in `*size`;
 * or a negative errno. */
int32_t kur_libc_fs_stat(const uint8_t *path, uintptr_t len, uint64_t *size) {
    int bad = text_ok(path, len);
    if (bad) return -bad;
    struct stat info;
    if (stat((const char *)path, &info) != 0) return -errno;
    *size = info.st_size < 0 ? 0 : (uint64_t)info.st_size;
    return S_ISREG(info.st_mode) ? 1 : S_ISDIR(info.st_mode) ? 2 : 3;
}

/* `remove(3)`: unlinks a file, or removes an empty directory. 0 or errno. */
int32_t kur_libc_fs_remove(const uint8_t *path, uintptr_t len) {
    int bad = text_ok(path, len);
    if (bad) return bad;
    return remove((const char *)path) == 0 ? 0 : errno;
}

/* `rename(2)`: 0 or errno. */
int32_t kur_libc_fs_rename(const uint8_t *from, uintptr_t from_len, const uint8_t *to, uintptr_t to_len) {
    int bad = text_ok(from, from_len);
    if (!bad) bad = text_ok(to, to_len);
    if (bad) return bad;
    return rename((const char *)from, (const char *)to) == 0 ? 0 : errno;
}

/* `mkdir(2)` with mode 0777, less the umask: 0 or errno. */
int32_t kur_libc_fs_mkdir(const uint8_t *path, uintptr_t len) {
    int bad = text_ok(path, len);
    if (bad) return bad;
    return mkdir((const char *)path, 0777) == 0 ? 0 : errno;
}

/* read(2), retried while a signal interrupts it: the count, 0 at EOF, or a
 * negative errno. */
intptr_t kur_libc_fs_read(int32_t fd, uint8_t *buf, uintptr_t len) {
    ssize_t n;
    do n = read(fd, buf, len); while (n < 0 && errno == EINTR);
    return n < 0 ? -(intptr_t)errno : (intptr_t)n;
}

/* write(2), retried while a signal interrupts it: the count or a negative
 * errno. */
intptr_t kur_libc_fs_write(int32_t fd, const uint8_t *buf, uintptr_t len) {
    ssize_t n;
    do n = write(fd, buf, len); while (n < 0 && errno == EINTR);
    return n < 0 ? -(intptr_t)errno : (intptr_t)n;
}

/* close(2): 0 or errno. Not retried on EINTR: Linux has released the
 * descriptor by then, and a retry could close one another thread opened. */
int32_t kur_libc_fs_close(int32_t fd) {
    return close(fd) == 0 ? 0 : errno;
}

/* The size of the regular file open on `fd`; `other` for anything else
 * that is not a directory; or a negative errno: EISDIR for a directory,
 * EFBIG for a size an intptr_t cannot hold. */
static intptr_t regular_size_or(int32_t fd, intptr_t other) {
    struct stat info;
    if (fstat(fd, &info) != 0) return -(intptr_t)errno;
    if (S_ISDIR(info.st_mode)) return -(intptr_t)EISDIR;
    if (!S_ISREG(info.st_mode)) return other;
    if (info.st_size < 0 || (uintmax_t)info.st_size > (uintmax_t)INTPTR_MAX) return -(intptr_t)EFBIG;
    return (intptr_t)info.st_size;
}

/* The size of the regular file open on `fd`, or a negative errno: EISDIR
 * for a directory, EINVAL for anything else that is not a regular file,
 * EFBIG for a size an intptr_t cannot hold. */
intptr_t kur_libc_fs_regular_size(int32_t fd) {
    return regular_size_or(fd, -(intptr_t)EINVAL);
}

/* How many bytes reading `fd` to its end is likely to give: a regular
 * file's size, 0 for a pipe, device or socket, whose size says nothing
 * about it; or a negative errno: EISDIR for a directory, EFBIG for a size
 * an intptr_t cannot hold. */
intptr_t kur_libc_fs_size_hint(int32_t fd) {
    return regular_size_or(fd, 0);
}

/* Whether a `std::io::Input!` holds standard input: the one reader of it
 * at a time, so that no bytes it has read ahead are skipped by another. */
static atomic_flag stdin_claimed = ATOMIC_FLAG_INIT;

/* Claims standard input for one reader: 0, or EBUSY while another holds
 * it. */
int32_t kur_libc_stdin_claim(void) {
    return atomic_flag_test_and_set(&stdin_claimed) ? EBUSY : 0;
}

/* Gives standard input back, for the next `kur_libc_stdin_claim`. */
void kur_libc_stdin_release(void) {
    atomic_flag_clear(&stdin_claimed);
}

/* Opaque entry borrowed until the next read or close on this DIR.
 * NULL with errno == 0 is EOF; otherwise NULL indicates an error.
 * No dirent layout crosses the ABI. */
uint8_t *kur_libc_fs_next_entry(uint8_t *handle) {
    errno = 0;
    return (uint8_t *)readdir((DIR *)handle);
}

uint8_t *kur_libc_fs_entry_name(uint8_t *entry) {
    return (uint8_t *)((struct dirent *)entry)->d_name;
}

/* Match directory walking: use the entry hint when available, follow links
 * otherwise, and classify failed observations as non-directories. Actual
 * source/directory access still performs strict validation in the caller. */
int32_t kur_libc_fs_entry_is_directory(uint8_t *handle, uint8_t *raw_entry) {
    struct dirent *entry = (struct dirent *)raw_entry;
#if !defined(KUR_LIBC_FS_NO_D_TYPE) && defined(DT_DIR) && defined(DT_UNKNOWN) && defined(DT_LNK)
    if (entry->d_type == DT_DIR) return 1;
    if (entry->d_type != DT_UNKNOWN && entry->d_type != DT_LNK) return 0;
#endif
    struct stat info;
    return fstatat(dirfd((DIR *)handle), entry->d_name, &info, 0) == 0 &&
           S_ISDIR(info.st_mode);
}

/* The handle is consumed even if closedir reports an error. */
int32_t kur_libc_fs_close_directory(uint8_t *handle) {
    return closedir((DIR *)handle) == 0 ? 0 : errno;
}

/* Starts the program `line` names, found on the PATH, with `count`
 * arguments, the program's name first: `line` is each argument followed by
 * a NUL, `len` bytes in all. The child gets this process's environment, and
 * SIGPIPE's default action back: an executable's C `main` ignores SIGPIPE
 * (`kur_libc_process_start`), and an ignored signal stays ignored across
 * `exec`. Its process id, or a negative errno: what `posix_spawnp` reports
 * (ENOENT for a program not found, or the errno of the `exec` that failed),
 * EINVAL when an argument holds a NUL of its own. */
int64_t kur_libc_process_spawn(const uint8_t *line, uintptr_t len, uintptr_t count) {
    if (count == 0 || count > SIZE_MAX / sizeof(char *) - 1) return -EINVAL;
    char **argv = malloc((count + 1) * sizeof(char *));
    if (!argv) return -ENOMEM;
    uintptr_t start = 0, n = 0;
    for (uintptr_t i = 0; i < len; i++) {
        if (line[i] == 0) {
            if (n < count) argv[n] = (char *)line + start;
            n++;
            start = i + 1;
        }
    }
    /* An argument holding a NUL of its own would split in two. */
    if (n != count || start != len) { free(argv); return -EINVAL; }
    argv[count] = NULL;
    posix_spawnattr_t attributes;
    int spawned = posix_spawnattr_init(&attributes);
    if (spawned != 0) { free(argv); return -(int64_t)spawned; }
    sigset_t defaults;
    sigemptyset(&defaults);
    sigaddset(&defaults, SIGPIPE);
    spawned = posix_spawnattr_setsigdefault(&attributes, &defaults);
    if (spawned == 0) spawned = posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETSIGDEF);
    pid_t pid;
    if (spawned == 0) spawned = posix_spawnp(&pid, argv[0], NULL, &attributes, argv, environ);
    posix_spawnattr_destroy(&attributes);
    free(argv);
    if (spawned != 0) return -(int64_t)spawned;
    return pid;
}

/* Waits for the child `pid`, again when a signal interrupts the wait; with
 * `block` 0, only looks. The exit status (0 to 255) when the child exits,
 * 256 plus the signal's number when a signal ends it, 512 when it still
 * runs and `block` is 0, or the negative errno `waitpid` reports. */
int64_t kur_libc_process_wait(int64_t pid, uint8_t block) {
    int status;
    pid_t got;
    while ((got = waitpid((pid_t)pid, &status, block ? 0 : WNOHANG)) < 0) {
        if (errno != EINTR) return -(int64_t)errno;
    }
    if (got == 0) return 512;
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 256 + (int64_t)WTERMSIG(status);
    return -(int64_t)ECHILD;
}

/* The number of processors this process may run on, at least 1: those of
 * its CPU affinity mask (`sched_getaffinity`) where the C library reports
 * it, else those online. */
uintptr_t kur_libc_process_cpus(void) {
#ifdef CPU_COUNT
    cpu_set_t allowed;
    if (sched_getaffinity(0, sizeof allowed, &allowed) == 0) {
        int n = CPU_COUNT(&allowed);
        if (n > 0) return (uintptr_t)n;
    }
#endif
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    return n < 1 ? 1 : (uintptr_t)n;
}

/* Readies the process for an executable's Kursal code; the C `main` the
 * compiler writes for `kursal build` calls it before anything else. Each of
 * the standard descriptors 0, 1 and 2 the process started without is opened
 * on /dev/null, so no file the program opens later takes its number and
 * receives what `print`, `eprint` or `read_line` mean for the standard
 * streams; the process aborts when one cannot be opened. SIGPIPE is ignored,
 * so a write to a closed pipe fails with EPIPE, which the output functions
 * return, instead of ending the process (`kur_libc_process_spawn` gives
 * children the default back). */
void kur_libc_process_start(void) {
    for (int fd = 0; fd < 3; fd++) {
        if (fcntl(fd, F_GETFD) == -1 && errno == EBADF) {
            /* The lowest free number: `fd`, since 0 to fd - 1 are open. */
            if (open("/dev/null", O_RDWR) != fd) abort();
        }
    }
    signal(SIGPIPE, SIG_IGN);
}

/* Reports the `std::io::Error` an executable's entry returned, its errno
 * `code`, on stderr: `error: ` and the C library's description of the
 * errno, then the errno itself, as `error: No such file or directory
 * (error 2)`. The C `main` the compiler writes for an entry returning
 * `Result[(), std::io::Error]` calls it before exiting with status 1. */
void kur_libc_process_report_error(int32_t code) {
    fprintf(stderr, "error: %s (error %d)\n", strerror(code), (int)code);
}

/* A reading of the monotonic clock (CLOCK_MONOTONIC) in nanoseconds: it
 * never goes back, and its start is arbitrary. POSIX hosts have this
 * clock, so `clock_gettime` does not fail on it; on one where it does, the
 * process aborts rather than report a time that could go back. */
uint64_t kur_libc_time_monotonic(void) {
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) abort();
    return (uint64_t)now.tv_sec * 1000000000u + (uint64_t)now.tv_nsec;
}

/* Sleeps for `nanos` nanoseconds, sleeping on for what is left when a
 * signal interrupts it. */
void kur_libc_time_sleep(uint64_t nanos) {
    struct timespec left = { (time_t)(nanos / 1000000000u), (long)(nanos % 1000000000u) };
    while (nanosleep(&left, &left) != 0 && errno == EINTR) {}
}
