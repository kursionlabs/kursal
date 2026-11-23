/* What every C file of the runtime starts with, before any system header:
 * the feature macros it is compiled under, the ABI (`libc.h`), and the
 * check of a path or name the Kursal side passes. Not itself part of the
 * ABI. */
#ifndef KUR_LIBC_COMMON_H
#define KUR_LIBC_COMMON_H

/* 64-bit file offsets, inode numbers and times on 32-bit glibc too. */
#define _FILE_OFFSET_BITS 64
#define _TIME_BITS 64
/* `sched_getaffinity`, `CPU_COUNT`, `pipe2` and the GNU `strerror_r`, where
 * the C library has them. */
#ifdef __APPLE__
/* `_XOPEN_SOURCE` would hide macOS's own extensions (`_SC_NPROCESSORS_ONLN`). */
#define _DARWIN_C_SOURCE
#else
#define _GNU_SOURCE
#define _DEFAULT_SOURCE
#define _XOPEN_SOURCE 700
#endif

#include "libc.h"
#include <errno.h>
#include <stdint.h>
#include <string.h>

/* A path or name is `len` bytes followed by a NUL the caller wrote: 0 when
 * none of the `len` is a NUL, else EINVAL, since C would read the path as
 * ending there. */
static inline int text_ok(const uint8_t *bytes, uintptr_t len) {
    return memchr(bytes, 0, len) ? EINVAL : 0;
}

#endif
