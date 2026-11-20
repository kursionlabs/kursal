/* Other programs, for `std::process`: starting one with its environment,
 * directory and standard streams, waiting for it, signalling it and
 * reading its pipes; and counting processors. */
#include "common.h"
#include <fcntl.h>
#include <poll.h>
#include <sched.h>
#include <signal.h>
#include <spawn.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

/* The environment the child gets: this process's, emptied first when
 * `clear` is set, then changed by the `ops` operations `env` holds, in
 * order, each `s` NAME NUL VALUE NUL (set NAME to VALUE) or `r` NAME NUL
 * (remove NAME), `len` bytes in all. NULL with `*bad` set to EINVAL when the
 * operations are not that (a name empty or holding an `=`, a NUL inside a
 * name or a value), or to ENOMEM. The array and the entries it set are freed
 * by `free_environment`; the others are this process's own. */
static char **build_environment(const uint8_t *env, uintptr_t len, uintptr_t ops, uint8_t clear,
                                uint8_t ***owned, uintptr_t *owned_count, int *bad) {
    uintptr_t inherited = 0;
    if (!clear) while (environ[inherited]) inherited++;
    if (ops > SIZE_MAX / sizeof(char *) - inherited - 1) { *bad = ENOMEM; return NULL; }
    char **table = malloc((inherited + ops + 1) * sizeof(char *));
    uint8_t **made = malloc((ops + 1) * sizeof(uint8_t *));
    if (!table || !made) { free(table); free(made); *bad = ENOMEM; return NULL; }
    uintptr_t n = 0, m = 0;
    for (uintptr_t i = 0; i < inherited; i++) table[n++] = environ[i];
    uintptr_t at = 0;
    for (uintptr_t op = 0; op < ops; op++) {
        if (at >= len || (env[at] != 's' && env[at] != 'r')) goto invalid;
        uint8_t kind = env[at++];
        const uint8_t *name = env + at;
        const uint8_t *name_end = memchr(name, 0, len - at);
        if (!name_end || name_end == name || memchr(name, '=', (size_t)(name_end - name))) goto invalid;
        uintptr_t name_len = (uintptr_t)(name_end - name);
        at += name_len + 1;
        /* The entries that name `name` go, in place. */
        uintptr_t kept = 0;
        for (uintptr_t i = 0; i < n; i++) {
            if (strncmp(table[i], (const char *)name, name_len) != 0 || table[i][name_len] != '=') table[kept++] = table[i];
        }
        n = kept;
        if (kind == 'r') continue;
        const uint8_t *value = env + at;
        const uint8_t *value_end = at < len ? memchr(value, 0, len - at) : NULL;
        if (!value_end) goto invalid;
        uintptr_t value_len = (uintptr_t)(value_end - value);
        at += value_len + 1;
        uint8_t *entry = malloc(name_len + value_len + 2);
        if (!entry) { *bad = ENOMEM; goto failed; }
        memcpy(entry, name, name_len);
        entry[name_len] = '=';
        memcpy(entry + name_len + 1, value, value_len + 1);
        made[m++] = entry;
        table[n++] = (char *)entry;
    }
    if (at != len) goto invalid;
    table[n] = NULL;
    *owned = made;
    *owned_count = m;
    return table;
invalid:
    *bad = EINVAL;
failed:
    for (uintptr_t i = 0; i < m; i++) free(made[i]);
    free(made);
    free(table);
    return NULL;
}

/* What `kur_libc_process_spawn` reads for one of the child's standard
 * streams, besides a descriptor of this process for it to get a copy of. */
#define KUR_STREAM_INHERIT (-1)
#define KUR_STREAM_NULL (-2)
#define KUR_STREAM_PIPE (-3)

