#!/usr/bin/env python3
"""MinorFlow tracer: reads a gem5 MinorCPU debug trace and writes the JSON the
MinorFlow viewer loads.

The trace is read in two streamed passes. The first finds the clock period
from the gaps between ticks, and the second recovers every instruction that
reached Execute. The JSON holds one record per instruction, with its stage
cycles, cache verdicts, branch outcome, bubble, pre-fetch wait and forwarding
producers, and beside the records the I-cache, D-cache and return address
stack events. Windowing, waits, stalls and drawing live in the viewer.

Usage:
    python3 MinorFlow_tracer.py daxpy_trace.txt -o daxpy.json
    python3 MinorFlow_tracer.py daxpy_trace.txt      # writes daxpy.json
    python3 MinorFlow_tracer.py daxpy_trace.txt --tpc 10000 --strict
"""
import argparse
import bisect
import collections
import json
import math
import os
import re
import sys
import time

# ============================================================================
# 3. Schema and output constants
# ============================================================================

# Bumped with every change a reader would misread.
SCHEMA_VERSION = 7
TOOL = "minorflow_tracer"

# The viewer and the scripts clip and shift a JSON by these lists, so they
# travel in metadata, and check_field_lists holds every record to them. A
# record leaves out a key whose value is null, so RECORD_FIELDS names them all.
RECORD_FIELDS = (
    "id", "pc", "disasm", "fu", "fu_idx", "is_compressed", "src_regs",
    "dest_regs", "line_seq_lo", "line_seq_hi", "wraps_line",
    "fe1_held_lo_cycle", "fe1_held_hi_cycle", "fe1_req_lo_cycle",
    "fe1_req_hi_cycle", "fe1_resp_lo_cycle", "fe1_resp_hi_cycle",
    "ic_miss_lo", "ic_miss_hi", "ic_retry_lo", "ic_retry_hi", "fe2_cycle",
    "dec_cycle", "ex_arrival_cycle", "ex_cycle", "fu_done_cycle",
    "scoreboard_return_cycle", "co_cycle", "flushed", "flush_cycle",
    "is_store", "lsq_push_cycle", "lsq_issue_cycle", "lsq_complete_cycle",
    "dc_miss", "dc_coalesced", "sb_push_cycle", "sb_delete_cycle",
    "collision_wait_cycles", "collision_replay_cycles", "fwd_producer_ids",
    "fwd_producer_regs", "data_arrival_cycle", "data_arrival_producer_id",
    "data_arrival_is_mem", "is_control", "bp_kind", "bp_predicted_taken",
    "bp_outcome", "bp_resolved_taken", "bp_decided_at", "fe1_redirect_cycle",
    "ras_push_cycle", "ras_pop_cycle", "ras_drop_cycle",
    "synthesised_stages", "n_pre_fetch_wait_cycles",
    "pre_fetch_wait_shared_id", "bubble_kind", "bubble_causer_id",
    "n_bubble_cycles", "bubble_shared_id", "bubble_ic_miss",
    "n_redirect_delay_cycles", "caused_bubble_kind",
    "caused_bubble_recovery_id", "n_caused_bubble_cycles",
    "n_caused_bubble_flushed",
)
CYCLE_FIELDS = (
    "fe1_held_lo_cycle", "fe1_held_hi_cycle", "fe1_req_lo_cycle",
    "fe1_req_hi_cycle", "fe1_resp_lo_cycle", "fe1_resp_hi_cycle",
    "fe2_cycle", "dec_cycle", "ex_arrival_cycle", "ex_cycle",
    "fu_done_cycle", "scoreboard_return_cycle", "co_cycle", "flush_cycle",
    "lsq_push_cycle", "lsq_issue_cycle", "lsq_complete_cycle",
    "sb_push_cycle", "sb_delete_cycle", "data_arrival_cycle",
    "fe1_redirect_cycle", "ras_push_cycle", "ras_pop_cycle",
    "ras_drop_cycle",
)
CYCLE_LIST_FIELDS = ("collision_wait_cycles", "collision_replay_cycles")
ID_FIELDS = (
    "data_arrival_producer_id", "pre_fetch_wait_shared_id",
    "bubble_causer_id", "bubble_shared_id", "caused_bubble_recovery_id",
)
ID_LIST_FIELDS = ("fwd_producer_ids",)
EVENT_FIELDS = {
    "ic_events.access_cycles": "cycle",
    "ic_events.miss_cycles": "cycle",
    "ic_events.coalesced_miss_cycles": "cycle",
    "ic_events.blocked_spans": [0, 1],
    "ic_events.charged_spans": [0, 1],
    "dc_events.access_cycles": "cycle",
    "dc_events.miss_cycles": "cycle",
    "dc_events.coalesced_miss_cycles": "cycle",
    "dc_events.store_access_cycles": "cycle",
    "dc_events.store_miss_cycles": "cycle",
    "dc_events.store_coalesced_miss_cycles": "cycle",
    "dc_events.blocked_spans": [0, 1],
    "dc_events.charged_spans": [0, 1],
    "ras_events.push_cycles": "cycle",
    "ras_events.pop_cycles": "cycle",
    "ras_events.drop_cycles": "cycle",
    "ras_events.depth": [0],
}
EVENT_TWIN_FIELDS = {
    "ic_events.access_addrs": "ic_events.access_cycles",
    "ic_events.miss_addrs": "ic_events.miss_cycles",
    "ic_events.coalesced_miss_addrs": "ic_events.coalesced_miss_cycles",
}

# The order README.md documents.
METADATA_KEY_ORDER = (
    "tool", "schema_version", "trace_path", "clock_period", "time_unit",
    "clock_period_source", "forward_delays", "forward_delays_measured",
    "fe1_line_width", "seen_line_families", "record_fields", "cycle_fields",
    "cycle_list_fields", "id_fields", "id_list_fields", "event_fields",
    "event_twin_fields", "degraded", "clipped", "stats",
)
# The streamed loader refuses a JSON whose metadata is not first or whose
# event objects arrive before instructions.
TOP_LEVEL_ORDER = (
    "metadata", "config_params", "symbols", "instructions", "ic_events",
    "dc_events", "ras_events",
)

# The trace does not record gem5's tick rate. time_unit assumes the default
# of 1 ps per tick.
TIME_UNIT = "1ps"

# ============================================================================
# 4. Core constants
# ============================================================================

# Distinct ticks pass 1 reads before it decides. Enough for the commonest gap
# to settle, and it reads only the start of a trace however large.
TPC_SAMPLE_TICKS = 20000

# A divisor of the commonest gap that this many sampled ticks, and this share
# of them, sit on while missing the gap itself is the real period. Factors up
# to TPC_SUBPERIOD_MAX_FACTOR are tried.
TPC_SUBPERIOD_MIN_TICKS = 8
TPC_SUBPERIOD_MIN_SHARE = 0.05
TPC_SUBPERIOD_MAX_FACTOR = 8

# gem5 refuses a forward delay below 1 and a longer gap is a stall, so only
# gaps up to MAX_FORWARD_DELAY count, and fewer than MIN_DELAY_SAMPLES of them
# leave gem5's default of 1.
MAX_FORWARD_DELAY = 16
MIN_DELAY_SAMPLES = 4

# A 32-bit instruction wraps when its bytes pass the end of its fetch line,
# and its low line is normally the line before, so the search back is short.
INSTR_BYTES = 4
WRAP_SEARCH_LINES = 32

# A cap, so a chain of producers that never settles cannot hold up the run.
# Reaching it with the last pass still changing prints a warning.
MAX_HAZARD_PASSES = 10

# The progress line is offered one line in this many plus one, a mask so
# the test stays cheap.
PROGRESS_LINE_MASK = 0x3FFFF

# How gem5 prints RISC-V's zero register in srcRegs and destRegs. A read of
# it never waits on a producer.
ZERO_REGISTER = "z"

# BaseCache::BlockedCause. 0 to 2 are stock, 3 to 5 are the patch's.
BLOCKED_CAUSES = {
    0: "no_mshrs",
    1: "no_wb_buffers",
    2: "no_targets",
    3: "victim_readout",
    4: "fence_flush",
    5: "refill_window",
}

# Execute redirects the front end on these, and Fetch2 squashes its
# predictor history for them on the same tick.
REDIRECTING_BRANCHES = (
    "UnpredictedBranch", "BadlyPredictedBranch", "BadlyPredictedBranchTarget",
)

# The message regexes below match from the start of the message, the text
# after the tick and the object's name. The id, commit and disassembly
# patterns are searched.
RE_TICK = re.compile(r"^\s*(?P<tick>\d+):")
RE_ID_LINE = re.compile(r"\d+/\S+/(?P<line_seq>\d+)")
RE_ID_EXEC = re.compile(r"\d+/\S+/(?P<line_seq>\d+)/(?P<fetch_seq>\d+)\.\d+")
RE_ID_FETCH = re.compile(r"\d+/\S+/(?P<line_seq>\d+)/(?P<fetch_seq>\d+)")
RE_COMPRESSED = re.compile(r"^c[_.]")

# branchPred, stock.
RE_BP_PARAM = re.compile(
    r"(?P<name>local predictor size|local counter bits|global predictor "
    r"size|global counter bits|choice predictor size|choice counter bits|"
    r"instruction shift amount|index mask|BTB entries|RAS size):\s+"
    r"(?P<value>\S+)")

# fetch1, stock.
RE_MINORLINE = re.compile(
    r"MinorLine: id=\S+/\S+/(?P<line_seq>\d+)\s+size=(?P<size>\d+)\s+"
    r"vaddr=0x(?P<vaddr>[0-9a-f]+)(?:\s+paddr=0x(?P<paddr>[0-9a-f]+))?")
RE_FETCH_REQUEST = re.compile(
    r"Issued fetch request to memory: (?P<line>\S+)")
RE_FETCH_RETRY = re.compile(r"recvRetry\b")
RE_BRANCH = re.compile(
    r"Changing stream on branch: (?P<type>\w+) target: \S+ (?P<inst>\S+) "
    r"pc:")

# fetch1, added by MinorCPU_CVA6.patch (fetch1WaitsForIcache).
RE_FETCH_HELD = re.compile(r"Line fetch held, icache busy: (?P<line>\S+)")

# fetch2 and decode, stock.
RE_FETCH2_DECODE = re.compile(r"decoder inst (?P<inst>\S+) pc:")
RE_PASSING = re.compile(
    r"Passing on inst:\s+(?P<inst>\S+)\s+pc:\s+\S+\s+\([^)]+\)")
RE_MICROOP = re.compile(r"Microop decomposition .*? inst: (?P<inst>\S+) pc:")

# execute, stock.
RE_MINORINST = re.compile(
    r"MinorInst: id=\d+/\S+/(?P<line_seq>\d+)/(?P<fetch_seq>\d+)\."
    r"(?P<exec_seq>\d+)\s+addr=(?P<pc>0x[0-9a-f]+)\s+"
    r"inst=\"(?P<disasm>[^\"]+)\"\s+class=(?P<fu>\w+)\s+"
    r"flags=\"(?P<flags>[^\"]*)\"\s+srcRegs=(?P<src>[^ ]*)\s+"
    r"destRegs=(?P<dest>[^ ]*)")
RE_TRYING = re.compile(
    r"Trying to issue inst:\s+(?P<inst>\S+)\s+pc:\s+(?P<pc>\S+)\s+"
    r"\((?P<name>[^)]+)\)\s+to FU:\s*\d+")
RE_ISSUING = re.compile(
    r"Issuing inst:\s+(?P<inst>\S+)\s+pc:\s+(?P<pc>\S+)\s+"
    r"\((?P<name>[^)]+)\)\s+into FU (?P<fu_idx>\d+)")
RE_FLUSH = re.compile(r"Discarding inst: (?P<inst>\S+) pc:")
RE_COMMIT_STALL = re.compile(
    r"Not committing inst:\s+\d+/\S+/\d+/(?P<fetch_seq>\d+)\.\d+.*?"
    r"stalled for (?P<cycles>\d+) more cycles")

