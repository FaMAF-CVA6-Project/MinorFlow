How to obtain trace in gem5 with our docker file

```bash
python3 [run_assembly_code.py/run_c_code.py] cva6_config.py [code.S/code.c]
build/RISCV/gem5.opt --debug-flags=Minor,MinorTrace,MinorTiming,CacheAll,ExecAll,Fetch,Decode,IEW,Commit,LSQ,Scoreboard,Writeback --debug-file=code.txt  --debug-start=0 --debug-end=80000000 cva6_config.py code
```