/* Starts the program `line` names, found on the PATH, with `count`
 * arguments, the program's name first: `line` is each argument followed by
 * a NUL, `len` bytes in all.
 *
 * The child's environment is this process's, emptied first when
 * `env_clear` is set, then changed by the `env_ops` changes `env` holds
 * (`build_environment`). Its working directory is `dir`, a path of
 * `dir_len` bytes and a NUL, when `dir` is not NULL, else this process's;
 * where the C library cannot change it (built with KUR_LIBC_NO_ADDCHDIR,
 * for one without `posix_spawn_file_actions_addchdir_np`), a `dir` is
 * ENOSYS. Each of its standard streams 0, 1 and 2 is what `streams` says
 * for it: KUR_STREAM_INHERIT for this process's own, KUR_STREAM_NULL for
 * /dev/null, KUR_STREAM_PIPE for a new pipe, or a descriptor of this
 * process, which the child gets a copy of. On return, `streams` holds this
 * process's end of each pipe made, a descriptor the caller owns, and -1 for
 * the other streams (and for every stream on failure, every pipe closed).
 * The child gets SIGPIPE's default action back: an executable's C `main`
 * ignores SIGPIPE (`kur_libc_process_start`), and an ignored signal stays
 * ignored across `exec`.
 *
 * Its process id, or a negative errno: what `posix_spawnp` reports (ENOENT
 * for a program not found, or the errno of the `exec` that failed), EINVAL
 * when an argument holds a NUL of its own or the environment's changes are
 * not well formed, or the errno of making a pipe. */
int64_t kur_libc_process_spawn(const uint8_t *line, uintptr_t len, uintptr_t count,
                               const uint8_t *env, uintptr_t env_len, uintptr_t env_ops, uint8_t env_clear,
                               const uint8_t *dir, uintptr_t dir_len, int32_t *streams) {
    int ends[3][2] = {{-1, -1}, {-1, -1}, {-1, -1}};
    int32_t wanted[3] = {streams[0], streams[1], streams[2]};
    for (int i = 0; i < 3; i++) streams[i] = -1;
    if (count == 0 || count > SIZE_MAX / sizeof(char *) - 1) return -EINVAL;
    if (dir && text_ok(dir, dir_len)) return -EINVAL;
#ifdef KUR_LIBC_NO_ADDCHDIR
    if (dir) return -ENOSYS;
#endif
    char **argv = malloc((count + 1) * sizeof(char *));
    if (!argv) return -ENOMEM;
    uintptr_t start = 0, n = 0;
    for (uintptr_t i = 0; i < len; i++) {
        if (line[i] == 0) {
            if (n < count) argv[n] = (char *)line + start;
            n++;
            start = i + 1;
        }
    }
    /* An argument holding a NUL of its own would split in two. */
    if (n != count || start != len) { free(argv); return -EINVAL; }
    argv[count] = NULL;
    char **envp = environ;
    uint8_t **made = NULL;
    uintptr_t made_count = 0;
    int failed = 0;
    if (env_clear || env_ops) {
        envp = build_environment(env, env_len, env_ops, env_clear, &made, &made_count, &failed);
        if (!envp) { free(argv); return -(int64_t)failed; }
    }
    /* Pipes are made close-on-exec, so no child gets an end it was not
     * given: the child's own end reaches it as its stream 0, 1 or 2, by
     * `dup2`, which clears the flag. */
    for (int i = 0; i < 3 && !failed; i++) {
        if (wanted[i] == KUR_STREAM_PIPE && pipe2(ends[i], O_CLOEXEC) != 0) failed = errno;
    }
    posix_spawn_file_actions_t actions;
    int actions_made = 0;
    if (!failed) {
        failed = posix_spawn_file_actions_init(&actions);
        actions_made = !failed;
    }
    for (int i = 0; i < 3 && !failed; i++) {
        if (wanted[i] == KUR_STREAM_NULL) {
            failed = posix_spawn_file_actions_addopen(&actions, i, "/dev/null", i == 0 ? O_RDONLY : O_WRONLY, 0);
        } else if (wanted[i] == KUR_STREAM_PIPE) {
            /* The child reads its standard input from the pipe's read end
             * and writes the others into the write end. */
            failed = posix_spawn_file_actions_adddup2(&actions, ends[i][i == 0 ? 0 : 1], i);
        } else if (wanted[i] >= 0) {
            failed = posix_spawn_file_actions_adddup2(&actions, wanted[i], i);
        }
    }
#ifndef KUR_LIBC_NO_ADDCHDIR
    if (dir && !failed) failed = posix_spawn_file_actions_addchdir_np(&actions, (const char *)dir);
#endif
    posix_spawnattr_t attributes;
    int attributes_made = 0;
    if (!failed) {
        failed = posix_spawnattr_init(&attributes);
        attributes_made = !failed;
    }
    if (!failed) {
        sigset_t defaults;
        sigemptyset(&defaults);
        sigaddset(&defaults, SIGPIPE);
        failed = posix_spawnattr_setsigdefault(&attributes, &defaults);
    }
    if (!failed) failed = posix_spawnattr_setflags(&attributes, POSIX_SPAWN_SETSIGDEF);
    pid_t pid = 0;
    if (!failed) failed = posix_spawnp(&pid, argv[0], &actions, &attributes, argv, envp);
    if (attributes_made) posix_spawnattr_destroy(&attributes);
    if (actions_made) posix_spawn_file_actions_destroy(&actions);
    for (int i = 0; i < 3; i++) {
        if (ends[i][0] < 0) continue;
        /* The child's end is the child's own now; this process's end is the
         * caller's, or closed when the child did not start. */
        int ours = i == 0 ? ends[i][1] : ends[i][0];
        close(i == 0 ? ends[i][0] : ends[i][1]);
        if (failed) close(ours);
        else streams[i] = ours;
    }
    for (uintptr_t i = 0; i < made_count; i++) free(made[i]);
    free(made);
    if (envp != environ) free(envp);
    free(argv);
    if (failed) return -(int64_t)failed;
    return pid;
}

