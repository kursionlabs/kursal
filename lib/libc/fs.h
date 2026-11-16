#ifndef KUR_LIBC_FS_H
#define KUR_LIBC_FS_H

#include <stdint.h>

/* Unsafe ABI: see README.md for pointer and ownership obligations. */
int32_t kur_libc_fs_error(void);
void kur_libc_fs_release(uint8_t *bytes);
uint8_t *kur_libc_fs_canonicalize(uint8_t *bytes, uintptr_t len);
uint8_t *kur_libc_fs_getenv(uint8_t *bytes, uintptr_t len);
int32_t kur_libc_fs_open_read(uint8_t *bytes, uintptr_t len);
int32_t kur_libc_fs_create(uint8_t *bytes, uintptr_t len);
intptr_t kur_libc_fs_read(int32_t fd, uint8_t *buf, uintptr_t len);
intptr_t kur_libc_fs_write(int32_t fd, const uint8_t *buf, uintptr_t len);
int32_t kur_libc_fs_close(int32_t fd);
intptr_t kur_libc_fs_regular_size(int32_t fd);
uint8_t *kur_libc_fs_open_directory(uint8_t *bytes, uintptr_t len);
uint8_t *kur_libc_fs_next_entry(uint8_t *handle);
uint8_t *kur_libc_fs_entry_name(uint8_t *entry);
int32_t kur_libc_fs_entry_is_directory(uint8_t *handle, uint8_t *entry);
int32_t kur_libc_fs_close_directory(uint8_t *handle);

#endif
