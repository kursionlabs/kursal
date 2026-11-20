/* The environment the process was started with, for `std::env`. */
#include "common.h"
#include <stdlib.h>

/* An environment variable's value as an independently owned string, or NULL
 * when it is unset (or its name holds a NUL or an '='). */
uint8_t *kur_libc_env_get(const uint8_t *name, uintptr_t len) {
    if (text_ok(name, len) || memchr(name, '=', len)) return NULL;
    const char *value = getenv((const char *)name);
    return value ? (uint8_t *)strdup(value) : NULL;
}
