#!/usr/bin/env python3
"""Measure the overhead profiles run_gem5.py subtracts to get NET.

A profile is what the harness around a measured region costs on its own:
the empty test_template of a suite, run on one build in one language, read
from the OFFICIAL column. This runs every template on the build and the
matched configuration of each variant, prints the profiles beside the ones
run_gem5.py carries, and with --write puts them into it.

Launch it from the gem5 root, where run_gem5.py is launched from:

  python3 scripts/measure_gem5_overhead.py                  # measure, compare
  python3 scripts/measure_gem5_overhead.py --variant patch  # one build only
  python3 scripts/measure_gem5_overhead.py --write          # and update
  python3 scripts/measure_gem5_overhead.py -n               # print the runs
"""
import argparse
import concurrent.futures
import importlib.util
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "run_gem5.py")

# The configuration each variant's profile is measured on: the matched one,
# the same the calibration runs, since a profile is in cycles of that core.
CONFIGS = {"stock": "gem5_config_CVA6.py",
           "patch": "gem5_config_CVA6_patch.py"}

# The template of each language, and where each suite's templates live, the
# container's layout first and then this repository's own.
TEMPLATES = {"c": "test_template.c", "asm": "test_template.S"}
SUITE_DIRS = ("benchmarks/config", "benchmarks/viewer", "benchmarks")
SUITE_MARKER = ".overhead_suite"

# The OFFICIAL row, in the order run_gem5.py prints it, and the profile keys.
KEYS = ("numCycles", "numInsts", "icache_miss", "dcache_miss",
        "icache_access", "dcache_access", "branch_pred", "branch_miss")
OFFICIAL = re.compile(r"^Clean result \(OFFICIAL\):\s*\[([^\]]*)\]", re.M)

# Where the runs write, beside the other results.
OUT_DIR = os.path.join("results", "overhead")


def suite_dirs():
    """{suite: folder} for every folder whose marker names a suite."""
    found = {}
    for rel in SUITE_DIRS:
        marker = os.path.join(rel, SUITE_MARKER)
        if os.path.isfile(marker):
            with open(marker) as handle:
                found.setdefault(handle.read().strip(), rel)
    return found


def load_runner():
    spec = importlib.util.spec_from_file_location("run_gem5", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def measure(job, dry_run):
    """Run one template and return (job, profile or an error message)."""
    suite, variant, lang, template = job
    label = f"{suite}_{variant}_{lang}"
    cmd = [sys.executable, RUNNER, CONFIGS[variant], template,
           "--variant", variant, "--lang", lang, "--no-trace",
           "--gem5-out-dir", os.path.join(OUT_DIR, "m5out", label),
           "--results-dir", os.path.join(OUT_DIR, "run", label)]
    if dry_run:
        return job, "  $ " + " ".join(cmd)
    done = subprocess.run(cmd, capture_output=True, text=True)
    match = OFFICIAL.search(done.stdout)
    if done.returncode != 0 or not match:
        tail = (done.stdout + done.stderr).strip().splitlines()[-3:]
        return job, "run failed: " + " | ".join(tail)
    values = [float(v) for v in match.group(1).split(",")[:len(KEYS)]]
    return job, dict(zip(KEYS, (int(v) for v in values)))


def render(table):
    """OVERHEAD_SUITES as run_gem5.py writes it."""
    out = ["OVERHEAD_SUITES = {"]
    for suite in table:
        out.append(f'    "{suite}": {{')
        for variant in table[suite]:
            out.append(f'        "{variant}": {{')
            for lang in table[suite][variant]:
                out.append(f'            "{lang}": {{')
                for key, value in table[suite][variant][lang].items():
                    pad = " " * (17 - len(key))
                    out.append(f'                "{key}":{pad}{value},')
                out.append("            },")
            out.append("        },")
        out.append("    },")
    out.append("}")
    return "\n".join(out)


def write_table(table):
    """Replace OVERHEAD_SUITES in run_gem5.py, and nothing else."""
    with open(RUNNER) as handle:
        text = handle.read()
    start = text.index("OVERHEAD_SUITES = {")
    end = text.index("\n}\n", start) + 2
    with open(RUNNER, "w") as handle:
        handle.write(text[:start] + render(table) + text[end:])


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Measure run_gem5.py's overhead profiles from the empty "
                    "templates.",
        epilog="Each profile runs the suite's empty template on the variant's "
               "build and its matched\nconfiguration, "
               + " and ".join(f"{v}: {c}" for v, c in CONFIGS.items()) + ".")
    parser.add_argument("--suite", choices=["config", "viewer", "all"],
                        default="all", help="Which suite. Defaults to both")
    parser.add_argument("--variant", choices=["stock", "patch", "all"],
                        default="all", help="Which build. Defaults to both")
    parser.add_argument("-j", "--jobs", type=int, default=4, metavar="N",
                        help="Runs at once. Defaults to 4")
    parser.add_argument("--write", action="store_true",
                        help="Put the measured profiles into run_gem5.py "
                             "beside this script")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Print the runs without starting them")
    args = parser.parse_args()

    if not os.path.isfile(RUNNER):
        print(f"[ERROR] {RUNNER} not found. This script sits beside "
              f"run_gem5.py.")
        return 2
    dirs = suite_dirs()
    suites = ["config", "viewer"] if args.suite == "all" else [args.suite]
    variants = ["stock", "patch"] if args.variant == "all" else [args.variant]
    jobs = []
    for suite in suites:
        if suite not in dirs:
            print(f"[ERROR] No folder under {', '.join(SUITE_DIRS)} has a "
                  f"{SUITE_MARKER} naming '{suite}'. Run this from the gem5 "
                  f"root.")
            return 2
        for variant in variants:
            for lang, name in TEMPLATES.items():
                jobs.append((suite, variant, lang,
                             os.path.join(dirs[suite], name)))

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max(1, args.jobs)) as pool:
        for job, result in pool.map(lambda j: measure(j, args.dry_run), jobs):
            if args.dry_run:
                print(result)
                continue
            if isinstance(result, str):
                print(f"[ERROR] {'/'.join(job[:3])}: {result}")
                return 1
            results[job[:3]] = result
            print(f"[INFO] {'/'.join(job[:3])} measured")
    if args.dry_run:
        return 0

    current = load_runner().OVERHEAD_SUITES
    table = {s: {v: dict(current[s][v]) for v in current[s]} for s in current}
    changed = 0
    for (suite, variant, lang), profile in sorted(results.items()):
        old = current.get(suite, {}).get(variant, {}).get(lang, {})
        diffs = [f"{k} {old.get(k)} -> {v}" for k, v in profile.items()
                 if old.get(k) != v]
        changed += bool(diffs)
        print(f"  {suite:6} {variant:5} {lang:3}  "
              + (", ".join(diffs) if diffs else "as run_gem5.py has it"))
        table.setdefault(suite, {}).setdefault(variant, {})[lang] = profile
    print(f"[INFO] {changed} of {len(results)} profile(s) differ from "
          f"run_gem5.py")
    if args.write and changed:
        write_table(table)
        print(f"[INFO] Wrote the profiles into {RUNNER}")
    elif changed:
        print("[INFO] --write puts them into run_gem5.py, or paste this:\n")
        print(render(table))
    return 0


if __name__ == "__main__":
    sys.exit(main())
