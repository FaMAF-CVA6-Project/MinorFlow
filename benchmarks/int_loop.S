  .section .text
  .global  main

main:
  li       a0, 0
  li       a1, 0
  call     m5_reset_stats

# -----------------------------------------------------------
# MAIN PROGRAM
# -----------------------------------------------------------
# Block A: a large dividend over a small divisor, near the worst case. The
# blocks are .rept copies rather than loops, so the measured region has no
# branch for the predictor to learn.
  li       t3, 0x7FFFFFFFFFFFFFFF
  li       t1, 3
  .rept    48
  div      t0, t3, t1
  andi     t1, t0, 7              # next divisor from result, stays small
  ori      t1, t1, 1              # never zero
  .endr

# Block B: a small dividend over a large divisor, the best case.
# The divisor is not 0x7FFF..., which would overflow to negative on the add.
  li       t3, 1
  li       t1, 0x4000000000000000
  .rept    48
  div      t0, t3, t1
  ori      t0, t0, 1
  add      t1, t1, t0             # the divisor stays huge and positive
  .endr

# Block C: the remainder form, in the worst-case shape of block A.
  li       t3, 0x7FFFFFFFFFFFFFFF
  li       t1, 5
  .rept    48
  rem      t0, t3, t1
  andi     t1, t0, 7
  ori      t1, t1, 1
  .endr
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
