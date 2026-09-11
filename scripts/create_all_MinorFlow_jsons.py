#!/usr/bin/env python3
"""Turn every gem5 debug trace in a folder into a MinorFlow viewer JSON.

Names the output after the input with '_trace' removed, so
daxpy_trace.config1.txt becomes daxpy.config1.json, which is what the sweep's
collected files and the viewer's sample name both expect.

    python3 scripts/create_all_MinorFlow_jsons.py        # the whole repository
    python3 scripts/create_all_MinorFlow_jsons.py results/run
    python3 scripts/create_all_MinorFlow_jsons.py -j 8
    python3 scripts/create_all_MinorFlow_jsons.py --force   # redo the JSONs
"""
import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# This script belongs to the MinorFlow repository and works inside it only.
# The fork's own traces are handled by scripts/create_all_CVA6_repo_jsons.py,
# which walks the whole checkout and calls this one for the submodule.
HERE = os.path.dirname(os.path.abspath(__file__))


def find_tracer():
    """(tracer, the folder it sits in). In this repository the
    tracer is one level above scripts/, and in a container
    scripts/ is the root with the viewer a folder below it."""
    above = os.path.dirname(HERE)
    for base in (above, os.path.join(above, "MinorFlow"),
                 os.path.join(os.curdir, "MinorFlow")):
        candidate = os.path.join(base, "MinorFlow_tracer.py")
        if os.path.isfile(candidate):
            return candidate, base
    return os.path.join(above, "MinorFlow_tracer.py"), above


TRACER, REPO_ROOT = find_tracer()

# Traces are read at a few tens of MB/s each and a large one holds a lot of
# state, so this is deliberately below the core count. Each worker only waits
# on a subprocess, which is why these are threads rather than processes.
DEFAULT_WORKERS = 4

TRACE_MARK = "_trace"
TRACE_END = ".txt"


def json_for(path):
    """daxpy_trace.config1.txt -> daxpy.config1.json"""
    base = path[:-len(TRACE_END)] if path.endswith(TRACE_END) else path
    return base.replace(TRACE_MARK, "") + ".json"


def run_one(trace, out_json, quiet, strict):
    cmd = [sys.executable, TRACER, trace, "-o", out_json]
    if quiet:
        cmd.append("--quiet")
    if strict:
        cmd.append("--strict")
    start = time.time()
    # Output is not captured: the tracer's progress line is the only sign of
    # life on a trace that takes minutes, and swallowing it left the batch
    # looking hung.
    code = subprocess.run(cmd).returncode
    took = time.time() - start
    name = os.path.basename(out_json)
    if code == 3:
        # The tracer's strict exit. The JSON was still written, so say what
        # happened rather than implying the conversion produced nothing.
        return (f"[DEGRADED] {name} written but the trace is degraded "
                f"(exit 3, see metadata.degraded)")
    if code != 0:
        return f"[ERROR]   {name} failed with exit code {code}"
    size = os.path.getsize(out_json) / (1024 * 1024)
    return f"[SUCCESS] {name} ({size:.1f} MB, {took:.0f}s)"


def main():
    parser = argparse.ArgumentParser(
        description="Run MinorFlow_tracer.py over every gem5 trace in a "
                    "folder.")
    parser.add_argument("folder", nargs="?", default=REPO_ROOT,
                        help="Folder holding the traces. Defaults to the one "
                             "this script sits in")
    parser.add_argument("-j", "--jobs", type=int, default=DEFAULT_WORKERS,
                        metavar="N",
                        help=f"Traces to convert at a time. Defaults to "
                             f"{DEFAULT_WORKERS}. Each holds a whole trace's "
                             f"state, so memory binds before cores do")
    parser.add_argument("--force", action="store_true",
                        help="Convert a trace even when its JSON already "
                             "exists and is newer")
    parser.add_argument("--quiet", action="store_true",
                        help="Pass --quiet to the tracer, dropping its "
                             "progress line")
    parser.add_argument("--strict", action="store_true",
                        help="Pass --strict to the tracer, so a trace "
                             "captured without one of the debug-flag line "
                             "families exits non-zero instead of passing for "
                             "a complete one. The JSONs are still written.")
    args = parser.parse_args()

    if not os.path.isfile(TRACER):
        print(f"[ERROR] No {TRACER}. This script runs inside the "
              f"MinorFlow repository, beside its tracer.")
        return 2
    if not os.path.isdir(args.folder):
        print(f"[ERROR] {args.folder} is not a folder")
        return 2

    folder = os.path.abspath(args.folder)
    traces = sorted(os.path.join(folder, f) for f in os.listdir(folder)
                    if f.endswith(TRACE_END) and TRACE_MARK in f)
    if not traces:
        print(f"[INFO] No *{TRACE_MARK}*{TRACE_END} files in {folder}")
        return 0

    todo, skipped = [], []
    for trace in traces:
        out_json = json_for(trace)
        if (not args.force and os.path.isfile(out_json)
                and os.path.getmtime(out_json) >= os.path.getmtime(trace)):
            skipped.append(os.path.basename(out_json))
        else:
            todo.append((trace, out_json))

    if skipped:
        print(f"[INFO] {len(skipped)} JSON(s) already up to date, use --force "
              f"to redo them: {', '.join(skipped)}")
    if not todo:
        return 0

    print(f"[INFO] Converting {len(todo)} trace(s) from {folder}")
    print(f"[INFO] {args.jobs} at a time\n")

    failed = 0
    degraded = 0
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(run_one, t, j, args.quiet, args.strict)
                   for t, j in todo]
        for future in as_completed(futures):
            line = future.result()
            failed += line.startswith("[ERROR]")
            degraded += line.startswith("[DEGRADED]")
            print(line)

    print(f"\n[INFO] {len(todo) - failed - degraded} of {len(todo)} "
          f"converted cleanly")
    if degraded:
        print(f"[WARN] {degraded} trace(s) converted but degraded. Their "
              f"JSONs are written and metadata.degraded says what is missing.")
    if failed or degraded:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
