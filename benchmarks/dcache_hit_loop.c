#include <gem5/m5ops.h>

#define N 256  /* 256 ints = 1 KiB, sits comfortably in the L1D */
#define REPS 4 /* a few hit passes after the warm-up */

volatile int arr[N];

int main(void)
{
#if defined(__riscv)
    __asm__ volatile(
        ".option push\n"
        ".option norelax\n"
        "1: auipc gp, %%pcrel_hi(__global_pointer$)\n"
        "   addi  gp, gp, %%pcrel_lo(1b)\n"
        ".option pop\n" ::: "gp");
#endif

    m5_reset_stats(0, 0);

    // MAIN PROGRAM
    long sum = 0;

    /* Warm-up pass: compulsory misses fill the cache, not measured. */
    for (int i = 0; i < N; i++)
        sum += arr[i];

    for (int rep = 0; rep < REPS; rep++)
        for (int i = 0; i < N; i++)
            sum += arr[i];
    // END OF MAIN PROGRAM

    m5_dump_stats(0, 0);

    m5_exit(0);
}
