#!/usr/bin/env python3
"""MinorFlow tracer: gem5 MinorCPU debug trace -> compact JSON for the viewer.

Windowing, bubble/stall/forwarding analysis and rendering all live in the viewer.

Usage:
    python3 minorflow_tracer.py trace.txt -o trace.json
    python3 minorflow_tracer.py trace.txt           # writes trace.json
    python3 minorflow_tracer.py trace.txt --stats    # also print a summary
"""
import time
import math
import os
import sys
import re
import json
import argparse

# ==============================================================================
# Regexes
# ==============================================================================
RE_TICK = re.compile(r'^\s*(\d+):')
RE_BP = re.compile(
    r'branchPred:\s+(local predictor size|local counter bits|global predictor '
    r'size|global counter bits|choice predictor size|choice counter bits|'
    r'instruction shift amount|index mask|BTB entries|RAS size):\s+(\S+)')
RE_MINORLINE = re.compile(
    r'fetch1: MinorLine: id=\S+/\S+/(\d+)\s+size=(\d+)\s+vaddr=0x([0-9a-f]+)')
RE_FETCHREQ = re.compile(r'fetch1: Issued fetch request to memory: (\S+)')
RE_FETCHRETRY = re.compile(r'fetch1: recvRetry\b')
RE_FETCHHELD = re.compile(r'fetch1: Line fetch held, icache busy: (\S+)')
RE_ID_LINE = re.compile(r'\d+/\S+/(\d+)')                       # -> lineSeq
# -> tid,line,fseq
RE_ID_FULL = re.compile(r'(\d+)/\S+/(\d+)/(\d+)\.\d+')
# decoder/passing (no .exec)
RE_ID_F2 = re.compile(r'(\d+)/\S+/(\d+)/(\d+)')
RE_ICACHE = re.compile(
    r'l1icaches: access for \w+ \[[0-9a-f]+:[0-9a-f]+\] IF (miss|hit)')
RE_DCACHE = re.compile(
    r'l1dcaches: access for (\w+) \[[0-9a-f]+:[0-9a-f]+\] (miss|hit)')
RE_LSQ = re.compile(
    r'lsq: Setting state from (\w+) to (\w+) for request: (\S+) pc:')
RE_SBCONS = re.compile(r'storeBuffer: Considering request: (\S+) pc:')
RE_SBDEL = re.compile(
    r'storeBuffer: Deleting request:.*?(\d+/\S+/\d+/(\d+)\.\d+)')
RE_F2DEC = re.compile(r'fetch2: decoder inst (\S+) pc:')
RE_MINORINST = re.compile(
    r'execute: MinorInst: id=\d+/\S+/(\d+)/(\d+)\.(\d+)\s+addr=(0x[0-9a-f]+)\s+'
    r'inst="([^"]+)"\s+class=(\w+)\s+flags="([^"]*)"\s+srcRegs=([^ ]*)\s+destRegs=([^ ]*)')
RE_SCOREBOARD = re.compile(
    r'scoreboard\d+: Marking up inst:\s+(\S+).*returnCycle:\s*(\d+)')
RE_PASSING = re.compile(
    r'decode: Passing on inst:\s+(\S+)\s+pc:\s+\S+\s+\([^)]+\)')
RE_TRYING = re.compile(
    r'Trying to issue inst:\s+(\S+)\s+pc:\s+(\S+)\s+\(([^)]+)\)\s+to FU:\s*(\d+)')
RE_ISSUING = re.compile(
    r'Issuing inst:\s+(\S+)\s+pc:\s+(\S+)\s+\(([^)]+)\)\s+into FU (\d+)')
RE_BRANCH = re.compile(
    r'Changing stream on branch: (\w+) target: (\S+) (\S+) pc:')
RE_DISCARD = re.compile(r'execute: Discarding inst: (\S+) pc:')
RE_COMMIT = re.compile(
    r'T0\s+:\s+(0x[0-9a-f]+)([^:]*?):\s+(.+?)\s+:\s+(\w+)'
    r'(?:.*?FetchSeq=(\d+))?')
RE_COMMIT_UPC = re.compile(r'\.\s*(\d+)\s*$')
RE_COMMIT_SYM = re.compile(r'@(\S+?)(?:\+(\d+))?\s*$')
RE_COMMITSTALL = re.compile(
    r'Not committing inst:\s+\d+/\S+/(\d+)/(\d+)\.(\d+).*?'
    r'stalled for (\d+) more cycles')
RE_DCCOALESCE = re.compile(r'l1dcaches: \w+ coalescing MSHR for ')
RE_ICCOALESCE = re.compile(r'l1icaches: \w+ coalescing MSHR for ')

# Added by MinorCPU_CVA6.patch. A colliding load waits in the LSQ while the
# store buffer drains, then pays a restart penalty. Neither line names its
# instruction, so the id comes from the request the LSQ reports on the tick.
RE_SBWAIT = re.compile(r'lsq: Load partly satisfied by store buffer')
RE_SBREPLAY = re.compile(r'lsq: Store collision cleared, (\d+) replay')
RE_LSQ_HELD = re.compile(
    r'lsq: No matching memory response for inst: (\S+) pc:')

# Cache port blocking, by cause. Stock gem5 emits it, but only the patch gives
# the L1D causes that dominate. The spans are in CPU cycles, so their total
# runs a little above the cache's own blockedCycles stat.
RE_CACHEBLOCK = re.compile(
    r'(l1[id]caches): (Blocking|Unblocking) for cause (\d+)')

# Return address stack. push and pop are stock and carry the depth. drop is
# the rasNoRecovery path on a squash, leaving the speculative op in place.
RE_RAS_PUSH = re.compile(r'ras: push: RAS\[\d+\] <= \S+\. Entries used: (\d+)')
RE_RAS_POP = re.compile(r'ras: pop: RAS\[\d+\] => \S+\. Entries used: (\d+)')
RE_RAS_DROP = re.compile(r'ras: RAS::drop leaving speculative op in place')