# scoreboard, stock.
RE_SCOREBOARD = re.compile(
    r"Marking up inst:\s+(?P<inst>\S+).*returnCycle:\s*(?P<cycle>\d+)")

# lsq and its store buffer, stock.
RE_LSQ_STATE = re.compile(
    r"Setting state from (?P<old>\w+) to (?P<new>\w+) for request: "
    r"(?P<inst>\S+) pc:")
RE_SB_CONSIDER = re.compile(r"Considering request: (?P<inst>\S+) pc:")
RE_SB_DELETE = re.compile(
    r"Deleting request:.*?\d+/\S+/\d+/(?P<fetch_seq>\d+)\.\d+")

# lsq. Store collision cleared is added by MinorCPU_CVA6.patch. The other two
# lines are stock, and only the patch makes them mean a collision.
RE_SB_WAIT = re.compile(r"Load partly satisfied by store buffer")
RE_LSQ_HELD = re.compile(
    r"No matching memory response for inst: (?P<inst>\S+) pc:")
RE_SB_REPLAY = re.compile(r"Store collision cleared, (?P<left>\d+) replay")

# l1icaches and l1dcaches, stock. Only the patch adds causes 3 to 5.
RE_IC_ACCESS = re.compile(
    r"access for \w+ \[(?P<addr>[0-9a-f]+):[0-9a-f]+\] IF "
    r"(?P<verdict>miss|hit)")
RE_DC_ACCESS = re.compile(
    r"access for (?P<command>\w+) \[(?P<addr>[0-9a-f]+):[0-9a-f]+\] "
    r"(?P<verdict>miss|hit)")
# What gem5's demand counters add up (cache/base.cc SUM_DEMAND). An LR, SC or
# AMO is a LoadLockedReq, StoreCondReq or SwapReq, which they leave out.
DEMAND_COMMANDS = frozenset(("ReadReq", "WriteReq", "WriteLineReq",
                             "ReadExReq", "ReadCleanReq", "ReadSharedReq"))
WRITE_COMMANDS = frozenset(("WriteReq", "WriteLineReq", "StoreCondReq",
                            "SwapReq"))
RE_COALESCE = re.compile(r"\w+ coalescing MSHR for ")
RE_CACHE_BLOCK = re.compile(
    r"(?P<edge>Blocking|Unblocking) for cause (?P<cause>\d+)")

# l1icaches and l1dcaches, added by MinorCPU_CVA6.patch: the accept-and-charge
# form of causes 3 and 5.
RE_WINDOW = re.compile(
    r"Window (?P<kind>trigger|overlap) charged (?P<cycles>\d+) cycles")

# ras, stock. A push onto a full stack or a pop off an empty one leaves the
# depth unchanged, so the operation is read from the line, never the depth.
# RAS::squash prints the depth after each repair a stock build makes.
RE_RAS_OP = re.compile(
    r"(?P<op>push|pop): RAS\[\d+\] (?:<=|=>) \S+\. "
    r"Entries used: (?P<used>\d+)")
RE_RAS_SQUASH = re.compile(
    r"RAS::squash Incorrect (?:push|pop)\..*Entries used: (?P<used>\d+)")

# ras, added by MinorCPU_CVA6.patch: with rasNoRecovery, RAS::drop leaves the
# speculative operation in place where RAS::squash would repair it.
RE_RAS_DROP = re.compile(r"RAS::drop leaving speculative op in place")

# ExecAll commit lines from the CPU object, stock.
RE_COMMIT = re.compile(
    r"T0\s+:\s+(?P<pc>0x[0-9a-f]+)(?P<where>[^:]*?):\s+(?P<disasm>.+?)\s+:"
    r"\s+(?P<fu>\w+)(?:.*?FetchSeq=(?P<exec_seq>\d+))?")
RE_COMMIT_UPC = re.compile(r"\.\s*(?P<upc>\d+)\s*$")
RE_COMMIT_SYM = re.compile(r"@(?P<name>\S+?)(?:\+(?P<offset>\d+))?\s*$")

# ============================================================================
# 5. Progress and logging
# ============================================================================

# SHARED BEGIN py-progress

# Needs: sys, time

PROGRESS_INTERVAL_S = 0.25
PLAIN_PROGRESS_INTERVAL_S = 5.0


class Progress:
    """One progress line on stderr: rewritten in place on a terminal, a new
    line every few seconds in a log, and nothing when quiet."""

    def __init__(self, label, total_bytes=0, quiet=False):
        self.label = label
        self.total_bytes = total_bytes
        self.live = not quiet and sys.stderr.isatty()
        self.plain = not quiet and not sys.stderr.isatty()
        self.start = time.time()
        self.last_emit = 0.0
        self.last_plain = self.start
        self.lines = 0
        self.records = 0
        self.bytes_done = 0

    def update(self, lines, records=0, bytes_done=0):
        self.lines, self.records, self.bytes_done = lines, records, bytes_done
        now = time.time()
        if now - self.last_emit < PROGRESS_INTERVAL_S:
            return
        self.last_emit = now
        if self.live:
            sys.stderr.write("\r" + self.message(now) + "   ")
            sys.stderr.flush()
        elif (self.plain
              and now - self.last_plain >= PLAIN_PROGRESS_INTERVAL_S):
            self.last_plain = now
            sys.stderr.write(self.message(now) + "\n")
            sys.stderr.flush()

    def message(self, now):
        # A count not known yet is left out rather than printed as zero.
        parts = [f"{self.lines:,} lines"]
        if self.records:
            parts.append(f"{self.records:,} instructions")
        if self.total_bytes and self.bytes_done:
            share = min(100, int(100 * self.bytes_done / self.total_bytes))
            parts.append(f"{share}%")
        parts.append(f"{now - self.start:.1f}s")
        return f"[{self.label}] " + ", ".join(parts)

    def done(self):
        """The final line, printed once."""
        if self.live:
            sys.stderr.write("\r" + self.message(time.time()) + "   \n")
        elif self.plain:
            sys.stderr.write(self.message(time.time()) + "\n")
        sys.stderr.flush()

# SHARED END py-progress


# SHARED BEGIN py-log

# Needs: sys


def log_info(message):
    print(f"[INFO] {message}", file=sys.stderr)


def log_warn(message):
    print(f"[WARN] {message}", file=sys.stderr)


def log_error(message):
    print(f"[ERROR] {message}", file=sys.stderr)

# SHARED END py-log


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


# ============================================================================
# 6. Input reader
# ============================================================================


class TraceRefused(Exception):
    """The trace cannot give a trustworthy clock period. advice says what
    the reader can do about it."""

    def __init__(self, message, advice):
        super().__init__(message)
        self.advice = advice


def tick_to_cycle(tick, ticks_per_cycle):
    """The cycle a tick falls in. An event part way through a cycle happened
    during that cycle, so this floors."""
    return tick // ticks_per_cycle


def sub_period(ticks, ticks_per_cycle):
    """The coarsest divisor of ticks_per_cycle that enough sampled ticks sit
    on while missing ticks_per_cycle itself, refined to the finest period
    those ticks share, or None when no divisor qualifies."""
    need = max(TPC_SUBPERIOD_MIN_TICKS,
               TPC_SUBPERIOD_MIN_SHARE * len(ticks))
    for factor in range(2, TPC_SUBPERIOD_MAX_FACTOR + 1):
        if ticks_per_cycle % factor:
            continue
        period = ticks_per_cycle // factor
        hits = [tick for tick in ticks
                if tick % ticks_per_cycle and tick % period == 0]
        if len(hits) >= need:
            return math.gcd(ticks_per_cycle,
                            *(tick % ticks_per_cycle for tick in hits))
    return None


def sample_ticks(path, progress):
    """The first TPC_SAMPLE_TICKS distinct ticks of the trace, sorted. Pass 1
    reads only this much, however large the trace."""
    tick_set = set()
    lines = chars = 0
    with open(path, "r", errors="replace") as f:
        for line in f:
            lines += 1
            chars += len(line)
            if not lines & PROGRESS_LINE_MASK:
                progress.update(lines, 0, chars)
            m = RE_TICK.match(line)
            if m:
                tick_set.add(int(m.group("tick")))
                if len(tick_set) >= TPC_SAMPLE_TICKS:
                    break
    progress.lines, progress.bytes_done = lines, chars
    progress.done()
    return sorted(tick_set)


def check_given_tpc(path, ticks, ticks_per_cycle):
    """Refuse a --tpc the sampled ticks contradict. Too large, ticks sit on a
    finer period between its edges, as detection tests. Too small, the ticks
    that sit on it also sit on a multiple of it, so its odd edges never
    happen. Raises TraceRefused."""
    period = sub_period(ticks, ticks_per_cycle)
    if period is not None:
        raise TraceRefused(
            f"--tpc {ticks_per_cycle:,} is too large for {path}: sampled "
            f"ticks sit on multiples of {period:,} between its edges, so "
            f"every cycle number would be {ticks_per_cycle // period} times "
            f"too small.", f"Pass --tpc {period:,}, or leave --tpc out.")
    on_given = sum(1 for tick in ticks if tick % ticks_per_cycle == 0)
    slack = max(TPC_SUBPERIOD_MIN_TICKS, TPC_SUBPERIOD_MIN_SHARE * len(ticks))
    for factor in range(TPC_SUBPERIOD_MAX_FACTOR, 1, -1):
        wider = ticks_per_cycle * factor
        on_wider = sum(1 for tick in ticks if tick % wider == 0)
        # Strictly fewer than slack ticks off the wider edges, since detection
        # takes slack or more as proof of the finer period. Equal would leave
        # a trace both refuse.
        if on_given - on_wider < slack:
            raise TraceRefused(
                f"--tpc {ticks_per_cycle:,} is too small for {path}: "
                f"{on_wider:,} of the {on_given:,} sampled ticks on its edges "
                f"also sit on multiples of {wider:,}, so every cycle number "
                f"would be {factor} times too large.",
                f"Pass --tpc {wider:,}, or leave --tpc out.")


def detect_tpc_streaming(path, progress):
    """Pass 1: ticks per cycle, the commonest gap between adjacent distinct
    ticks in the first TPC_SAMPLE_TICKS of them.

    The commonest gap, not the smallest, because gaps between clock edges are
    whole multiples of the period, while an event scheduled off the clock
    makes a short gap of no meaning. infer_forward_delays takes the smallest
    instead, because a stall only ever lengthens a stage gap. A stall-heavy
    start can still make a multiple of the period the commonest gap, which
    sub_period catches. Raises TraceRefused rather than guessing."""
    ticks = sample_ticks(path, progress)

    # No default: a wrong period rescales every cycle number without a
    # visible sign.
    gaps = collections.Counter(
        b - a for a, b in zip(ticks, ticks[1:]) if b > a)
    if not gaps:
        raise TraceRefused(
            f"Could not find the clock period in {path}: it holds "
            f"{len(ticks)} distinct tick value(s) and no gap between them. "
            f"Every cycle number depends on the period, so the tracer stops "
            f"rather than guessing.",
            "Check that the trace is a gem5 MinorCPU debug trace with tick "
            "prefixes, or pass --tpc.")
    ticks_per_cycle = gaps.most_common(1)[0][0]

    period = sub_period(ticks, ticks_per_cycle)
    if period is not None:
        on_period = sum(1 for tick in ticks
                        if tick % ticks_per_cycle and tick % period == 0)
        raise TraceRefused(
            f"The clock period in {path} is not trustworthy: the commonest "
            f"gap between ticks is {ticks_per_cycle:,}, but {on_period:,} of "
            f"{len(ticks):,} sampled ticks sit on multiples of {period:,} "
            f"between those edges, so the period is probably {period:,} and "
            f"every cycle number would be {ticks_per_cycle // period} times "
            f"too small.", "Pass --tpc to override.")

    off_edge = [tick for tick in ticks if tick % ticks_per_cycle]
    if off_edge:
        shown = ", ".join(f"{tick:,}" for tick in off_edge[:4])
        log_info(f"{len(off_edge):,} of {len(ticks):,} sampled ticks fall "
                 f"between clock edges ({shown}). They are events gem5 "
                 f"scheduled off the clock, and each counts in the cycle it "
                 f"falls in.")
    return ticks_per_cycle


