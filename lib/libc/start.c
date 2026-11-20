/* What the C `main` of an executable calls: start-up, which readies the
 * standard descriptors, SIGPIPE and the hash seed, and the report of an
 * error the entry returned; and the hash seed start-up drew. */
#include "common.h"
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#ifdef __APPLE__
#include <sys/random.h>
#endif

/* The process's hash seed, which `kur_libc_process_start` draws from the
 * operating system before any Kursal code runs and nothing changes after,
 * so threads read it without a lock. */
static uint64_t hash_seed[2];
static int hash_seed_drawn;

/* Readies the process for an executable's Kursal code; the C `main` the
 * compiler writes for `kursal build` calls it before anything else. Each of
 * the standard descriptors 0, 1 and 2 the process started without is opened
 * on /dev/null, so no file the program opens later takes its number and
 * receives what `print`, `eprint` or `read_line` mean for the standard
 * streams; the process aborts when one cannot be opened. SIGPIPE is ignored,
 * so a write to a closed pipe fails with EPIPE, which the output functions
 * return, instead of ending the process (`kur_libc_process_spawn` gives
 * children the default back). The hash seed's 16 bytes are drawn with
 * `getentropy`; the process aborts when the operating system has none to
 * give. */
void kur_libc_process_start(void) {
    for (int fd = 0; fd < 3; fd++) {
        if (fcntl(fd, F_GETFD) == -1 && errno == EBADF) {
            /* The lowest free number: `fd`, since 0 to fd - 1 are open. */
            if (open("/dev/null", O_RDWR) != fd) abort();
        }
    }
    signal(SIGPIPE, SIG_IGN);
    if (getentropy(hash_seed, sizeof hash_seed) != 0) abort();
    hash_seed_drawn = 1;
}

/* Word `word` (0 or 1) of the process's hash seed, `std::process::hash_seed`.
 * The process aborts, saying why, when `kur_libc_process_start` has not
 * drawn the seed: a program whose C `main` is not the one `kursal build`
 * writes calls it first. */
uint64_t kur_libc_process_hash_seed(uint32_t word) {
    if (!hash_seed_drawn) {
        fputs("error: the hash seed is read before kur_libc_process_start drew it\n", stderr);
        abort();
    }
    return hash_seed[word & 1u];
}

/* Reports the `std::io::Error` an executable's entry returned, its errno
 * `code`, on stderr: `error: ` and the C library's description of the
 * errno, then the errno itself, as `error: No such file or directory
 * (error 2)`. The C `main` the compiler writes for an entry returning
 * `Result[(), std::io::Error]` calls it before exiting with status 1. */
void kur_libc_process_report_error(int32_t code) {
    fprintf(stderr, "error: %s (error %d)\n", strerror(code), (int)code);
}
