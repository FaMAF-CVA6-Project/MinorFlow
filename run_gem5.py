#!/usr/bin/env python3
"""
Run a gem5 RISC-V simulation and consolidate the metrics.
Accepts both C (.c) and assembly (.S/.s/.asm) programs. The input type is
detected from the extension and can be forced with --lang.
"""
import sys
import os
import subprocess
import re
import argparse

# ==============================================================================
# GLOBAL CONFIGURATION
# ==============================================================================
GEM5_ROOT = os.getcwd()
GCC_CMD = "riscv64-unknown-elf-gcc"
OBJDUMP_CMD = "riscv64-unknown-elf-objdump"
GEM5_BIN = "./build/RISCV/gem5.opt"
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

# ==============================================================================
# OVERHEAD PROFILES (CVA6 configuration)
# ==============================================================================
OVERHEAD_PROFILES = {
    "c": {
        "numCycles":        0,
        "numInsts":         0,
        "icache_miss":      0,
        "dcache_miss":      0,
        "icache_access":    0,
        "dcache_access":    0,
        "branch_pred":      0,
        "branch_miss":      0,
        "simSeconds":       0.0,   # in seconds
    },
    "asm": {
        "numCycles":        0,
        "numInsts":         0,
        "icache_miss":      0,
        "dcache_miss":      0,
        "icache_access":    0,
        "dcache_access":    0,
        "branch_pred":      0,
        "branch_miss":      0,
        "simSeconds":       0.0,   # in seconds
    },
}

