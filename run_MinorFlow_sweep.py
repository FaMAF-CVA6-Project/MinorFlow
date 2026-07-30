#!/usr/bin/env python3
"""
Run the MinorFlow configuration sweep.

Reads the TEST table out of gem5_config_MinorFlow.py and, for each entry,
sets TEST to it and runs only the workloads that entry was made for:

    #   4   fetch1LineWidth and snap 4 -> 16    workload: icache_pressure

An entry whose workload is 'all' runs every workload named anywhere in the
table, which is the set the sweep has been exercised with.

Outputs are collected as <test>_trace.config<N>.txt, <test>_clean.config<N>.txt
and <test>.config<N>.list so one configuration's results never overwrite another's.

Run this from the gem5 root, like run_gem5.py. The configuration file is
restored when the sweep ends, fails or is interrupted.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

# ==============================================================================
# CONFIGURATION
# ==============================================================================
DEFAULT_CONFIG = "gem5_config_MinorFlow.py"
DEFAULT_TESTS_DIR = "programs"
DEFAULT_OUT_DIR = "MinorFlow_sweep_results"

RUNNER_NAME = "run_gem5.py"

# What run_gem5.py needs from the gem5 root, used to check where we are.
GEM5_BIN = os.path.join("build", "RISCV", "gem5.opt")

# Where run_gem5.py has gem5 write, cleared after each collected run.
GEM5_OUT_DIR = "m5out"

# Workloads to use for 'all' when the table names none at all. Empty here
# because the MinorFlow table annotates every entry.
DEFAULT_ALL_TESTS = []

# Extensions tried when turning a workload name into a file, in this order.
EXT_PRIORITY = [".c", ".S", ".s", ".asm", ".sx"]

# These ids are authoritative: the comment table only annotates them.
ENTRY_RE = re.compile(r'^\s*(\d+):\s*\(\s*"([^"]*)"', re.M)

# '#   4   fetch1LineWidth and snap 4 -> 16    workload: icache_pressure'
ROW_RE = re.compile(r'^#\s+(\d+)\s+(\S.*)$')
CONTINUATION_RE = re.compile(r'^#\s{4,}(\S.*)$')

# 'TEST = 1'. Written so it cannot match the 'TESTS = {' table.
SELECTOR_RE = re.compile(r'^(TEST[ \t]*=[ \t]*)(\d+)([ \t]*(?:#.*)?)$', re.M)

SEP = "=" * 70


def find_beside_script(name, what, extra=()):
    """Locate a file next to this script, then in the cwd, then anywhere in
    extra."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, name), os.path.abspath(name)]
    candidates += [os.path.join(here, p, name) for p in extra]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    print(f"[ERROR] {what} ({name}) not found next to this script or in the "
          f"current directory.")
    sys.exit(2)


def parse_table(text):
    """Parse the TEST table into {id: (description, workloads)}.

    Ids and names come from the TESTS dict, workloads from the comment table
    above it, whose rows may wrap onto continuation lines."""
    entries = {int(n): name for n, name in ENTRY_RE.findall(text)}
    if not entries:
        return {}

    # Accumulate each comment row, including its continuation lines.
    comments = {}
    current = None
    for line in text.splitlines():
        row = ROW_RE.match(line)
        if row:
            current = int(row.group(1))
            comments[current] = row.group(2).strip()
            continue
        continuation = CONTINUATION_RE.match(line)
        if continuation and current is not None:
            comments[current] += " " + continuation.group(1).strip()
            continue
        if not line.startswith("#"):
            current = None

    table = {}
    for test_id, name in entries.items():
        comment = comments.get(test_id, "")
        workloads = []
        if "workload:" in comment:
            tail = comment.split("workload:", 1)[1]
            tail = re.sub(r"\(.*?\)", "", tail)
            workloads = [w.strip() for w in tail.split(",") if w.strip()]
        table[test_id] = (name, workloads)
    return table


def resolve_all(table):
    """The 'all' workload set: every workload the table names explicitly."""
    named = []
    for _, workloads in table.values():
        for workload in workloads:
            if workload.lower() != "all" and workload not in named:
                named.append(workload)
    return sorted(named) if named else list(DEFAULT_ALL_TESTS)


