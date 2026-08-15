#!/usr/bin/env python3
"""Remove everything the gem5 run scripts generate.

Covers the folders the runners write into the directory they are launched from
(m5out/, batch_results/, the two sweep result folders) and the run_results/
each runner leaves next to itself.

Only the fixed names listed below are ever removed, so a tests/ folder or
anything else tracked in git cannot be caught by mistake. The folders shared
by name with the Verilator flow are taken only when a gem5 runner sits beside
them, so this never sweeps up a Verilator run's results.

Launch it from the gem5 root, the same place run_gem5.py is launched from, so
that the folders gem5 wrote there are found:

  python3 clean_gem5.py             # list, then ask
  python3 clean_gem5.py -y          # delete without asking
  python3 clean_gem5.py --dry-run   # list only
  python3 clean_gem5.py m5out_daxpy # plus a --gem5-out-dir run
"""
import os
import sys
import shutil
import argparse

# Folders the runners create in the directory they are launched from. Matched
# only at the top of each search root, which is where they land.
ROOT_DIRS = {
    "m5out":                      "run_gem5.py: gem5's output, stats and binary",
    "batch_results":              "run_all_gem5_benchmarks.py",
    "CVA6_testing_sweep_results": "run_CVA6_testing_sweep.py",
    "mem_tests_results":          "run_memory_latency_sweep.py",
}

# Folders that appear beside a runner script. Matched at any depth, but only
# when one of the gem5 runners sits in the same folder.
SIBLING_DIRS = {
    "run_results": "run_gem5.py: the files worth keeping",
    "__pycache__": "left behind by python",
}

RUNNERS = {"run_gem5.py", "run_all_gem5_benchmarks.py",
           "run_CVA6_testing_sweep.py", "run_memory_latency_sweep.py"}

# Never descended into. These cannot hold a generated folder, and build/ holds
# the gem5.opt binary the runners call, so walking it is pure cost.
PRUNE_DIRS = {".git", "build", "vendor", "node_modules", "install", "work-ver"}

# This repository, two levels up from benchmarks/gem5/.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def search_roots():
    """The working directory (the gem5 root) plus this repository.

    A run leaves folders in both: gem5 writes m5out/ where it was launched,
    while run_results/ lands next to the runner script."""
    roots = []
    seen = set()
    for root in (os.getcwd(), REPO_ROOT):
        real = os.path.realpath(root)
        # Refuse to walk from a place where a stray match would be a disaster.
        if real in ("/", os.path.expanduser("~")):
            print(f"[WARN] Skipping the search root {real}: too broad. "
                  f"Run this from the gem5 root instead.")
            continue
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            roots.append(root)
    return roots


def find_targets(roots, extra):
    """Collect every generated folder under the roots, plus any named by hand.

    A match is never descended into: it is about to be deleted whole, so its
    contents cannot add anything."""
    found = []
    seen = set()

    def add(path, reason):
        real = os.path.realpath(path)
        if real not in seen and os.path.isdir(path):
            seen.add(real)
            found.append((path, reason))

    for path in extra:
        if os.path.isdir(path):
            add(path, "named on the command line")
        else:
            print(f"[WARN] Not a folder, ignored: {path}")

    for root in roots:
        for name, reason in ROOT_DIRS.items():
            add(os.path.join(root, name), reason)

        for dirpath, dirnames, filenames in os.walk(root):
            beside_runner = RUNNERS.intersection(filenames)
            keep = []
            for name in dirnames:
                full = os.path.join(dirpath, name)
                if os.path.realpath(full) in seen:
                    continue          # already taken, and taken whole
                if name in SIBLING_DIRS and beside_runner:
                    add(full, SIBLING_DIRS[name])
                elif name not in PRUNE_DIRS and not name.startswith("."):
                    keep.append(name)
            dirnames[:] = keep

    return sorted(found)


def folder_size(path):
    """Bytes held under path. Broken links and races are counted as zero."""
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def human(size):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


def main():
    parser = argparse.ArgumentParser(
        description="Delete the folders the gem5 run scripts generate.")
    parser.add_argument("extra", nargs="*",
                        help="Extra folders to delete, for runs made with a "
                             "custom --gem5-out-dir")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Delete without asking for confirmation")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="List what would be deleted and stop")
    args = parser.parse_args()

    roots = search_roots()
    if not roots:
        print("[ERROR] No usable search root")
        sys.exit(1)

    print("[INFO] Searching in: " + ", ".join(os.path.abspath(r)
                                              for r in roots))
    targets = find_targets(roots, args.extra)

    if not targets:
        print("[INFO] Nothing to clean")
        return

    print("\n" + "=" * 70)
    print("TO DELETE")
    print("=" * 70)
    total = 0
    for path, reason in targets:
        size = folder_size(path)
        total += size
        print(f"{human(size):>10}  {os.path.abspath(path)}")
        print(f"{'':>10}  ({reason})")
    print("=" * 70)
    print(f"{len(targets)} folder(s), {human(total)}\n")

    if args.dry_run:
        print("[INFO] Dry run, nothing was deleted")
        return

    if not args.yes:
        try:
            reply = input("Delete these? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[INFO] Cancelled")
            return
        if reply not in ("y", "yes"):
            print("[INFO] Cancelled")
            return

    deleted = 0
    for path, _ in targets:
        try:
            shutil.rmtree(path)
            deleted += 1
        except OSError as e:
            print(f"[WARN] Could not delete {path}: {e}")

    print(f"[INFO] Deleted {deleted} of {len(targets)} folder(s), "
          f"{human(total)} freed")


if __name__ == "__main__":
    main()
