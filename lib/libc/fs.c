/* 64-bit file offsets, inode numbers and times on 32-bit glibc too, before
 * any header reads them. */
#define _FILE_OFFSET_BITS 64
#define _TIME_BITS 64
#define _DEFAULT_SOURCE
#define _XOPEN_SOURCE 700
#include "fs.h"
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

/* Length-delimited paths: no truncation at an embedded NUL, no PATH_MAX. */
static char *path_copy(const uint8_t *bytes, uintptr_t len) {
    if (len == SIZE_MAX) { errno = EOVERFLOW; return NULL; }
    if (memchr(bytes, 0, len)) { errno = EINVAL; return NULL; }
    char *path = malloc(len + 1);
    if (!path) { errno = ENOMEM; return NULL; }
    memcpy(path, bytes, len);
    path[len] = 0;
    return path;
}

/* Read immediately after a failed operation, before allocation/cleanup. */
int32_t kur_libc_fs_error(void) { return errno; }

void kur_libc_fs_release(uint8_t *bytes) { free(bytes); }

uint8_t *kur_libc_fs_canonicalize(uint8_t *bytes, uintptr_t len) {
    char *path = path_copy(bytes, len);
    if (!path) return NULL;
    char *resolved = realpath(path, NULL);
    int saved = errno;
    free(path);
    errno = saved;
    return (uint8_t *)resolved;
}

/* An environment variable's value as an independently owned string, or NULL
 * when it is unset. */
uint8_t *kur_libc_fs_getenv(uint8_t *bytes, uintptr_t len) {
    char *name = path_copy(bytes, len);
    if (!name) return NULL;
    const char *value = getenv(name);
    free(name);
    return value ? (uint8_t *)strdup(value) : NULL;
}

uint8_t *kur_libc_fs_open_directory(uint8_t *bytes, uintptr_t len) {
    char *path = path_copy(bytes, len);
    if (!path) return NULL;
    DIR *directory = opendir(path);
    int saved = errno;
    free(path);
    errno = saved;
    return (uint8_t *)directory;
}

/* A fresh descriptor on success; negative errno on failure. */
int32_t kur_libc_fs_open_read(uint8_t *bytes, uintptr_t len) {
    char *path = path_copy(bytes, len);
    if (!path) return -errno;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    int saved = errno;
    free(path);
    return fd < 0 ? -saved : fd;
}

/* Creates the file or empties it, for writing: a fresh descriptor on
 * success, negative errno on failure. A new file gets mode 0644, less the
 * umask. */
int32_t kur_libc_fs_create(uint8_t *bytes, uintptr_t len) {
    char *path = path_copy(bytes, len);
    if (!path) return -errno;
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    int saved = errno;
    free(path);
    return fd < 0 ? -saved : fd;
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

/* The size of the regular file open on `fd`, or a negative errno: EISDIR
 * for a directory, EINVAL for anything else that is not a regular file,
 * EFBIG for a size an intptr_t cannot hold. */
intptr_t kur_libc_fs_regular_size(int32_t fd) {
    struct stat info;
    if (fstat(fd, &info) != 0) return -(intptr_t)errno;
    if (S_ISDIR(info.st_mode)) return -(intptr_t)EISDIR;
    if (!S_ISREG(info.st_mode)) return -(intptr_t)EINVAL;
    if (info.st_size < 0 || (uintmax_t)info.st_size > (uintmax_t)INTPTR_MAX) return -(intptr_t)EFBIG;
    return (intptr_t)info.st_size;
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
