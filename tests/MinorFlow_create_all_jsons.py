#!/usr/bin/env python3
"""Turn every gem5 debug trace in a folder into a MinorFlow viewer JSON.

Names the output after the input with '_trace' removed, so
daxpy_trace.config1.txt becomes daxpy.config1.json, which is what the sweep's
collected files and the viewer's sample name both expect.

    python3 MinorFlow_create_all_jsons.py            # this script's folder
    python3 MinorFlow_create_all_jsons.py ../run_results
    python3 MinorFlow_create_all_jsons.py -j 8
    python3 MinorFlow_create_all_jsons.py --force    # redo existing JSONs
"""
import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# The tracer, resolved from this file rather than from the working directory,
# so the script runs from anywhere.
HERE = os.path.dirname(os.path.abspath(__file__))
TRACER = os.path.join(HERE, os.pardir, "MinorFlow_tracer.py")

DEFAULT_WORKERS = 4

TRACE_MARK = "_trace"
TRACE_END = ".txt"


def json_for(path):
    """daxpy_trace.config1.txt -> daxpy.config1.json"""
    base = path[:-len(TRACE_END)] if path.endswith(TRACE_END) else path
    return base.replace(TRACE_MARK, "") + ".json"


def run_one(trace, out_json, quiet):
    cmd = [sys.executable, TRACER, trace, "-o", out_json]
    if quiet:
        cmd.append("--quiet")
    start = time.time()
    code = subprocess.run(cmd).returncode
    took = time.time() - start
    name = os.path.basename(out_json)
    if code != 0:
        return f"[ERROR]   {name} failed with exit code {code}"
    size = os.path.getsize(out_json) / (1024 * 1024)
    return f"[SUCCESS] {name} ({size:.1f} MB, {took:.0f}s)"


def main():
    parser = argparse.ArgumentParser(
        description="Run MinorFlow_tracer.py over every gem5 trace in a "
                    "folder.")
    parser.add_argument("folder", nargs="?", default=HERE,
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
    args = parser.parse_args()

    if not os.path.isfile(TRACER):
        print(f"[ERROR] Tracer not found at {TRACER}. This script expects to "
              f"sit in tests/ inside the MinorFlow repository.")
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
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = [pool.submit(run_one, t, j, args.quiet) for t, j in todo]
        for future in as_completed(futures):
            line = future.result()
            failed += line.startswith("[ERROR]")
            print(line)

    print(f"\n[INFO] {len(todo) - failed} of {len(todo)} converted")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
