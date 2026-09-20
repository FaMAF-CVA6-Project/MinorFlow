#!/usr/bin/env python3
"""Run a gem5 RISC-V simulation and consolidate the metrics.

Accepts both C (.c) and assembly (.S/.s/.asm/.sx) programs. The input type
is detected from the extension and can be forced with --lang. Run it from
the gem5 root:

    python3 scripts/run_gem5.py configs/gem5_config_MinorFlow.py daxpy.S
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys

# =============================================================================
# GLOBAL CONFIGURATION
# =============================================================================
GEM5_ROOT = os.getcwd()

# Where gem5 writes: stats.txt, the debug trace, the disassembly. gem5's
# m5out, moved under results/.
GEM5_OUT_DIR = os.path.join("results", "m5out")

# Where each run leaves a copy of the files worth keeping, under the root
# rather than beside this script, so everything a run writes is in one place.
# The originals stay in GEM5_OUT_DIR.
RESULTS_DIR = os.path.join("results", "run")
GCC_CMD = "riscv64-unknown-elf-gcc"
OBJDUMP_CMD = "riscv64-unknown-elf-objdump"

# The builds living side by side in one gem5 tree, named by their build
# directory. build/RISCV is the stock one every checkout has, so only the
# patched ones are built.
#
# build/RISCV_EXP is a third, for working on the patch. It is reached with
# --variant patch --build RISCV_EXP rather than by a variant of its own,
# since its overhead profile is the patched one.
GEM5_BUILDS = {
    "stock": "RISCV",
    "patch": "RISCV_PATCH",
}
DEFAULT_VARIANT = "stock"

# Tried in order inside a build directory, so a .fast build is picked up too.
GEM5_BINARY_NAMES = ("gem5.opt", "gem5.fast", "gem5.debug")

# A SimObject only the patch adds, so its presence in the binary is what tells
# a patched build from a stock one. Checked before every run, because the two
# are told apart by nothing else once they are built.
PATCH_MARKER = b"Axi2MemPort"


def find_patch():
    """The patch to hash, the copy under gem5_configs/config/ that a push
    refreshes, or None outside an image. The image deletes the copy it
    applied at the root, so that one is never there to read."""
    candidate = os.path.join(GEM5_ROOT, "gem5_configs", "config",
                             "MinorCPU_CVA6.patch")
    return candidate if os.path.isfile(candidate) else None


# The patch a run was built from. The image records the hash of the patch it
# applied, so a table says which transcription produced it and an edited but
# unrebuilt patch is caught before the numbers are believed.
PATCH_FILE = find_patch()
BUILT_PATCH_HASH = os.path.join(GEM5_ROOT, ".built_patch_sha1")
M5_INCLUDE = os.path.join(GEM5_ROOT, "include")
M5_OP_ASM = os.path.join(GEM5_ROOT, "util/m5/src/abi/riscv/m5op.S")

# Freestanding, since a test calls only the m5 ops and has no C library.
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

# The debug flags behind every line MinorFlow_tracer.py reads: MinorLine and
# MinorInst (MinorTrace), Execute, LSQ and scoreboard lines (Minor), Fetch1,
# Fetch2 and the predictor (Fetch), Decode, the caches, commits and the RAS.
DEBUG_FLAGS = ("Minor", "MinorTrace", "CacheAll", "ExecAll", "Fetch",
               "Decode", "RAS")

# Lines of each captured stream echoed when a step fails. The whole of both
# goes to the log either way. This is only what the terminal is worth.
ERROR_TAIL_LINES = 40

# =============================================================================
# OVERHEAD PROFILES
# =============================================================================
# Scaffolding around the measured region, subtracted to get NET. Indexed by
# suite, variant and language. 'config' is the fork's calibration set and
# 'viewer' this repository's, whose templates differ.
OVERHEAD_SUITES = {
    "config": {
        "patch": {
            "c": {
                "numCycles":        26,
                "numInsts":         6,
                "icache_miss":      2,
                "dcache_miss":      0,
                "icache_access":    16,
                "dcache_access":    0,
                "branch_pred":      5,
                "branch_miss":      1,
            },
            "asm": {
                "numCycles":        26,
                "numInsts":         6,
                "icache_miss":      2,
                "dcache_miss":      0,
                "icache_access":    16,
                "dcache_access":    0,
                "branch_pred":      5,
                "branch_miss":      1,
            },
        },
        "stock": {
            "c": {
                "numCycles":        45,
                "numInsts":         6,
                "icache_miss":      3,
                "dcache_miss":      0,
                "icache_access":    22,
                "dcache_access":    0,
                "branch_pred":      6,
                "branch_miss":      3,
            },
            "asm": {
                "numCycles":        41,
                "numInsts":         6,
                "icache_miss":      3,
                "dcache_miss":      0,
                "icache_access":    21,
                "dcache_access":    0,
                "branch_pred":      5,
                "branch_miss":      3,
            },
        },
    },
    "viewer": {
        "patch": {
            "c": {
                "numCycles":        21,
                "numInsts":         5,
                "icache_miss":      1,
                "dcache_miss":      0,
                "icache_access":    15,
                "dcache_access":    0,
                "branch_pred":      4,
                "branch_miss":      1,
            },
            "asm": {
                "numCycles":        25,
                "numInsts":         5,
                "icache_miss":      1,
                "dcache_miss":      0,
                "icache_access":    15,
                "dcache_access":    0,
                "branch_pred":      4,
                "branch_miss":      1,
            },
        },
        "stock": {
            "c": {
                "numCycles":        20,
                "numInsts":         5,
                "icache_miss":      0,
                "dcache_miss":      0,
                "icache_access":    16,
                "dcache_access":    0,
                "branch_pred":      5,
                "branch_miss":      2,
            },
            "asm": {
                "numCycles":        17,
                "numInsts":         5,
                "icache_miss":      0,
                "dcache_miss":      0,
                "icache_access":    12,
                "dcache_access":    0,
                "branch_pred":      3,
                "branch_miss":      1,
            },
        },
    },
}


def resolve_input(path):
    """A config or test path, resolved against the gem5 root and then against
    this script's own repository.

    The script has to run from the gem5 root, because that is where gem5's
    build/, include/ and m5op.S are."""
    if not path or os.path.exists(path):
        return path
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    # The current directory first, which in the container is the root holding
    # configs/ and benchmarks/, then this script's own repository.
    for base in (os.curdir, repo, here):
        candidate = os.path.join(base, path)
        if base != os.curdir and os.path.exists(candidate):
            return candidate
        # Also the bare name inside the usual folders, so 'daxpy.S' finds
        # benchmarks/config/daxpy.S and a configuration its gem5_configs/
        # folder. The frozen CARLA2026 copy is last, so the live one wins.
        for sub in ("gem5_configs/config", "gem5_configs/viewer", "configs",
                    "benchmarks/config", "benchmarks/viewer", "benchmarks",
                    "gem5_configs/CARLA2026"):
            candidate = os.path.join(base, sub, os.path.basename(path))
            if os.path.exists(candidate):
                return candidate
    return path


# A one-line file in a benchmark directory naming the overhead suite its
# programs belong to.
SUITE_MARKER = ".overhead_suite"


# SHARED BEGIN py-suite-marker

# Needs: os, SUITE_MARKER, OVERHEAD_SUITES


def read_suite_marker(src_file):
    """The suite declared beside the test, or None.

    Looks in the test's own directory and the two above it, so a benchmark in
    a subdirectory still finds its set's marker."""
    if not src_file:
        return None
    d = os.path.dirname(os.path.abspath(src_file))
    for _ in range(3):
        marker = os.path.join(d, SUITE_MARKER)
        if os.path.isfile(marker):
            try:
                with open(marker) as f:
                    name = f.read().strip()
            except OSError:
                return None
            if name in OVERHEAD_SUITES:
                return name
            print(f"[WARN] {marker} names '{name}', which is not one of "
                  f"{sorted(OVERHEAD_SUITES)}. Ignoring it.")
            return None
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None

