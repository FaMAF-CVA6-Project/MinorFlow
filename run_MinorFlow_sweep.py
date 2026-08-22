#!/usr/bin/env python3
"""Run the MinorFlow configuration sweep. Each TEST entry runs against the
workloads it names, outputs carry a .config<N> tag, and every metrics table is
gathered into one metrics.txt. Run it from the gem5 root.
"""
import argparse
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

# ==============================================================================
# CONFIGURATION
# ==============================================================================
DEFAULT_CONFIG = "gem5_config_MinorFlow.py"
DEFAULT_TESTS_DIR = "MinorFlow_benchmarks"
DEFAULT_OUT_DIR = "MinorFlow_sweep_results"

RUNNER_NAME = "run_gem5.py"

# What run_gem5.py needs from the gem5 root, used to check where we are.
GEM5_BIN = os.path.join("build", "RISCV", "gem5.opt")

# Where run_gem5.py has gem5 write, cleared after each collected run.
GEM5_OUT_DIR = "m5out"

# Runs to keep in flight at once. Deliberately below the core count:
# each holds a gem5 process and writes a trace, so memory and disk
# bind before cores do.
DEFAULT_JOBS = min(4, os.cpu_count() or 1)

# How a collected file and a config copy are labelled.
LABEL = "config"
SELECTOR_NAME = "TEST"
UNIT = "configuration"

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

# What the runner writes above its metrics table, and where the sweep gathers
# every one of those tables once the runs are done.
METRICS_MARKER = "RESULTS TABLE"
METRICS_FILE = "metrics.txt"


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


def split_own_args(argv):
    """Split the command line into this script's arguments and the
    configuration's. Everything after a '--' is the configuration's, verbatim,
    which is what a flag taking a value or colliding with ours needs."""
    if "--" in argv:
        cut = argv.index("--")
        return argv[:cut], argv[cut + 1:]
    return argv, []


def parse_table(text):
    """Parse the TEST table into {id: (description, workloads)}. Ids and names
    come from the TESTS dict, workloads from the comment table above it, whose
    rows may wrap onto continuation lines."""
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
    # Compare on the stem, so a workload typed with an extension or as a
    # path still finds its neighbours.
    stem = os.path.splitext(os.path.basename(name))[0].lower()
    return [e for e in entries
            if os.path.splitext(e)[1] in EXT_PRIORITY
            and stem in os.path.splitext(e)[0].lower()]


def resolve_test_file(name, tests_dir):
    """Turn a workload into a path. The table writes a bare name, but a name
    with its extension and a path to a file are what a person types on
    --tests, so all three resolve rather than only the first."""
    # A path, absolute or relative to the working directory, taken as given.
    if os.path.isfile(name):
        return name

    stem, ext = os.path.splitext(name)
    if ext in EXT_PRIORITY:
        # A name that already carries its extension, inside the tests folder.
        candidate = os.path.join(tests_dir, name)
        if os.path.isfile(candidate):
            return candidate
        # Fall through on the stem: the same workload under a different
        # extension is a likelier intent than no match at all.
        name = stem

    matches = [os.path.join(tests_dir, name + e) for e in EXT_PRIORITY
               if os.path.isfile(os.path.join(tests_dir, name + e))]
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


def driver_results_dir(runner):
    """The run_results/ folder run_gem5.py copies its keepers into."""
    return os.path.join(os.path.dirname(os.path.abspath(runner)), "run_results")


def job_dirs(runner, label):
    """The private folders one run works in. Each job gets its own, so
    concurrent runs cannot overwrite each other's stats.txt, trace or
    binary."""
    return (os.path.join(GEM5_OUT_DIR, label),
            os.path.join(driver_results_dir(runner), label))


def write_config_copy(text, config_id, dest_dir, base_name):
    """Write a copy of the configuration with the selector set to config_id.
    The sweep runs copies rather than editing in place, so the file is never
    modified, nothing needs restoring, and runs can go in parallel."""
    new_text, count = SELECTOR_RE.subn(
        lambda m: f"{m.group(1)}{config_id}{m.group(3)}", text, count=1)
    if count != 1:
        print(f"[ERROR] No '{SELECTOR_NAME} = <n>' line found in the "
              f"configuration, so the {UNIT} cannot be selected.")
        sys.exit(2)
    path = os.path.join(dest_dir, f"{LABEL}{config_id}_{base_name}")
    with open(path, "w") as f:
        f.write(new_text)
    return path


def collect(job_results, config_id, out_dir, want_trace):
    """Move a finished run's four files out under their .LABEL<N> names."""
    collected = []
    try:
        produced = sorted(os.listdir(job_results))
    except OSError:
        produced = []

    for name in produced:
        if "_trace." in name and not want_trace:
            continue
        stem, ext = os.path.splitext(name)
        try:
            shutil.move(os.path.join(job_results, name),
                        os.path.join(out_dir,
                                     f"{stem}.{LABEL}{config_id}{ext}"))
            collected.append(name)
        except OSError as e:
            print(f"[WARN] Could not collect {name}: {e}")

    if not collected:
        print(f"[WARN] Nothing to collect from {job_results}")
    return collected