RE_WINDOW = re.compile(
    r'(l1[id]caches): Window (trigger|overlap) charged (\d+) cycles')

# BaseCache::BlockedCause. 0 to 2 are stock, 3 to 5 are the patch's.
BLOCKED_CAUSES = {
    0: 'no_mshrs',
    1: 'no_wb_buffers',
    2: 'no_targets',
    3: 'victim_readout',
    4: 'fence_flush',
    5: 'refill_window',
}

RE_COMPRESSED = re.compile(r'^c[_.]')


class Progress:
    """In-place stderr progress reporter, throttled to a few updates/second."""

    def __init__(self, label, total_bytes=0, enabled=True):
        self.label = label
        self.total_bytes = total_bytes
        self.enabled = enabled and sys.stderr.isatty()
        # Off a TTY, fall back to periodic newline updates.
        self.force_plain = enabled and not sys.stderr.isatty()
        self.start = time.time()
        self.last_emit = 0.0
        self.lines = 0
        self.insts = 0

    def update(self, lines, insts, bytes_done=0, final=False):
        self.lines = lines
        self.insts = insts
        now = time.time()
        if not final and (now - self.last_emit) < 0.25:
            return
        self.last_emit = now
        elapsed = now - self.start
        pct = ''
        if self.total_bytes and bytes_done:
            pct = f" · {min(100, int(100 * bytes_done / self.total_bytes))}%"
        msg = (f"[{self.label}] {lines:,} lines · {insts:,} insts"
               f"{pct} · {elapsed:.1f}s")
        if self.enabled:
            sys.stderr.write('\r' + msg + '   ')
            sys.stderr.flush()
        elif self.force_plain and (final or int(elapsed) % 5 == 0):
            sys.stderr.write(msg + '\n')
            sys.stderr.flush()

    def done(self):
        self.update(self.lines, self.insts, final=True)
        if self.enabled:
            sys.stderr.write('\n')
            sys.stderr.flush()


TPC_SAMPLE_TICKS = 20000


def detect_tpc_streaming(path, progress=None, sample=TPC_SAMPLE_TICKS):
    """Pass 1: ticks-per-cycle = the MODE of positive deltas between unique
    adjacent tick values."""
    tick_set = set()
    lines = 0
    bytes_done = 0
    with open(path, 'r', errors='replace') as f:
        for line in f:
            lines += 1
            bytes_done += len(line)
            m = RE_TICK.match(line)
            if m:
                tick_set.add(int(m.group(1)))
                if sample and len(tick_set) >= sample:
                    break
            if progress is not None and (lines & 0x3FFFF) == 0:
                progress.update(lines, 0, bytes_done)
    if progress is not None:
        progress.update(lines, 0, bytes_done, final=True)

    ticks = sorted(tick_set)
    tpc = 10000
    if len(ticks) > 1:
        delta_count = {}
        for i in range(1, len(ticks)):
            d = ticks[i] - ticks[i - 1]
            if d > 0:
                delta_count[d] = delta_count.get(d, 0) + 1
        best_delta, best_count = 0, 0
        for d, c in delta_count.items():
            if c > best_count:
                best_count, best_delta = c, d
        if best_delta > 0:
            tpc = best_delta
    return tpc


def round_half_up(x):
    """Match JS Math.round (round half UP, not banker's rounding)."""
    return math.floor(x + 0.5)


def infer_forward_delays(execute_map, fetch1_map, fetch2_map, decode_map, issue_first):
    """Infer MinorCPU forward delays from the minimum observed stage gap.
    Falls back to gem5 default 1 when fewer than 4 valid samples."""
    d_f1f2, d_f2dec, d_dectr = [], [], []
    for seq, ex in execute_map.items():
        f1 = fetch1_map.get(ex['lineSeq'])
        f2 = fetch2_map.get(seq)
        dc = decode_map.get(seq)
        tr = issue_first.get(seq)
        if f1 is not None and f2 is not None:
            d_f1f2.append(f2 - f1)
        if f2 is not None and dc is not None:
            d_f2dec.append(dc - f2)
        if dc is not None and tr is not None:
            d_dectr.append(tr - dc)

    def pick(deltas, fallback):
        valid = [d for d in deltas if 1 <= d <= 16]
        if len(valid) < 4:
            return fallback
        return min(valid)

    return pick(d_f1f2, 1), pick(d_f2dec, 1), pick(d_dectr, 1)