# SHARED END py-suite-marker


def default_suite(src_file):
    """The suite the .overhead_suite beside the test names, or None after
    saying why. The suite decides what is subtracted from every reported
    cycle count, so it is never guessed from a path."""
    named = read_suite_marker(src_file)
    if named is None:
        choices = " or --suite ".join(sorted(OVERHEAD_SUITES))
        print(f"[ERROR] No {SUITE_MARKER} beside {src_file} or in the two "
              f"folders above it, so the overhead table to subtract is "
              f"unknown. Pass --suite {choices}, or add a {SUITE_MARKER} "
              f"file naming one beside the benchmarks.", file=sys.stderr)
    return named


# =============================================================================
# METRICS MAP
# =============================================================================
METRICS_MAP = {
    "numCycles":         r"cores\.core\.numCycles",
    "numInsts":          r"cores\.core\.commitStats0\.numInsts\s",
    "icache_miss":       r"l1icaches\.overallMshrMisses::total",
    "dcache_miss":       r"l1dcaches\.overallMshrMisses::total",
    "icache_access":     r"l1icaches\.demandAccesses::total",
    "dcache_access":     r"l1dcaches\.demandAccesses::total",
    "icache_preempt":    r"l1icaches\.preemptionBlockedCycles",
    "dcache_preempt":    r"l1dcaches\.preemptionBlockedCycles",
    "icache_win_trig":   r"l1icaches\.windowTriggerCycles",
    "dcache_win_trig":   r"l1dcaches\.windowTriggerCycles",
    "icache_win_over":   r"l1icaches\.windowOverlapCycles",
    "dcache_win_over":   r"l1dcaches\.windowOverlapCycles",
    "bp_look_d_cond":    r"branchPred\.btb\.lookups::DirectCond\b",
    "bp_look_d_uncond":  r"branchPred\.btb\.lookups::DirectUncond\b",
    "bp_look_i_cond":    r"branchPred\.btb\.lookups::IndirectCond\b",
    "bp_look_i_uncond":  r"branchPred\.btb\.lookups::IndirectUncond\b",
    "bp_look_call_d":    r"branchPred\.btb\.lookups::CallDirect\b",
    "bp_look_call_i":    r"branchPred\.btb\.lookups::CallIndirect\b",
    "bp_look_return":    r"branchPred\.btb\.lookups::Return\b",
    # mispredicted_0, the thread suffix gem5 writes.
    "bp_misp_d_cond":    r"branchPred\.mispredicted_0::DirectCond\b",
    "bp_misp_d_uncond":  r"branchPred\.mispredicted_0::DirectUncond\b",
    "bp_misp_i_cond":    r"branchPred\.mispredicted_0::IndirectCond\b",
    "bp_misp_i_uncond":  r"branchPred\.mispredicted_0::IndirectUncond\b",
    "bp_misp_call_d":    r"branchPred\.mispredicted_0::CallDirect\b",
    "bp_misp_call_i":    r"branchPred\.mispredicted_0::CallIndirect\b",
    "bp_misp_return":    r"branchPred\.mispredicted_0::Return\b",
    "simSeconds":        r"simSeconds",
    "simTicks":          r"simTicks",
    "simFreq":           r"simFreq",
    "ipc":               r"cores\.core\.ipc",
}