def keep_failed_config(config_copy, job_gem5_out):
    """Save the configuration a failed run used, beside that run's output. The
    copies live in a temporary folder deleted when the sweep ends, so the
    traceback would otherwise name a path that no longer exists."""
    try:
        os.makedirs(job_gem5_out, exist_ok=True)
        dest = os.path.join(job_gem5_out, os.path.basename(config_copy))
        shutil.copy2(config_copy, dest)
        return dest
    except OSError as e:
        print(f"[WARN] Could not keep the configuration copy: {e}")
        return None


def discard_run(job_gem5_out, job_results):
    """Delete a run's working folders once it has been collected. A debug
    trace runs to hundreds of megabytes per run. Only this run's folders go, so
    a failed run's output survives the sweep."""
    for path in (job_gem5_out, job_results):
        shutil.rmtree(path, ignore_errors=True)


def prune_empty(path):
    """Remove a folder the sweep has emptied, leaving anything else alone.
    Deliberately not a recursive delete, a plain run_gem5.py run writes
    straight into these folders and that output is not the sweep's."""
    try:
        if os.path.isdir(path) and not os.listdir(path):
            os.rmdir(path)
    except OSError:
        pass


def extract_metrics(report_path):
    """The metrics section of a _report.txt, or None if it holds none. The
    file is the measured disassembly then the metrics table, so everything from
    the rule above the table's title to the end is what is wanted."""
    try:
        with open(report_path) as f:
            lines = f.read().splitlines()
    except OSError as e:
        print(f"[WARN] Could not read {report_path}: {e}")
        return None

    for i, line in enumerate(lines):
        if line.startswith(METRICS_MARKER):
            # Take the rule above the title too, so the block arrives boxed.
            start = i - 1 if i and set(lines[i - 1]) == {"="} else i
            return "\n".join(lines[start:]).rstrip()

    return None