def suggest(name, tests_dir):
    """Files that look close to a workload name that did not resolve."""
    try:
        entries = sorted(os.listdir(tests_dir))
    except OSError:
        return []
    return [e for e in entries
            if os.path.splitext(e)[1] in EXT_PRIORITY
            and name.lower() in os.path.splitext(e)[0].lower()]


def resolve_test_file(name, tests_dir):
    """Turn a workload name into a path, trying the known extensions."""
    matches = [os.path.join(tests_dir, name + ext) for ext in EXT_PRIORITY
               if os.path.isfile(os.path.join(tests_dir, name + ext))]
    if not matches:
        return None
    if len(matches) > 1:
        print(f"[WARN] '{name}' matches more than one file: " +
              ", ".join(os.path.basename(m) for m in matches) +
              f". Using {os.path.basename(matches[0])}.")
    return matches[0]


def parse_config_selection(spec, table):
    """Parse '1,4-6' into a sorted list of configuration ids."""
    if not spec:
        return sorted(table)

    selected = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            low, _, high = chunk.partition("-")
            try:
                selected.update(range(int(low), int(high) + 1))
            except ValueError:
                print(f"[ERROR] Bad configuration range: '{chunk}'")
                sys.exit(2)
        else:
            try:
                selected.add(int(chunk))
            except ValueError:
                print(f"[ERROR] Bad configuration id: '{chunk}'")
                sys.exit(2)

    unknown = sorted(selected - set(table))
    if unknown:
        print(f"[ERROR] No such configuration(s): "
              f"{', '.join(str(u) for u in unknown)}. "
              f"The table has {min(table)}-{max(table)}.")
        sys.exit(2)
    return sorted(selected)


def build_plan(table, config_ids, tests_dir, override_tests):
    """Build [(config_id, description, [test paths])], warning about workloads
    with no matching file."""
    all_tests = override_tests if override_tests else resolve_all(table)
    plan = []
    missing = []

    for config_id in config_ids:
        description, workloads = table[config_id]

        if override_tests:
            names = list(override_tests)
        elif not workloads or any(w.lower() == "all" for w in workloads):
            names = list(all_tests)
        else:
            names = list(workloads)

        paths = []
        for name in names:
            path = resolve_test_file(name, tests_dir)
            if path:
                paths.append(path)
            elif name not in missing:
                missing.append(name)
        plan.append((config_id, description, paths))

    for name in missing:
        hints = suggest(name, tests_dir)
        print(f"[WARN] No file in {tests_dir} for the workload '{name}' "
              f"(looked for {name} plus {', '.join(EXT_PRIORITY)})." +
              (f" Did you mean {' or '.join(hints)}?" if hints else ""))

    # A configuration left with nothing to run would otherwise be skipped in
    # silence, which reads as 'swept' when it was not.
    empty = [config_id for config_id, _, paths in plan if not paths]
    if empty:
        print(f"[WARN] {len(empty)} configuration(s) have no runnable "
              f"workload and will be skipped: " +
              ", ".join(f"config{c}" for c in empty))
    if missing or empty:
        print()
    return plan


def select_config(text, config_id, config_path):
    """Write the configuration file with TEST set to config_id."""
    new_text, count = SELECTOR_RE.subn(
        lambda m: f"{m.group(1)}{config_id}{m.group(3)}", text, count=1)
    if count != 1:
        print(f"[ERROR] No 'TEST = <n>' line found in {config_path}, so the "
              f"configuration cannot be selected.")
        sys.exit(2)
    with open(config_path, "w") as f:
        f.write(new_text)


def driver_results_dir(runner):
    """The run_results/ folder run_gem5.py copies its keepers into."""
    return os.path.join(os.path.dirname(os.path.abspath(runner)), "run_results")


def output_paths(results_dir, test_name):
    """The three files run_gem5.py leaves in run_results/ for this test."""
    return {
        "trace": os.path.join(results_dir, f"{test_name}_trace.txt"),
        "clean": os.path.join(results_dir, f"{test_name}_clean.txt"),
        "list": os.path.join(results_dir, f"{test_name}.list"),
    }


def discard_run(results_dir):
    """Delete what the run left behind, once it has been collected.

    A debug trace runs to hundreds of megabytes, and a sweep produces one per
    run, so keeping m5out around would cost more disk than the whole sweep is
    worth. Everything of value is already in the out directory."""
    for path in (results_dir, GEM5_OUT_DIR):
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


