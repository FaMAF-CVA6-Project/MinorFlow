#!/usr/bin/env python3
"""
Run a gem5 RISC-V simulation and consolidate the metrics.
Accepts both C (.c) and assembly (.S/.s/.asm) programs. The input type is
detected from the extension and can be forced with --lang.
"""
import sys
import os
import shutil
import subprocess
import re
import argparse

# ==============================================================================
# GLOBAL CONFIGURATION
# ==============================================================================
GEM5_ROOT = os.getcwd()

# Where gem5 writes: stats.txt, the debug trace, the disassembly. This is
# gem5's own default output folder.
GEM5_OUT_DIR = "m5out"

# Folder next to this script where each run leaves a copy of the files worth
# keeping. The originals stay in GEM5_OUT_DIR.
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "run_results")
GCC_CMD = "riscv64-unknown-elf-gcc"
OBJDUMP_CMD = "riscv64-unknown-elf-objdump"
# The build to run, by directory name under build/. --build picks another,
# so any build in the tree can be used without editing this.
DEFAULT_BUILD = "RISCV"

# Tried in order inside a build directory, so a .fast build is picked up too.
GEM5_BINARY_NAMES = ("gem5.opt", "gem5.fast", "gem5.debug")

# A SimObject only the CVA6 patch adds. The overhead profile below was measured
# on a stock build, so a patched one is worth saying out loud.
PATCH_MARKER = b"Axi2MemPort"
M5_INCLUDE = os.path.join(GEM5_ROOT, "include")
M5_OP_ASM = os.path.join(GEM5_ROOT, "util/m5/src/abi/riscv/m5op.S")

# Compile flags common to both paths.
COMMON_CFLAGS = [
    "-static",
    "-mcmodel=medany",
    "-fvisibility=hidden",
    "-nostdlib",
    "-nostartfiles",
    "-lgcc",
    "-march=rv64gc_zba_zbb_zbs_zbc_zbkb_zbkx_zkne_zknd_zknh",
    "-mabi=lp64d",
]
# The C path adds these. -e main fixes the entry point (there is no crt0).
C_EXTRA_CFLAGS = ["-fno-builtin", "-e", "main"]

# Extensions recognised per input type.
C_EXTS = {".c"}
ASM_EXTS = {".s", ".asm", ".sx"}   # .S is handled separately (case-sensitive)

# The _report.txt holds two sections: the measured region of the disassembly,
# then the metrics table.
RULE = "=" * 70
METRICS_MARKER = "RESULTS TABLE"
CODE_BANNER = [RULE, "DISASSEMBLED CODE", RULE]
CODE_END_BANNER = [RULE, "END OF DISASSEMBLED CODE", RULE]

# Lines of each captured stream echoed when a step fails. The whole of both
# goes to the log either way. This is only what the terminal is worth.
ERROR_TAIL_LINES = 40

# ==============================================================================
# OVERHEAD PROFILES (Reference Core no patch, measured on MinorFlow benchmarks)
# ==============================================================================
OVERHEAD_PROFILES = {
    "c": {
        "numCycles":        20,
        "numInsts":         5,
        "icache_miss":      0,
        "dcache_miss":      0,
        "icache_access":    10,
        "dcache_access":    0,
        "branch_pred":      3,
        "branch_miss":      2,
    },
    "asm": {
        "numCycles":        37,
        "numInsts":         5,
        "icache_miss":      1,
        "dcache_miss":      0,
        "icache_access":    10,
        "dcache_access":    0,
        "branch_pred":      4,
        "branch_miss":      2,
    },
}