def write_metrics_file(out_dir, entries, info):
    """Gather every run's metrics table into one metrics.txt. entries is
    [(label, report file)] in plan order, so the file reads like the summary
    above it. A run with no table is named, not skipped."""
    blocks, missing = [], []
    for label, report_path in entries:
        block = extract_metrics(report_path)
        if block is None:
            missing.append(label)
            continue
        blocks.append(f">>> {label}\n{block}")

    if missing:
        print(f"[WARN] No metrics table for: {', '.join(missing)}")
    if not blocks:
        print(f"[WARN] No metrics tables found, so no {METRICS_FILE} written")
        return None

    path = os.path.join(out_dir, METRICS_FILE)
    try:
        with open(path, "w") as f:
            f.write(f"{SEP}\nALL METRICS\n{SEP}\n")
            for line in info:
                f.write(line + "\n")
            f.write(f"{SEP}\n\n")
            f.write("\n\n".join(blocks) + "\n")
    except OSError as e:
        print(f"[WARN] Could not write {path}: {e}")
        return None

    print(f"[INFO] {len(blocks)} metrics table(s) gathered in {path}")
    return path


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
               f"the table.\nRun this from the gem5 root, like run_gem5.py.\n"
               f"\n"
               f"Any flag this script does not define is passed on to the "
               f"configuration\nbeing swept, through run_gem5.py and the same "
               f"for every run of the\nsweep. Put them after a '--' when a "
               f"flag takes a value or shares a\nname with one of ours.")
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
                             "names. A bare name, a file name with its "
                             "extension, or a path all work")
    parser.add_argument("-j", "--jobs", type=int, default=DEFAULT_JOBS,
                        help=f"How many runs to keep in flight. Defaults "
                             f"to {DEFAULT_JOBS} here. gem5 is single-"
                             f"threaded, so this scales with cores until "
                             f"memory or disk bandwidth binds. 1 runs "
                             f"them one by one and streams the output "
                             f"live")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                        help=f"Where to collect the results. Defaults to "
                             f"{DEFAULT_OUT_DIR}/")
    parser.add_argument("--no-trace", action="store_true",
                        help="Forwarded to run_gem5.py: no debug trace, "
                             "metrics only")
    parser.add_argument("--list", action="store_true",
                        help="Print the plan and exit, touching nothing")
    own_argv, after_separator = split_own_args(sys.argv[1:])
    args, unrecognised = parser.parse_known_args(own_argv)

    # A bare word among the leftovers is nearly always a mistyped option, and
    # a flag that takes a value has to go after the '--' anyway. Refuse it
    # rather than have every run in the sweep fail the same way inside gem5.
    stray = [a for a in unrecognised if not a.startswith("-")]
    if stray:
        print(f"[ERROR] Unrecognised argument(s): {' '.join(stray)}. "
              f"Flags for the configuration are passed straight through, "
              f"anything that takes a value goes after a '--'.")
        sys.exit(2)
    config_args = unrecognised + after_separator

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
    print(f"Jobs      : {max(1, args.jobs)}")
    print(f"Tracing   : "
          f"{'disabled (--no-trace)' if args.no_trace else 'enabled'}")
    if config_args:
        print(f"Cfg flags : {' '.join(config_args)}")
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
    config_dir = tempfile.mkdtemp(prefix="sweep_configs_")
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    jobs = max(1, args.jobs)

    base_name = os.path.basename(config_path)
    print(f"\n[INFO] Sweeping copies of {base_name}. The file itself is "
          f"never modified.")

    # One lock around the reporting, so a finished run's output arrives as one
    # block instead of interleaved with another's.
    report_lock = threading.Lock()
    results = []
    sweep_start = time.time()

    def run_one(index, total, config_id, config_copy, path):
        test_name = os.path.splitext(os.path.basename(path))[0]
        label = f"{LABEL}{config_id}_{test_name}"
        job_gem5_out, job_results = job_dirs(runner, label)
        # Anything left from an earlier sweep would otherwise be collected.
        discard_run(job_gem5_out, job_results)

        cmd = [sys.executable, runner, config_copy, path,
               "--gem5-out-dir", job_gem5_out,
               "--results-dir", job_results]
        if args.no_trace:
            cmd.append("--no-trace")
        # After a '--', so run_gem5.py hands them to the configuration whatever
        # they are named.
        if config_args:
            cmd.extend(["--"] + config_args)

        start = time.time()
        if jobs == 1:
            print("\n" + SEP)
            print(f"[{index}/{total}] {LABEL}{config_id}: {test_name}")
            print(SEP + "\n")
            code = subprocess.run(cmd).returncode
            output = None
        else:
            done = subprocess.run(cmd, capture_output=True, text=True)
            code, output = done.returncode, done.stdout + done.stderr
        elapsed = time.time() - start

        with report_lock:
            if output is not None:
                print("\n" + SEP)
                print(f"[{index}/{total}] {LABEL}{config_id}: {test_name}")
                print(SEP + "\n")
                print(output, end="" if output.endswith("\n") else "\n")
            if code != 0:
                # Leave the outputs in place: they are what there is to
                # debug with. run_gem5.py writes the whole of gem5's stdout
                # and stderr into that folder as <test>_error.log.
                kept = keep_failed_config(config_copy, job_gem5_out)
                print(f"[WARN] '{test_name}' failed with exit code {code}. "
                      f"Its output is left in {job_gem5_out}. Continuing.")
                if kept:
                    print(f"[WARN] The configuration it ran is kept as "
                          f"{kept}")
            else:
                collected = collect(job_results, config_id, out_dir,
                                    not args.no_trace)
                if collected:
                    print(f"[INFO] Collected {len(collected)} file(s) into "
                          f"{out_dir} as *.{LABEL}{config_id}.*")
                discard_run(job_gem5_out, job_results)
            results.append((index, config_id, test_name, code, elapsed))

    try:
        # One copy of the configuration per id, each with its selector set.
        queue = []
        for entry in plan:
            config_id, paths = entry[0], entry[-1]
            if not paths:
                continue
            config_copy = write_config_copy(config_text, config_id,
                                            config_dir, base_name)
            for path in paths:
                queue.append((config_id, config_copy, path))

        total = len(queue)
        if jobs > 1:
            print(f"[INFO] Running {jobs} at a time.\n")
        if jobs == 1:
            for index, (cid, copy, path) in enumerate(queue, 1):
                run_one(index, total, cid, copy, path)
        else:
            with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
                for future in [pool.submit(run_one, i, total, cid, copy, path)
                               for i, (cid, copy, path)
                               in enumerate(queue, 1)]:
                    future.result()
    except KeyboardInterrupt:
        print("\n[WARN] Interrupted. Stopping the sweep.")
    finally:
        shutil.rmtree(config_dir, ignore_errors=True)

    # Report in plan order, not the order the runs finished.
    ordered = [(cid, name, code, elapsed)
               for _, cid, name, code, elapsed in sorted(results)]
    failed = print_summary(ordered, time.time() - sweep_start)

    # Only a run that passed left a table behind to gather.
    write_metrics_file(
        out_dir,
        [(f"{LABEL}{cid} / {name}",
          os.path.join(out_dir, f"{name}_report.{LABEL}{cid}.txt"))
         for cid, name, code, _ in ordered if code == 0],
        [f"Config   : {base_name}",
         f"Cfg flags: {' '.join(config_args) if config_args else '(none)'}",
         f"Tests dir: {os.path.abspath(args.tests_dir)}",
         f"Runs     : {len(ordered)}, {len(ordered) - failed} passed"])

    print(f"[INFO] Results in {out_dir}")
    if failed:
        print(f"[INFO] The failed run(s) left their output under "
              f"{os.path.abspath(GEM5_OUT_DIR)}")
    # Whatever the sweep emptied goes, anything a plain run_gem5.py run left
    # in there stays.
    prune_empty(driver_results_dir(runner))
    prune_empty(GEM5_OUT_DIR)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
