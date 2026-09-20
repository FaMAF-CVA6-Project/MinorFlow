#include <gem5/m5ops.h>

/* Stresses the branch predictor: a chain of compares on pseudo-random data
 * spread over all five of classify's ranges, so each branch goes either way
 * with no pattern to learn. */

static volatile int data[256];

/* The first value and the width of each range classify tells apart, with the
 * two open-ended ranges cut to 500 values. */
static const int range_first[5] = {-500, 0, 100, 1000, 5000};
static const int range_width[5] = {500, 100, 900, 4000, 500};

__attribute__((noinline)) static int classify(int x)
{
    if (x < 0)
        return 0;
    if (x < 100)
        return 1;
    if (x < 1000)
        return 2;
    if (x < 5000)
        return 3;
    return 4;
}

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
    unsigned int seed = 0x12345678u;
    for (int i = 0; i < 256; i++)
    {
        seed = seed * 1103515245u + 12345u;
        // A range from 8 bits of the seed and an offset into it from 12 more,
        // by multiply and shift, so no divide adds to the branches' cost.
        unsigned int range = (((seed >> 20) & 0xffu) * 5u) >> 8;
        unsigned int offset = ((seed >> 8) & 0xfffu) * range_width[range];
        data[i] = range_first[range] + (int)(offset >> 12);
    }

    int counts[5] = {0, 0, 0, 0, 0};
    for (int iter = 0; iter < 10; iter++)
    {
        for (int i = 0; i < 256; i++)
        {
            counts[classify(data[i])]++;
        }
    }

    static volatile int sink;
    sink = counts[0] + counts[1] + counts[2] + counts[3] + counts[4];
    // END OF MAIN PROGRAM

    m5_dump_stats(0, 0);

    m5_exit(0);
}
