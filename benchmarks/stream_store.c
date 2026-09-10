#include <gem5/m5ops.h>

#define N (256 * 1024) /* 1 MiB, far past the 32 KiB L1D */
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
    for (int i = 0; i < N; i += 16) /* stride 16 ints = 64 B = one line per store */
        arr[i] = i;
    // END OF MAIN PROGRAM

    m5_dump_stats(0, 0);

    m5_exit(0);
}