PRETTY_NAMES = {
    "numCycles": "Cycles",
    "numInsts": "Instructions",
    "icache_miss": "I-cache misses",
    "dcache_miss": "D-cache misses",
    "icache_access": "I-cache accesses",
    "dcache_access": "D-cache accesses",
    "branch_pred": "Branches",
    "branch_miss": "Mispredicts + unpredicted",
    "simSeconds": "Time (us)",
    "ipc": "IPC",
}

CVA6_EXTRA = {
    "icache_access": ("icache_preempt", "icache_win_trig", "icache_win_over"),
    "dcache_access": ("dcache_preempt", "dcache_win_trig", "dcache_win_over"),
}

# The seven branch types gem5 keeps a counter for, as METRICS_MAP names them.
BTB_LOOKUP_KEYS = ("bp_look_d_cond", "bp_look_d_uncond", "bp_look_i_cond",
                   "bp_look_i_uncond", "bp_look_call_d", "bp_look_call_i",
                   "bp_look_return")
MISPREDICTED_KEYS = ("bp_misp_d_cond", "bp_misp_d_uncond", "bp_misp_i_cond",
                     "bp_misp_i_uncond", "bp_misp_call_d", "bp_misp_call_i",
                     "bp_misp_return")


class RunFailed(Exception):
    """A step failed and has already said why, so main exits with 1."""