# ==============================================================================
# METRICS MAP
# ==============================================================================
METRICS_MAP = {
    "numCycles":         r"cores\.core\.numCycles",
    "numInsts":          r"cores\.core\.commitStats0\.numInsts\s",
    "icache_miss":       r"l1icaches\.overallMisses::total",
    "dcache_miss_read":  r"l1dcaches\.ReadReq.misses::total",
    "dcache_miss_write": r"l1dcaches\.WriteReq.misses::total",
    "icache_access":     r"l1icaches\.overallAccesses::total",
    "dcache_access":     r"l1dcaches\.overallAccesses::total",
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


def compile_program(src_file, lang):
    """Compile src_file according to its type (C or asm). Return the binary path."""
    source_dir = os.path.dirname(src_file)
    base_name = os.path.splitext(os.path.basename(src_file))[0]
    bin_file = os.path.join(source_dir, base_name) if source_dir else base_name

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
            print("[ERROR]", result.stderr)
            sys.exit(1)
    except FileNotFoundError:
        print(f"[ERROR] Compiler not found: {GCC_CMD}")
        sys.exit(1)

    return bin_file


def run_gem5(config_file, bin_file, no_trace, program_name):
    out_dir = "results"
    os.makedirs(out_dir, exist_ok=True)

    stats_path = os.path.join(out_dir, "stats.txt")
    if os.path.exists(stats_path):
        os.remove(stats_path)

    cmd = [GEM5_BIN]

    # Unless '--no-trace' is set, add the requested debug flags.
    if not no_trace:
        trace_file = f"{program_name}_trace.txt"
        print(f"[INFO] Enabling detailed debug traces in: "
              f"{os.path.join(out_dir, trace_file)}")
        cmd.extend([
            "--debug-flags=Minor,MinorTrace,MinorTiming,CacheAll,ExecAll,"
            "Fetch,Decode,IEW,Commit,LSQ,Scoreboard,Writeback",
            f"--debug-file={trace_file}",
        ])

    cmd.extend(["-d", out_dir, config_file, bin_file])

    print(f"[INFO] Running gem5 simulation using '{config_file}'")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("[ERROR]", result.stderr)
        sys.exit(1)
    return stats_path


def generate_and_show_codelist(bin_file, program_name):
    out_dir = "results"
    os.makedirs(out_dir, exist_ok=True)
    list_file = os.path.join(out_dir, f"{program_name}.list")
    clean_file = os.path.join(out_dir, f"{program_name}_clean.txt")

    print(f"[INFO] Generating disassembled code in: {list_file}")

    cmd = [OBJDUMP_CMD, "-d", "-S", "-l", bin_file]

    try:
        with open(list_file, "w") as f:
            subprocess.run(cmd, stdout=f, check=True)
    except subprocess.CalledProcessError as e:
        print("[ERROR]", e)
        sys.exit(1)

    print("\n" + "=" * 70)
    print("DISASSEMBLED CODE")
    print("=" * 70)

    try:
        with open(list_file, "r") as f, open(clean_file, "w") as f_clean:
            for line in f:
                print(line, end='')
                f_clean.write(line)
                if "jal" in line and "<m5_dump_stats>" in line:
                    break
    except FileNotFoundError:
        print(f"[WARN] Could not read the generated file {list_file}")
        return None

    print("\n" + "=" * 70 + "\n")
    print(f"[INFO] Clean file saved in: {clean_file}")
    return clean_file


def parse_stats(stats_path):
    print("[INFO] Extracting statistics")
    results = {key: 0.0 for key in METRICS_MAP}

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

    # Consolidate D-Cache misses (read + write).
    results["dcache_miss"] = (results.get("dcache_miss_read", 0) +
                              results.get("dcache_miss_write", 0))

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


def print_table(results, overhead, clean_file=None):
    output_buffer = []

    output_buffer.append("\n" + "=" * 70)
    output_buffer.append("RESULTS TABLE")
    output_buffer.append("=" * 70)
    output_buffer.append(f"{'METRIC':<25} | {'OFFICIAL':>15} | {'NET':>15}")
    output_buffer.append("=" * 70)

    keys_order = ["numCycles", "numInsts", "icache_miss", "dcache_miss",
                  "icache_access", "dcache_access", "branch_pred",
                  "branch_miss", "simSeconds", "ipc"]

    clean_array_official = []
    clean_array_corrected = []

    # Pre-compute the corrected IPC (with overhead removed).
    raw_insts = results.get("numInsts", 0)
    net_insts = max(0, raw_insts - overhead.get("numInsts", 0))

    raw_cycles = results.get("numCycles", 1)
    net_cycles = max(
        1, raw_cycles - overhead.get("numCycles", 0))  # avoid div/0

    corrected_ipc = net_insts / net_cycles if net_cycles > 0 else 0

    for key in keys_order:
        val_official = results.get(key, 0)
        label = PRETTY_NAMES.get(key, key)
        ovh = overhead.get(key, 0)

        if key == "ipc":
            val_corrected = corrected_ipc
        else:
            val_corrected = max(0, val_official - ovh)

        if key == "simSeconds":
            val_off_us = val_official * 1_000_000
            val_cor_us = val_corrected * 1_000_000
            clean_array_official.append(round(val_off_us))
            clean_array_corrected.append(round(val_cor_us))
            fmt_off = f"{int(val_off_us)}"
            fmt_cor = f"{int(val_cor_us)}"
        elif key == "ipc":
            clean_array_official.append(round(val_official, 4))
            clean_array_corrected.append(round(val_corrected, 4))
            fmt_off = f"{val_official:.4f}"
            fmt_cor = f"{val_corrected:.4f}"
        else:
            clean_array_official.append(int(val_official))
            clean_array_corrected.append(int(val_corrected))
            fmt_off = f"{int(val_official)}"
            fmt_cor = f"{int(val_corrected)}"

        output_buffer.append(f"{label:<25} | {fmt_off:>15} | {fmt_cor:>15}")

    output_buffer.append("=" * 70 + "\n")
    output_buffer.append(f"Clean result (OFFICIAL):  {clean_array_official}")
    output_buffer.append(
        f"Clean result (NET):       {clean_array_corrected}\n")

    for line in output_buffer:
        print(line)

    if clean_file and os.path.exists(clean_file):
        try:
            with open(clean_file, "a") as f_clean:
                f_clean.write("\n\n")
                for line in output_buffer:
                    f_clean.write(line + "\n")
            print(f"[INFO] Metrics successfully consolidated in: {clean_file}")
        except Exception as e:
            print(f"[WARN] Could not save the metrics to the file: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run a gem5 RISC-V simulation (C or assembly) and "
                    "consolidate reports.")
    parser.add_argument("config_file",
                        help="Path to the gem5 configuration file (.py)")
    parser.add_argument("src_file",
                        help="Path to the program: C (.c) or assembly (.S/.s/.asm)")
    parser.add_argument("--lang", choices=["c", "asm"], default="auto",
                        help="Force the input type and overhead profile. "
                             "Defaults to detection by extension.")
    parser.add_argument("--no-trace", action="store_true",
                        help="Disable collection of detailed debug traces.")

    args = parser.parse_args()

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

    program_name = os.path.splitext(os.path.basename(src_file))[0]

    binary = compile_program(src_file, lang)
    stats_file = run_gem5(config_file, binary, args.no_trace, program_name)

    clean_file = generate_and_show_codelist(binary, program_name)

    metrics = parse_stats(stats_file)
    print_table(metrics, overhead, clean_file)
