#define _DEFAULT_SOURCE
#include "thread.h"
#include <errno.h>
#include <pthread.h>
#include <stdlib.h>

/* pthread_create's start routine takes exactly one void*, so this bundles
 * the caller's entry function and its argument for the trampoline below to
 * recover. The trampoline frees the bundle before calling the entry. */
typedef struct {
    uint8_t *(*entry)(uint8_t *);
    uint8_t *arg;
} kur_thread_start;

static void *kur_libc_thread_trampoline(void *raw) {
    kur_thread_start *start = (kur_thread_start *)raw;
    uint8_t *(*entry)(uint8_t *) = start->entry;
    uint8_t *arg = start->arg;
    free(start);
    return (void *)entry(arg);
}

/* Read immediately after a failed operation, before allocation/cleanup. */
int32_t kur_libc_thread_error(void) { return errno; }

uint8_t *kur_libc_thread_create(uint8_t *(*entry)(uint8_t *), uint8_t *arg) {
    kur_thread_start *start = malloc(sizeof *start);
    if (!start) { errno = ENOMEM; return NULL; }
    start->entry = entry;
    start->arg = arg;
    pthread_t *handle = malloc(sizeof *handle);
    if (!handle) { free(start); errno = ENOMEM; return NULL; }
    int status = pthread_create(handle, NULL, kur_libc_thread_trampoline, start);
    if (status != 0) { free(handle); free(start); errno = status; return NULL; }
    return (uint8_t *)handle;
}

/* The handle is consumed regardless of whether pthread_join succeeds. */
int32_t kur_libc_thread_join(uint8_t *handle, uint8_t **out_result) {
    pthread_t *h = (pthread_t *)handle;
    void *result = NULL;
    int status = pthread_join(*h, &result);
    free(h);
    if (status != 0) return status;
    if (out_result) *out_result = (uint8_t *)result;
    return 0;
}

/* The handle is consumed regardless of whether pthread_detach succeeds. */
int32_t kur_libc_thread_detach(uint8_t *handle) {
    pthread_t *h = (pthread_t *)handle;
    int status = pthread_detach(*h);
    free(h);
    return status;
}