def parse(line_source, tpc, progress=None, total_bytes=0):
    """Pass 2: build per-instruction records from an iterable of trace lines.
    line_source may be a file handle or a list of strings, and tpc must already
    be detected via detect_tpc_streaming."""
    # ---- Pass 2: event-by-event extraction ---------------------------------
    fetch1_map = {}     # lineSeq -> cycle of MinorLine response
    fetch1_req = {}     # lineSeq -> cycle of "Issued fetch request"
    fetch1_retried = set()   # lineSeqs whose request was a retry after recvRetry
    fetch1_held = {}    # lineSeq -> first cycle held at a busy icache
    # recvRetry seen, next Issued fetch request is the retry
    retry_pending = [False]
    fetch1_vaddr = {}   # lineSeq -> vaddr base
    fetch2_map = {}     # fetchSeq -> cycle of "decoder inst"
    decode_map = {}     # fetchSeq -> cycle of "Passing on inst"
    # fetchSeq -> {cycle, lineSeq, pc, instr, fu, flags, src, dest, predictedTaken}
    execute_map = {}
    issue_first = {}    # fetchSeq -> first "Trying to issue" cycle
    issue_ok = {}       # fetchSeq -> "Issuing inst" cycle
    issue_fu = {}       # fetchSeq -> FU index
    scoreboard_map = {}  # fetchSeq -> returnCycle
    branch_events = {}  # fetchSeq -> [{cycle, type, target}, ...]
    discard_map = {}    # fetchSeq -> discard cycle
    lsq_events = {}     # fetchSeq -> {pushCycle, issueCycle, completeCycle, isStore}
    storebuf_events = {}  # fetchSeq -> {pushCycle, deleteCycle}
    ic_miss_cycles = set()
    ic_hit_cycles = set()
    ic_miss_log = []
    ic_hit_log = []
    dcache_by_cycle = {}  # cycle -> {miss, isWrite, coalesced}
    collision_wait = {}   # fetchSeq -> [cycles held by a store collision]
    collision_replay = {}  # fetchSeq -> [cycles of the restart penalty]
    pending_collision = [None]   # cycle whose collision still needs its id
    held_by_collision = [None]   # fetchSeq the replay countdown belongs to
    collision_unresolved = [0]   # waits that found no instruction to hang on
    ras_push = {}         # fetchSeq -> cycle its call pushed a return address
    ras_pop = {}          # fetchSeq -> cycle its return popped one
    ras_drop = {}         # fetchSeq -> cycle its squash left the stack alone
    ras_depth = []        # [[cycle, entries used], ...] after each push or pop
    ras_drop_cycles = []  # every drop, including any with no id to hang it on
    last_f2 = [None, None]       # (cycle, fetchSeq) of the newest fetch2 line
    last_discard = [None, None]  # (cycle, fetchSeq) of the newest discard line
    blocked_open = {}     # (cache, cause) -> cycle the block began
    blocked_spans = {}    # cache -> {cause name: [[start, end], ...]}
    charge_spans = {}     # cache -> {window kind: [[start, end], ...]}
    commit_list = []    # {cycle, pc, instr, fu, fetchSeq, upc}
    exmin_map = {}
    # symbol name -> lowest base address, harvested from the exec trace.
    sym_base = {}
    observed_line_size = [None]   # boxed so inner assignment is visible
    branch_pred_info = {}

    has_minor_execute = [False]

    line_no = 0
    bytes_done = 0
    for raw in line_source:
        line_no += 1
        bytes_done += len(raw)
        if progress is not None and (line_no & 0x3FFFF) == 0:
            progress.update(line_no, len(execute_map), bytes_done)
        l = raw.strip()
        if not l:
            continue
        tm = RE_TICK.match(l)
        if not tm:
            continue
        cycle = round_half_up(int(tm.group(1)) / tpc)

        # A collision line names no instruction, so the id comes from the
        # request the LSQ reports holding on the same tick.
        if pending_collision[0] is not None:
            if pending_collision[0] != cycle:
                pending_collision[0] = None
                held_by_collision[0] = None
                collision_unresolved[0] += 1
            elif 'No matching memory response' in l:
                mm = RE_LSQ_HELD.search(l)
                if mm:
                    mmm = RE_ID_FULL.search(mm.group(1))
                    if mmm:
                        fseq = int(mmm.group(3))
                        collision_wait.setdefault(fseq, []).append(cycle)
                        held_by_collision[0] = fseq
                        pending_collision[0] = None

        m = RE_BP.search(l)
        if m:
            if m.group(1) not in branch_pred_info:
                branch_pred_info[m.group(1)] = m.group(2)
            continue

        m = RE_MINORLINE.search(l)
        if m:
            line_seq = int(m.group(1))
            if line_seq not in fetch1_map:
                fetch1_map[line_seq] = cycle
            if line_seq not in fetch1_vaddr:
                fetch1_vaddr[line_seq] = int(m.group(3), 16)
            if observed_line_size[0] is None:
                observed_line_size[0] = int(m.group(2))
            continue

        if RE_FETCHRETRY.search(l):
            retry_pending[0] = True
            continue

        if 'icache busy' in l:
            m = RE_FETCHHELD.search(l)
            if m:
                mm = RE_ID_LINE.search(m.group(1))
                if mm:
                    line_seq = int(mm.group(1))
                    if line_seq not in fetch1_held:
                        fetch1_held[line_seq] = cycle
                continue

        m = RE_FETCHREQ.search(l)
        if m:
            mm = RE_ID_LINE.search(m.group(1))
            if mm:
                line_seq = int(mm.group(1))
                if line_seq not in fetch1_req:
                    fetch1_req[line_seq] = cycle
                    if retry_pending[0]:
                        fetch1_retried.add(line_seq)
            retry_pending[0] = False
            continue

        m = RE_ICACHE.search(l)
        if m:
            if m.group(1) == 'miss':
                ic_miss_cycles.add(cycle)
                ic_miss_log.append([cycle, False])   # [cycle, coalesced]
            else:
                ic_hit_cycles.add(cycle)
                ic_hit_log.append(cycle)
            continue

        if RE_ICCOALESCE.search(l):
            # Printed in the same tick, straight after the access it belongs to,
            # so it marks the most recent unmarked miss of this cycle.
            for e in reversed(ic_miss_log):
                if e[0] != cycle:
                    break
                if not e[1]:
                    e[1] = True
                    break
            continue

        m = RE_DCACHE.search(l)
        if m:
            dcache_by_cycle.setdefault(cycle, []).append({
                'miss': m.group(2) == 'miss',
                'isWrite': m.group(1).startswith('Write'),
                'coalesced': False,
            })
            continue

        if RE_DCCOALESCE.search(l):
            # Attach to the most recent unmarked miss of this cycle: the
            # coalescing line follows its own access line in the same tick.
            for e in reversed(dcache_by_cycle.get(cycle, [])):
                if e['miss'] and not e['coalesced']:
                    e['coalesced'] = True
                    break
            continue

        # Guarded on a substring first: these three are rare and the loop
        # runs once per line of a trace that reaches hundreds of megabytes.
        if 'partly satisfied' in l and RE_SBWAIT.search(l):
            pending_collision[0] = cycle
            continue

        if 'collision cleared' in l:
            m = RE_SBREPLAY.search(l)
            if m:
                if held_by_collision[0] is not None:
                    collision_replay.setdefault(
                        held_by_collision[0], []).append(cycle)
                    if int(m.group(1)) == 0:
                        held_by_collision[0] = None
                else:
                    collision_unresolved[0] += 1
                continue

        # The RAS names no instruction. A push or a pop belongs to the call or
        # return fetch2 saw this cycle, and a drop belongs to the instruction whose
        # squash discarded it. Anything with no anchor still counts globally.
        if 'ras: ' in l:
            m = RE_RAS_PUSH.search(l) or RE_RAS_POP.search(l)
            if m:
                ras_depth.append([cycle, int(m.group(1))])
                if last_f2[0] == cycle:
                    target = ras_push if 'push:' in l else ras_pop
                    target.setdefault(last_f2[1], cycle)
                continue
            if RE_RAS_DROP.search(l):
                ras_drop_cycles.append(cycle)
                if last_discard[0] == cycle:
                    ras_drop.setdefault(last_discard[1], cycle)
                continue

        if 'Window ' in l:
            m = RE_WINDOW.search(l)
            if m:
                cycles = int(m.group(3))
                charge_spans.setdefault(m.group(1), {}).setdefault(
                    f'window_{m.group(2)}', []).append([cycle, cycle + cycles])
                continue

        if 'for cause ' in l:
            m = RE_CACHEBLOCK.search(l)
            if m:
                key = (m.group(1), int(m.group(3)))
                if m.group(2) == 'Blocking':
                    blocked_open.setdefault(key, cycle)
                else:
                    start = blocked_open.pop(key, None)
                    if start is not None:
                        cause = BLOCKED_CAUSES.get(
                            key[1], f'cause_{key[1]}')
                        blocked_spans.setdefault(key[0], {}).setdefault(
                            cause, []).append([start, cycle])
                continue

        m = RE_COMMITSTALL.search(l)
        if m:
            fseq = int(m.group(2))
            end = cycle + int(m.group(4))
            if end > exmin_map.get(fseq, 0):
                exmin_map[fseq] = end
            continue

        m = RE_LSQ.search(l)
        if m:
            mm = RE_ID_FULL.search(m.group(3))
            if mm:
                fseq = int(mm.group(3))
                ev = lsq_events.setdefault(fseq, {})
                s_from, s_to = m.group(1), m.group(2)
                if s_from == 'NotIssued' and s_to == 'InTranslation':
                    ev['pushCycle'] = cycle
                if s_to in ('RequestIssuing', 'StoreBufferIssuing'):
                    ev.setdefault('issueCycle', cycle)
                if s_to == 'Complete':
                    ev['completeCycle'] = cycle
                if 'Store' in s_from or 'Store' in s_to:
                    ev['isStore'] = True
            continue

        m = RE_SBCONS.search(l)
        if m:
            mm = RE_ID_FULL.search(m.group(1))
            if mm:
                fseq = int(mm.group(3))
                if fseq not in storebuf_events:
                    storebuf_events[fseq] = {
                        'pushCycle': cycle, 'deleteCycle': None}
            continue

        m = RE_SBDEL.search(l)
        if m:
            fseq = int(m.group(2))
            if fseq in storebuf_events:
                storebuf_events[fseq]['deleteCycle'] = cycle
            continue

        m = RE_F2DEC.search(l)
        if m:
            mm = RE_ID_F2.search(m.group(1))
            if mm:
                fseq = int(mm.group(3))
                if fseq not in fetch2_map:
                    fetch2_map[fseq] = cycle
                last_f2[0], last_f2[1] = cycle, fseq
            continue

        m = RE_MINORINST.search(l)
        if m:
            line_seq = int(m.group(1))
            fseq = int(m.group(2))
            execute_map[fseq] = {
                'cycle': cycle, 'lineSeq': line_seq,
                'execSeq': int(m.group(3)),
                'pc': m.group(4), 'instr': m.group(5), 'fu': m.group(6),
                'flags': m.group(7), 'src': m.group(8), 'dest': m.group(9),
                'predictedTaken': 'predictedTaken' in l,
            }
            continue

        m = RE_SCOREBOARD.search(l)
        if m:
            mm = RE_ID_FULL.search(m.group(1))
            if mm:
                fseq = int(mm.group(3))
                if fseq not in scoreboard_map:
                    scoreboard_map[fseq] = int(m.group(2))
            continue

        m = RE_PASSING.search(l)
        if m:
            mm = RE_ID_F2.search(m.group(1))
            if mm:
                fseq = int(mm.group(3))
                if fseq not in decode_map:
                    decode_map[fseq] = cycle
            continue

        m = RE_TRYING.search(l)
        if m:
            mm = RE_ID_FULL.search(m.group(1))
            if mm:
                line_seq = int(mm.group(2))
                fseq = int(mm.group(3))
                has_minor_execute[0] = True
                if fseq not in issue_first:
                    issue_first[fseq] = cycle
                if fseq not in execute_map:
                    execute_map[fseq] = {
                        'cycle': cycle, 'lineSeq': line_seq,
                        'pc': m.group(2), 'instr': m.group(3),
                        'fu': '', 'src': '', 'dest': '', 'flags': '',
                        'predictedTaken': False,
                    }
            continue

        m = RE_ISSUING.search(l)
        if m:
            mm = RE_ID_FULL.search(m.group(1))
            if mm:
                line_seq = int(mm.group(2))
                fseq = int(mm.group(3))
                if fseq not in issue_ok:
                    issue_ok[fseq] = cycle
                if fseq not in issue_fu:
                    issue_fu[fseq] = int(m.group(4))
                if fseq not in execute_map:
                    execute_map[fseq] = {
                        'cycle': cycle, 'lineSeq': line_seq,
                        'pc': m.group(2), 'instr': m.group(3),
                        'fu': '', 'src': '', 'dest': '', 'flags': '',
                        'predictedTaken': False,
                    }
                execute_map[fseq]['cycle'] = cycle
            continue

        m = RE_BRANCH.search(l)
        if m:
            mm = RE_ID_FULL.search(m.group(3))
            if mm:
                fseq = int(mm.group(3))
                branch_events.setdefault(fseq, []).append(
                    {'cycle': cycle, 'type': m.group(1), 'target': m.group(2)})
            continue

        m = RE_DISCARD.search(l)
        if m:
            mm = RE_ID_FULL.search(m.group(1))
            if mm:
                fseq = int(mm.group(3))
                if fseq not in discard_map:
                    discard_map[fseq] = cycle
                last_discard[0], last_discard[1] = cycle, fseq
            continue

        if 'T0' in l:
            m = RE_COMMIT.search(l)
            if m:
                sym = RE_COMMIT_SYM.search(m.group(2).strip())
                if sym:
                    base = int(m.group(1), 16) - int(sym.group(2) or 0)
                    # Lowest base wins if a name is ever seen with inconsistent
                    # deltas, so a symbol cannot drift upwards through the file.
                    prev = sym_base.get(sym.group(1))
                    if prev is None or base < prev:
                        sym_base[sym.group(1)] = base
                upc = RE_COMMIT_UPC.search(m.group(2))
                commit_list.append({
                    'cycle': cycle, 'pc': m.group(1),
                    'instr': m.group(3).strip(), 'fu': m.group(4),
                    'fetchSeq': int(m.group(5)) if m.group(5) is not None else None,
                    'upc': int(upc.group(1)) if upc else None,
                })

    if progress is not None:
        progress.update(line_no, len(execute_map), bytes_done, final=True)
        progress.done()
        print(f"[pass 2/2] done: {line_no:,} lines, {len(execute_map):,} "
              f"instructions, {len(commit_list):,} commits", file=sys.stderr)
        print("[build] assembling instruction records…", file=sys.stderr)

    # ---- Forward-delay inference -------------------------------------------
    pipe_f1f2, pipe_f2dec, pipe_dec_ex = infer_forward_delays(
        execute_map, fetch1_map, fetch2_map, decode_map, issue_first)

    # ---- Commit lookup tables ----------------------------------------------
    # gem5 splits a RISC-V atomic into a load and a store committing on
    # different cycles, but decode gives both the same fetchSeqNum.
    merged = []
    for cm in commit_list:
        prev = merged[-1] if merged else None
        if (prev is not None
                and prev['pc'] == cm['pc']
                and cm['upc'] is not None and prev['_upcLast'] is not None
                and cm['upc'] == prev['_upcLast'] + 1
                and cm['fetchSeq'] is not None and prev['_seqLast'] is not None
                and cm['fetchSeq'] == prev['_seqLast'] + 1):
            prev['cycle'] = cm['cycle']
            prev['_upcLast'] = cm['upc']
            prev['_seqLast'] = cm['fetchSeq']
        else:
            e = dict(cm)
            e['_upcLast'] = cm['upc']
            e['_seqLast'] = cm['fetchSeq']
            merged.append(e)
    commit_list = merged

    commit_by_fetchseq = {}
    for cm in commit_list:
        if cm['fetchSeq'] is not None:
            commit_by_fetchseq[cm['fetchSeq']] = cm
    commit_by_pc = {}
    for cm in commit_list:
        commit_by_pc.setdefault(cm['pc'], []).append(cm)
    pc_ptr = {}

    line_size = observed_line_size[0]

    records = []
    for seq in sorted(execute_map.keys()):
        ex = execute_map[seq]
        f1c = fetch1_map.get(ex['lineSeq'])
        f2real = fetch2_map.get(seq)
        try_c = issue_first.get(seq)
        iss_c = issue_ok.get(seq)
        fu_idx = issue_fu.get(seq)
        ret_cyc = scoreboard_map.get(seq)
        dec_real = decode_map.get(seq)

        # Estimation: prefer real, estimate the rest from neighbours.
        f2est = (f1c + pipe_f1f2) if f1c is not None else None
        f2_final = f2real if f2real is not None else f2est
        dec_est = (f2_final + pipe_f2dec) if f2_final is not None else None
        dec_fin = dec_real if dec_real is not None else dec_est

        base = try_c if try_c is not None else (
            iss_c if iss_c is not None else ex['cycle'])
        dec_f = dec_fin if dec_fin is not None else (base - pipe_dec_ex)
        f2_f = f2_final if f2_final is not None else (dec_f - pipe_f2dec)
        f1_f = f1c if f1c is not None else (f2_f - pipe_f1f2)
        dto_f = dec_f + pipe_dec_ex
        cm_entry = commit_by_fetchseq.get(ex.get('execSeq'))
        is_discarded = seq in discard_map
        if cm_entry is not None:
            cmc = cm_entry['cycle']
            cm_entry['_used'] = True
        else:
            cmc = None
            if not is_discarded:
                pool = commit_by_pc.get(ex['pc'], [])
                p = pc_ptr.get(ex['pc'], 0)
                while p < len(pool) and pool[p].get('_used'):
                    p += 1
                if p < len(pool):
                    cmc = pool[p]['cycle']
                    pool[p]['_used'] = True
                    p += 1
                pc_ptr[ex['pc']] = p

        ex_fu = iss_c if iss_c is not None else ex['cycle']
        lsq_evt = lsq_events.get(seq)

        # fuDone: loads use memComplete, else scoreboard returnCycle, else cm, else ex+1.
        if (lsq_evt is not None
                and lsq_evt.get('isStore') is not True
                and lsq_evt.get('completeCycle') is not None):
            fu_done = lsq_evt['completeCycle']
        else:
            fu_done = ret_cyc if ret_cyc is not None else (
                cmc if cmc is not None else ex_fu + 1)

        exmin = exmin_map.get(seq)
        if exmin is not None:
            fu_done = max(fu_done, exmin if cmc is None else min(exmin, cmc))

        # ---- Line-wrap detection (32-bit inst straddling a cache line) ------
        wraps_line = False
        wrap_prev_line_seq = None
        f1reqA = f1respA = icMissA = None
        icRetryA = icRetryB = None
        f1holdA = f1holdB = None
        f1reqB = f1respB = icMissB = None
        flags = ex['flags'] or ''
        instr = ex['instr'] or ''
        pc = ex['pc']
        if line_size is not None and pc:
            is_compressed = bool(RE_COMPRESSED.match(instr))
            pc_int = int(pc, 16)
            offset = pc_int & (line_size - 1)
            if (not is_compressed) and offset + 4 > line_size:
                curr_vaddr = fetch1_vaddr.get(ex['lineSeq'])
                if curr_vaddr is not None:
                    prev_vaddr = curr_vaddr - line_size
                    for ls in range(ex['lineSeq'] - 1, max(-1, ex['lineSeq'] - 33), -1):
                        if fetch1_vaddr.get(ls) == prev_vaddr:
                            wrap_prev_line_seq = ls
                            break
                    if wrap_prev_line_seq is not None:
                        prev_req = fetch1_req.get(wrap_prev_line_seq)
                        prev_resp = fetch1_map.get(wrap_prev_line_seq)
                        curr_req = fetch1_req.get(ex['lineSeq'])
                        curr_resp = fetch1_map.get(ex['lineSeq'])
                        if prev_req is not None:
                            wraps_line = True
                            f1reqA = prev_req
                            f1respA = prev_resp
                            icMissA = prev_req in ic_miss_cycles
                            icRetryA = wrap_prev_line_seq in fetch1_retried
                            f1holdA = fetch1_held.get(wrap_prev_line_seq)
                            f1reqB = curr_req
                            f1respB = curr_resp
                            icMissB = (
                                curr_req in ic_miss_cycles) if curr_req is not None else None
                            icRetryB = ex['lineSeq'] in fetch1_retried
                            f1holdB = fetch1_held.get(ex['lineSeq'])

        ic_retry = ex['lineSeq'] in fetch1_retried
        f1_hold = fetch1_held.get(ex['lineSeq'])
        # fetchReqCyc / icMiss (aggregate, with wrap override)
        fetch_req_cyc = fetch1_req.get(ex['lineSeq'])
        ic_miss = (
            fetch_req_cyc in ic_miss_cycles) if fetch_req_cyc is not None else None
        if wraps_line:
            fetch_req_cyc = f1reqA
            if icMissA or icMissB:
                ic_miss = True
            f1_hold = f1holdA
        if f1_hold is not None and (fetch_req_cyc is None
                                    or f1_hold >= fetch_req_cyc):
            f1_hold = None

        # ---- Branch model --------------------------------------------------
        is_cond = 'IsCondControl' in flags
        is_ctrl = 'IsControl' in flags
        br_evs = branch_events.get(seq)
        br_last = br_evs[-1] if br_evs else None
        br_type = br_last['type'] if br_last else None

        if not is_ctrl:
            branch_kind = None
        else:
            is_call = 'IsCall' in flags
            is_return = 'IsReturn' in flags
            is_direct = 'IsDirectControl' in flags
            is_indirect = 'IsIndirectControl' in flags
            if is_return:
                branch_kind = 'Return'
            elif is_call:
                branch_kind = 'CallDirect' if is_direct else 'CallIndirect'
            elif is_direct:
                branch_kind = 'DirectCond' if is_cond else 'DirectUncond'
            elif is_indirect:
                branch_kind = 'IndirectCond' if is_cond else 'IndirectUncond'
            else:
                branch_kind = None

        is_cond_correct_nt = (
            not is_discarded) and is_ctrl and br_type is None

        if is_discarded or not is_ctrl:
            branch_outcome = None
        elif br_type is None:
            branch_outcome = 'correct' if is_cond_correct_nt else None
        elif br_type == 'UnpredictedBranch':
            branch_outcome = 'unpred'
        elif br_type.startswith('Badly'):
            branch_outcome = 'mispred'
        else:
            branch_outcome = 'correct'

        if is_discarded or not is_ctrl:
            branch_actual_taken = None
        elif not is_cond:
            branch_actual_taken = True
        elif br_type == 'BadlyPredictedBranch':
            branch_actual_taken = False
        elif br_type == 'UnpredictedBranch':
            branch_actual_taken = True
        elif br_type and br_type.startswith('Badly'):
            branch_actual_taken = True
        else:
            branch_actual_taken = ex['predictedTaken'] is True

        if is_discarded or not is_ctrl:
            branch_caught_at = None
        elif br_type is None:
            branch_caught_at = 'fetch2' if is_cond_correct_nt else None
        elif br_type == 'UnpredictedBranch':
            branch_caught_at = 'execute'
        elif br_type.startswith('Badly'):
            branch_caught_at = 'execute'
        else:
            branch_caught_at = 'fetch2'

        branch_resolve_cyc = br_last['cycle'] if br_last else None

        serialize_after = ((not is_discarded) and (not is_ctrl)
                           and br_type is not None and 'Serialize' in flags)

        sb_evt = storebuf_events.get(seq)

        records.append({
            'seq': seq, 'pc': pc, 'instr': instr, 'fu': ex['fu'],
            'compressed': bool(RE_COMPRESSED.match(instr)),
            'src': ex['src'], 'dest': ex['dest'], 'flags': flags,
            'fuIdx': fu_idx, 'lineSeq': ex['lineSeq'],
            'f1req': fetch_req_cyc,
            'f1hold': f1_hold,
            'f1': f1_f, 'f2': f2_f, 'dec': dec_f, 'dtoe': dto_f,
            'exbuf': try_c, 'ex': ex_fu, 'fuDone': fu_done, 'cm': cmc,
            'icMiss': ic_miss,
            'icRetry': ic_retry,
            'wrapsLine': wraps_line, 'wrapPrevLineSeq': wrap_prev_line_seq,
            'f1reqA': f1reqA, 'f1respA': f1respA, 'icMissA': icMissA, 'icRetryA': icRetryA,
            'f1holdA': f1holdA, 'f1holdB': f1holdB,
            'f1reqB': f1reqB, 'f1respB': f1respB, 'icMissB': icMissB, 'icRetryB': icRetryB,
            'memPush': lsq_evt.get('pushCycle') if lsq_evt else None,
            'memIssue': lsq_evt.get('issueCycle') if lsq_evt else None,
            'memComplete': lsq_evt.get('completeCycle') if lsq_evt else None,
            'isStore': lsq_evt.get('isStore') if lsq_evt else None,
            'dcMiss': _dc_miss(lsq_evt, dcache_by_cycle),
            'dcMissIsStore': _dc_miss_store(lsq_evt, dcache_by_cycle),
            'sbPush': sb_evt.get('pushCycle') if sb_evt else None,
            'sbDelete': sb_evt.get('deleteCycle') if sb_evt else None,
            'collisionWait': collision_wait.get(seq) or None,
            'collisionReplay': collision_replay.get(seq) or None,
            'rasPush': ras_push.get(seq),
            'rasPop': ras_pop.get(seq),
            'rasDropped': ras_drop.get(seq),
            'flushCycle': branch_resolve_cyc,
            'flushed': is_discarded,
            'isControl': is_ctrl,
            'predictedTaken': ex['predictedTaken'] is True,
            'branchOutcome': branch_outcome,
            'branchActualTaken': branch_actual_taken,
            'branchCaughtAt': branch_caught_at,
            'branchKind': branch_kind,
            'serializeAfter': serialize_after,
            'estimated': dec_real is None and f1c is None and f2real is None,
            '_real_f2': f2real, '_real_dec': dec_real, '_real_ret': ret_cyc,
        })

    # ---- MinorExecute-only mnemonic enrichment -----------------------------
    if has_minor_execute[0] and records:
        enrich_by_pc = {}
        for cm in commit_list:
            enrich_by_pc.setdefault(
                cm['pc'], {'instr': cm['instr'], 'fu': cm['fu']})
        for rec in records:
            e = enrich_by_pc.get(rec['pc'])
            if e is None or not e['instr']:
                continue
            rec['instr'] = e['instr']
            rec['fu'] = e['fu']
            rec['compressed'] = bool(
                RE_COMPRESSED.match(rec['instr'] or ''))

    # Accesses count every access, matching overallAccesses. Misses count only
    # those that opened an MSHR, matching overallMshrMisses.
    ic_access_cycles = sorted(ic_hit_log + [e[0] for e in ic_miss_log])
    ic_miss_arr = sorted(e[0] for e in ic_miss_log if not e[1])

    # Counted from the access log, since per-instruction attribution misses
    # store writebacks that retire after commit. Cycles carry multiplicity, a
    # dirty-victim writeback can share a cycle with a demand access.
    dc_access_cycles = sorted(
        c for c, evs in dcache_by_cycle.items() for _ in evs)
    dc_miss_cycles = sorted(c for c, evs in dcache_by_cycle.items()
                            for e in evs if e['miss'])
    dc_store_access_cycles = sorted(
        c for c, evs in dcache_by_cycle.items() for e in evs if e['isWrite'])
    dc_store_miss_cycles = sorted(c for c, evs in dcache_by_cycle.items(
    ) for e in evs if e['miss'] and e['isWrite'])
    dc_mshr_miss_cycles = sorted(
        c for c, evs in dcache_by_cycle.items()
        for e in evs if e['miss'] and not e['coalesced'])
    dc_store_mshr_miss_cycles = sorted(
        c for c, evs in dcache_by_cycle.items()
        for e in evs if e['miss'] and not e['coalesced'] and e['isWrite'])

    return {
        'metadata': {
            'tool': 'minorflow_tracer',
            'schema_version': 3,
            'clock_period_ps': tpc,
            'pipe_delays': {'f1_f2': pipe_f1f2, 'f2_dec': pipe_f2dec, 'dec_ex': pipe_dec_ex},
            'n_instructions': len(records),
            'has_minor_execute': has_minor_execute[0],
            'observed_line_size': line_size,
            'unattributed_collisions': collision_unresolved[0],
        },
        'config_params': branch_pred_info,
        # Sorted base/name pairs, so the viewer can resolve a branch target to
        # the symbol containing it without carrying the whole map per record.
        'symbols': sorted(([b, n] for n, b in sym_base.items()),
                          key=lambda e: e[0]),
        'ic_events': {
            'access_cycles': ic_access_cycles,
            'miss_cycles': ic_miss_arr,
            'blocked_spans': blocked_spans.get('l1icaches', {}),
            'charge_spans': charge_spans.get('l1icaches', {}),
        },
        'dc_events': {
            'access_cycles': dc_access_cycles,
            'miss_cycles': dc_miss_cycles,
            'store_access_cycles': dc_store_access_cycles,
            'store_miss_cycles': dc_store_miss_cycles,
            'mshr_miss_cycles': dc_mshr_miss_cycles,
            'store_mshr_miss_cycles': dc_store_mshr_miss_cycles,
            'blocked_spans': blocked_spans.get('l1dcaches', {}),
            'charge_spans': charge_spans.get('l1dcaches', {}),
        },
        'ras_events': {
            'depth': ras_depth,
            'drop_cycles': ras_drop_cycles,
        },
        'instructions': records,
    }


