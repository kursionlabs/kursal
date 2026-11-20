/* The file system by path, for `std::fs`: what a path names, directories,
 * removing, renaming and canonicalizing, and C strings handed back. */
#include "common.h"
#include <dirent.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>

/* Read immediately after a failed operation, before allocation/cleanup. */
int32_t kur_libc_fs_error(void) { return errno; }

/* Frees a string the C library allocated and a call here handed back: a
 * canonical path, or a variable's value (`env.c`). */
void kur_libc_fs_release(uint8_t *bytes) { free(bytes); }

uint8_t *kur_libc_fs_canonicalize(const uint8_t *path, uintptr_t len) {
    int bad = text_ok(path, len);
    if (bad) { errno = bad; return NULL; }
    return (uint8_t *)realpath((const char *)path, NULL);
}

uint8_t *kur_libc_fs_open_directory(const uint8_t *path, uintptr_t len) {
    int bad = text_ok(path, len);
    if (bad) { errno = bad; return NULL; }
    return (uint8_t *)opendir((const char *)path);
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
