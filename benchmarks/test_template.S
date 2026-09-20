  .section .text
  .global  main

main:
  li       a0, 0
  li       a1, 0
  call     m5_reset_stats

# -----------------------------------------------------------
# MAIN PROGRAM
# -----------------------------------------------------------
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
