# tests/

Trace JSONs, and the sample the viewer loads on its own.

Nothing here is tracked. gem5 debug traces run to gigabytes and their JSONs to hundreds of megabytes, so this directory is generated rather than committed, and a fresh clone finds only this file. That is deliberate, and it is why the README links to `tests/` resolve.

## What lands here

| File                                     | Made by                                                                                                                              |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `<name>_trace.txt`                       | `scripts/run_gem5.py`, copied from `run_results/`                                                                                    |
| `<name>.json`                            | `MinorFlow_tracer.py`, or `scripts/create_all_MinorFlow_jsons.py` over a folder of traces                                            |
| `daxpy.config1.json`, `daxpy.config1.js` | `scripts/make_MinorFlow_sample.py`. This pair is the sample `MinorFlow.html` loads when opened with no file, under exactly this name |
| `oversized*.json`                        | `scripts/make_MinorFlow_oversized.py`, for testing the viewer's record ceiling                                                       |

## Filling it

```bash
cd /path/to/gem5
python3 /path/to/MinorFlow/scripts/run_gem5.py configs/gem5_config_MinorFlow.py daxpy.S
python3 MinorFlow_tracer.py tests/daxpy_trace.txt -o tests/daxpy.json
python3 scripts/make_MinorFlow_sample.py tests/daxpy.json -o tests/daxpy.config1
```

Add `--strict` to the tracer in a batch run: it exits non-zero when a debug-flag line family is missing from the capture, and `metadata.degraded` in the JSON says which.