# ============================================================================
# 7. Event extraction
# ============================================================================


def new_exec_entry(cycle, line_seq, pc, disasm):
    """An instruction seen issuing before its MinorInst line, which fills
    the rest."""
    return {
        "cycle": cycle, "line_seq": line_seq, "exec_seq": None, "pc": pc,
        "disasm": disasm, "fu": "", "flags": "", "src": "", "dest": "",
        "predicted_taken": False,
    }


def new_lsq_event():
    """An instruction's LSQ activity. Every LSQ event is a memory access and
    only a store names a Store state, so is_store starts False."""
    return {"push_cycle": None, "issue_cycle": None, "complete_cycle": None,
            "is_store": False}


class LineParser:
    """Pass 2: the events of every trace line, kept per instruction and per
    mechanism. Each line goes to the handler of the object that printed it,
    and the family census falls out of which objects appeared.

    Memory is O(instructions), not O(file): nothing is evicted. A whole run
    measured about 6.3 KiB resident per instruction, 256 MiB for daxpy's
    41,083 and 881 MiB for branch_stress's 145,531, so the viewers' own
    MAX_STREAM_INSTRUCTIONS of 500,000 would need about 3 GiB."""

    # The last part of an object's name, to its handler. Any other object
    # goes to on_cpu, which keeps only ExecAll's commit lines.
    HANDLERS = {
        "fetch1": "on_fetch1",
        "fetch2": "on_fetch2",
        "decode": "on_decode",
        "execute": "on_execute",
        "lsq": "on_lsq",
        "storeBuffer": "on_store_buffer",
        "l1icaches": "on_icache",
        "l1dcaches": "on_dcache",
        "branchPred": "on_branch_predictor",
        "ras": "on_ras",
    }

    # Objects whose lines prove a debug-flag family was captured, by the
    # mechanism name metadata.degraded would otherwise list.
    FAMILIES = {
        "l1icaches": "icache",
        "l1dcaches": "dcache",
        "lsq": "lsq",
        "storeBuffer": "store_buffer",
        "ras": "ras",
    }

    def __init__(self, ticks_per_cycle):
        self.ticks_per_cycle = ticks_per_cycle
        self.handlers = {}
        self.seen_families = set()
        self.n_lines = 0
        self.tick = 0
        self.ends_cleanly = True
        self.n_chars = 0
        self.last_cycle = 0

        self.bp_params = {}
        self.fe1_line_width = None
        self.fe1_resp = {}
        self.fe1_req = {}
        self.fe1_retried = set()
        self.fe1_held = {}
        self.fe1_vaddr = {}
        self.fe1_paddr = {}
        self.retry_pending = False
        self.branch_events = {}
        self.fe2_cycles = {}
        self.last_fe2 = (None, None)
        self.dec_cycles = {}

        self.execute_map = {}
        self.issue_first = {}
        self.issue_ok = {}
        self.issue_fu = {}
        self.flush_cycles_by_id = {}
        self.commit_stall_end = {}
        self.scoreboard_return = {}

        self.lsq_events = {}
        self.sb_events = {}
        self.collision_wait = {}
        self.collision_replay = {}
        self.pending_collision = None
        self.held_by_collision = None
        self.n_unattributed_collisions = 0

        # One access shape for both caches: cycle, addr, is_store, miss and
        # coalesced, in trace order, and demand on the D-cache's.
        self.ic_accesses = []
        self.ic_miss_cycles = set()
        self.dc_accesses = []
        # (cycle, paddr) to the first verdict there, so two fetches sharing
        # a cycle are told apart by address.
        self.ic_verdict_at = {}
        self.blocked_open = {}
        self.blocked_spans = {"icache": [], "dcache": []}
        self.charged_spans = {"icache": [], "dcache": []}
        self.n_blocked_spans_closed_at_end = 0

        self.ras_push = {}
        self.ras_pop = {}
        self.ras_push_cycles = []
        self.ras_pop_cycles = []
        self.ras_drop_cycles = []
        self.ras_depth = []

        self.commits = []
        self.symbol_base = {}

    def feed(self, lines, progress):
        """Read every line of an iterable, a file or a list of strings."""
        ticks_per_cycle = self.ticks_per_cycle
        handlers = self.handlers
        raw = "\n"
        for raw in unglue(lines):
            self.n_lines += 1
            self.n_chars += len(raw)
            if not self.n_lines & PROGRESS_LINE_MASK:
                progress.update(self.n_lines, len(self.execute_map),
                                self.n_chars)
            head, sep, rest = raw.partition(": ")
            if not sep:
                continue
            head = head.strip()
            if not head.isdigit():
                continue
            name, _, message = rest.partition(": ")
            message = message.rstrip()
            tick = int(head)
            cycle = tick_to_cycle(tick, ticks_per_cycle)
            self.tick = tick
            self.last_cycle = cycle
            if self.pending_collision is not None:
                self.settle_collision(cycle, message)
            handler = handlers.get(name)
            if handler is None:
                handler = self.handler_for(name)
            handler(cycle, message)
        # A trace gem5 was killed while writing, or one cut by hand, ends
        # part way through a line.
        self.ends_cleanly = raw.endswith("\n")
        progress.lines, progress.bytes_done = self.n_lines, self.n_chars
        progress.records = len(self.execute_map)
        progress.done()

    def handler_for(self, name):
        """The handler of one object's lines, resolved once per name."""
        part = name.rsplit(".", 1)[-1]
        if part in self.FAMILIES:
            self.seen_families.add(self.FAMILIES[part])
        if part.startswith("scoreboard"):
            handler = self.on_scoreboard
        else:
            handler = getattr(self, self.HANDLERS.get(part, "on_cpu"))
        self.handlers[name] = handler
        return handler

    def finish(self):
        """Close what the end of the trace left open."""
        if self.pending_collision is not None:
            self.n_unattributed_collisions += 1
        for (cache, cause), start in self.blocked_open.items():
            self.blocked_spans[cache].append(
                [start, max(start, self.last_cycle),
                 BLOCKED_CAUSES.get(cause, f"cause_{cause}")])
            self.n_blocked_spans_closed_at_end += 1
        self.blocked_open = {}

    def settle_collision(self, cycle, message):
        """A collision line names no instruction, so the LSQ line on the same
        tick supplies it. A tick that ends without one leaves it
        unattributed."""
        if self.pending_collision != cycle:
            self.pending_collision = None
            self.held_by_collision = None
            self.n_unattributed_collisions += 1
            return
        m = RE_LSQ_HELD.match(message)
        if m:
            ids = RE_ID_EXEC.search(m.group("inst"))
            if ids:
                fetch_seq = int(ids.group("fetch_seq"))
                self.collision_wait.setdefault(fetch_seq, []).append(cycle)
                self.held_by_collision = fetch_seq
                self.pending_collision = None

    def on_branch_predictor(self, cycle, message):
        m = RE_BP_PARAM.match(message)
        if m:
            self.bp_params.setdefault(m.group("name"), m.group("value"))

    def on_fetch1(self, cycle, message):
        if message.startswith("MinorLine: "):
            m = RE_MINORLINE.match(message)
            if m:
                line_seq = int(m.group("line_seq"))
                self.fe1_resp.setdefault(line_seq, cycle)
                self.fe1_vaddr.setdefault(line_seq, int(m.group("vaddr"), 16))
                if m.group("paddr"):
                    self.fe1_paddr.setdefault(line_seq,
                                              int(m.group("paddr"), 16))
                if self.fe1_line_width is None:
                    self.fe1_line_width = int(m.group("size"))
        elif message.startswith("Issued fetch request"):
            m = RE_FETCH_REQUEST.match(message)
            if m:
                self.seen_families.add("fetch")
                ids = RE_ID_LINE.search(m.group("line"))
                if ids:
                    line_seq = int(ids.group("line_seq"))
                    if line_seq not in self.fe1_req:
                        self.fe1_req[line_seq] = cycle
                        if self.retry_pending:
                            self.fe1_retried.add(line_seq)
                self.retry_pending = False
        elif message.startswith("Changing stream on branch"):
            m = RE_BRANCH.match(message)
            if m:
                # An id prints its exec number only once Decode has set it
                # (dyn_inst.cc:71), which a Fetch2 prediction often has not.
                ids = RE_ID_FETCH.search(m.group("inst"))
                if ids:
                    self.branch_events.setdefault(
                        int(ids.group("fetch_seq")), []).append(
                            (cycle, m.group("type")))
        elif message.startswith("Line fetch held"):
            m = RE_FETCH_HELD.match(message)
            if m:
                ids = RE_ID_LINE.search(m.group("line"))
                if ids:
                    self.fe1_held.setdefault(int(ids.group("line_seq")),
                                             cycle)
        elif RE_FETCH_RETRY.match(message):
            self.retry_pending = True

    def on_fetch2(self, cycle, message):
        m = RE_FETCH2_DECODE.match(message)
        if m:
            self.seen_families.add("fetch")
            ids = RE_ID_FETCH.search(m.group("inst"))
            if ids:
                fetch_seq = int(ids.group("fetch_seq"))
                self.fe2_cycles.setdefault(fetch_seq, cycle)
                self.last_fe2 = (cycle, fetch_seq)

    def on_decode(self, cycle, message):
        # A macro-op prints one Microop decomposition line per micro-op
        # instead of Passing on inst, and the first one is its decode.
        m = RE_PASSING.match(message) or RE_MICROOP.match(message)
        if m:
            self.seen_families.add("decode")
            ids = RE_ID_FETCH.search(m.group("inst"))
            if ids:
                self.dec_cycles.setdefault(int(ids.group("fetch_seq")), cycle)

    def on_execute(self, cycle, message):
        if message.startswith("MinorInst: "):
            m = RE_MINORINST.match(message)
            if m:
                self.seen_families.add("minor_inst")
                fetch_seq = int(m.group("fetch_seq"))
                # Each micro-op of an atomic prints its own line. The first
                # one's exec_seq is what ExecAll's merged commit carries.
                earlier = self.execute_map.get(fetch_seq)
                exec_seq = int(m.group("exec_seq"))
                if earlier is not None and earlier["exec_seq"] is not None:
                    exec_seq = earlier["exec_seq"]
                self.execute_map[fetch_seq] = {
                    "cycle": cycle, "line_seq": int(m.group("line_seq")),
                    "exec_seq": exec_seq, "pc": m.group("pc"),
                    "disasm": m.group("disasm"), "fu": m.group("fu"),
                    "flags": m.group("flags"), "src": m.group("src"),
                    "dest": m.group("dest"),
                    "predicted_taken": "predictedTaken" in message,
                }
        elif message.startswith("Trying to issue inst"):
            self.seen_families.add("execute")
            m = RE_TRYING.match(message)
            ids = m and RE_ID_EXEC.search(m.group("inst"))
            if ids:
                fetch_seq = int(ids.group("fetch_seq"))
                self.issue_first.setdefault(fetch_seq, cycle)
                self.execute_map.setdefault(fetch_seq, new_exec_entry(
                    cycle, int(ids.group("line_seq")), m.group("pc"),
                    m.group("name")))
        elif message.startswith("Issuing inst"):
            m = RE_ISSUING.match(message)
            ids = m and RE_ID_EXEC.search(m.group("inst"))
            if ids:
                fetch_seq = int(ids.group("fetch_seq"))
                self.issue_ok.setdefault(fetch_seq, cycle)
                self.issue_fu.setdefault(fetch_seq, int(m.group("fu_idx")))
                entry = self.execute_map.setdefault(fetch_seq, new_exec_entry(
                    cycle, int(ids.group("line_seq")), m.group("pc"),
                    m.group("name")))
                entry["cycle"] = cycle
        elif message.startswith("Discarding inst"):
            self.seen_families.add("execute")
            m = RE_FLUSH.match(message)
            ids = m and RE_ID_EXEC.search(m.group("inst"))
            if ids:
                self.flush_cycles_by_id.setdefault(
                    int(ids.group("fetch_seq")), cycle)
        elif message.startswith("Not committing inst"):
            m = RE_COMMIT_STALL.match(message)
            if m:
                fetch_seq = int(m.group("fetch_seq"))
                end = cycle + int(m.group("cycles"))
                if end > self.commit_stall_end.get(fetch_seq, 0):
                    self.commit_stall_end[fetch_seq] = end

    def on_scoreboard(self, cycle, message):
        m = RE_SCOREBOARD.match(message)
        ids = m and RE_ID_EXEC.search(m.group("inst"))
        if ids:
            self.scoreboard_return.setdefault(int(ids.group("fetch_seq")),
                                              int(m.group("cycle")))

    def on_lsq(self, cycle, message):
        if message.startswith("Setting state from"):
            m = RE_LSQ_STATE.match(message)
            ids = m and RE_ID_EXEC.search(m.group("inst"))
            if ids:
                event = self.lsq_events.setdefault(
                    int(ids.group("fetch_seq")), new_lsq_event())
                old, new = m.group("old"), m.group("new")
                if old == "NotIssued" and new == "InTranslation":
                    event["push_cycle"] = cycle
                if (new in ("RequestIssuing", "StoreBufferIssuing")
                        and event["issue_cycle"] is None):
                    event["issue_cycle"] = cycle
                if new == "Complete":
                    event["complete_cycle"] = cycle
                if "Store" in old or "Store" in new:
                    event["is_store"] = True
        elif RE_SB_WAIT.match(message):
            self.pending_collision = cycle
        elif message.startswith("Store collision cleared"):
            m = RE_SB_REPLAY.match(message)
            if m:
                if self.held_by_collision is None:
                    self.n_unattributed_collisions += 1
                    return
                self.collision_replay.setdefault(
                    self.held_by_collision, []).append(cycle)
                if int(m.group("left")) == 0:
                    self.held_by_collision = None

    def on_store_buffer(self, cycle, message):
        if message.startswith("Considering request"):
            m = RE_SB_CONSIDER.match(message)
            ids = m and RE_ID_EXEC.search(m.group("inst"))
            if ids:
                self.sb_events.setdefault(
                    int(ids.group("fetch_seq")),
                    {"push_cycle": cycle, "delete_cycle": None})
        elif message.startswith("Deleting request"):
            m = RE_SB_DELETE.match(message)
            if m:
                event = self.sb_events.get(int(m.group("fetch_seq")))
                if event is not None:
                    event["delete_cycle"] = cycle

    def on_icache(self, cycle, message):
        if message.startswith("access for "):
            m = RE_IC_ACCESS.match(message)
            if m:
                addr = int(m.group("addr"), 16)
                miss = m.group("verdict") == "miss"
                self.ic_accesses.append({
                    "cycle": cycle, "addr": addr, "is_store": False,
                    "miss": miss, "coalesced": False})
                self.ic_verdict_at.setdefault((cycle, addr), miss)
                if miss:
                    self.ic_miss_cycles.add(cycle)
        else:
            self.on_cache_state(cycle, message, "icache", self.ic_accesses)

    def on_dcache(self, cycle, message):
        if message.startswith("access for "):
            m = RE_DC_ACCESS.match(message)
            if m:
                command = m.group("command")
                self.dc_accesses.append({
                    "cycle": cycle, "addr": int(m.group("addr"), 16),
                    "is_store": command in WRITE_COMMANDS,
                    "demand": command in DEMAND_COMMANDS,
                    "miss": m.group("verdict") == "miss",
                    "coalesced": False})
        else:
            self.on_cache_state(cycle, message, "dcache", self.dc_accesses)

    def on_cache_state(self, cycle, message, cache, accesses):
        """Coalescing, blocking and charged windows, alike in both caches."""
        if " coalescing MSHR for " in message:
            if RE_COALESCE.match(message):
                # Printed straight after its own access line on the same
                # tick, so it marks the newest unmarked miss of this cycle.
                for access in reversed(accesses):
                    if access["cycle"] != cycle:
                        break
                    if access["miss"] and not access["coalesced"]:
                        access["coalesced"] = True
                        break
        elif "for cause " in message:
            m = RE_CACHE_BLOCK.match(message)
            if m:
                # A block set or cleared between clock edges first meets the
                # CPU at the next edge, where BaseCache's curCycle() also
                # rounds it for blockedCycles.
                edge = -(-self.tick // self.ticks_per_cycle)
                key = (cache, int(m.group("cause")))
                if m.group("edge") == "Blocking":
                    self.blocked_open.setdefault(key, edge)
                else:
                    start = self.blocked_open.pop(key, None)
                    if start is not None:
                        self.blocked_spans[cache].append(
                            [start, edge,
                             BLOCKED_CAUSES.get(key[1], f"cause_{key[1]}")])
        elif message.startswith("Window "):
            m = RE_WINDOW.match(message)
            if m:
                self.charged_spans[cache].append(
                    [cycle, cycle + int(m.group("cycles")),
                     f"window_{m.group('kind')}"])

    def on_ras(self, cycle, message):
        # The RAS names no instruction. A push or pop belongs to the call or
        # return Fetch2 decoded on the same tick.
        m = RE_RAS_OP.match(message)
        if m:
            is_push = m.group("op") == "push"
            (self.ras_push_cycles if is_push
             else self.ras_pop_cycles).append(cycle)
            self.ras_depth.append([cycle, int(m.group("used"))])
            fe2_cycle, fetch_seq = self.last_fe2
            if fe2_cycle == cycle:
                (self.ras_push if is_push else self.ras_pop).setdefault(
                    fetch_seq, cycle)
            return
        m = RE_RAS_SQUASH.match(message)
        if m:
            self.ras_depth.append([cycle, int(m.group("used"))])
        elif RE_RAS_DROP.match(message):
            self.ras_drop_cycles.append(cycle)

    def on_cpu(self, cycle, message):
        if "T0" not in message:
            return
        m = RE_COMMIT.search(message)
        if not m:
            return
        where = m.group("where")
        symbol = RE_COMMIT_SYM.search(where.strip())
        if symbol:
            base = int(m.group("pc"), 16) - int(symbol.group("offset") or 0)
            # The lowest base wins when a name is seen with inconsistent
            # offsets, so a symbol cannot drift upwards through the trace.
            name = symbol.group("name")
            if base < self.symbol_base.get(name, base + 1):
                self.symbol_base[name] = base
        upc = RE_COMMIT_UPC.search(where)
        exec_seq = m.group("exec_seq")
        self.commits.append({
            "cycle": cycle, "pc": m.group("pc"),
            "disasm": m.group("disasm").strip(), "fu": m.group("fu"),
            "exec_seq": int(exec_seq) if exec_seq is not None else None,
            "upc": int(upc.group("upc")) if upc else None,
        })


# execute.cc prints "Committing no cost inst" without a newline, so the next
# line gem5 prints, a Discarding inst among them, is glued behind it.
RE_GLUED = re.compile(r"(?<=[^\d\s])(?=\d+: [\w.]+: )")


def unglue(lines):
    """The trace's lines with each glued line split back out."""
    for raw in lines:
        if "Committing no cost inst" in raw:
            yield from RE_GLUED.split(raw)
        else:
            yield raw


def parse_trace(path, ticks_per_cycle, progress):
    """Pass 2 over the whole trace, streamed."""
    parser = LineParser(ticks_per_cycle)
    with open(path, "r", errors="replace") as f:
        parser.feed(f, progress)
    parser.finish()
    return parser


# ============================================================================
# 8. Record assembly
# ============================================================================


def infer_forward_delays(parser):
    """gem5's three forward delays, each the smallest observed gap between
    its two stages, with whether it was measured or left at gem5's default.

    The smallest, not the commonest, and deliberately: a forward delay is
    fixed and stalls only lengthen the observed gap, so the floor of the
    distribution is the parameter. The commonest gap measures how stalled
    the pipeline usually is, 4 cycles for fe1_fe2 and 15 for dec_ex on daxpy
    where all three delays are 1."""
    gaps = {"fe1_fe2": [], "fe2_dec": [], "dec_ex": []}
    for fetch_seq, entry in parser.execute_map.items():
        fe1 = parser.fe1_resp.get(entry["line_seq"])
        fe2 = parser.fe2_cycles.get(fetch_seq)
        dec = parser.dec_cycles.get(fetch_seq)
        issue = parser.issue_first.get(fetch_seq)
        if fe1 is not None and fe2 is not None:
            gaps["fe1_fe2"].append(fe2 - fe1)
        if fe2 is not None and dec is not None:
            gaps["fe2_dec"].append(dec - fe2)
        if dec is not None and issue is not None:
            gaps["dec_ex"].append(issue - dec)
    delays, measured = {}, {}
    for key, values in gaps.items():
        valid = [gap for gap in values if 1 <= gap <= MAX_FORWARD_DELAY]
        measured[key] = len(valid) >= MIN_DELAY_SAMPLES
        delays[key] = min(valid) if measured[key] else 1
    return delays, measured


def bind_commits(commits, exec_seqs=frozenset()):
    """A function giving a record's commit cycle from ExecAll's commits.

    gem5 splits a RISC-V atomic into micro-ops committing on different
    cycles, and ExecAll's FetchSeq= holds the execSeqNum (decode.cc:122),
    which is consecutive across them, so they merge into the first. A record
    binds exactly by its exec_seq, else to the next unused commit at its PC
    that no record's exec_seq names, and a flushed record never binds by
    PC."""
    merged = []
    for commit in commits:
        prev = merged[-1] if merged else None
        if (prev is not None and prev["pc"] == commit["pc"]
                and commit["upc"] is not None
                and prev["last_upc"] is not None
                and commit["upc"] == prev["last_upc"] + 1
                and commit["exec_seq"] is not None
                and prev["last_exec_seq"] is not None
                and commit["exec_seq"] == prev["last_exec_seq"] + 1):
            prev["cycle"] = commit["cycle"]
            prev["last_upc"] = commit["upc"]
            prev["last_exec_seq"] = commit["exec_seq"]
        else:
            merged.append(dict(commit, last_upc=commit["upc"],
                               last_exec_seq=commit["exec_seq"],
                               used=False))
    commit_by_exec_seq = {commit["exec_seq"]: commit for commit in merged
                          if commit["exec_seq"] is not None}
    by_pc = {}
    for commit in merged:
        by_pc.setdefault(commit["pc"], []).append(commit)
    cursor = {}

    def commit_cycle(entry, flushed):
        commit = commit_by_exec_seq.get(entry["exec_seq"])
        if commit is not None:
            commit["used"] = True
            return commit["cycle"]
        if flushed:
            return None
        pool = by_pc.get(entry["pc"], [])
        at = cursor.get(entry["pc"], 0)
        while at < len(pool) and (pool[at]["used"]
                                  or pool[at]["exec_seq"] in exec_seqs):
            at += 1
        cycle = None
        if at < len(pool):
            cycle = pool[at]["cycle"]
            pool[at]["used"] = True
            at += 1
        cursor[entry["pc"]] = at
        return cycle

    commit_cycle.merged = merged
    return commit_cycle


def bind_ic_miss(parser, counts, line_seq, req_cycle):
    """Whether a line's fetch missed the I-cache, None when not observed.
    Keyed on the line's physical address as well as the cycle, so two
    fetches sharing a cycle cannot take each other's verdict."""
    if "icache" not in parser.seen_families or req_cycle is None:
        return None
    paddr = parser.fe1_paddr.get(line_seq)
    if paddr is not None:
        verdict = parser.ic_verdict_at.get((req_cycle, paddr))
        if verdict is not None:
            return verdict
    # Counted, because this is the only path where a second fetch on the
    # cycle could supply the verdict.
    counts["n_ic_bound_without_addr"] += 1
    return req_cycle in parser.ic_miss_cycles


def dc_verdict(lsq_event, dc_by_cycle):
    """(dc_miss, dc_coalesced) for an instruction's own D-cache access, each
    None without an access at its issue cycle.

    Prefers accesses matching the instruction's direction, so a store sharing
    its issue cycle with a load normally cannot take the load's verdict. That
    is a preference, not a guarantee: with no same-direction access on the
    cycle any access there is taken. Measured on daxpy the fallback fires 0
    times against 12,290 direction matches, so daxpy never exercises it."""
    if lsq_event is None or lsq_event["issue_cycle"] is None:
        return None, None
    accesses = dc_by_cycle.get(lsq_event["issue_cycle"])
    if not accesses:
        return None, None
    own = [access for access in accesses
           if access["is_store"] == lsq_event["is_store"]] or accesses
    return (any(access["miss"] and not access["coalesced"] for access in own),
            any(access["miss"] and access["coalesced"] for access in own))


def wrap_fields(parser, counts, entry, is_compressed):
    """The low line of a 32-bit instruction whose bytes pass the end of its
    fetch line, or None when it does not wrap or the low line was not
    found with a request."""
    width = parser.fe1_line_width
    if width is None or is_compressed:
        return None
    # CVA6Flow_tracer.py tests against FETCH_BYTES instead. The two agree
    # only when fetch1LineWidth is 4.
    if (int(entry["pc"], 16) & (width - 1)) + INSTR_BYTES <= width:
        return None
    vaddr = parser.fe1_vaddr.get(entry["line_seq"])
    if vaddr is None:
        return None
    first = entry["line_seq"] - 1
    for line_seq in range(first, max(-1, first - WRAP_SEARCH_LINES), -1):
        if parser.fe1_vaddr.get(line_seq) == vaddr - width:
            break
    else:
        counts["n_wrap_partner_not_found"] += 1
        return None
    if parser.fe1_req.get(line_seq) is None:
        counts["n_wrap_partner_without_request"] += 1
        return None
    return line_seq


def branch_model(entry, flushed, events):
    """The branch fields of a record, from gem5's flags and the last stream
    change Fetch1 made for it."""
    flags = entry["flags"]
    is_control = "IsControl" in flags
    is_cond = "IsCondControl" in flags
    last_type = events[-1][1] if events else None
    kind = None
    if is_control:
        is_direct = "IsDirectControl" in flags
        if "IsReturn" in flags:
            kind = "Return"
        elif "IsCall" in flags:
            kind = "CallDirect" if is_direct else "CallIndirect"
        elif is_direct:
            kind = "DirectCond" if is_cond else "DirectUncond"
        elif "IsIndirectControl" in flags:
            kind = "IndirectCond" if is_cond else "IndirectUncond"
    outcome = resolved_taken = decided_at = None
    if is_control and not flushed:
        if last_type == "UnpredictedBranch":
            outcome = "unpred"
        elif last_type is not None and last_type.startswith("Badly"):
            outcome = "mispred"
        else:
            # Execute never redirected it: no stream change at all, or
            # only Fetch2's own prediction, which stood.
            outcome = "correct"
        decided_at = "fe2" if outcome == "correct" else "ex"
        if not is_cond or last_type == "UnpredictedBranch":
            resolved_taken = True
        elif last_type == "BadlyPredictedBranch":
            resolved_taken = False
        elif outcome == "mispred":
            resolved_taken = True
        else:
            resolved_taken = entry["predicted_taken"]
    return {
        "is_control": is_control,
        "bp_kind": kind,
        "bp_predicted_taken": entry["predicted_taken"],
        "bp_outcome": outcome,
        "bp_resolved_taken": resolved_taken,
        "bp_decided_at": decided_at,
        # Fetch1 changed stream one latch hop after Fetch2 predicted or
        # Execute redirected, so this is Fetch1's cycle, not the resolution.
        "fe1_redirect_cycle": events[-1][0] if events else None,
    }


def build_record(parser, counts, context, fetch_seq):
    """One schema 7 record in the order of README.md, every key present
    until the writer leaves out the null ones. The whole-run fields of
    section 9 start null and are filled there."""
    entry = parser.execute_map[fetch_seq]
    delays = context["delays"]
    flushed = fetch_seq in parser.flush_cycles_by_id

    # The commit log fills a record that never printed its own text, and one
    # Minor printed as (invalid), which it prints for every No_OpClass
    # instruction, a fence among them (minor/dyn_inst.cc).
    enrich = context["enrich_by_pc"].get(entry["pc"])
    disasm, fu = entry["disasm"], entry["fu"]
    if enrich is not None and enrich["disasm"]:
        if disasm == "(invalid)":
            disasm = ""
        disasm = disasm or enrich["disasm"]
        fu = fu or enrich["fu"]
    is_compressed = bool(RE_COMPRESSED.match(disasm))

    line_seq = entry["line_seq"]
    fe1_seen = parser.fe1_resp.get(line_seq)
    fe2_seen = parser.fe2_cycles.get(fetch_seq)
    dec_seen = parser.dec_cycles.get(fetch_seq)
    issue_first = parser.issue_first.get(fetch_seq)
    issue_ok = parser.issue_ok.get(fetch_seq)
    returned = parser.scoreboard_return.get(fetch_seq)

    # A stage the trace did not show is synthesised from its neighbours
    # through the forward delays, forwards from Fetch1 where that was seen
    # and backwards from the issue attempt otherwise.
    fe2 = fe2_seen
    if fe2 is None and fe1_seen is not None:
        fe2 = fe1_seen + delays["fe1_fe2"]
    dec = dec_seen
    if dec is None and fe2 is not None:
        dec = fe2 + delays["fe2_dec"]
    if dec is None:
        base = issue_first if issue_first is not None else (
            issue_ok if issue_ok is not None else entry["cycle"])
        dec = base - delays["dec_ex"]
    if fe2 is None:
        fe2 = dec - delays["fe2_dec"]
    fe1 = fe1_seen if fe1_seen is not None else fe2 - delays["fe1_fe2"]
    ex = issue_ok if issue_ok is not None else entry["cycle"]
    co = context["commit_cycle"](entry, flushed)

    # A load's unit is done when its request completes, anything else when
    # the scoreboard says its result returns, then raised to the end of any
    # commit stall.
    lsq = parser.lsq_events.get(fetch_seq)
    if (lsq is not None and not lsq["is_store"]
            and lsq["complete_cycle"] is not None):
        fu_done = lsq["complete_cycle"]
    elif returned is not None:
        fu_done = returned
    else:
        fu_done = co if co is not None else ex + 1
    stall_end = parser.commit_stall_end.get(fetch_seq)
    if stall_end is not None:
        fu_done = max(fu_done, stall_end if co is None else min(stall_end, co))

    low = wrap_fields(parser, counts, entry, is_compressed)
    wraps = low is not None
    if wraps:
        # A wrap's fetch starts at its low line, so the low line leads.
        req_lo = parser.fe1_req[low]
        miss_lo = bind_ic_miss(parser, counts, low, req_lo)
        held_lo = parser.fe1_held.get(low)
        retry_lo = low in parser.fe1_retried
        req_hi = parser.fe1_req.get(line_seq)
        resp_lo, resp_hi = parser.fe1_resp.get(low), fe1
        miss_hi = bind_ic_miss(parser, counts, line_seq, req_hi)
        held_hi = parser.fe1_held.get(line_seq)
        retry_hi = line_seq in parser.fe1_retried
    else:
        req_lo = parser.fe1_req.get(line_seq)
        miss_lo = bind_ic_miss(parser, counts, line_seq, req_lo)
        held_lo = parser.fe1_held.get(line_seq)
        retry_lo = line_seq in parser.fe1_retried
        req_hi = resp_hi = miss_hi = held_hi = retry_hi = None
        resp_lo = fe1
    # A hold only counts before the line's own request.
    if held_lo is not None and (req_lo is None or held_lo >= req_lo):
        held_lo = None
    if held_hi is not None and (req_hi is None or held_hi >= req_hi):
        held_hi = None

    synthesised = [stage for stage, seen in (
        ("fe1", fe1_seen), ("fe2", fe2_seen), ("dec", dec_seen))
        if seen is None]
    if returned is None and entry["dest"].strip():
        synthesised.append("fu_done")

    dc_miss, dc_coalesced = dc_verdict(lsq, context["dc_by_cycle"])
    sb = parser.sb_events.get(fetch_seq)
    branch = branch_model(entry, flushed,
                          parser.branch_events.get(fetch_seq))
    if (not flushed and not branch["is_control"]
            and branch["fe1_redirect_cycle"] is not None
            and "Serialize" in entry["flags"]):
        context["serialise_ids"].add(fetch_seq)

    return {
        "id": fetch_seq,
        "pc": entry["pc"],
        "disasm": disasm,
        "fu": fu,
        "fu_idx": parser.issue_fu.get(fetch_seq),
        "is_compressed": is_compressed,
        "src_regs": entry["src"],
        "dest_regs": entry["dest"],
        "line_seq_lo": low if wraps else line_seq,
        "line_seq_hi": line_seq if wraps else None,
        "wraps_line": wraps,
        "fe1_held_lo_cycle": held_lo,
        "fe1_held_hi_cycle": held_hi,
        "fe1_req_lo_cycle": req_lo,
        "fe1_req_hi_cycle": req_hi,
        "fe1_resp_lo_cycle": resp_lo,
        "fe1_resp_hi_cycle": resp_hi,
        "ic_miss_lo": miss_lo,
        "ic_miss_hi": miss_hi,
        "ic_retry_lo": retry_lo,
        "ic_retry_hi": retry_hi,
        "fe2_cycle": fe2,
        "dec_cycle": dec,
        "ex_arrival_cycle": dec + delays["dec_ex"],
        "ex_cycle": ex,
        "fu_done_cycle": fu_done,
        "scoreboard_return_cycle": returned,
        "co_cycle": co,
        "flushed": flushed,
        "flush_cycle": parser.flush_cycles_by_id.get(fetch_seq),
        "is_store": lsq["is_store"] if lsq is not None else None,
        "lsq_push_cycle": lsq["push_cycle"] if lsq is not None else None,
        "lsq_issue_cycle": lsq["issue_cycle"] if lsq is not None else None,
        "lsq_complete_cycle": (lsq["complete_cycle"] if lsq is not None
                               else None),
        "dc_miss": dc_miss,
        "dc_coalesced": dc_coalesced,
        "sb_push_cycle": sb["push_cycle"] if sb is not None else None,
        "sb_delete_cycle": sb["delete_cycle"] if sb is not None else None,
        "collision_wait_cycles": parser.collision_wait.get(fetch_seq),
        "collision_replay_cycles": parser.collision_replay.get(fetch_seq),
        "fwd_producer_ids": None,
        "fwd_producer_regs": None,
        "data_arrival_cycle": None,
        "data_arrival_producer_id": None,
        "data_arrival_is_mem": None,
        **branch,
        "ras_push_cycle": parser.ras_push.get(fetch_seq),
        "ras_pop_cycle": parser.ras_pop.get(fetch_seq),
        "ras_drop_cycle": None,
        "synthesised_stages": synthesised,
        "n_pre_fetch_wait_cycles": None,
        "pre_fetch_wait_shared_id": None,
        "bubble_kind": None,
        "bubble_causer_id": None,
        "n_bubble_cycles": None,
        "bubble_shared_id": None,
        "bubble_ic_miss": None,
        "n_redirect_delay_cycles": None,
        "caused_bubble_kind": None,
        "caused_bubble_recovery_id": None,
        "n_caused_bubble_cycles": None,
        "n_caused_bubble_flushed": None,
    }


def build_records(parser, delays):
    """Every record in id order, the ids of the records that end in a
    serialise flush, and the assembly counters."""
    counts = {"n_ic_bound_without_addr": 0, "n_wrap_partner_not_found": 0,
              "n_wrap_partner_without_request": 0}
    enrich_by_pc = {}
    for commit in parser.commits:
        enrich_by_pc.setdefault(commit["pc"], commit)
    dc_by_cycle = {}
    for access in parser.dc_accesses:
        dc_by_cycle.setdefault(access["cycle"], []).append(access)
    context = {
        "delays": delays, "enrich_by_pc": enrich_by_pc,
        "dc_by_cycle": dc_by_cycle, "serialise_ids": set(),
        "commit_cycle": bind_commits(parser.commits, {
            entry["exec_seq"] for entry in parser.execute_map.values()
            if entry["exec_seq"] is not None}),
    }
    records = [build_record(parser, counts, context, fetch_seq)
               for fetch_seq in sorted(parser.execute_map)]
    return records, context["serialise_ids"], counts


# ============================================================================
# 9. Whole-run derived fields
# ============================================================================


def split_regs(text):
    return [reg.strip() for reg in text.split(",")
            if reg.strip() and reg.strip() != "-"]


def forwarding_producers(records):
    """For each source register, the latest earlier record writing it that
    was not flushed. Flushed consumers keep their producers, since the viewer
    draws their arrows in the flushed colour."""
    last_writer = {}
    for rec in records:
        ids, regs = [], []
        for reg in split_regs(rec["src_regs"]):
            if reg == ZERO_REGISTER or reg in regs:
                continue
            producer = last_writer.get(reg)
            if producer is not None:
                ids.append(producer)
                regs.append(reg)
        rec["fwd_producer_ids"] = ids or None
        rec["fwd_producer_regs"] = regs or None
        if not rec["flushed"]:
            for reg in split_regs(rec["dest_regs"]):
                if reg != ZERO_REGISTER:
                    last_writer[reg] = rec["id"]


def data_hazards(records):
    """Move fu_done_cycle past the latest forwarded operand's arrival, to a
    fixed point, since a moved producer can move its consumers. Returns the
    passes taken and whether the last one changed nothing."""
    by_id = {rec["id"]: rec for rec in records}
    original = {}
    passes, changed = 0, True
    while changed and passes < MAX_HAZARD_PASSES:
        passes += 1
        changed = False
        for rec in records:
            if not rec["fwd_producer_ids"]:
                continue
            latest, producer_id = rec["ex_cycle"], None
            for candidate in rec["fwd_producer_ids"]:
                arrived = by_id[candidate]["fu_done_cycle"] + 1
                if arrived > latest:
                    latest, producer_id = arrived, candidate
            if producer_id is None:
                continue
            # The unit's own latency is kept from before any move, and a
            # record never finishes after it commits.
            unit_latency = (original.setdefault(rec["id"],
                                                rec["fu_done_cycle"])
                            - rec["ex_cycle"])
            moved = latest + unit_latency
            if rec["co_cycle"] is not None:
                moved = min(moved, rec["co_cycle"])
            if moved > rec["fu_done_cycle"]:
                producer = by_id[producer_id]
                rec["fu_done_cycle"] = moved
                rec["data_arrival_cycle"] = latest
                rec["data_arrival_producer_id"] = producer_id
                rec["data_arrival_is_mem"] = (
                    producer["lsq_issue_cycle"] is not None
                    and producer["is_store"] is not True)
                changed = True
    return passes, not changed


def tag_bubbles(records, serialise_ids, ic_miss_cycles):
    """The bubble before each record that resumed after a redirect or a
    serialise flush, on both ends, then shared with the records fetched on
    the same request as the one that resumed."""
    commits = sorted((rec for rec in records
                      if not rec["flushed"] and rec["co_cycle"] is not None),
                     key=lambda rec: (rec["co_cycle"], rec["id"]))
    position = {rec["id"]: at for at, rec in enumerate(commits)}
    misses = sorted(set(ic_miss_cycles))
    bubbles, caused = {}, {}
    for rec in records:
        is_branch = rec["bp_outcome"] is not None
        is_flush = not is_branch and rec["id"] in serialise_ids
        if not (is_branch or is_flush):
            continue
        at = position.get(rec["id"])
        if at is None or at + 1 >= len(commits):
            continue
        recovery = commits[at + 1]
        start, end = rec["fe1_redirect_cycle"], recovery["fe1_req_lo_cycle"]
        if start is None or end is None:
            continue
        refill = rec["bp_outcome"] in ("mispred", "unpred")
        gap = max(0, end - start)
        first_miss = bisect.bisect_left(misses, start)
        ic_miss = (refill and gap > 0 and first_miss < len(misses)
                   and misses[first_miss] <= end)
        kind = ("flush" if is_flush else rec["bp_outcome"] if refill
                else "pred_taken")
        # On a clash the later redirect caused the refill, so it keeps it.
        # Only a refill or a flush pays Execute's transit back to Fetch1.
        held = bubbles.get(recovery["id"])
        if held is None or start > held["start"]:
            bubbles[recovery["id"]] = {
                "kind": kind, "causer": rec["id"], "gap": gap,
                "start": start, "ic_miss": ic_miss,
                "delay": (max(0, start - rec["co_cycle"])
                          if refill or is_flush else 0),
            }
        caused[rec["id"]] = (recovery["id"], gap, kind)
    # On a clash one bubble has one owner, so the losing causer names none.
    caused = {causer: entry for causer, entry in caused.items()
              if bubbles[entry[0]]["causer"] == causer}

    owners = {}
    for rec in records:
        bubble = bubbles.get(rec["id"])
        if (not rec["flushed"] and bubble is not None
                and rec["fe1_req_lo_cycle"] is not None
                and (bubble["gap"] >= 1 or bubble["delay"] >= 1)):
            owners.setdefault(rec["fe1_req_lo_cycle"], rec["id"])
    shared = {}
    for rec in records:
        if rec["id"] in bubbles or rec["flushed"]:
            continue
        fetches = [rec["fe1_req_lo_cycle"], rec["fe1_req_hi_cycle"]]
        for cycle in fetches:
            owner = owners.get(cycle) if cycle is not None else None
            if owner is not None:
                shared[rec["id"]] = owner
                bubbles[rec["id"]] = bubbles[owner]
                break

    flushed_ids = [rec["id"] for rec in records if rec["flushed"]]
    for rec in records:
        bubble = bubbles.get(rec["id"])
        if bubble is not None:
            rec["bubble_kind"] = bubble["kind"]
            rec["bubble_causer_id"] = bubble["causer"]
            rec["n_bubble_cycles"] = bubble["gap"]
            rec["bubble_shared_id"] = shared.get(rec["id"])
            rec["bubble_ic_miss"] = bubble["ic_miss"]
            rec["n_redirect_delay_cycles"] = bubble["delay"]
        if rec["id"] in caused:
            recovery, gap, kind = caused[rec["id"]]
            low, high = sorted((rec["id"], recovery))
            rec["caused_bubble_kind"] = kind
            rec["caused_bubble_recovery_id"] = recovery
            rec["n_caused_bubble_cycles"] = gap
            rec["n_caused_bubble_flushed"] = (
                bisect.bisect_left(flushed_ids, high)
                - bisect.bisect_right(flushed_ids, low))


def last_request_cycle(rec):
    """The request of a record's last fetch line, the high one on a wrap."""
    if rec["wraps_line"] and rec["fe1_req_hi_cycle"] is not None:
        return rec["fe1_req_hi_cycle"]
    return rec["fe1_req_lo_cycle"]


def pre_fetch_waits(records):
    """The cycles each fetch waited beyond the run's shortest gap between
    two fetch requests, measured from the previous non-flushed record's last
    request, and shared across the records one request fetched. A wrap's own
    wait between its two requests is its high-line wait, so a record fetched
    on that high request waits nothing more."""
    shortest, prev = None, None
    for rec in records:
        if rec["flushed"]:
            continue
        req = rec["fe1_req_lo_cycle"]
        prev_req = last_request_cycle(prev) if prev is not None else None
        if req is not None and prev_req is not None and req > prev_req:
            gap = req - prev_req
            shortest = gap if shortest is None else min(shortest, gap)
        prev = rec
    # A run with no two requests to compare has no shortest gap, and 1 is
    # the least any fetch can follow another by.
    shortest = shortest or 1

    last = group_first = None
    for rec in records:
        req = rec["fe1_req_lo_cycle"]
        if req is None:
            if not rec["flushed"]:
                last = rec
            continue
        if rec["flushed"]:
            rec["n_pre_fetch_wait_cycles"] = 0
            continue
        if last is not None and req == last["fe1_req_lo_cycle"]:
            rec["n_pre_fetch_wait_cycles"] = (
                last["n_pre_fetch_wait_cycles"] or 0)
            rec["pre_fetch_wait_shared_id"] = (
                group_first["id"] if group_first is not None else last["id"])
            last = rec
            continue
        group_first = rec
        if last is None or last["fe1_req_lo_cycle"] is None:
            rec["n_pre_fetch_wait_cycles"] = 0
            last = rec
            continue
        rec["n_pre_fetch_wait_cycles"] = max(
            0, req - last_request_cycle(last) - shortest)
        last = rec


def bind_ras_drops(records, drop_cycles, branch_events):
    """Tie each RAS drop to the youngest record whose speculative push or
    pop it left standing. Returns the drops tied to no record, which
    metadata.stats counts.

    Fetch2 drops on the tick Execute's redirect reaches it, youngest
    prediction first, for every prediction above the redirecting branch
    (BPredUnit::squash), so the candidates are the uncommitted records above
    that branch that pushed or popped before the drop."""
    redirect_at = {}
    for fetch_seq, events in branch_events.items():
        for cycle, kind in events:
            if kind in REDIRECTING_BRANCHES:
                redirect_at.setdefault(cycle, fetch_seq)
    candidates = sorted(
        (rec for rec in records
         if rec["co_cycle"] is None
         and (rec["ras_push_cycle"] is not None
              or rec["ras_pop_cycle"] is not None)),
        key=lambda rec: rec["id"], reverse=True)
    unbound = 0
    for cycle in drop_cycles:
        branch = redirect_at.get(cycle)
        chosen = None
        if branch is not None:
            for rec in candidates:
                if rec["id"] <= branch:
                    break
                operated = min(c for c in (rec["ras_push_cycle"],
                                           rec["ras_pop_cycle"])
                               if c is not None)
                if rec["ras_drop_cycle"] is None and operated <= cycle:
                    chosen = rec
                    break
        if chosen is None:
            unbound += 1
        else:
            chosen["ras_drop_cycle"] = cycle
    return unbound


# ============================================================================
# 10. Degradation census
# ============================================================================

# SHARED BEGIN py-degraded

# Needs: sys, py-log

DEGRADED_NAME_WIDTH = 22


class DegradedCensus:
    """The mechanisms a run could not resolve. They travel in the JSON
    because stderr is gone once a long run has finished."""

    def __init__(self):
        self.entries = []

    def require(self, present, mechanism, effect):
        """Record mechanism as degraded unless present is true."""
        if not present:
            self.entries.append({"mechanism": mechanism, "effect": effect})


def print_degraded(degraded):
    """Printed last, so it is the final thing on screen after a long run."""
    if not degraded:
        log_info("All mechanisms resolved. metadata.degraded is empty.")
        return
    print(f"[DEGRADED] {len(degraded)} mechanism(s) did not resolve. The "
          f"affected fields are null or empty, not measured.",
          file=sys.stderr)
    for entry in degraded:
        print(f"           {entry['mechanism']:<{DEGRADED_NAME_WIDTH}} "
              f"{entry['effect']}", file=sys.stderr)
    print("           metadata.degraded in the JSON records the same list.",
          file=sys.stderr)


def exit_status(degraded, strict, input_noun):
    """3 under --strict when anything is degraded, else 0."""
    if degraded and strict:
        log_error(f"--strict was given and the {input_noun} is degraded, "
                  f"exiting with 3.")
        return 3
    return 0

# SHARED END py-degraded


def build_degraded(parser, records):
    """What the trace carried and whether it is usable, as the entries of
    metadata.degraded. A missing --debug-flags entry is the commonest way to
    a wrong answer, since an absent family reads as a measured zero."""
    seen = parser.seen_families
    census = DegradedCensus()
    census.require(
        "minor_inst" in seen, "minor_inst",
        "No MinorInst line was captured, so src_regs and dest_regs are "
        "empty, no branch is classified, fwd_producer_ids finds no producer, "
        "fu comes only from the commit log, an instruction flushed before "
        "issue has no record and no bubble is found.")
    # A trace without these still yields a record for every instruction, so
    # the figures they feed would otherwise read as measured.
    census.require(
        "fetch" in seen or not records, "fetch",
        "No Fetch line was captured, so fe1_req_lo_cycle, fe1_req_hi_cycle "
        "and ic_miss_lo are null, every fe2_cycle is synthesised, no "
        "prediction or redirect is seen, and no bubble, pre-fetch wait or "
        "fe1_redirect_cycle is found.")
    census.require(
        "decode" in seen or not records, "decode",
        "No Decode line was captured, so every dec_cycle and "
        "ex_arrival_cycle is synthesised from the forward delays and the "
        "Fetch2 and Decode input buffer waits are not measured.")
    census.require(
        "execute" in seen or not records, "execute",
        "No MinorExecute line was captured, so no flush is seen: flushed is "
        "false and flush_cycle null on every record, and a wrong-path "
        "record reads as neither committed nor flushed. fu_idx and "
        "scoreboard_return_cycle are null.")
    census.require(
        "icache" in seen, "icache",
        "ic_miss_lo and ic_miss_hi are null and every ic_events array but "
        "line_paddr is empty, so no I-cache miss can be told from a hit and "
        "bubble_ic_miss is never true.")
    census.require(
        "dcache" in seen, "dcache",
        "dc_miss and dc_coalesced are null and every dc_events array is "
        "empty.")
    # A program with no loads or stores prints no lsq or storeBuffer lines
    # however the flags were set, so their absence only counts when there
    # was memory work. FP and vector loads and stores have op classes too.
    did_memory = any(word in rec["fu"] for rec in records
                     for word in ("Mem", "Load", "Store"))
    census.require(
        "lsq" in seen or not did_memory, "lsq",
        "lsq_push_cycle, lsq_issue_cycle, lsq_complete_cycle, is_store, "
        "dc_miss and dc_coalesced are null on every memory instruction.")
    census.require(
        "store_buffer" in seen or not did_memory, "store_buffer",
        "sb_push_cycle and sb_delete_cycle are null on every store.")
    census.require(
        "ras" in seen, "ras",
        "ras_push_cycle, ras_pop_cycle and ras_drop_cycle are null and every "
        "ras_events array is empty, which RAS in --debug-flags when capturing "
        "fills.")
    # These ask whether what the trace held is usable, which the census of
    # families cannot answer: a truncated trace has every family.
    census.require(
        parser.ends_cleanly, "truncated",
        "The trace's last line has no newline, so the file was cut part way "
        "through a line: instructions stops early, the run's last commits "
        "and its m5_dump_stats are missing, and every count in "
        "metadata.stats is a lower bound.")
    census.require(
        bool(records), "no_records",
        "No instruction was recovered from the trace, so instructions is "
        "empty.")
    census.require(
        any(not rec["flushed"] and rec["co_cycle"] is not None
            for rec in records), "no_commits",
        "No instruction committed, which a whole run of any program cannot "
        "produce, so every co_cycle is null, no bubble is found, "
        "fu_done_cycle and data_arrival_cycle lose the commit stall, and the "
        "trace is most likely truncated or captured without ExecAll.")
    return census.entries


# ============================================================================
# 11. Metadata and writer
# ============================================================================


def build_stats(parser, records, counts, hazard_passes, n_ras_unbound):
    """metadata.stats: whole-run counters, every one prefixed n_."""
    kinds = collections.Counter(
        rec["bubble_kind"] for rec in records
        if rec["bubble_kind"] is not None and rec["bubble_shared_id"] is None)
    return {
        "n_records": len(records),
        "n_committed": sum(1 for rec in records
                           if not rec["flushed"]
                           and rec["co_cycle"] is not None),
        "n_flushed": sum(1 for rec in records if rec["flushed"]),
        "n_unattributed_collisions": parser.n_unattributed_collisions,
        "n_wrap_partner_not_found": counts["n_wrap_partner_not_found"],
        "n_wrap_partner_without_request": counts[
            "n_wrap_partner_without_request"],
        "n_ic_bound_without_addr": counts["n_ic_bound_without_addr"],
        "n_blocked_spans_closed_at_end": parser.n_blocked_spans_closed_at_end,
        "n_ras_drops_unbound": n_ras_unbound,
        "n_hazard_passes": hazard_passes,
        "bubbles": {
            "n_pred_taken": kinds["pred_taken"],
            "n_unpred": kinds["unpred"],
            "n_mispred": kinds["mispred"],
            "n_flush": kinds["flush"],
            "n_flushed": sum(rec["n_caused_bubble_flushed"] or 0
                             for rec in records),
            "n_cycles": sum(rec["n_caused_bubble_cycles"] or 0
                            for rec in records),
        },
    }


def build_metadata(trace_path, ticks_per_cycle, period_source, delays,
                   measured, parser, degraded, stats):
    """metadata, in METADATA_KEY_ORDER."""
    metadata = {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "trace_path": trace_path,
        "clock_period": ticks_per_cycle,
        "time_unit": TIME_UNIT,
        "clock_period_source": period_source,
        "forward_delays": delays,
        "forward_delays_measured": measured,
        "fe1_line_width": parser.fe1_line_width,
        "seen_line_families": sorted(parser.seen_families),
        "record_fields": list(RECORD_FIELDS),
        "cycle_fields": list(CYCLE_FIELDS),
        "cycle_list_fields": list(CYCLE_LIST_FIELDS),
        "id_fields": list(ID_FIELDS),
        "id_list_fields": list(ID_LIST_FIELDS),
        "event_fields": EVENT_FIELDS,
        "event_twin_fields": EVENT_TWIN_FIELDS,
        "degraded": degraded,
        "clipped": None,
        "stats": stats,
    }
    if tuple(metadata) != METADATA_KEY_ORDER:
        raise ValueError("build_metadata drifted from METADATA_KEY_ORDER")
    return metadata


def event_objects(parser):
    """ic_events, dc_events and ras_events, every array sorted by cycle."""
    ic = parser.ic_accesses
    access = sorted([a["cycle"], a["addr"]] for a in ic)
    # miss_cycles counts the misses that opened an MSHR, as gem5's
    # demandMshrMisses does, and a coalesced miss opened none.
    miss = sorted([a["cycle"], a["addr"]] for a in ic
                  if a["miss"] and not a["coalesced"])
    coalesced = sorted([a["cycle"], a["addr"]] for a in ic
                       if a["miss"] and a["coalesced"])
    ic_events = {
        "access_cycles": [cycle for cycle, _ in access],
        "access_addrs": [addr for _, addr in access],
        "miss_cycles": [cycle for cycle, _ in miss],
        "miss_addrs": [addr for _, addr in miss],
        "coalesced_miss_cycles": [cycle for cycle, _ in coalesced],
        "coalesced_miss_addrs": [addr for _, addr in coalesced],
        # Without the address of every line a reader cannot tell which of
        # several accesses on one cycle belongs to a record.
        "line_paddr": sorted([line_seq, paddr] for line_seq, paddr
                             in parser.fe1_paddr.items()),
        "blocked_spans": sorted(parser.blocked_spans["icache"]),
        "charged_spans": sorted(parser.charged_spans["icache"]),
    }
    # From the access log rather than the records, since a store's writeback
    # can retire after commit. A cycle repeats once per access on it.
    dc = {name: [] for name in (
        "access_cycles", "miss_cycles", "coalesced_miss_cycles",
        "store_access_cycles", "store_miss_cycles",
        "store_coalesced_miss_cycles")}
    for a in parser.dc_accesses:
        # An instruction still takes its dc_miss from a non-demand access.
        if not a["demand"]:
            continue
        prefixes = ("", "store_") if a["is_store"] else ("",)
        for prefix in prefixes:
            dc[prefix + "access_cycles"].append(a["cycle"])
            if a["miss"]:
                kind = ("coalesced_miss_cycles" if a["coalesced"]
                        else "miss_cycles")
                dc[prefix + kind].append(a["cycle"])
    dc_events = {name: sorted(cycles) for name, cycles in dc.items()}
    dc_events["blocked_spans"] = sorted(parser.blocked_spans["dcache"])
    dc_events["charged_spans"] = sorted(parser.charged_spans["dcache"])
    ras_events = {
        "push_cycles": parser.ras_push_cycles,
        "pop_cycles": parser.ras_pop_cycles,
        "drop_cycles": parser.ras_drop_cycles,
        "depth": parser.ras_depth,
    }
    return ic_events, dc_events, ras_events


# SHARED BEGIN py-json-writer

# Needs: json, os

JSON_SEPARATORS = (",", ":")
FIELD_LIST_SUFFIXES = {
    "cycle_fields": "_cycle",
    "cycle_list_fields": "_cycles",
    "id_fields": "_id",
    "id_list_fields": "_ids",
}


def check_field_lists(records, metadata, core_names=()):
    """Raise unless metadata.record_fields names every key a record carries,
    in the order records write them, and the other field lists agree with
    it, since the pages and scripts trust the lists. A record may leave out
    any key, which reads as null. core_names are keys copied from a core
    whose suffix means nothing."""
    fields = metadata["record_fields"]
    position = {key: at for at, key in enumerate(fields)}
    if len(position) != len(fields):
        raise ValueError("metadata.record_fields names a key twice")
    for rec in records:
        last = -1
        for key in rec:
            if key not in position:
                raise ValueError(f"record {rec.get('id')} carries {key}, "
                                 f"missing from metadata.record_fields")
            if position[key] < last:
                raise ValueError(f"record {rec.get('id')} writes {key} out "
                                 f"of the order of metadata.record_fields")
            last = position[key]
    for list_name, suffix in FIELD_LIST_SUFFIXES.items():
        listed = metadata[list_name]
        stray = [key for key in listed
                 if key not in position or not key.endswith(suffix)]
        if stray:
            raise ValueError(f"metadata.{list_name} names {stray}, which "
                             f"record_fields lacks or which lack {suffix}")
        # n_ keys are counts, whatever their suffix says.
        unlisted = [key for key in fields if key.endswith(suffix)
                    and not key.startswith("n_") and key not in listed
                    and key not in core_names]
        if unlisted:
            raise ValueError(f"metadata.record_fields names {unlisted}, "
                             f"missing from metadata.{list_name}")
    events = metadata["event_fields"]
    orphans = [twin for twin, base in metadata["event_twin_fields"].items()
               if base not in events]
    if orphans:
        raise ValueError(f"metadata.event_twin_fields pairs {orphans} with "
                         f"an array event_fields does not name")


def row_paths(data):
    """The arrays of objects that leave out their null keys: the records,
    and every event array of objects. metadata keeps its nulls."""
    events = data["metadata"]["event_fields"]
    return {"instructions"} | {
        path for path, where in events.items()
        if isinstance(where, list) and where and isinstance(where[0], str)}


def fits_one_line(values):
    """Scalars, or short tuples of scalars such as spans and depth pairs."""
    return not any(isinstance(value, dict) or (
        isinstance(value, list)
        and any(isinstance(item, (dict, list)) for item in value))
        for value in values)


def _write_value(out, value, path, depth, rows):
    pad = "  " * depth
    if isinstance(value, dict) and value:
        out.write("{\n")
        last = len(value) - 1
        for i, (key, member) in enumerate(value.items()):
            child = f"{path}.{key}" if path else key
            out.write(f"{pad}  {json.dumps(key)}: ")
            _write_value(out, member, child, depth + 1, rows)
            out.write(",\n" if i < last else "\n")
        out.write(pad + "}")
    elif isinstance(value, list) and value and (
            path in rows or not fits_one_line(value)):
        out.write("[\n")
        last = len(value) - 1
        for i, element in enumerate(value):
            if path in rows:
                element = {key: item for key, item in element.items()
                           if item is not None}
            out.write(pad + "  ")
            out.write(json.dumps(element, separators=JSON_SEPARATORS))
            out.write(",\n" if i < last else "\n")
        out.write(pad + "]")
    else:
        out.write(json.dumps(value, separators=JSON_SEPARATORS))


def write_json(path, data):
    """Write data with its keys in order, through a temporary file, so an
    interrupted run never leaves a half JSON under the final name. Objects
    are indented by 2, a record or an event object takes one line, and an
    array of scalars or of short tuples takes one line."""
    rows = row_paths(data)
    partial = path + ".partial"
    try:
        with open(partial, "w") as out:
            _write_value(out, data, "", 0, rows)
            out.write("\n")
        os.replace(partial, path)
    finally:
        if os.path.exists(partial):
            os.remove(partial)

# SHARED END py-json-writer


# ============================================================================
# 12. Command line
# ============================================================================


def build_parser():
    parser = argparse.ArgumentParser(
        description="Read a gem5 MinorCPU debug trace and write the MinorFlow "
                    "JSON the viewer loads.")
    parser.add_argument(
        "trace", help="The gem5 MinorCPU debug trace (.txt, .log or .trace)")
    parser.add_argument(
        "-o", "--out", metavar="PATH",
        help="Where to write the JSON. Defaults to the trace path with its "
             ".txt, .log or .trace extension replaced by .json and _trace "
             "dropped from the file name")
    parser.add_argument("--quiet", action="store_true",
                        help="Leave out the progress line")
    parser.add_argument(
        "--tpc", "--ticks-per-cycle", dest="ticks_per_cycle", type=int,
        metavar="TICKS",
        help="Ticks per CPU cycle, in place of the detected period, for a "
             "trace detection refuses. The start of the trace is still "
             "checked, and a period its ticks show to be a multiple or a "
             "fraction of the real one is refused. At the gem5 default tick "
             "rate, 10000 for the 100 MHz Reference Core and 20000 for a "
             "50 MHz core")
    parser.add_argument(
        "--strict", action="store_true",
        help="Exit with 3 when metadata.degraded is not empty. The JSON is "
             "still written. Use this in batch runs so a degraded trace is "
             "not mistaken for a complete one")
    return parser


def default_out_path(trace):
    """The trace path with its .txt, .log or .trace extension replaced by
    .json and _trace dropped from the file name, the name
    create_all_MinorFlow_jsons.py gives, so daxpy_trace.txt writes
    daxpy.json."""
    folder, name = os.path.split(trace)
    for ext in (".txt", ".log", ".trace"):
        if name.endswith(ext):
            name = name[:-len(ext)]
            break
    at = (name + ".").rfind("_trace.")
    if at > 0:
        name = name[:at] + name[at + len("_trace"):]
    return os.path.join(folder, name + ".json")


def print_warnings(metadata, hazards_converged):
    """What a reader of the JSON should know that is not a degradation."""
    stats = metadata["stats"]
    if stats["n_unattributed_collisions"]:
        log_warn(f"{stats['n_unattributed_collisions']:,} store collision "
                 f"line(s) could not be tied to an instruction, so their "
                 f"collision_wait_cycles and collision_replay_cycles are "
                 f"missing from the records.")
    if stats["n_ras_drops_unbound"]:
        log_warn(f"{stats['n_ras_drops_unbound']:,} RAS drop(s) could not be "
                 f"tied to a record, so they are in ras_events.drop_cycles "
                 f"with no ras_drop_cycle behind them.")
    if stats["n_ic_bound_without_addr"]:
        log_warn(f"{stats['n_ic_bound_without_addr']:,} I-cache verdict(s) "
                 f"were bound by cycle alone, with no paddr for the line. A "
                 f"second fetch on the same cycle could have supplied the "
                 f"verdict.")
    if stats["n_wrap_partner_not_found"]:
        log_warn(f"{stats['n_wrap_partner_not_found']:,} wrapping "
                 f"instruction(s) had no low-line fetch within "
                 f"{WRAP_SEARCH_LINES} lines back, so wraps_line is false on "
                 f"them, they are drawn as single-line fetches, and the low "
                 f"line's I-cache verdict, retry and hold are lost.")
    if stats["n_wrap_partner_without_request"]:
        log_warn(f"{stats['n_wrap_partner_without_request']:,} wrapping "
                 f"instruction(s) found their low line but no fetch request "
                 f"for it, so line_seq_hi is null on them and they are drawn "
                 f"as single-line fetches.")
    measured = metadata["forward_delays_measured"]
    defaulted = [key for key, value in measured.items() if not value]
    if defaulted:
        log_warn(f"Forward delay(s) {', '.join(defaulted)} had fewer than "
                 f"{MIN_DELAY_SAMPLES} observed stage gaps, so they default "
                 f"to 1 rather than being measured, and any stage "
                 f"synthesised from them is an estimate.")
    if not hazards_converged:
        log_warn(f"The forwarding fixed point was still moving fu_done_cycle "
                 f"after {MAX_HAZARD_PASSES} passes, so some data hazards may "
                 f"end early.")
    if not stats["n_records"]:
        log_warn("No MinorCPU instruction was recovered, so the trace may not "
                 "be a gem5 MinorCPU debug trace. Check that it was captured "
                 "with the --debug-flags list in the README, which includes "
                 "Minor and MinorTrace.")


# SHARED BEGIN py-summary

# Needs: re, sys

TIME_UNIT_SECONDS = {"fs": 1e-15, "ps": 1e-12, "ns": 1e-9, "us": 1e-6,
                     "ms": 1e-3, "s": 1.0}


def clock_text(metadata):
    """The Clock period row, from the three keys both JSONs share."""
    period, unit = metadata["clock_period"], metadata["time_unit"]
    text = f"{period:,} units of {unit}"
    match = re.fullmatch(r"(\d+)(fs|ps|ns|us|ms|s)", unit or "")
    if match and period:
        seconds = (period * int(match.group(1))
                   * TIME_UNIT_SECONDS[match.group(2)])
        text += f" ({1 / seconds / 1e6:g} MHz)"
    return f"{text}, {metadata['clock_period_source']}"


def elapsed_text(seconds, input_bytes):
    """The Elapsed row, with the rate the input was read at."""
    rate = input_bytes / (1 << 20) / max(seconds, 1e-9)
    return f"{seconds:.1f}s ({rate:.1f} MiB/s)"


def print_summary_rows(title, rows):
    """The closing summary as aligned label : value rows on stdout, flushed
    so a log capturing both streams keeps it above the degraded report."""
    width = max(len(label) for label, _ in rows)
    print(title)
    for label, value in rows:
        print(f"  {label:<{width}} : {value}")
    sys.stdout.flush()

# SHARED END py-summary


def print_summary(trace, out, metadata, ic_events, elapsed):
    """The closing summary, on stdout so a batch log can keep it apart."""
    stats = metadata["stats"]
    delays = metadata["forward_delays"]
    print_summary_rows("MinorFlow tracer summary", (
        ("Input", trace),
        ("Output", out),
        ("Records", f"{stats['n_records']:,}"),
        ("Committed", f"{stats['n_committed']:,}"),
        ("Flushed", f"{stats['n_flushed']:,}"),
        ("Clock period", clock_text(metadata)),
        ("Forward delays", f"{delays['fe1_fe2']}/{delays['fe2_dec']}/"
                           f"{delays['dec_ex']}"),
        ("I-cache accesses", f"{len(ic_events['access_cycles']):,}"),
        ("I-cache misses", f"{len(ic_events['miss_cycles']):,}"),
        ("Elapsed", elapsed_text(elapsed, os.path.getsize(trace))),
    ))


def main():
    args = build_parser().parse_args()
    if args.ticks_per_cycle is not None and args.ticks_per_cycle < 1:
        log_error(f"--tpc must be at least 1, not {args.ticks_per_cycle}.")
        return 2
    if not os.path.isfile(args.trace):
        log_error(f"Trace not found: {args.trace}")
        print("        Check the path and try again.", file=sys.stderr)
        return 1
    total_bytes = os.path.getsize(args.trace)
    log_info(f"Reading {args.trace} ({human(total_bytes)})")
    started = time.time()

    if args.ticks_per_cycle is not None:
        ticks_per_cycle, period_source = args.ticks_per_cycle, "option"
        log_info("Pass 1 of 2: checking the period --tpc gives against the "
                 "start of the trace.")
        try:
            check_given_tpc(args.trace, sample_ticks(
                args.trace, Progress("INFO", total_bytes, args.quiet)),
                ticks_per_cycle)
        except TraceRefused as refusal:
            log_error(str(refusal))
            print(f"        {refusal.advice}", file=sys.stderr)
            return 2
    else:
        log_info("Pass 1 of 2: finding the clock period.")
        try:
            ticks_per_cycle = detect_tpc_streaming(
                args.trace, Progress("INFO", total_bytes, args.quiet))
        except TraceRefused as refusal:
            log_error(str(refusal))
            print(f"        {refusal.advice}", file=sys.stderr)
            return 2
        period_source = "detected"
    log_info(f"Pass 2 of 2: reading the instructions, at "
             f"{ticks_per_cycle:,} ticks per cycle ({period_source}).")
    parser = parse_trace(args.trace, ticks_per_cycle,
                         Progress("INFO", total_bytes, args.quiet))

    delays, measured = infer_forward_delays(parser)
    records, serialise_ids, counts = build_records(parser, delays)
    ic_events, dc_events, ras_events = event_objects(parser)
    forwarding_producers(records)
    hazard_passes, hazards_converged = data_hazards(records)
    tag_bubbles(records, serialise_ids, ic_events["miss_cycles"])
    pre_fetch_waits(records)
    n_ras_unbound = bind_ras_drops(records, ras_events["drop_cycles"],
                                   parser.branch_events)

    degraded = build_degraded(parser, records)
    stats = build_stats(parser, records, counts, hazard_passes, n_ras_unbound)
    try:
        metadata = build_metadata(args.trace, ticks_per_cycle, period_source,
                                  delays, measured, parser, degraded, stats)
        data = {
            "metadata": metadata,
            "config_params": parser.bp_params,
            "symbols": sorted([base, name] for name, base
                              in parser.symbol_base.items()),
            "instructions": records,
            "ic_events": ic_events,
            "dc_events": dc_events,
            "ras_events": ras_events,
        }
        if tuple(data) != TOP_LEVEL_ORDER:
            raise ValueError("main drifted from TOP_LEVEL_ORDER")
        check_field_lists(records, metadata)
    except ValueError as err:
        log_error(f"The JSON was not written: {err}.")
        return 1

    out = args.out or default_out_path(args.trace)
    log_info(f"Writing {out}")
    write_json(out, data)

    print_warnings(metadata, hazards_converged)
    print_summary(args.trace, out, metadata, ic_events,
                  time.time() - started)
    print_degraded(degraded)
    return exit_status(degraded, args.strict, "trace")


if __name__ == "__main__":
    sys.exit(main())
