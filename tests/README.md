# tests/

Tracer JSONs, and the sample JSONs the viewer's Load sample button offers.

Only this file is committed, and it is kept so the folder exists in a fresh clone. gem5 debug traces run to gigabytes and their JSONs to hundreds of megabytes, so everything else here is generated, samples included.

The FaMAF CVA6 Project, which this viewer was written for, fills this folder while it builds its gem5 image: a full sample of every program in `benchmarks/`, written with `-n 0` so each one is a whole run. In a clone the folder is empty until the samples are made, and the Load sample button appears only once `samples.js` lists one.

## What lands here

| File                                                   | Made by                                                                                                                                                                                        |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `<name>_trace.txt`                                     | `scripts/run_gem5.py`, which leaves it in `results/run/` under the gem5 root, copied here by hand                                                                                              |
| `<name>.json`                                          | `MinorFlow_tracer.py`, or `scripts/create_all_MinorFlow_jsons.py` over a folder of traces                                                                                                      |
| `<name>.sample.js`, `<name>.sample.json`, `samples.js` | `scripts/make_MinorFlow_sample.py`. The page reads the manifest `samples.js`, served or opened from disk, and offers every sample it lists that its own tracer wrote at its own schema version |
| `oversized*.json`                                      | `scripts/make_MinorFlow_oversized.py -o tests/oversized.json`, for testing the viewer's byte and record limits. Without `-o` it writes to the working directory                                |

## Filling it

```bash
cd /path/to/gem5
python3 /path/to/MinorFlow/scripts/run_gem5.py configs/gem5_config_MinorFlow.py daxpy.S
cp results/run/daxpy_trace.txt /path/to/MinorFlow/tests/
cd /path/to/MinorFlow
python3 MinorFlow_tracer.py tests/daxpy_trace.txt -o tests/daxpy.json
python3 scripts/make_MinorFlow_sample.py tests/daxpy.json
```

Add `--strict` to the tracer in a batch run: it exits with 3 when a debug-flag line family is missing from the capture, or the trace holds no instruction or no commit, and `metadata.degraded` in the JSON says which.
