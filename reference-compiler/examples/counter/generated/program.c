#include <stddef.h>
#include <stdint.h>
#define KUR_TRAP() __builtin_trap()
/* A call the C compiler must make a tail call, where it can be told to: Clang's `musttail`. */
#if defined(__clang__) && defined(__has_attribute)
#if __has_attribute(musttail)
#define KUR_MUSTTAIL __attribute__((musttail))
#endif
#endif
#ifndef KUR_MUSTTAIL
#define KUR_MUSTTAIL
#endif
/* A component's address by integer arithmetic: projection never dereferences, so it is defined for any
   pointer value, null and dangling included. The `__typeof__` operands are not evaluated. */
#define KUR_FIELD(p, m) ((__typeof__(&(p)->m))((uintptr_t)(p) + offsetof(__typeof__(*(p)), m)))
static inline int8_t kur_dec_i8(uint64_t b) { b &= 0xFF; return b < 0x80 ? (int8_t)b : (int8_t)((int64_t)b - 0x100); }
static inline int16_t kur_dec_i16(uint64_t b) { b &= 0xFFFF; return b < 0x8000 ? (int16_t)b : (int16_t)((int64_t)b - 0x10000); }
static inline int32_t kur_dec_i32(uint64_t b) { b &= 0xFFFFFFFF; return b < 0x80000000 ? (int32_t)b : (int32_t)((int64_t)b - 0x100000000); }
static inline int64_t kur_dec_i64(uint64_t b) { return b <= (uint64_t)INT64_MAX ? (int64_t)b : -INT64_C(1) - (int64_t)(UINT64_MAX - b); }
/* usize and isize are pointer-sized: uintptr_t and intptr_t, of KUR_PTR_BITS bits. */
#define KUR_PTR_BITS (8 * sizeof(uintptr_t))
static inline intptr_t kur_dec_isize(uintptr_t b) { return b <= (uintptr_t)INTPTR_MAX ? (intptr_t)b : -(intptr_t)1 - (intptr_t)(UINTPTR_MAX - b); }

typedef struct K_counter_Counter K_counter_Counter;
struct K_counter_Counter { uint32_t f_value; };
static int32_t _ZN4kurs7counter4main17hc155b651fa495d9cE(void);
static uint32_t _ZN4kurs7counter4bump17h02c8f876ee35ceafE(K_counter_Counter* restrict entry_counter);
static int32_t _ZN4kurs7counter4main17hc155b651fa495d9cE(void) {
  K_counter_Counter p_counter_1;
  uint32_t entry_v2;
  uint32_t entry_v6;
  _Bool entry_v7;
  uint32_t join13_r12;
  uint32_t join13_v16;
  int32_t join13_v17;
  L_entry: ;
  entry_v2 = UINT64_C(40);
  p_counter_1.f_value = entry_v2;
  entry_v6 = UINT64_C(40);
  entry_v7 = (p_counter_1.f_value == entry_v6);
  if (entry_v7) {
    goto L_b8;
  } else {
    goto L_b9;
  }
  L_b8: ;
  join13_r12 = _ZN4kurs7counter4bump17h02c8f876ee35ceafE(&p_counter_1);
  goto L_join13;
  L_b9: ;
  join13_r12 = UINT64_C(0);
  goto L_join13;
  L_join13: ;
  join13_v16 = (uint32_t)((uint64_t)(p_counter_1.f_value) + (uint64_t)(join13_r12));
  join13_v17 = kur_dec_i32((uint64_t)(join13_v16));
  return join13_v17;
}

static uint32_t _ZN4kurs7counter4bump17h02c8f876ee35ceafE(K_counter_Counter* restrict entry_counter) {
  uint32_t entry_v3;
  uint32_t entry_v4;
  L_entry: ;
  entry_v3 = UINT64_C(1);
  entry_v4 = (uint32_t)((uint64_t)((*entry_counter).f_value) + (uint64_t)(entry_v3));
  (*entry_counter).f_value = entry_v4;
  return (*entry_counter).f_value;
}

int main(void) { return (int)_ZN4kurs7counter4main17hc155b651fa495d9cE(); }
