#!/usr/bin/env python3
"""MinorFlow tracer: gem5 MinorCPU debug trace -> compact JSON for the viewer.

Windowing, bubble/stall/forwarding analysis and rendering all live in the viewer.

Usage:
    python3 minorflow_tracer.py trace.txt -o trace.json
    python3 minorflow_tracer.py trace.txt           # writes trace.json
    python3 minorflow_tracer.py trace.txt --stats    # also print a summary
"""
import time
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
    r'execute: MinorInst: id=\d+/\S+/(\d+)/(\d+)\.\d+\s+addr=(0x[0-9a-f]+)\s+'
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
    r'T0\s+:\s+(0x[0-9a-f]+)\s+@\S+\s+:\s+(.+?)\s+:\s+(\w+)(?:.*?FetchSeq=(\d+))?')

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


def detect_tpc_streaming(path, progress=None):
    """Pass 1: ticks-per-cycle = the MODE of positive deltas between unique
    adjacent tick values. Holds only the set of distinct ticks."""
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


def detect_tpc(lines):
    """Non-streaming variant of detect_tpc_streaming, for in-memory input."""
    tick_set = set()
    for line in lines:
        m = RE_TICK.match(line)
        if m:
            tick_set.add(int(m.group(1)))
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
    import math
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

    line_source may be an open file handle or a list of strings. tpc must
    already be detected via detect_tpc_streaming / detect_tpc.
    """
    # ---- Pass 2: event-by-event extraction ---------------------------------
    fetch1_map = {}     # lineSeq -> cycle of MinorLine response
    fetch1_req = {}     # lineSeq -> cycle of "Issued fetch request"
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
    dcache_by_cycle = {}  # cycle -> {miss, isWrite}
    commit_list = []    # {cycle, pc, instr, fu, fetchSeq}
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

        m = RE_FETCHREQ.search(l)
        if m:
            mm = RE_ID_LINE.search(m.group(1))
            if mm:
                line_seq = int(mm.group(1))
                if line_seq not in fetch1_req:
                    fetch1_req[line_seq] = cycle
            continue

        m = RE_ICACHE.search(l)
        if m:
            (ic_miss_cycles if m.group(1) == 'miss' else ic_hit_cycles).add(cycle)
            continue

        m = RE_DCACHE.search(l)
        if m:
            dcache_by_cycle.setdefault(cycle, []).append({
                'miss': m.group(2) == 'miss',
                'isWrite': m.group(1).startswith('Write'),
            })
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
            continue

        m = RE_MINORINST.search(l)
        if m:
            line_seq = int(m.group(1))
            fseq = int(m.group(2))
            execute_map[fseq] = {
                'cycle': cycle, 'lineSeq': line_seq,
                'pc': m.group(3), 'instr': m.group(4), 'fu': m.group(5),
                'flags': m.group(6), 'src': m.group(7), 'dest': m.group(8),
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
            continue

        if 'T0' in l:
            m = RE_COMMIT.search(l)
            if m:
                commit_list.append({
                    'cycle': cycle, 'pc': m.group(1),
                    'instr': m.group(2).strip(), 'fu': m.group(3),
                    'fetchSeq': int(m.group(4)) if m.group(4) is not None else None,
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

        # Commit cycle: prefer exact FetchSeq, else per-PC pairing.
        cm_entry = commit_by_fetchseq.get(seq)
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

        # ---- Line-wrap detection (32-bit inst straddling a cache line) ------
        wraps_line = False
        wrap_prev_line_seq = None
        f1reqA = f1respA = icMissA = None
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
                            f1reqB = curr_req
                            f1respB = curr_resp
                            icMissB = (
                                curr_req in ic_miss_cycles) if curr_req is not None else None

        # fetchReqCyc / icMiss (aggregate, with wrap override)
        fetch_req_cyc = fetch1_req.get(ex['lineSeq'])
        ic_miss = (
            fetch_req_cyc in ic_miss_cycles) if fetch_req_cyc is not None else None
        if wraps_line:
            fetch_req_cyc = f1reqA
            if icMissA or icMissB:
                ic_miss = True

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
            not is_discarded) and is_ctrl and is_cond and br_type is None

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
            'f1': f1_f, 'f2': f2_f, 'dec': dec_f, 'dtoe': dto_f,
            'exbuf': try_c, 'ex': ex_fu, 'fuDone': fu_done, 'cm': cmc,
            'icMiss': ic_miss,
            'wrapsLine': wraps_line, 'wrapPrevLineSeq': wrap_prev_line_seq,
            'f1reqA': f1reqA, 'f1respA': f1respA, 'icMissA': icMissA,
            'f1reqB': f1reqB, 'f1respB': f1respB, 'icMissB': icMissB,
            'memPush': lsq_evt.get('pushCycle') if lsq_evt else None,
            'memIssue': lsq_evt.get('issueCycle') if lsq_evt else None,
            'memComplete': lsq_evt.get('completeCycle') if lsq_evt else None,
            'isStore': lsq_evt.get('isStore') if lsq_evt else None,
            'dcMiss': _dc_miss(lsq_evt, dcache_by_cycle),
            'dcMissIsStore': _dc_miss_store(lsq_evt, dcache_by_cycle),
            'sbPush': sb_evt.get('pushCycle') if sb_evt else None,
            'sbDelete': sb_evt.get('deleteCycle') if sb_evt else None,
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
            enrich_by_pc.setdefault(cm['pc'], []).append(
                {'instr': cm['instr'], 'fu': cm['fu']})
        enrich_used = {}
        for rec in records:
            lst = enrich_by_pc.get(rec['pc'], [])
            ui = enrich_used.get(rec['pc'], 0)
            if ui < len(lst):
                rec['instr'] = lst[ui]['instr']
                rec['fu'] = lst[ui]['fu']
                rec['compressed'] = bool(
                    RE_COMPRESSED.match(rec['instr'] or ''))
                enrich_used[rec['pc']] = ui + 1

    ic_access_cycles = sorted(ic_hit_cycles | ic_miss_cycles)
    ic_miss_arr = sorted(ic_miss_cycles)

    # Counted from the access log rather than per-instruction: per-instruction
    # attribution misses store writebacks, which retire after commit. Cycles
    # are emitted with multiplicity, since a dirty-victim writeback can share
    # a cycle with a demand access.
    dc_access_cycles = sorted(
        c for c, evs in dcache_by_cycle.items() for _ in evs)
    dc_miss_cycles = sorted(c for c, evs in dcache_by_cycle.items()
                            for e in evs if e['miss'])
    dc_store_access_cycles = sorted(
        c for c, evs in dcache_by_cycle.items() for e in evs if e['isWrite'])
    dc_store_miss_cycles = sorted(c for c, evs in dcache_by_cycle.items(
    ) for e in evs if e['miss'] and e['isWrite'])

    return {
        'metadata': {
            'tool': 'minorflow_tracer',
            'schema_version': 1,
            'clock_period_ps': tpc,
            'pipe_delays': {'f1_f2': pipe_f1f2, 'f2_dec': pipe_f2dec, 'dec_ex': pipe_dec_ex},
            'n_instructions': len(records),
            'has_minor_execute': has_minor_execute[0],
            'observed_line_size': line_size,
        },
        'config_params': branch_pred_info,
        'ic_events': {
            'access_cycles': ic_access_cycles,
            'miss_cycles': ic_miss_arr,
        },
        'dc_events': {
            'access_cycles': dc_access_cycles,
            'miss_cycles': dc_miss_cycles,
            'store_access_cycles': dc_store_access_cycles,
            'store_miss_cycles': dc_store_miss_cycles,
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


def parse_file(path, show_progress=True):
    """Streaming entry point: pass 1 detects ticks-per-cycle, pass 2 builds
    the instruction records. Neither pass materialises the file."""
    total_bytes = 0
    try:
        total_bytes = os.path.getsize(path)
    except OSError:
        pass

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
    data = parse_file(args.trace, show_progress=not args.quiet)

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