def _dc_miss(lsq_evt, dcache_by_cycle):
    """Did this instruction's own DCache access miss? Matches on access
    direction so a store sharing its issue cycle with a load cannot pick up
    the load's result. Falls back to any access at that cycle."""
    if not lsq_evt or lsq_evt.get('issueCycle') is None:
        return None
    evs = dcache_by_cycle.get(lsq_evt['issueCycle'])
    if not evs:
        return None
    is_store = lsq_evt.get('isStore') is True
    matching = [e for e in evs if e['isWrite'] == is_store] or evs
    return any(e['miss'] for e in matching)


def _dc_miss_store(lsq_evt, dcache_by_cycle):
    # Used alongside dcMiss to pick the store-miss cell over the load-miss one.
    if not lsq_evt:
        return None
    return True if lsq_evt.get('isStore') is True else None


def parse_file(path, show_progress=True, tpc=None):
    """Streaming entry point: pass 1 detects ticks-per-cycle, pass 2 builds
    the instruction records. Neither pass materialises the file."""
    total_bytes = 0
    try:
        total_bytes = os.path.getsize(path)
    except OSError:
        pass

    if tpc:
        if show_progress:
            print(f"[pass 1/2] skipped: clock period {tpc} ps given on the "
                  f"command line", file=sys.stderr)
    else:
        p1 = Progress('pass 1/2', total_bytes, enabled=show_progress)
        tpc = detect_tpc_streaming(path, progress=p1)
        p1.done()
        if show_progress:
            print(f"[pass 1/2] done: clock period {tpc} ps "
                  f"(detected from tick deltas)", file=sys.stderr)

    p2 = Progress('pass 2/2', total_bytes, enabled=show_progress)
    with open(path, 'r', errors='replace') as f:
        data = parse(f, tpc, progress=p2, total_bytes=total_bytes)
    return data


