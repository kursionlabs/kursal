#ifndef KUR_LIBC_H
#define KUR_LIBC_H

#include <stdint.h>

/* The C runtime's ABI, by the file that defines it; the `libc` package's
 * modules declare, once each, the functions Kursal code calls. Unsafe: see README.md for pointer and ownership obligations. A path or
 * name is `len` bytes followed by a NUL. */

/* io.c */
uintptr_t kur_libc_error_message(int32_t code, uint8_t *out, uintptr_t cap);
int32_t kur_libc_error_kind(int32_t code);
int32_t kur_libc_fs_open_read(const uint8_t *path, uintptr_t len);
int32_t kur_libc_fs_create(const uint8_t *path, uintptr_t len);
intptr_t kur_libc_fs_read(int32_t fd, uint8_t *buf, uintptr_t len);
intptr_t kur_libc_fs_write(int32_t fd, const uint8_t *buf, uintptr_t len);
int32_t kur_libc_fs_close(int32_t fd);
int32_t kur_libc_fs_regular_size(int32_t fd, uint64_t *size);
intptr_t kur_libc_fs_size_hint(int32_t fd);
int32_t kur_libc_stdin_claim(void);
void kur_libc_stdin_release(void);

/* fs.c */
int32_t kur_libc_fs_error(void);
void kur_libc_fs_release(uint8_t *bytes);
uint8_t *kur_libc_fs_canonicalize(const uint8_t *path, uintptr_t len);
int32_t kur_libc_fs_stat(const uint8_t *path, uintptr_t len, uint64_t *size);
int32_t kur_libc_fs_remove(const uint8_t *path, uintptr_t len);
int32_t kur_libc_fs_rename(const uint8_t *from, uintptr_t from_len, const uint8_t *to, uintptr_t to_len);
int32_t kur_libc_fs_mkdir(const uint8_t *path, uintptr_t len);
uint8_t *kur_libc_fs_open_directory(const uint8_t *path, uintptr_t len);
uint8_t *kur_libc_fs_next_entry(uint8_t *handle);
uint8_t *kur_libc_fs_entry_name(uint8_t *entry);
int32_t kur_libc_fs_entry_is_directory(uint8_t *handle, uint8_t *entry);
int32_t kur_libc_fs_close_directory(uint8_t *handle);

/* env.c */
uint8_t *kur_libc_env_get(const uint8_t *name, uintptr_t len);

/* process.c */
int64_t kur_libc_process_spawn(const uint8_t *line, uintptr_t len, uintptr_t count,
                               const uint8_t *env, uintptr_t env_len, uintptr_t env_ops, uint8_t env_clear,
                               const uint8_t *dir, uintptr_t dir_len, int32_t *streams);
int64_t kur_libc_process_wait(int64_t pid, uint8_t block);
int32_t kur_libc_process_kill(int64_t pid);
int32_t kur_libc_process_signal(int64_t pid, int32_t signal);
int32_t kur_libc_process_poll(int32_t a, int32_t b);
uintptr_t kur_libc_process_cpus(void);
_Noreturn void kur_libc_process_exit(int32_t status);

/* start.c */
void kur_libc_process_start(void);
uint64_t kur_libc_process_hash_seed(uint32_t word);
void kur_libc_process_report_error(int32_t code);

/* time.c */
uint64_t kur_libc_time_monotonic(void);
void kur_libc_time_sleep(uint64_t nanos);

/* thread.c */
int32_t kur_libc_thread_error(void);
uint8_t *kur_libc_thread_create(uint8_t *(*entry)(uint8_t *), uint8_t *arg);
int32_t kur_libc_thread_join(uint8_t *handle, uint8_t **out_result);

#endif