/* Waits for the child `pid`, again when a signal interrupts the wait; with
 * `block` 0, only looks. The exit status (0 to 255) when the child exits,
 * 256 plus the signal's number when a signal ends it, 512 when it still
 * runs and `block` is 0, or the negative errno `waitpid` reports. */
int64_t kur_libc_process_wait(int64_t pid, uint8_t block) {
    int status;
    pid_t got;
    while ((got = waitpid((pid_t)pid, &status, block ? 0 : WNOHANG)) < 0) {
        if (errno != EINTR) return -(int64_t)errno;
    }
    if (got == 0) return 512;
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 256 + (int64_t)WTERMSIG(status);
    return -(int64_t)ECHILD;
}

/* Sends SIGKILL to the child `pid`: 0, or the errno of `kill(2)`. */
int32_t kur_libc_process_kill(int64_t pid) {
    return kill((pid_t)pid, SIGKILL) == 0 ? 0 : errno;
}

/* Sends the signal numbered `signal` to the child `pid`: 0, or the errno of
 * `kill(2)` (EINVAL for a number that is no signal). */
int32_t kur_libc_process_signal(int64_t pid, int32_t signal) {
    return kill((pid_t)pid, signal) == 0 ? 0 : errno;
}

/* Waits until descriptor `a` or `b` can be read without blocking, or has
 * reached its end or failed, again when a signal interrupts the wait: bit
 * 0 set for `a`, bit 1 for `b`, or a negative errno. A negative descriptor
 * is passed over. */
int32_t kur_libc_process_poll(int32_t a, int32_t b) {
    struct pollfd fds[2] = {{a, POLLIN, 0}, {b, POLLIN, 0}};
    int n;
    do n = poll(fds, 2, -1); while (n < 0 && errno == EINTR);
    if (n < 0) return -(int32_t)errno;
    return (fds[0].revents ? 1 : 0) | (fds[1].revents ? 2 : 0);
}

/* The number of processors this process may run on, at least 1: those of
 * its CPU affinity mask (`sched_getaffinity`) where the C library reports
 * it, else those online. */
uintptr_t kur_libc_process_cpus(void) {
#ifdef CPU_COUNT
    cpu_set_t allowed;
    if (sched_getaffinity(0, sizeof allowed, &allowed) == 0) {
        int n = CPU_COUNT(&allowed);
        if (n > 0) return (uintptr_t)n;
    }
#endif
    long n = sysconf(_SC_NPROCESSORS_ONLN);
    return n < 1 ? 1 : (uintptr_t)n;
}