def collect(results_dir, test_name, config_id, out_dir, want_trace):
    """Move this run's three files out under their .config<N> names."""
    produced = output_paths(results_dir, test_name)
    wanted = {
        "trace": f"{test_name}_trace.config{config_id}.txt",
        "clean": f"{test_name}_clean.config{config_id}.txt",
        "list": f"{test_name}.config{config_id}.list",
    }

    collected = 0
    for key, source in produced.items():
        if key == "trace" and not want_trace:
            continue
        if not os.path.isfile(source):
            print(f"[WARN] Expected output missing: {source}")
            continue
        try:
            shutil.move(source, os.path.join(out_dir, wanted[key]))
            collected += 1
        except OSError as e:
            print(f"[WARN] Could not collect {source}: {e}")

    if collected:
        print(f"[INFO] Collected {collected} file(s) into {out_dir} as "
              f"{test_name}*.config{config_id}.*")
    return collected


def clear_stale_outputs(results_dir, test_name):
    """Remove the previous run's files so nothing stale gets collected."""
    for path in output_paths(results_dir, test_name).values():
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass


def format_duration(seconds):
    minutes, secs = divmod(int(seconds), 60)
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{seconds:.1f}s"


def print_plan(plan, all_tests):
    print(f"[INFO] 'all' resolves to {len(all_tests)} workload(s): " +
          (", ".join(all_tests) if all_tests else "nothing") + "\n")
    total = 0
    for config_id, description, paths in plan:
        names = [os.path.basename(p) for p in paths]
        total += len(paths)
        print(f"  config{config_id:<3} {description}")
        print(f"      {len(names)} test(s): " +
              (", ".join(names) if names else "none"))
    print(f"\n[INFO] {len(plan)} configuration(s), {total} run(s) total.")
    return total


def print_summary(results, total_elapsed):
    print("\n" + SEP)
    print("SWEEP SUMMARY")
    print(SEP)
    print(f"{'CONFIG':>8} | {'TEST':<28} | {'STATUS':>10} | {'TIME':>9}")
    print(SEP)

    for config_id, name, code, elapsed in results:
        status = "OK" if code == 0 else f"FAILED ({code})"
        print(f"{('config' + str(config_id)):>8} | {name[:28]:<28} | "
              f"{status:>10} | {format_duration(elapsed):>9}")

    passed = sum(1 for _, _, code, _ in results if code == 0)
    failed = len(results) - passed

    print(SEP)
    print(f"{len(results)} run, {passed} passed, {failed} failed, "
          f"total {format_duration(total_elapsed)}")
    print(SEP + "\n")

    if failed:
        print("[WARN] Failed: " + ", ".join(
            f"config{c}/{n}" for c, n, code, _ in results if code != 0))
    return failed