def format_cache_size(value):
    """Render a cache size as KiB or MiB, from a byte count or a gem5
    string such as 16KiB, which is returned as it is."""
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
                for tag, name in (("l1icache", "icache"),
                                  ("l1dcache", "dcache")):
                    if tag in section:
                        geometry.setdefault(name, {})[key] = value.strip()
    except OSError as e:
        print(f"[WARN] Could not read {config_path}: {e}. "
              f"The cache geometry is reported as '?'")
        return {}

    for name in ("icache", "dcache"):
        if not geometry.get(name):
            print(f"[WARN] No L1 {name[0].upper()}-cache section in "
                  f"{config_path}. Its geometry is reported as '?'")
    return geometry


def build_table_header(engine, core, program, geometry, build=""):
    """Title, core and build, one per line. For gem5 the core is the
    configuration and its flags. The build line is what makes a gathered
    metrics file say which binary produced it."""
    parts = [f"{METRICS_MARKER} {engine} {program}"]
    for name, label in (("icache", "I-cache"), ("dcache", "D-cache")):
        cache = geometry.get(name, {})
        size = format_cache_size(cache.get("size", ""))
        assoc = cache.get("assoc") or "?"
        parts.append(f"{label}: {size}/{assoc}")
    lines = ["  ".join(parts), f"Config: {core}"]
    if build:
        lines.append(f"Build: {build}")
    return lines


def detect_lang(src_file, override):
    """Decide whether the input is C or assembly."""
    if override in ("c", "asm"):
        return override
    ext = os.path.splitext(src_file)[1]
    if ext == ".S":
        return "asm"
    low = ext.lower()
    if low in C_EXTS:
        return "c"
    if low in ASM_EXTS:
        return "asm"
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

    print(f"[ERROR] {what} failed with exit code {result.returncode}",
          file=sys.stderr)
    print(f"[ERROR] Command: {' '.join(cmd)}", file=sys.stderr)

    for name in ("stderr", "stdout"):
        text = (getattr(result, name) or "").strip()
        if not text:
            continue
        lines = text.splitlines()
        shown = lines[-ERROR_TAIL_LINES:]
        if len(lines) > len(shown):
            print(f"[ERROR] --- last {len(shown)} of {len(lines)} {name} "
                  f"lines ---", file=sys.stderr)
        else:
            print(f"[ERROR] --- {name} ---", file=sys.stderr)
        for line in shown:
            print(f"  {line}", file=sys.stderr)

    if log_path:
        print(f"[ERROR] Full output: {log_path}", file=sys.stderr)


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

    sources = [src_file, M5_OP_ASM]
    cmd = [GCC_CMD] + sources + cflags + ["-o", bin_file]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print(f"[ERROR] Compiler not found: {GCC_CMD}", file=sys.stderr)
        raise RunFailed()
    if result.returncode != 0:
        report_failure("Compilation", cmd, result, out_dir, base_name)
        raise RunFailed()

    return bin_file


