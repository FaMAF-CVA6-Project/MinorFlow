  .data
N:
  .dword   4096           # Size of the vectors
Alpha:
  .dword   2              # Scalar multiplier

  .bss
  .align   3
X:
  .zero    32768          # Vector X (4096)*8 bytes
Y:
  .zero    32768          # Vector Y (4096)*8 bytes
Z:
  .zero    32768          # Vector Z (4096)*8 bytes

  .section .text
  .global  main

main:
  li       a0, 0
  li       a1, 0
  call     m5_reset_stats

# -----------------------------------------------------------
# MAIN PROGRAM
# -----------------------------------------------------------
  la       t0, N          # Address of N
  ld       t1, 0(t0)      # t1 = N

  la       t2, Alpha      # Address of Alpha
  ld       t3, 0(t2)      # t3 = Alpha (integer)
  fcvt.d.l f10, t3        # f10 = (double) Alpha

  la       s2, X          # Base of X
  la       s3, Y          # Base of Y
  la       s4, Z          # Base of Z

  mv       s5, s2         # Offset X -> s5
  mv       s6, s3         # Offset Y -> s6
  mv       s7, s4         # Offset Z -> s7

  li       t4, 0          # Loop index i = 0

  .align   4
loop:
  fld      f5, 0(s5)      # f5 = X[i]
  fld      f6, 0(s6)      # f6 = Y[i]
  fmul.d   f7, f10, f5    # f7 = alpha * X[i]
  fadd.d   f8, f7, f6     # f8 = f7 + Y[i]
  fsd      f8, 0(s7)      # Z[i] = f8

  addi     s5, s5, 8
  addi     s6, s6, 8
  addi     s7, s7, 8

  addi     t4, t4, 1      # i++
  blt      t4, t1, loop   # while i < N
# -----------------------------------------------------------
# END OF MAIN PROGRAM
# -----------------------------------------------------------

  li       a0, 0
  li       a1, 0
  call     m5_dump_stats

# PADDING
  .rept    50
  nop
  .endr

# SALIDA
  call     m5_exit