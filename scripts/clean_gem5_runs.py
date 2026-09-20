#!/usr/bin/env python3
"""Remove everything the gem5 run scripts generate, which is all of
results/: m5out/, run/, batch/, the sweeps and parity/. Only the fixed names
below are removed: those folders under each search root, and every
__pycache__ below a root, which a container collects from scons as well.

Launch it from the gem5 root, where run_gem5.py is launched from:

  python3 scripts/clean_gem5_runs.py              # list, then ask
  python3 scripts/clean_gem5_runs.py -y           # delete without asking
  python3 scripts/clean_gem5_runs.py --dry-run    # list only
  python3 scripts/clean_gem5_runs.py my_results   # plus a folder named by hand
"""
import argparse
import os
import shutil
import sys

# Every folder a run writes, under results/ at the top of each search root,
# which is where the runners put them. The last two come from the CVA6 fork's
# own scripts, which share a gem5 tree with these.
ROOT_DIRS = {
    "results/m5out":             "run_gem5.py: gem5's output, stats, binary",
    "results/run":               "run_gem5.py: the files worth keeping",
    "results/batch":             "run_all_gem5_benchmarks.py",
    "results/sweep_MinorFlow":   "run_MinorFlow_sweep.py",
    "results/sweep_gem5_config": "run_gem5_config_sweep.py",
    "results/parity":            "check_patch_parity.py",
    "results/overhead":          "measure_gem5_overhead.py",
}

# Deleted wherever they appear under a search root. A container collects
# these under every folder it runs a script from, not only beside the runners.
ANY_DEPTH_DIRS = {
    "__pycache__": "left behind by Python",
}

# Never descended into. These cannot hold a generated folder, and build/ holds
# the gem5.opt binary the runners call, so walking it is pure cost.
PRUNE_DIRS = {".git", "build", "vendor", "node_modules", "install"}


# SHARED BEGIN py-repo-root

# Needs: os


def repo_root():
    """The nearest folder above this script holding a .git, so a moved tree
    needs no parent count fixed. Without one, as in a release archive, the
    parent of the script's folder, which is the documented layout."""
    here = os.path.dirname(os.path.abspath(__file__))
    path = here
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return os.path.dirname(here)
        path = parent

# SHARED END py-repo-root


REPO_ROOT = repo_root()


def search_roots():
    """The working directory, normally the gem5 root, and this repository.
    The runners write results/ under the directory they are launched from,
    which can be either."""
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
    """Collect every generated folder under the roots, plus any named by
    hand. A match is never descended into. It is about to be deleted whole,
    so its contents cannot add anything."""
    found = []
    seen = set()

    def add(path, reason):
        """Take a folder once, and say whether path is a folder at all."""
        if not os.path.isdir(path):
            return False
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            found.append((path, reason))
        return True

    for path in extra:
        if not add(path, "named on the command line"):
            print(f"[WARN] Not a folder, ignored: {path}")

    for root in roots:
        for name, reason in ROOT_DIRS.items():
            add(os.path.join(root, name), reason)

        for dirpath, dirnames, _ in os.walk(root):
            keep = []
            for name in dirnames:
                full = os.path.join(dirpath, name)
                if os.path.realpath(full) in seen:
                    continue
                if name in ANY_DEPTH_DIRS:
                    add(full, ANY_DEPTH_DIRS[name])
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


def main():
    parser = argparse.ArgumentParser(
        description="Delete the folders the gem5 run scripts generate.")
    parser.add_argument("extra", nargs="*",
                        help="Extra folders to delete, for the output of a "
                             "run given --out-dir or --gem5-out-dir")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Delete without asking for confirmation")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be deleted and stop")
    args = parser.parse_args()

    roots = search_roots()
    if not roots:
        print("[ERROR] No usable search root", file=sys.stderr)
        return 1

    print("[INFO] Searching in: " + ", ".join(os.path.abspath(r)
                                              for r in roots))
    targets = find_targets(roots, args.extra)

    if not targets:
        print("[INFO] Nothing to clean")
        return 0

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
        return 0

    if not args.yes:
        try:
            reply = input("Delete these? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[INFO] Cancelled")
            return 0
        if reply not in ("y", "yes"):
            print("[INFO] Cancelled")
            return 0

    deleted = 0
    for path, _ in targets:
        try:
            shutil.rmtree(path)
            deleted += 1
        except OSError as e:
            print(f"[ERROR] Could not delete {path}: {e}", file=sys.stderr)

    print(f"[INFO] Deleted {deleted} of {len(targets)} folder(s), "
          f"{human(total)} freed")
    return 0 if deleted == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
