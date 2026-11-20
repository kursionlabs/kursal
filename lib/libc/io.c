/* Descriptors and errors: `std::io`'s reads, writes, opens and closes, the
 * claim on standard input, and the description and kind of an errno. */
#include "common.h"
#include <fcntl.h>
#include <stdatomic.h>
#include <sys/stat.h>
#include <unistd.h>

/* The C library's description of errno `code`, as `strerror` gives it, into
 * `out`: how many bytes, at most `cap`, and no NUL. Thread-safe: it reads
 * through `strerror_r`, the GNU form under glibc (which returns the text)
 * and the POSIX one elsewhere (which fills the buffer). */
uintptr_t kur_libc_error_message(int32_t code, uint8_t *out, uintptr_t cap) {
    char text[256];
#if defined(__GLIBC__) && defined(_GNU_SOURCE)
    const char *description = strerror_r(code, text, sizeof text);
#else
    const char *description = strerror_r(code, text, sizeof text) == 0 ? text : "Unknown error";
#endif
    size_t n = strlen(description);
    if (n > cap) n = cap;
    memcpy(out, description, n);
    return n;
}

/* The kind of errno `code`, numbered as `std::io::ErrorKind` orders its
 * variants from 1, so that no host's errno numbers are written in Kursal
 * code: 1 NotFound, 2 PermissionDenied, 3 AlreadyExists, 4 IsADirectory,
 * 5 NotADirectory, 6 DirectoryNotEmpty, 7 InvalidInput, 8 BrokenPipe,
 * 9 Interrupted, 10 WouldBlock, 11 TimedOut, 12 Busy, 13 StorageFull,
 * 14 OutOfMemory, 15 Unsupported; 0 for any other. */
int32_t kur_libc_error_kind(int32_t code) {
    switch (code) {
    case ENOENT: return 1;
    case EACCES: case EPERM: return 2;
    case EEXIST: return 3;
    case EISDIR: return 4;
    case ENOTDIR: return 5;
    case ENOTEMPTY: return 6;
    case EINVAL: return 7;
    case EPIPE: return 8;
    case EINTR: return 9;
    case EAGAIN: return 10;
#if EWOULDBLOCK != EAGAIN
    case EWOULDBLOCK: return 10;
#endif
    case ETIMEDOUT: return 11;
    case EBUSY: return 12;
    case ENOSPC: return 13;
    case ENOMEM: return 14;
    case ENOSYS: return 15;
#if ENOTSUP != ENOSYS
    case ENOTSUP: return 15;
#endif
    default: return 0;
    }
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