def main():
    parser = argparse.ArgumentParser(
        description="Run the MinorFlow configuration sweep: each entry of the "
                    "TEST table, with the workloads it was made for.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Always sweeps {DEFAULT_CONFIG}, which it is written for. An "
               f"entry whose\nworkload is 'all' runs every workload named in "
               f"the table.\nRun this from the gem5 root, like run_gem5.py.")
    parser.add_argument("--config", default="",
                        help=f"Sweep a different configuration file. The "
                             f"sweep is written for {DEFAULT_CONFIG} and uses "
                             f"it unless this says otherwise, so only pass it "
                             f"for a copy or a variant of that file")
    parser.add_argument("--configs", default="",
                        help="Which configurations to run, e.g. '1,4-6'. "
                             "Defaults to all of them")
    parser.add_argument("--tests-dir", default=DEFAULT_TESTS_DIR,
                        help=f"Folder holding the workloads. Defaults to "
                             f"{DEFAULT_TESTS_DIR}/")
    parser.add_argument("--tests", default="",
                        help="Comma-separated workloads to run for every "
                             "configuration, instead of the ones the table "
                             "names")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                        help=f"Where to collect the results. Defaults to "
                             f"{DEFAULT_OUT_DIR}/")
    parser.add_argument("--no-trace", action="store_true",
                        help="Forwarded to run_gem5.py: no debug trace, "
                             "metrics only")
    parser.add_argument("--list", action="store_true",
                        help="Print the plan and exit, touching nothing")
    args = parser.parse_args()

    # Keep our output interleaved correctly with each run_gem5.py run.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    config_path = (os.path.abspath(args.config) if args.config
                   else find_beside_script(
                       DEFAULT_CONFIG, "Sweep config",
                       # In the repository the config lives with the viewer it
                       # belongs to, two levels up from here.
                       extra=[os.path.join("..", "..", "viewers",
                                           "MinorFlow")]))
    if not os.path.isfile(config_path):
        print(f"[ERROR] The configuration file '{config_path}' does not exist")
        sys.exit(2)

    with open(config_path) as f:
        config_text = f.read()

    table = parse_table(config_text)
    if not table:
        print(f"[ERROR] No TEST table found in {config_path}. Expected a "
              f"'TESTS = {{...}}' dict keyed by integer.")
        sys.exit(2)

    config_ids = parse_config_selection(args.configs, table)
    override_tests = [t.strip() for t in args.tests.split(",") if t.strip()]
    all_tests = override_tests if override_tests else resolve_all(table)

    print(SEP)
    print("MINORFLOW SWEEP")
    print(SEP)
    print(f"Config    : {config_path}")
    print(f"Tests dir : {os.path.abspath(args.tests_dir)}")
    print(f"Out dir   : {os.path.abspath(args.out_dir)}")
    print(f"Tracing   : "
          f"{'disabled (--no-trace)' if args.no_trace else 'enabled'}")
    print(SEP + "\n")

    if not os.path.isdir(args.tests_dir):
        print(f"[ERROR] The tests folder {args.tests_dir} does not exist")
        sys.exit(2)

    plan = build_plan(table, config_ids, args.tests_dir, override_tests)
    total_runs = print_plan(plan, all_tests)

    if args.list:
        print("[INFO] Listing only, nothing run.")
        return 0
    if not total_runs:
        print("[ERROR] Nothing to run.")
        sys.exit(2)

    # run_gem5.py resolves the gem5 root from the cwd, so this has to be run
    # from there. Say so now instead of failing later on a missing binary.
    if not os.path.isfile(GEM5_BIN):
        print(f"[ERROR] {GEM5_BIN} not found in {os.getcwd()}. "
              f"Run this from the gem5 root.")
        sys.exit(2)

    runner = find_beside_script(RUNNER_NAME, "Runner")
    results_dir = driver_results_dir(runner)
    os.makedirs(args.out_dir, exist_ok=True)

    print(f"\n[INFO] Backed up {config_path}, restored when the sweep ends.")

    results = []
    sweep_start = time.time()

    try:
        for config_id, description, paths in plan:
            if not paths:
                continue

            print("\n" + SEP)
            print(f"config{config_id}: {description}")
            print(SEP)
            select_config(config_text, config_id, config_path)
            print(f"[INFO] TEST = {config_id}\n")

            for index, path in enumerate(paths, 1):
                test_name = os.path.splitext(os.path.basename(path))[0]
                print("\n" + "-" * 70)
                print(f"[config{config_id}] [{index}/{len(paths)}] "
                      f"{os.path.basename(path)}")
                print("-" * 70 + "\n")

                clear_stale_outputs(results_dir, test_name)

                cmd = [sys.executable, runner, config_path, path]
                if args.no_trace:
                    cmd.append("--no-trace")

                start = time.time()
                code = subprocess.run(cmd).returncode
                elapsed = time.time() - start

                if code != 0:
                    # Leave the outputs in place: they are what there is
                    # to debug with.
                    print(f"\n[WARN] '{test_name}' failed with exit code "
                          f"{code}. Continuing with the rest.")
                else:
                    collect(results_dir, test_name, config_id,
                            args.out_dir, not args.no_trace)
                    discard_run(results_dir)
                results.append((config_id, test_name, code, elapsed))

    except KeyboardInterrupt:
        print("\n[WARN] Interrupted. Stopping the sweep.")
    finally:
        with open(config_path, "w") as f:
            f.write(config_text)
        print(f"\n[INFO] Restored {config_path}")

    failed = print_summary(results, time.time() - sweep_start)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
