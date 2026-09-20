#!/usr/bin/env python3
"""Turn every gem5 debug trace in a folder into a MinorFlow viewer JSON.

Names each JSON after its trace with _trace removed, so
daxpy_trace.config1.txt becomes daxpy.config1.json, the name the sweep's
collected files use. A degraded trace still gets its JSON, and makes the batch
exit 3. This script belongs to the MinorFlow repository, and runs from its
scripts/ or from a container's scripts/ beside MinorFlow/. The fork's
scripts/create_all_CVA6_repo_jsons.py walks the whole checkout and calls this
one for the submodule.

    python3 scripts/create_all_MinorFlow_jsons.py        # the repository root
    python3 scripts/create_all_MinorFlow_jsons.py results/run
    python3 scripts/create_all_MinorFlow_jsons.py -j 8
    python3 scripts/create_all_MinorFlow_jsons.py --dry-run  # the plan only
    python3 scripts/create_all_MinorFlow_jsons.py --force    # redo the JSONs
    python3 scripts/create_all_MinorFlow_jsons.py --no-strict  # allow degraded
"""
import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))


def find_tracer():
    """(tracer, the folder it sits in). In this repository the tracer is one
    level above scripts/, and in a container scripts/ is the root with the
    viewer a folder below it."""
    above = os.path.dirname(HERE)
    for base in (above, os.path.join(above, "MinorFlow"),
                 os.path.join(os.curdir, "MinorFlow")):
        candidate = os.path.join(base, "MinorFlow_tracer.py")
        if os.path.isfile(candidate):
            return candidate, base
    return os.path.join(above, "MinorFlow_tracer.py"), above


TRACER, REPO_ROOT = find_tracer()

# Traces are read at a few tens of MiB/s each and a large one holds a lot of
# state, so this is deliberately below the core count. Each worker only waits
# on a subprocess, which is why these are threads rather than processes.
DEFAULT_WORKERS = 4

# The dot keeps foo_tracer_notes.txt and x_traceback.txt out of the batch.
TRACE_MARK = "_trace."
TRACE_END = ".txt"


# SHARED BEGIN py-human-size

# Needs: none


def human(size):
    """A size in bytes as whole B, or as KiB, MiB or GiB with one decimal."""
    if size < 1024:
        return f"{size:.0f} B"
    for unit in ("KiB", "MiB", "GiB"):
        size /= 1024
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"

# SHARED END py-human-size


def json_for(path):
    """daxpy_trace.config1.txt -> daxpy.config1.json, changing only the file
    name, so a folder whose name holds _trace stays as it is."""
    folder, name = os.path.split(path)
    at = name.rfind(TRACE_MARK)
    if at >= 0:
        name = name[:at] + "." + name[at + len(TRACE_MARK):]
    if name.endswith(TRACE_END):
        name = name[:-len(TRACE_END)]
    return os.path.join(folder, name + ".json")


def run_one(trace, out_json, quiet, strict):
    """Convert one trace, returning its outcome, ok, degraded or failed,
    and the line that reports it."""
    name = os.path.basename(out_json)
    cmd = [sys.executable, TRACER, trace, "-o", out_json]
    if quiet:
        cmd.append("--quiet")
    if strict:
        cmd.append("--strict")
    print(f"[INFO] Converting {os.path.basename(trace)} to {name}",
          flush=True)
    start = time.time()
    # Output is not captured, so with -j 1 the tracer's progress line shows a
    # trace that takes minutes is not hung, and its warnings reach the log.
    code = subprocess.run(cmd).returncode
    took = time.time() - start
    if code == 3:
        # The tracer's strict exit. The JSON was still written, so say what
        # happened rather than implying the conversion produced nothing.
        return "degraded", (f"[WARN] {name} written, but the trace is "
                            f"degraded (exit 3, see metadata.degraded).")
    if code != 0:
        return "failed", f"[ERROR] {name} failed with exit code {code}."
    return "ok", (f"[INFO] {name} written "
                  f"({human(os.path.getsize(out_json))}, {took:.0f}s)")


def main():
    parser = argparse.ArgumentParser(
        description="Run MinorFlow_tracer.py over every gem5 debug trace in "
                    "a folder.")
    parser.add_argument("folder", nargs="?", default=REPO_ROOT,
                        help="Folder holding the traces, not searched "
                             "recursively. Defaults to the folder holding "
                             "MinorFlow_tracer.py")
    parser.add_argument("-j", "--jobs", type=int, default=DEFAULT_WORKERS,
                        metavar="N",
                        help=f"Traces to convert at a time. Defaults to "
                             f"{DEFAULT_WORKERS}. Each holds a whole trace's "
                             f"state, so memory binds before cores do. With "
                             f"more than 1 the tracers run with --quiet, so "
                             f"their progress lines do not interleave")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print which traces would be converted to which "
                             "JSONs, and convert nothing")
    parser.add_argument("--force", action="store_true",
                        help="Convert a trace even when its JSON already "
                             "exists and is at least as new")
    parser.add_argument("--quiet", action="store_true",
                        help="Pass --quiet to the tracer, dropping its "
                             "progress line")
    parser.add_argument("--no-strict", action="store_true",
                        help="Do not pass --strict to the tracer. By default "
                             "a trace captured without one of the debug-flag "
                             "line families, or a truncated or empty trace, "
                             "counts as degraded, and the batch ends with "
                             "exit 3. The JSONs are written either way")
    args = parser.parse_args()

    if not os.path.isfile(TRACER):
        print(f"[ERROR] No {TRACER}. This script runs inside the MinorFlow "
              f"repository, beside its tracer.", file=sys.stderr)
        return 2
    if not os.path.isdir(args.folder):
        print(f"[ERROR] Folder not found: {args.folder}", file=sys.stderr)
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
    if args.dry_run:
        print(f"[INFO] Would convert {len(todo)} trace(s) from {folder}, "
              f"{args.jobs} at a time:")
        for trace, out_json in todo:
            print(f"[INFO]   {os.path.basename(trace)} "
                  f"({human(os.path.getsize(trace))}) -> "
                  f"{os.path.basename(out_json)}")
        return 0

    jobs = max(1, args.jobs)
    print(f"[INFO] Converting {len(todo)} trace(s) from {folder}, {jobs} at "
          f"a time\n")
    failed = degraded = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(run_one, trace, out_json,
                               args.quiet or jobs > 1, not args.no_strict)
                   for trace, out_json in todo]
        for future in as_completed(futures):
            outcome, line = future.result()
            failed += outcome == "failed"
            degraded += outcome == "degraded"
            print(line, file=sys.stderr if outcome == "failed"
                  else sys.stdout, flush=True)

    print(f"\n[INFO] {len(todo) - failed - degraded} of {len(todo)} "
          f"converted cleanly")
    if degraded:
        print(f"[WARN] {degraded} JSON(s) written but degraded. "
              f"metadata.degraded in each says what is missing.")
    # 3 is the tracer's own code for degraded, kept apart from 1 so a caller
    # can tell a run where nothing failed from one where something did.
    if failed:
        return 1
    return 3 if degraded else 0


if __name__ == "__main__":
    sys.exit(main())