# ==============================================================================
# METRICS MAP
# ==============================================================================
METRICS_MAP = {
    "numCycles":         r"cores\.core\.numCycles",
    "numInsts":          r"cores\.core\.commitStats0\.numInsts\s",
    "icache_miss":       r"l1icaches\.overallMisses::total",
    "dcache_miss":       r"l1dcaches\.overallMshrMisses::total",
    "icache_access":     r"l1icaches\.overallAccesses::total",
    "dcache_access":     r"l1dcaches\.overallAccesses::total",
    "icache_preempt":    r"l1icaches\.preemptionBlockedCycles",
    "dcache_preempt":    r"l1dcaches\.preemptionBlockedCycles",
    "bp_look_d_cond":    r"branchPred\.btb\.lookups::DirectCond\b",
    "bp_look_d_uncond":  r"branchPred\.btb\.lookups::DirectUncond\b",
    "bp_look_i_cond":    r"branchPred\.btb\.lookups::IndirectCond\b",
    "bp_look_i_uncond":  r"branchPred\.btb\.lookups::IndirectUncond\b",
    "bp_look_call_d":    r"branchPred\.btb\.lookups::CallDirect\b",
    "bp_look_call_i":    r"branchPred\.btb\.lookups::CallIndirect\b",
    "bp_look_return":    r"branchPred\.btb\.lookups::Return\b",
    "bp_misp_d_cond":    r"branchPred\.mispredicted::DirectCond\b",
    "bp_misp_d_uncond":  r"branchPred\.mispredicted::DirectUncond\b",
    "bp_misp_i_cond":    r"branchPred\.mispredicted::IndirectCond\b",
    "bp_misp_i_uncond":  r"branchPred\.mispredicted::IndirectUncond\b",
    "bp_misp_call_d":    r"branchPred\.mispredicted::CallDirect\b",
    "bp_misp_call_i":    r"branchPred\.mispredicted::CallIndirect\b",
    "bp_misp_return":    r"branchPred\.mispredicted::Return\b",
    "bp_cond_incorrect": r"branchPred\.condIncorrect\b",
    "simSeconds":        r"simSeconds",
    "simTicks":          r"simTicks",
    "simFreq":           r"simFreq",
    "ipc":               r"cores\.core\.ipc",
}

PRETTY_NAMES = {
    "numCycles": "Cycles",
    "numInsts": "Instructions",
    "icache_miss": "I-Cache Misses",
    "dcache_miss": "D-Cache Misses",
    "icache_access": "I-Cache Accesses",
    "dcache_access": "D-Cache Accesses",
    "branch_pred": "Branches",
    "branch_miss": "Branch Miss + Unpred",
    "simSeconds": "Time (us)",
    "ipc": "IPC",
}

CVA6_PREEMPTION = {
    "icache_access": "icache_preempt",
    "dcache_access": "dcache_preempt",
}


def format_cache_size(value):
    """Render a cache size as KiB/MiB. Accepts a byte count or a gem5 string."""
    text = str(value).strip()
    if not text:
        return "?"
    if not text.isdigit():
        return text                     # already something like '16KiB'
    num = int(text)
    for unit, step in (("MiB", 1024 * 1024), ("KiB", 1024)):
        if num >= step and num % step == 0:
            return f"{num // step}{unit}"
    return f"{num}B"


def format_metric(value, decimals=4):
    """Render a table value: thousands grouped, decimals only when it has any,
    so a count reads as 1,234,567 and an IPC as 0.8523 down the same column. A
    real number landing on a whole one drops the trailing zeros."""
    try:
        number = round(float(value), decimals)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.{decimals}f}"


