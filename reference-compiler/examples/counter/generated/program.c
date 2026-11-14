#include <stddef.h>
#include <stdint.h>
typedef struct { unsigned char dummy; } KurUnit;
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

typedef struct K_counter_Counter K_counter_Counter;
struct K_counter_Counter { uint32_t f_value; };
static int32_t _ZN4kurs7counter4main17h3c4d00f3096e51d3E(void);
static uint32_t _ZN4kurs7counter4bump17hb7ec39266b253daeE(K_counter_Counter* restrict entry_counter);
static int32_t _ZN4kurs7counter4main17h3c4d00f3096e51d3E(void) {
  K_counter_Counter p_counter_1;
  uint32_t entry_v2;
  K_counter_Counter entry_v3;
  uint32_t entry_v5;
  uint32_t entry_v7;
  _Bool entry_v9;
  uint32_t b10_v12;
  uint32_t b11_v13;
  uint32_t join15_r14;
  uint32_t join15_v17;
  uint32_t join15_v21;
  int32_t join15_v22;
  L_entry: ;
  entry_v2 = UINT64_C(40);
  entry_v3.f_value = entry_v2;
  p_counter_1 = entry_v3;
  entry_v5 = p_counter_1.f_value;
  entry_v7 = UINT64_C(40);
  entry_v9 = (entry_v5 == entry_v7);
  if (entry_v9) {
    goto L_b10;
  } else {
    goto L_b11;
  }
  L_b10: ;
  b10_v12 = _ZN4kurs7counter4bump17hb7ec39266b253daeE(&p_counter_1);
  join15_r14 = b10_v12;
  goto L_join15;
  L_b11: ;
  b11_v13 = UINT64_C(0);
  join15_r14 = b11_v13;
  goto L_join15;
  L_join15: ;
  join15_v17 = p_counter_1.f_value;
  join15_v21 = (uint32_t)((uint64_t)(join15_v17) + (uint64_t)(join15_r14));
  join15_v22 = kur_dec_i32((uint64_t)(join15_v21));
  return join15_v22;
}

static uint32_t _ZN4kurs7counter4bump17hb7ec39266b253daeE(K_counter_Counter* restrict entry_counter) {
  uint32_t entry_v2_1;
  uint32_t entry_v4_1;
  uint32_t entry_v6_1;
  uint32_t entry_v8_1;
  L_entry: ;
  entry_v2_1 = (*entry_counter).f_value;
  entry_v4_1 = UINT64_C(1);
  entry_v6_1 = (uint32_t)((uint64_t)(entry_v2_1) + (uint64_t)(entry_v4_1));
  (*entry_counter).f_value = entry_v6_1;
  entry_v8_1 = (*entry_counter).f_value;
  return entry_v8_1;
}

int main(void) { return (int)_ZN4kurs7counter4main17h3c4d00f3096e51d3E(); }
