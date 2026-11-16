#ifndef KUR_LIBC_THREAD_H
#define KUR_LIBC_THREAD_H

#include <stdint.h>

/* Unsafe ABI: see README.md for ownership and lifetime obligations. */
int32_t kur_libc_thread_error(void);
uint8_t *kur_libc_thread_create(uint8_t *(*entry)(uint8_t *), uint8_t *arg);
int32_t kur_libc_thread_join(uint8_t *handle, uint8_t **out_result);

#endif
