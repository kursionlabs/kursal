#ifndef KUR_LIBC_FS_H
#define KUR_LIBC_FS_H

#include <stdint.h>

/* Unsafe ABI: see README.md for pointer and ownership obligations. A path or
 * name is `len` bytes followed by a NUL. */
int32_t kur_libc_fs_error(void);
void kur_libc_fs_release(uint8_t *bytes);
uint8_t *kur_libc_fs_canonicalize(const uint8_t *path, uintptr_t len);
uint8_t *kur_libc_fs_getenv(const uint8_t *name, uintptr_t len);
int32_t kur_libc_fs_open_read(const uint8_t *path, uintptr_t len);
int32_t kur_libc_fs_create(const uint8_t *path, uintptr_t len);
int32_t kur_libc_fs_stat(const uint8_t *path, uintptr_t len, uint64_t *size);
int32_t kur_libc_fs_remove(const uint8_t *path, uintptr_t len);
int32_t kur_libc_fs_rename(const uint8_t *from, uintptr_t from_len, const uint8_t *to, uintptr_t to_len);
int32_t kur_libc_fs_mkdir(const uint8_t *path, uintptr_t len);
intptr_t kur_libc_fs_read(int32_t fd, uint8_t *buf, uintptr_t len);
intptr_t kur_libc_fs_write(int32_t fd, const uint8_t *buf, uintptr_t len);
int32_t kur_libc_fs_close(int32_t fd);
intptr_t kur_libc_fs_regular_size(int32_t fd);
uint8_t *kur_libc_fs_open_directory(const uint8_t *path, uintptr_t len);
uint8_t *kur_libc_fs_next_entry(uint8_t *handle);
uint8_t *kur_libc_fs_entry_name(uint8_t *entry);
int32_t kur_libc_fs_entry_is_directory(uint8_t *handle, uint8_t *entry);
int32_t kur_libc_fs_close_directory(uint8_t *handle);
int64_t kur_libc_process_run(const uint8_t *line, uintptr_t len, uintptr_t count);
void kur_libc_process_report_error(int32_t code);
void kur_libc_time_sleep(uint64_t nanos);

#endif