def main():
    ap = argparse.ArgumentParser(
        description='Parse a gem5 MinorCPU debug trace into MinorFlow JSON.')
    ap.add_argument(
        'trace', help='Path to the gem5 MinorCPU debug trace (.txt/.log)')
    ap.add_argument(
        '-o', '--out', help='Output JSON path (default: <trace>.json)')
    ap.add_argument('--stats', action='store_true',
                    help='Print a short summary')
    ap.add_argument('--quiet', action='store_true',
                    help='Suppress progress output')
    ap.add_argument('--tpc', type=int, default=None, metavar='TICKS',
                    help='Ticks per CPU cycle, skipping the detection pass. '
                         '20000 for a 50 MHz core at the gem5 default tick '
                         'rate. Only worth giving on a very large trace')
    args = ap.parse_args()

    if not os.path.isfile(args.trace):
        print(f"[ERROR] Trace file not found: {args.trace}", file=sys.stderr)
        print("        Check the path and try again.", file=sys.stderr)
        sys.exit(1)

    try:
        size = os.path.getsize(args.trace)
        print(f"[INFO] Reading {args.trace} ({size / (1024*1024):.1f} MB)",
              file=sys.stderr)
    except OSError:
        pass

    t0 = time.time()
    data = parse_file(args.trace, show_progress=not args.quiet,
                      tpc=args.tpc)

    md = data['metadata']
    if md.get('unattributed_collisions'):
        print(f"[WARN] {md['unattributed_collisions']} store collision(s) "
              f"could not be tied to an instruction, so their strips are "
              f"missing from the timeline.", file=sys.stderr)
    if md['n_instructions'] and not data.get('ras_events', {}).get('depth'):
        print("[INFO] No 'ras:' lines in this trace, so the RAS push, pop and "
              "drop markers will be absent. Add RAS to --debug-flags when "
              "capturing if you want them.", file=sys.stderr)

    if data['metadata']['n_instructions'] == 0:
        print("[WARNING] No MinorCPU instructions were parsed from this file. It "
              "may not be a valid gem5 MinorCPU debug trace. The parser expects "
              "Minor debug-flag lines such as MinorTrace. Check that the trace was "
              "generated with the correct gem5 --debug-flags (for example "
              "--debug-flags=MinorTrace).", file=sys.stderr)

    out = args.out
    if not out:
        base = args.trace
        for ext in ('.txt', '.log', '.trace'):
            if base.endswith(ext):
                base = base[: -len(ext)]
                break
        out = base + '.json'

    print(f"[write] writing JSON to {out}…", file=sys.stderr)
    with open(out, 'w') as f:
        json.dump(data, f, separators=(',', ':'))

    elapsed = time.time() - t0
    md = data['metadata']
    print(f"[INFO] Wrote {out}")
    print(f"[INFO] {md['n_instructions']:,} instructions, "
          f"clock period {md['clock_period_ps']} ps, "
          f"forward delays {md['pipe_delays']['f1_f2']}/"
          f"{md['pipe_delays']['f2_dec']}/{md['pipe_delays']['dec_ex']}")
    print(f"[INFO] Total time {elapsed:.1f}s", file=sys.stderr)

    if args.stats:
        recs = data['instructions']
        committed = sum(
            1 for r in recs if not r['flushed'] and r['cm'] is not None)
        flushed = sum(1 for r in recs if r['flushed'])
        print(f"[STATS] committed={committed} flushed={flushed} "
              f"ic_access={len(data['ic_events']['access_cycles'])} "
              f"ic_miss={len(data['ic_events']['miss_cycles'])}")


if __name__ == '__main__':
    main()
