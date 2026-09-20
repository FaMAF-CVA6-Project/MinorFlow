  .data
N:
  .dword   4096
Alpha:
  .dword   2

  .bss
  .align   3
X:
  .zero    32768
Y:
  .zero    32768
Z:
  .zero    32768

  .section .text
  .global  main

main:
  li       a0, 0
  li       a1, 0
  call     m5_reset_stats

# -----------------------------------------------------------
# MAIN PROGRAM
# -----------------------------------------------------------
  la       t0, N
  ld       t1, 0(t0)

  la       t2, Alpha
  ld       t3, 0(t2)
  fcvt.d.l f10, t3

  la       s2, X
  la       s3, Y
  la       s4, Z

  mv       s5, s2         # Cursor into X
  mv       s6, s3         # Cursor into Y
  mv       s7, s4         # Cursor into Z

  li       t4, 0

# Z[i] = alpha * X[i] + Y[i], into a third vector for three 32 KiB streams
# through the 32 KiB D-cache, two in DAXPY's in-place form. The loop starts on
# a 16-byte boundary, so it opens a fetch line at every width the sweep uses.
  .align   4
loop:
  fld      f5, 0(s5)
  fld      f6, 0(s6)
  fmul.d   f7, f10, f5
  fadd.d   f8, f7, f6
  fsd      f8, 0(s7)

  addi     s5, s5, 8
  addi     s6, s6, 8
  addi     s7, s7, 8

  addi     t4, t4, 1
  blt      t4, t1, loop
# -----------------------------------------------------------
# END OF MAIN PROGRAM
# -----------------------------------------------------------

  li       a0, 0
  li       a1, 0
  call     m5_dump_stats

# Padding, so what the front end fetches past the call to m5_dump_stats
# before the stats are dumped is nops, which predict no branch, and never
# the call to m5_exit.
  .rept    50
  nop
  .endr

  call     m5_exit