def read_cache_geometry(out_dir):
    """Read the L1 geometry gem5 actually instantiated. config.ini is dumped
    next to stats.txt on every run, so this reports the caches the simulation
    was built with rather than the defaults in the configuration file."""
    geometry = {}
    section = ""
    config_path = os.path.join(out_dir, "config.ini")
    try:
        with open(config_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    section = line[1:-1].lower()
                    continue
                key, sep, value = line.partition("=")
                key = key.strip()
                if not sep or key not in ("size", "assoc"):
                    continue
                for tag, name in (("l1icache", "icache"), ("l1dcache", "dcache")):
                    if tag in section:
                        geometry.setdefault(name, {})[key] = value.strip()
    except OSError as e:
        print(f"[WARN] Could not read {config_path}: {e}. "
              f"The cache geometry is reported as '?'")
        return {}

    for name in ("icache", "dcache"):
        if not geometry.get(name):
            print(f"[WARN] No L1 {name[0]}-cache section in {config_path}. "
                  f"Its geometry is reported as '?'")
    return geometry


def build_table_header(engine, config, program, geometry):
    """The table title, over two lines. What was measured goes on the first
    and the configuration on the second, the configuration and its flags being
    long enough to push a single title past the width of the table."""
    parts = [f"RESULTS TABLE {engine} {program}"]
    for name, label in (("icache", "ICache"), ("dcache", "DCache")):
        cache = geometry.get(name, {})
        size = format_cache_size(cache.get("size", ""))
        assoc = cache.get("assoc") or "?"
        parts.append(f"{label}: {size}/{assoc}")
    return ["  ".join(parts), f"Config: {config}"]


def detect_lang(src_file, override):
    """Decide whether the input is C or assembly."""
    if override in ("c", "asm"):
        return override
    root, ext = os.path.splitext(src_file)
    if ext == ".S":
        return "asm"
    low = ext.lower()
    if low in C_EXTS:
        return "c"
    if low in ASM_EXTS:
        return "asm"
    # No clear hint: assume C and warn.
    print(f"[WARN] Unrecognised extension '{ext}'. Assuming C. "
          f"Use --lang c|asm to force.")
    return "c"


def report_failure(what, cmd, result, out_dir, program_name):
    """Say why a step failed and leave the whole of it on disk. Both streams
    go to a log in out_dir and the end of each is printed, since gem5 puts its
    traceback on stderr but the line explaining it on stdout."""
    log_path = os.path.join(out_dir, f"{program_name}_error.log")
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(log_path, "w") as f:
            f.write(f"$ {' '.join(cmd)}\n\nexit code: {result.returncode}\n")
            f.write(f"\n----- stdout -----\n{result.stdout or '(empty)'}\n")
            f.write(f"\n----- stderr -----\n{result.stderr or '(empty)'}\n")
    except OSError as e:
        print(f"[WARN] Could not write the failure log: {e}")
        log_path = None

    print(f"[ERROR] {what} failed with exit code {result.returncode}")
    print(f"[ERROR] Command: {' '.join(cmd)}")

    for name in ("stderr", "stdout"):
        text = (getattr(result, name) or "").strip()
        if not text:
            continue
        lines = text.splitlines()
        shown = lines[-ERROR_TAIL_LINES:]
        if len(lines) > len(shown):
            print(f"[ERROR] --- last {len(shown)} of {len(lines)} {name} "
                  f"lines ---")
        else:
            print(f"[ERROR] --- {name} ---")
        for line in shown:
            print(f"  {line}")

    if log_path:
        print(f"[ERROR] Full output: {log_path}")


def compile_program(src_file, lang, out_dir):
    """Compile src_file by its type, C or asm, and return the binary path. It
    is built inside out_dir rather than beside the source, so a run stays in
    one place and two runs of a test cannot write the same file."""
    base_name = os.path.splitext(os.path.basename(src_file))[0]
    os.makedirs(out_dir, exist_ok=True)
    bin_file = os.path.join(out_dir, base_name)

    print(f"[INFO] Compiling ({lang}) {src_file} -> {bin_file}")

    cflags = list(COMMON_CFLAGS)
    if lang == "c":
        cflags += C_EXTRA_CFLAGS
    cflags.append(f"-I{M5_INCLUDE}")

    # Both C and asm link m5op.S to resolve the m5_* ops.
    sources = [src_file, M5_OP_ASM]
    cmd = [GCC_CMD] + sources + cflags + ["-o", bin_file]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            report_failure("Compilation", cmd, result, out_dir, base_name)
            sys.exit(1)
    except FileNotFoundError:
        print(f"[ERROR] Compiler not found: {GCC_CMD}")
        sys.exit(1)

    return bin_file


def split_own_args(argv):
    """Split the command line into this script's arguments and the
    configuration's. Everything after a '--' is the configuration's, verbatim,
    which is what a flag taking a value or colliding with ours needs."""
    if "--" in argv:
        cut = argv.index("--")
        return argv[:cut], argv[cut + 1:]
    return argv, []


def resolve_gem5_bin(spec):
    """Find the binary a --build value names, or None.

    Accepts a build directory name (RISCV), a path to one (build/RISCV) or a
    path to the binary itself, so any build in the tree can be run.
    """
    if os.path.isfile(spec):
        return spec
    for directory in (spec, os.path.join("build", spec)):
        for name in GEM5_BINARY_NAMES:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def build_is_patched(path):
    """Whether this gem5 binary carries the patch, or None if it cannot be read."""
    overlap = len(PATCH_MARKER) - 1
    try:
        with open(path, "rb") as handle:
            tail = b""
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    return False
                if PATCH_MARKER in tail + chunk:
                    return True
                tail = chunk[-overlap:]
    except OSError:
        return None


def run_gem5(config_file, bin_file, no_trace, program_name, out_dir,
             config_args=(), gem5_bin=None):
    os.makedirs(out_dir, exist_ok=True)

    stats_path = os.path.join(out_dir, "stats.txt")
    if os.path.exists(stats_path):
        os.remove(stats_path)

    cmd = [gem5_bin or resolve_gem5_bin(DEFAULT_BUILD)]

    # Unless '--no-trace' is set, add the requested debug flags.
    if not no_trace:
        trace_file = f"{program_name}_trace.txt"
        print(f"[INFO] Enabling detailed debug traces in: "
              f"{os.path.join(out_dir, trace_file)}")
        cmd.extend([
            # RAS is stock but off by default. It carries the stack depth and,
            # on a patched build, the drop the no-recovery model takes instead
            # of repairing the stack on a squash.
            "--debug-flags=Minor,MinorTrace,MinorTiming,CacheAll,ExecAll,"
            "Fetch,Decode,IEW,Commit,LSQ,Scoreboard,Writeback,RAS",
            f"--debug-file={trace_file}",
        ])

    cmd.extend(["-d", out_dir, config_file, bin_file])

    # gem5 hands everything after the script's path to the script, so a
    # configuration's own flags, such as the lab configuration's
    # --port-model, ride along here untouched.
    cmd.extend(config_args)

    print(f"[INFO] Running gem5 simulation using '{config_file}'")
    if config_args:
        print(f"[INFO] Passing to {os.path.basename(config_file)}: "
              f"{' '.join(config_args)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        report_failure("gem5", cmd, result, out_dir, program_name)
        sys.exit(1)
    return stats_path


def generate_and_show_codelist(bin_file, program_name, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    list_file = os.path.join(out_dir, f"{program_name}.list")
    report_file = os.path.join(out_dir, f"{program_name}_report.txt")

    print(f"[INFO] Generating disassembled code in: {list_file}")

    cmd = [OBJDUMP_CMD, "-d", "-S", "-l", bin_file]

    try:
        with open(list_file, "w") as f:
            subprocess.run(cmd, stdout=f, check=True)
    except subprocess.CalledProcessError as e:
        print("[ERROR]", e)
        sys.exit(1)

    print()
    for line in CODE_BANNER:
        print(line)

    try:
        with open(list_file, "r") as f, open(report_file, "w") as f_report:
            f_report.write("\n".join(CODE_BANNER) + "\n")
            last = "\n"
            for line in f:
                print(line, end='')
                f_report.write(line)
                last = line
                if "jal" in line and "<m5_dump_stats>" in line:
                    break
            if not last.endswith("\n"):
                f_report.write("\n")
            f_report.write("\n".join(CODE_END_BANNER) + "\n")
    except FileNotFoundError:
        print(f"[WARN] Could not read the generated file {list_file}")
        return None

    for line in CODE_END_BANNER:
        print(line)
    print()
    print(f"[INFO] Clean file saved in: {report_file}")
    return report_file


def collect_results(program_name, out_dir, results_dir):
    """Copy the four files worth keeping into run_results/. The trace is what
    the viewer renders, the .list the disassembly, the _report.txt the measured
    region plus the metrics table, and the stats gem5's own numbers."""
    try:
        os.makedirs(results_dir, exist_ok=True)
    except OSError as e:
        print(f"[WARN] Could not create {results_dir}: {e}")
        return

    copied = []
    for name, kept_as in ((f"{program_name}_trace.txt", None),
                          (f"{program_name}.list", None),
                          (f"{program_name}_report.txt", None),
                          ("stats.txt", f"{program_name}_stats.txt")):
        source = os.path.join(out_dir, name)
        # With --no-trace there is no trace to copy, so a missing source here
        # is expected rather than a problem.
        if not os.path.isfile(source):
            continue
        kept_as = kept_as or name
        try:
            shutil.copy2(source, os.path.join(results_dir, kept_as))
            copied.append(kept_as)
        except OSError as e:
            print(f"[WARN] Could not copy {source}: {e}")

    if copied:
        print(f"[INFO] Copied to {results_dir}: {', '.join(copied)}")


def parse_stats(stats_path):
    print("[INFO] Extracting statistics")
    results = {key: 0.0 for key in METRICS_MAP}
    # None rather than zero: a stock build never writes these, and gem5 omits
    # one that is zero, so the two cases have to stay apart from a real count.
    for key in CVA6_PREEMPTION.values():
        results[key] = None

    block_count = 0
    in_target_block = False

    try:
        with open(stats_path, 'r') as f:
            for line in f:
                if "Begin Simulation Statistics" in line:
                    block_count += 1
                    # Assume the ROI is in the first stats block.
                    in_target_block = (block_count == 1)

                if in_target_block:
                    for key, regex in METRICS_MAP.items():
                        if re.search(regex, line):
                            parts = line.split()
                            if len(parts) >= 2:
                                try:
                                    results[key] = float(parts[1])
                                except ValueError:
                                    pass
    except FileNotFoundError:
        print("[ERROR] stats.txt not found")
        sys.exit(1)

    # Total branches = all SEVEN BTB-lookup buckets
    results["branch_pred"] = (results.get("bp_look_d_cond", 0) +
                              results.get("bp_look_d_uncond", 0) +
                              results.get("bp_look_i_cond", 0) +
                              results.get("bp_look_i_uncond", 0) +
                              results.get("bp_look_call_d", 0) +
                              results.get("bp_look_call_i", 0) +
                              results.get("bp_look_return", 0))

    # Mispred + Unpred = sum of branchPred.mispredicted::* over the seven
    # types.
    misp_by_type = (results.get("bp_misp_d_cond", 0) +
                    results.get("bp_misp_d_uncond", 0) +
                    results.get("bp_misp_i_cond", 0) +
                    results.get("bp_misp_i_uncond", 0) +
                    results.get("bp_misp_call_d", 0) +
                    results.get("bp_misp_call_i", 0) +
                    results.get("bp_misp_return", 0))
    cond_incorrect = results.get("bp_cond_incorrect", 0)
    results["branch_miss"] = cond_incorrect if cond_incorrect > 0 else misp_by_type

    return results


def print_table(results, overhead, report_file=None,
                header=("RESULTS TABLE",), show_cva6=False):
    output_buffer = []

    # The rule is widened when the title is longer, so the box never breaks.
    # A third column needs 18 more, which is the minimum the rule can be.
    width = max(79 if show_cva6 else 70, max(len(line) for line in header))

    output_buffer.append("\n" + "=" * width)
    output_buffer.extend(header)
    output_buffer.append("=" * width)
    columns = f"{'METRIC':<25} | {'OFFICIAL':>15} | {'NET':>15}"
    if show_cva6:
        columns += f" | {'NET (CVA6)':>15}"
    output_buffer.append(columns)
    output_buffer.append("=" * width)

    keys_order = ["numCycles", "numInsts", "icache_miss", "dcache_miss",
                  "icache_access", "dcache_access", "branch_pred",
                  "branch_miss", "simSeconds", "ipc"]

    clean_array_official = []
    clean_array_corrected = []
    clean_array_cva6 = []

    # Pre-compute the corrected IPC (with overhead removed).
    raw_insts = results.get("numInsts", 0)
    net_insts = max(0, raw_insts - overhead.get("numInsts", 0))

    raw_cycles = results.get("numCycles", 1)
    net_cycles = max(
        1, raw_cycles - overhead.get("numCycles", 0))  # avoid div/0

    corrected_ipc = net_insts / net_cycles if net_cycles > 0 else 0

    # stats.txt rounds simSeconds to six decimals, which at these runtimes
    # cuts the time off at the whole microsecond. simTicks keeps the full
    # resolution, so the time comes from there when the tick rate is beside it.
    time_us = results.get("simSeconds", 0) * 1_000_000
    ticks = results.get("simTicks", 0)
    tick_freq = results.get("simFreq", 0)
    if ticks and tick_freq:
        time_us = ticks / tick_freq * 1_000_000

    # The time is the cycle count read through the clock, so the net time is
    # the net cycles read through the same clock. Scaling by the ratio takes
    # the clock from the run itself and needs no frequency here.
    net_cycle_count = max(0, raw_cycles - overhead.get("numCycles", 0))
    net_time_us = time_us * net_cycle_count / raw_cycles if raw_cycles else 0.0

    for key in keys_order:
        val_official = results.get(key, 0)
        label = PRETTY_NAMES.get(key, key)
        ovh = overhead.get(key, 0)

        if key == "ipc":
            val_corrected = corrected_ipc
        else:
            val_corrected = max(0, val_official - ovh)

        # The CVA6 column carries the same scaffolding subtraction as NET, and
        # repeats NET on every row the patch has no counter for, so it reads as
        # one complete alternative rather than a scattering of cells.
        source = CVA6_PREEMPTION.get(key)
        preempt = results.get(source) if source else None
        val_cva6 = (val_corrected if preempt is None
                    else max(0, val_official + preempt - ovh))

        if key == "simSeconds":
            val_off_us = time_us
            val_cor_us = net_time_us
            clean_array_official.append(round(val_off_us, 4))
            clean_array_corrected.append(round(val_cor_us, 4))
            clean_array_cva6.append(round(val_cor_us, 4))
            fmt_off = format_metric(val_off_us)
            fmt_cor = format_metric(val_cor_us)
            fmt_cva6 = fmt_cor
        elif key == "ipc":
            clean_array_official.append(round(val_official, 4))
            clean_array_corrected.append(round(val_corrected, 4))
            clean_array_cva6.append(round(val_cva6, 4))
            fmt_off = format_metric(val_official)
            fmt_cor = format_metric(val_corrected)
            fmt_cva6 = format_metric(val_cva6)
        else:
            clean_array_official.append(int(val_official))
            clean_array_corrected.append(int(val_corrected))
            clean_array_cva6.append(int(val_cva6))
            fmt_off = format_metric(int(val_official))
            fmt_cor = format_metric(int(val_corrected))
            fmt_cva6 = format_metric(int(val_cva6))

        row = f"{label:<25} | {fmt_off:>15} | {fmt_cor:>15}"
        if show_cva6:
            row += f" | {fmt_cva6:>15}"
        output_buffer.append(row)

    output_buffer.append("=" * width + "\n")
    output_buffer.append(f"Clean result (OFFICIAL):  {clean_array_official}")
    output_buffer.append(f"Clean result (NET):       {clean_array_corrected}")
    if show_cva6:
        output_buffer.append(f"Clean result (NET CVA6):  {clean_array_cva6}")
    output_buffer.append("")

    for line in output_buffer:
        print(line)

    if report_file and os.path.exists(report_file):
        try:
            with open(report_file, "a") as f_report:
                # output_buffer opens with its own blank line.
                for line in output_buffer:
                    f_report.write(line + "\n")
            print(f"[INFO] Metrics successfully consolidated in: {report_file}")
        except Exception as e:
            print(f"[WARN] Could not save the metrics to the file: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run a gem5 RISC-V simulation (C or assembly) and "
                    "consolidate reports.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Any flag this script does not define is passed on to the "
               "configuration,\nso a configuration's own options work here:\n"
               "\n"
               "  run_gem5.py gem5_config_CVA6.py daxpy.S "
               "--no-port-model --no-fill-phase\n"
               "\n"
               "Put them after a '--' when a flag takes a value or shares a "
               "name with\none of ours:\n"
               "\n"
               "  run_gem5.py gem5_config_CVA6.py daxpy.S -- "
               "--no-port-model")
    parser.add_argument("config_file",
                        help="Path to the gem5 configuration file (.py)")
    parser.add_argument("src_file",
                        help="Path to the program: C (.c) or assembly (.S/.s/.asm)")
    parser.add_argument("--lang", choices=["c", "asm"], default="auto",
                        help="Force the input type and overhead profile. "
                             "Defaults to detection by extension.")
    parser.add_argument("--build", default=DEFAULT_BUILD, metavar="NAME",
                        help=f"Which build to run: a directory name under "
                             f"build/, a path to one, or a path to the binary "
                             f"itself. Defaults to {DEFAULT_BUILD}")
    parser.add_argument("--no-trace", action="store_true",
                        help="Disable collection of detailed debug traces.")
    parser.add_argument("--gem5-out-dir", default=GEM5_OUT_DIR,
                        help=f"Where gem5 writes, and where the test is "
                             f"compiled. Defaults to {GEM5_OUT_DIR}/. Give "
                             f"concurrent runs one each, so they cannot "
                             f"overwrite each other's stats.txt")
    parser.add_argument("--results-dir", default=RESULTS_DIR,
                        help="Where the four files worth keeping are "
                             "copied. Defaults to run_results/ next to this "
                             "script")

    own_argv, after_separator = split_own_args(sys.argv[1:])
    args, unrecognised = parser.parse_known_args(own_argv)
    config_args = unrecognised + after_separator

    config_file = args.config_file
    src_file = args.src_file

    if not os.path.exists(config_file):
        print(f"[ERROR] The configuration file '{config_file}' does not exist")
        sys.exit(1)
    if not os.path.exists(src_file):
        print(f"[ERROR] The program file '{src_file}' does not exist")
        sys.exit(1)

    lang = detect_lang(src_file, args.lang)
    overhead = OVERHEAD_PROFILES[lang]

    gem5_bin = resolve_gem5_bin(args.build)
    if gem5_bin is None:
        print(f"[ERROR] No gem5 binary found for '{args.build}'. Looked for "
              f"{', '.join(GEM5_BINARY_NAMES)} in '{args.build}' and in "
              f"'{os.path.join('build', args.build)}'.")
        sys.exit(1)
    print(f"[INFO] Build: {gem5_bin}")
    if build_is_patched(gem5_bin):
        print(f"[WARN] '{gem5_bin}' is a patched build. The overhead profile "
              f"here was measured on a stock one, so the NET figures may not "
              f"apply. Use --build to pick a stock build.")

    program_name = os.path.splitext(os.path.basename(src_file))[0]

    binary = compile_program(src_file, lang, args.gem5_out_dir)
    stats_file = run_gem5(config_file, binary, args.no_trace, program_name,
                          args.gem5_out_dir, config_args, gem5_bin)

    report_file = generate_and_show_codelist(binary, program_name,
                                            args.gem5_out_dir)

    metrics = parse_stats(stats_file)
    geometry = read_cache_geometry(args.gem5_out_dir)
    # The flags ride along: they are what separates one run of a configuration
    # from another, so a table without them cannot be told apart.
    config_label = " ".join([os.path.basename(config_file)] + list(config_args))
    header = build_table_header("gem5", config_label,
                                os.path.basename(src_file), geometry)
    # The column appears exactly when the run produced the counters, which is
    # to say when a patched build ran. A stock build writes none of them.
    show_cva6 = any(metrics.get(key) is not None
                    for key in CVA6_PREEMPTION.values())
    print_table(metrics, overhead, report_file, header, show_cva6)

    # Done last, so the _report.txt copied out already carries the table.
    collect_results(program_name, args.gem5_out_dir, args.results_dir)