# SHARED BEGIN py-split-own-args

# Needs: none


def split_own_args(argv):
    """Split the command line into this script's arguments and the
    configuration's. Everything after a '--' is the configuration's, verbatim,
    which is what a flag taking a value or colliding with ours needs."""
    if "--" in argv:
        cut = argv.index("--")
        return argv[:cut], argv[cut + 1:]
    return argv, []

# SHARED END py-split-own-args


def resolve_gem5_bin(spec):
    """Find the binary a --build value names, or None.

    Accepts a build directory name (RISCV), a path to one (build/RISCV) or a
    path to the binary itself, so any build in the tree can be run."""
    if os.path.isfile(spec):
        return spec
    for directory in (spec, os.path.join("build", spec)):
        for name in GEM5_BINARY_NAMES:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def build_is_patched(path):
    """Whether this gem5 binary carries the patch, or None if it cannot be
    read."""
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


def patch_fingerprint():
    """(built, current) short hashes of the patch, either possibly None.

    'built' is what the image recorded when it applied the patch, 'current' is
    the file sitting there now."""
    def read_hash(path, hasher):
        """hasher applied to the open file, or None when it cannot be read."""
        try:
            with open(path, "rb") as handle:
                return hasher(handle)
        except OSError:
            return None

    # An empty marker holds no hash rather than failing a finished run.
    built = read_hash(BUILT_PATCH_HASH,
                      lambda h: (h.read().decode().split() or [""])[0][:12]
                      or None)
    current = None
    if PATCH_FILE is not None:
        current = read_hash(PATCH_FILE,
                            lambda h: hashlib.sha1(h.read()).hexdigest()[:12])
    return built, current


