/* The monotonic clock and sleeping, for `std::time`. */
#include "common.h"
#include <stdlib.h>
#include <time.h>

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
