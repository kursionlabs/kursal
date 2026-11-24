/* The environment the process was started with, for `std::env`. */
#include "common.h"
#include <stdlib.h>

/* An environment variable's value as an independently owned string, or NULL
 * when it is unset (or its name holds a NUL or an '='). Aborts when there is
 * no memory for the copy, as the library's allocations do, so NULL always
 * means unset. */
uint8_t *kur_libc_env_get(const uint8_t *name, uintptr_t len) {
    if (text_ok(name, len) || memchr(name, '=', len)) return NULL;
    const char *value = getenv((const char *)name);
    if (!value) return NULL;
    char *copy = strdup(value);
    if (!copy) abort();
    return (uint8_t *)copy;
}