def run_gem5(config_file, bin_file, no_trace, program_name, out_dir,
             gem5_bin, config_args=()):
    """Run gem5 on the binary, with the debug trace unless no_trace, and
    return the path of the stats.txt it writes."""
    os.makedirs(out_dir, exist_ok=True)

    stats_path = os.path.join(out_dir, "stats.txt")
    if os.path.exists(stats_path):
        os.remove(stats_path)

    # Resolved and checked by the caller, so a missing binary is reported
    # there rather than as a TypeError out of subprocess.
    cmd = [gem5_bin]

    # The trace is the expensive part, which is what --no-trace skips.
    if not no_trace:
        trace_file = f"{program_name}_trace.txt"
        print(f"[INFO] Enabling detailed debug traces in: "
              f"{os.path.join(out_dir, trace_file)}")
        cmd.extend([
            # Every family MinorFlow_tracer.py reads, and nothing else, since
            # the trace runs to gigabytes. RAS is stock but off by default.
            "--debug-flags=" + ",".join(DEBUG_FLAGS),
            f"--debug-file={trace_file}",
        ])

    cmd.extend(["-d", out_dir, config_file, bin_file])

    # gem5 hands everything after the script's path to the script, so a
    # configuration's own flags ride along here untouched.
    cmd.extend(config_args)

    print(f"[INFO] Running gem5 simulation using '{config_file}'")
    if config_args:
        print(f"[INFO] Passing to {os.path.basename(config_file)}: "
              f"{' '.join(config_args)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        report_failure("gem5", cmd, result, out_dir, program_name)
        raise RunFailed()
    return stats_path


def generate_codelist(bin_file, program_name, out_dir):
    """Disassemble the binary into <program>.list, and start the
    _report.txt with the listing up to the call to m5_dump_stats, the
    measured region. Returns the _report.txt path, or None."""
    os.makedirs(out_dir, exist_ok=True)
    list_file = os.path.join(out_dir, f"{program_name}.list")
    report_file = os.path.join(out_dir, f"{program_name}_report.txt")

    print(f"[INFO] Generating disassembled code in: {list_file}")

    cmd = [OBJDUMP_CMD, "-d", "-S", "-l", bin_file]

    try:
        with open(list_file, "w") as f:
            subprocess.run(cmd, stdout=f, check=True)
    except FileNotFoundError:
        print(f"[ERROR] Disassembler not found: {OBJDUMP_CMD}",
              file=sys.stderr)
        raise RunFailed()
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        raise RunFailed()

    written = 0
    found_end = False
    try:
        with open(list_file, "r") as f, open(report_file, "w") as f_report:
            f_report.write("\n".join(CODE_BANNER) + "\n")
            last = "\n"
            for line in f:
                f_report.write(line)
                written += 1
                last = line
                if "jal" in line and "<m5_dump_stats>" in line:
                    found_end = True
                    break
            if not last.endswith("\n"):
                f_report.write("\n")
            f_report.write("\n".join(CODE_END_BANNER) + "\n")
    except FileNotFoundError:
        print(f"[WARN] Could not read the generated file {list_file}")
        return None

    if not found_end:
        print(f"[WARN] No 'jal <m5_dump_stats>' in {list_file}, so the whole "
              f"disassembly was written rather than the measured region. "
              f"Check that the test calls m5_dump_stats.")
    print(f"[INFO] Disassembly ({written} lines) saved in: {report_file}")
    return report_file


def collect_results(program_name, out_dir, results_dir):
    """Copy the four files worth keeping into results/run/. The trace is the
    tracer's input, the .list the disassembly, the _report.txt the measured
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
    """The METRICS_MAP values from the first statistics block of stats.txt,
    with the branch sums the table shows."""
    print("[INFO] Extracting statistics")
    results = {key: 0.0 for key in METRICS_MAP}
    # None rather than zero: a stock build never writes these, and gem5 omits
    # one that is zero, so the two cases have to stay apart from a real count.
    for keys in CVA6_EXTRA.values():
        for key in keys:
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
        print("[ERROR] stats.txt not found", file=sys.stderr)
        raise RunFailed()

    # Branches: the seven BTB-lookup buckets summed. The viewer's Branches
    # count sits at or below this, since Fetch2 also predicts wrong-path
    # instructions discarded before Execute, which never become records.
    results["branch_pred"] = sum(results[key] for key in BTB_LOOKUP_KEYS)

    # Mispredicted plus unpredicted, over the same seven types.
    results["branch_miss"] = sum(results[key] for key in MISPREDICTED_KEYS)

    return results


def print_table(results, overhead, report_file=None,
                header=(METRICS_MARKER,), show_cva6=False):
    """Print the OFFICIAL and NET table, with the NET (CVA6) column when
    show_cva6, and append it to the _report.txt."""
    output_buffer = []

    # The rule is widened when the title is longer, so the box never breaks.
    # The column header row is 61 characters with two value columns and 79
    # with three, so the floors of 70 and 79 always hold it.
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

    raw_insts = results.get("numInsts", 0)
    net_insts = max(0, raw_insts - overhead.get("numInsts", 0))

    raw_cycles = results["numCycles"]
    # At least one cycle, so an overhead as large as the run cannot divide
    # by zero.
    net_cycles = max(1, raw_cycles - overhead.get("numCycles", 0))
    corrected_ipc = net_insts / net_cycles

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
        found = [results.get(k) for k in CVA6_EXTRA.get(key, ())
                 if results.get(k) is not None]
        val_cva6 = (val_corrected if not found
                    else max(0, val_official + sum(found) - ovh))

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
            print(
                f"[INFO] Metrics successfully consolidated in: {report_file}")
        except OSError as e:
            print(f"[WARN] Could not save the metrics to the file: {e}")


def main():
    """Parse the command line, run the four steps and print the table.
    Returns the exit code."""
    parser = argparse.ArgumentParser(
        description="Run a gem5 RISC-V simulation (C or assembly) and "
                    "consolidate reports.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Any flag this script does not define is passed on to the "
               "configuration,\nso a configuration's own options work here:\n"
               "\n"
               "  run_gem5.py my_config.py daxpy.S --some-config-flag\n"
               "\n"
               "Put them after a '--' when a flag takes a value or shares a "
               "name with\none of ours:\n"
               "\n"
               "  run_gem5.py my_config.py daxpy.S -- --some-config-flag 4")
    parser.add_argument("config_file",
                        help="Path to the gem5 configuration file (.py)")
    parser.add_argument("src_file",
                        help="Path to the program: C (.c) or assembly "
                             "(.S/.s/.asm/.sx)")
    parser.add_argument("--lang", choices=["auto", "c", "asm"],
                        default="auto",
                        help="Force the input type, which selects both the "
                             "compile flags and the overhead profile. "
                             "Defaults to auto, detection by extension")
    parser.add_argument("--suite", choices=sorted(OVERHEAD_SUITES),
                        default=None,
                        help="Which overhead table to subtract. 'config' is "
                             "the fork's calibration benchmarks, 'viewer' "
                             "the programs written while developing "
                             "MinorFlow. Defaults to the .overhead_suite "
                             "marker beside the test or in the two folders "
                             "above it, and the run stops without one")
    parser.add_argument("--variant", choices=sorted(GEM5_BUILDS),
                        default=DEFAULT_VARIANT,
                        help=f"Which build to run and whose overhead profile "
                             f"to subtract. Defaults to {DEFAULT_VARIANT}, "
                             f"build/{GEM5_BUILDS[DEFAULT_VARIANT]}")
    parser.add_argument("--skip-build-check", action="store_true",
                        help="Run even when the build does not match "
                             "--variant. The overhead profile is then almost "
                             "certainly wrong, so only for a deliberate "
                             "cross-check")
    parser.add_argument("--build", default=None, metavar="NAME",
                        help="Run a different build: a directory name under "
                             "build/, a path to one, or a path to the binary "
                             "itself. RISCV_EXP is the one for working on the "
                             "patch. The overhead profile still follows "
                             "--variant")
    parser.add_argument("--no-trace", action="store_true",
                        help="Do not write the trace, and report metrics only")
    parser.add_argument("--gem5-out-dir", default=GEM5_OUT_DIR,
                        help=f"Where gem5 writes, and where the test is "
                             f"compiled. Defaults to {GEM5_OUT_DIR}/. Give "
                             f"concurrent runs one each, so they cannot "
                             f"overwrite each other's stats.txt")
    parser.add_argument("--results-dir", default=RESULTS_DIR,
                        help="Where the four files worth keeping are "
                             "copied. Defaults to results/run/ under the "
                             "current directory, the gem5 root")

    own_argv, after_separator = split_own_args(sys.argv[1:])
    args, unrecognised = parser.parse_known_args(own_argv)
    config_args = unrecognised + after_separator

    # Paths first. Both the suite marker and the error messages below want
    # the resolved path, not the one typed: the marker sits beside the test
    # in its own repository, which is not where the gem5 root is.
    config_file = resolve_input(args.config_file)
    src_file = resolve_input(args.src_file)

    # Resolved here rather than as an argparse default: it reads the test's
    # path, which is not known until now.
    if not os.path.exists(config_file):
        print(f"[ERROR] Configuration not found: {config_file}",
              file=sys.stderr)
        return 1
    if not os.path.exists(src_file):
        print(f"[ERROR] Test not found: {src_file}", file=sys.stderr)
        return 1
    if args.suite is None:
        args.suite = default_suite(src_file)
        if args.suite is None:
            return 1

    lang = detect_lang(src_file, args.lang)
    overhead = OVERHEAD_SUITES[args.suite][args.variant][lang]
    print(f"[INFO] Overhead table: {args.suite}/{args.variant}/{lang}")

    build_spec = args.build or GEM5_BUILDS[args.variant]
    gem5_bin = resolve_gem5_bin(build_spec)
    if gem5_bin is None:
        print(f"[ERROR] No gem5 binary found for '{build_spec}'. Looked for "
              f"{', '.join(GEM5_BINARY_NAMES)} in '{build_spec}' and in "
              f"'{os.path.join('build', build_spec)}'.", file=sys.stderr)
        return 1
    if args.build:
        print(f"[INFO] Build: {gem5_bin} (overhead profile: {args.variant})")
    else:
        print(f"[INFO] Build: {args.variant} ({gem5_bin})")

    # The two builds are indistinguishable from the outside, so a mislabelled
    # run would report the wrong NET figures with nothing to show for it.
    patched = build_is_patched(gem5_bin)
    wanted_patched = args.variant == "patch"
    if patched is None:
        print(f"[WARN] Could not read '{gem5_bin}' to check which build it is")
    elif patched != wanted_patched:
        found = "patched" if patched else "stock"
        want = "patched" if wanted_patched else "stock"
        message = (f"'{gem5_bin}' is a {found} build but --variant "
                   f"{args.variant} expects a {want} one")
        if args.skip_build_check:
            print(f"[WARN] {message}. Continuing because --skip-build-check "
                  f"was given, so the NET figures do not apply to this build.")
        else:
            print(f"[ERROR] {message}. Pick the other --variant, point "
                  f"--build at the right build, or pass --skip-build-check.",
                  file=sys.stderr)
            return 1

    program_name = os.path.splitext(os.path.basename(src_file))[0]
    # A plain run leaves its binary in results/m5out/ under the test's name,
    # where a batch could otherwise ask for a folder of the same name.
    for folder in (args.gem5_out_dir, args.results_dir):
        if os.path.exists(folder) and not os.path.isdir(folder):
            print(f"[ERROR] '{folder}' is a file, not a folder. Move it, or "
                  f"pass another --gem5-out-dir or --results-dir.",
                  file=sys.stderr)
            return 1

    try:
        binary = compile_program(src_file, lang, args.gem5_out_dir)
        stats_file = run_gem5(config_file, binary, args.no_trace,
                              program_name, args.gem5_out_dir, gem5_bin,
                              config_args)
        report_file = generate_codelist(binary, program_name,
                                        args.gem5_out_dir)
        metrics = parse_stats(stats_file)
    except RunFailed:
        return 1
    geometry = read_cache_geometry(args.gem5_out_dir)
    # The flags ride along: they are what separates one run of a configuration
    # from another, so a table without them cannot be told apart.
    config_label = " ".join(
        [os.path.basename(config_file)] + list(config_args))
    # Both on the header, so a gathered metrics file says what produced it.
    build_label = f"{gem5_bin}  (overhead: {args.suite}/{args.variant}/{lang})"
    built_patch, current_patch = patch_fingerprint()
    if built_patch:
        build_label += f"  (patch {built_patch})"
    if built_patch and current_patch and built_patch != current_patch:
        print(f"[WARN] {PATCH_FILE} is now {current_patch}, but this gem5 was "
              f"built from {built_patch}. Rebuild the image, or the run does "
              f"not carry the patch you are reading.")
    header = build_table_header(f"gem5 [{args.variant}]", config_label,
                                os.path.basename(src_file), geometry,
                                build_label)
    # The column appears exactly when the run produced the counters, which is
    # to say when a patched build ran. A stock build writes none of them.
    show_cva6 = any(metrics.get(key) is not None
                    for keys in CVA6_EXTRA.values() for key in keys)
    print_table(metrics, overhead, report_file, header, show_cva6)

    # Done last, so the _report.txt copied out already carries the table.
    collect_results(program_name, args.gem5_out_dir, args.results_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
