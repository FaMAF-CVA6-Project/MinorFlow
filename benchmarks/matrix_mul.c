#include <gem5/m5ops.h>

#define N 4

static int A[N][N];
static int B[N][N];
static volatile int C[N][N];

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
    for (int i = 0; i < N; i++)
    {
        for (int j = 0; j < N; j++)
        {
            A[i][j] = (i * 7 + j * 3) & 0xff;
            B[i][j] = (i * 5 + j * 11) & 0xff;
        }
    }

    for (int rep = 0; rep < 16; rep++)
    {
        for (int i = 0; i < N; i++)
        {
            for (int j = 0; j < N; j++)
            {
                int sum = 0;
                for (int k = 0; k < N; k++)
                {
                    sum += A[i][k] * B[k][j];
                }
                C[i][j] = sum;
            }
        }
    }

    static volatile int sink;
    int total = 0;

    for (int i = 0; i < N; i++)
        for (int j = 0; j < N; j++)
            total += C[i][j];
    sink = total;
    // END OF MAIN PROGRAM

    m5_dump_stats(0, 0);

    m5_exit(0);
}
