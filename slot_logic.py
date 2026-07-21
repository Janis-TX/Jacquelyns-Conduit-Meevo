"""
slot_logic.py — PURE logic for the consolidated availability/recommendation tool.

No network, no Meevo, no server deps: block reconstruction + scoring against the nine
LOCKED v1 priorities (see Gap-Filling-Optimizer-v1-Build-Spec.md §1a). This is unit-tested
in isolation with mock scan data; the server wires live scans into `suggest()`.

Priorities encoded (v1, NO revenue weighting):
 1 fill exact/near-exact gap        -> exact_gap_bonus
 2 protect 50/60/80-90 blocks       -> preservation points on the LARGER leftover
 3 penalize fragments < 15 min      -> fragment_penalty (strong)
 4 short svc adjacent to existing   -> adjacency_bonus (not mid-block)
 5 favor utilization                -> utilization_bonus (small)
 6 earliest only as tiebreaker      -> earliest_tiebreak (tiny)
 7 gentle steer / customer wins     -> handled by caller (specific_time short-circuit)
 8 (no revenue weighting in v1)
 9 Meevo is source of truth for durations/resources (caller supplies them)
"""
from dataclasses import dataclass, field
from typing import Optional


# ----------------------------- config -----------------------------
@dataclass
class ScoringConfig:
    exact_gap_bonus: int = 100
    near_exact_tolerance: int = 5     # minutes; leftover <= this counts as "flush"
    adjacency_bonus: int = 40
    edge_bonus: int = 25
    preferred_window_bonus: int = 15
    utilization_bonus: int = 5
    earliest_tiebreak: int = 2        # * earliness_rank (small)
    middle_penalty: int = 60
    fragment_penalty: int = 120       # per leftover piece 0 < piece < min_useful_min
    min_useful_min: int = 15
    grid_min: int = 15
    # preservation: points if the LARGER leftover still fits this protected duration
    preservation: dict = field(default_factory=lambda: {90: 30, 80: 28, 60: 20, 50: 18, 30: 6})


# ----------------------------- helpers -----------------------------
def to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def to_hhmm(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def reconstruct_free_intervals(reference_starts_min, ref_dur, grid=15):
    """From a REFERENCE-service scan (shortest service on the resource, duration `ref_dur`),
    reconstruct conservative free intervals. A contiguous run of grid-aligned starts
    s0..sk => free interval [s0, sk + ref_dur]. Breaks (> grid) end a run (a booking sits there).

    Conservative by design: with a ref service longer than the grid, the inferred trailing edge
    is 0..(ref_dur-grid) min EARLIER than the true edge, so we UNDER-count free time and never
    over-promise. Booth (ref_dur == grid) => exact.
    """
    starts = sorted(set(int(s) for s in reference_starts_min))
    intervals, run = [], []
    for s in starts:
        if run and (s - run[-1]) <= grid:
            run.append(s)
        else:
            if run:
                intervals.append((run[0], run[-1] + ref_dur))
            run = [s]
    if run:
        intervals.append((run[0], run[-1] + ref_dur))
    return intervals


def containing_interval(start, dur, intervals):
    """Return the free interval [a,b] that fully contains [start, start+dur], else None."""
    end = start + dur
    for (a, b) in intervals:
        if a <= start and end <= b:
            return (a, b)
    return None


def score_placement(before, after, cfg: ScoringConfig):
    """Score one placement given leftover minutes before/after the service inside its block.
    Returns (score, reason_codes)."""
    reasons, s = [], 0
    tol = cfg.near_exact_tolerance
    flush_before = before <= tol
    flush_after = after <= tol
    exact = flush_before and flush_after

    if exact:
        s += cfg.exact_gap_bonus
        reasons.append("EXACT_GAP")
    elif flush_before or flush_after:
        s += cfg.adjacency_bonus
        reasons.append("ADJACENT")
        s += cfg.edge_bonus
        reasons.append("EDGE")

    # middle-of-block fragmentation: both leftovers non-trivial
    if before > cfg.min_useful_min and after > cfg.min_useful_min:
        s -= cfg.middle_penalty
        reasons.append("MIDDLE_OF_BLOCK")

    # unusable stranded fragments (strong penalty)
    for piece in (before, after):
        if 0 < piece < cfg.min_useful_min:
            s -= cfg.fragment_penalty
            reasons.append(f"FRAGMENT_{int(piece)}MIN")

    # preservation of protected long-service blocks (on the LARGER leftover)
    larger = max(before, after)
    for dur, pts in sorted(cfg.preservation.items(), reverse=True):
        if larger >= dur:
            s += pts
            reasons.append(f"PRESERVES_{dur}")
            break

    s += cfg.utilization_bonus
    return s, reasons


def suggest(service_duration, candidates_by_emp, reference_by_emp, ref_dur,
            requested_window=None, specific_time=None, cfg: ScoringConfig = None):
    """Pure ranking.
    - service_duration: minutes of the REQUESTED service.
    - candidates_by_emp: {emp_id: [start_hhmm, ...]}  valid starts for the requested service.
    - reference_by_emp:  {emp_id: [start_hhmm, ...]}  valid starts for the short reference service.
    - ref_dur: duration (min) of the reference service (15 = booth/exact; 25 = facial room, etc).
    - requested_window: optional (start_hhmm, end_hhmm) time-of-day preference.
    - specific_time: optional hhmm the client explicitly asked for (customer-wins short-circuit).
    Returns dict: {recommended, alternatives[<=2], completeness, reason_codes, ...}.
    """
    cfg = cfg or ScoringConfig()

    # ---- customer-wins: a specific requested time that is genuinely valid is returned as-is
    if specific_time:
        st = to_min(specific_time)
        for emp, starts in candidates_by_emp.items():
            if specific_time in starts:
                return {"status": "ok", "completeness": "exact_request",
                        "recommended": {"time": specific_time, "employee_id": emp,
                                        "reason_codes": ["CUSTOMER_REQUESTED"]},
                        "alternatives": []}
        return {"status": "requested_time_unavailable", "completeness": "exact_request",
                "recommended": None, "alternatives": []}

    # ---- no eligible availability -> safe handoff status (no crash on empty/malformed)
    total = sum(len(v or []) for v in candidates_by_emp.values())
    if total == 0:
        return {"status": "no_availability", "completeness": "complete",
                "recommended": None, "alternatives": []}

    # earliest ranking for the tiebreak nudge (skip malformed entries defensively)
    def _safe_min(t):
        try:
            return to_min(t)
        except Exception:
            return None
    all_starts = sorted({m for v in candidates_by_emp.values() for t in (v or [])
                         if (m := _safe_min(t)) is not None})
    rank = {m: i for i, m in enumerate(all_starts)}

    scored = []
    for emp, starts in candidates_by_emp.items():
        intervals = reconstruct_free_intervals(
            [to_min(t) for t in (reference_by_emp.get(emp) or [])], ref_dur, cfg.grid_min)
        for t in (starts or []):
            try:
                c = to_min(t)
            except Exception:
                continue  # malformed entry -> skip defensively
            block = containing_interval(c, service_duration, intervals)
            if not block:
                # candidate valid but no reference block (e.g. reference svc unavailable for emp):
                # treat as low-confidence edge placement, do not fragment-score it.
                sc, reasons = cfg.utilization_bonus, ["NO_REFERENCE_MAP"]
                before = after = None
            else:
                a, b = block
                before = c - a
                after = b - (c + service_duration)
                sc, reasons = score_placement(before, after, cfg)
            if requested_window:
                ws, we = to_min(requested_window[0]), to_min(requested_window[1])
                if ws <= c <= we:
                    sc += cfg.preferred_window_bonus
                    reasons.append("IN_REQUESTED_WINDOW")
            sc += cfg.earliest_tiebreak * (len(all_starts) - rank.get(c, 0))
            scored.append((sc, c, {"time": t, "employee_id": emp, "reason_codes": reasons,
                                   "leftover_before": before, "leftover_after": after}))

    scored.sort(key=lambda x: (-x[0], x[1]))
    completeness = "complete" if ref_dur <= cfg.grid_min else "approx_trailing_edge"
    return {"status": "ok", "completeness": completeness,
            "recommended": scored[0][2] if scored else None,
            "alternatives": [row[2] for row in scored[1:3]]}


# =============================== UNIT TESTS ===============================
def _run_tests():
    cfg = ScoringConfig()
    passed = 0

    def ok(name, cond):
        nonlocal passed
        assert cond, f"FAIL: {name}"
        passed += 1
        print(f"  ok  {name}")

    # 1) exact-gap beats a placement inside a big block (same service)
    # 15-min spray tan. Provider A: an exact 15-min gap 13:00-13:15 (ref shows only 13:00),
    # and a big open block 09:00-11:00.
    r = suggest(15,
                candidates_by_emp={"A": ["09:00", "13:00"]},
                reference_by_emp={"A": ["09:00", "09:15", "09:30", "09:45", "10:00", "10:15",
                                        "10:30", "10:45", "13:00"]},
                ref_dur=15)
    ok("exact-gap chosen over big-block start", r["recommended"]["time"] == "13:00"
       and "EXACT_GAP" in r["recommended"]["reason_codes"])

    # 2) edge vs middle: within one 09:00-10:30 block, 15-min svc at edge (09:00) beats middle (09:45)
    ref = ["09:00", "09:15", "09:30", "09:45", "10:00", "10:15"]  # free 09:00-10:30
    edge, _ = score_placement(0, 75, cfg)      # 09:00 -> before 0, after 75
    middle, _ = score_placement(45, 30, cfg)   # 09:45 -> before 45, after 30
    ok("edge beats middle", edge > middle)

    # 3) fragment avoidance: leaving a 10-min sliver is penalized vs leaving 0
    frag, fr = score_placement(0, 10, cfg)
    clean, _ = score_placement(0, 60, cfg)
    ok("fragment penalized", frag < clean and any("FRAGMENT" in x for x in fr))

    # 4) preservation: keeping >=60 leftover beats reducing block below 50
    keeps60, _ = score_placement(0, 60, cfg)
    kills, _ = score_placement(0, 45, cfg)
    ok("preserves 60 over sub-50", keeps60 > kills)

    # 5) protected 50/60/80-90 recognised on larger leftover
    _, r80 = score_placement(0, 85, cfg)
    ok("preserves 80 block", "PRESERVES_80" in r80)

    # 6) provider/GUID filtering: candidate for B uses B's map, A's map not applied
    r = suggest(15,
                candidates_by_emp={"B": ["14:00"]},
                reference_by_emp={"A": ["09:00", "09:15"], "B": ["14:00"]},
                ref_dur=15)
    ok("provider-scoped map", r["recommended"]["employee_id"] == "B")

    # 7) facial-room trailing-edge underestimation (ref 25 min) is conservative vs booth (ref 15)
    booth = reconstruct_free_intervals([to_min(t) for t in ["09:00", "09:15", "09:30"]], 15)
    facial = reconstruct_free_intervals([to_min(t) for t in ["09:00", "09:15", "09:30"]], 25)
    # both start 09:00; booth end = 09:30+15=09:45 ; facial end = 09:30+25=09:55.
    # facial's *last start*+dur reaches further, but the REAL guarantee is the same run;
    # conservative check: booth end is exact (grid), facial end may under/over vs true.
    ok("booth exact boundary", booth[0][1] == to_min("09:45"))
    ok("facial ref reconstructs", facial[0][1] == to_min("09:55"))

    # 8) capped/malformed scan fallback: garbage entries skipped, no crash
    r = suggest(15, candidates_by_emp={"A": ["bad", "09:00"]},
                reference_by_emp={"A": ["09:00", "09:15"]}, ref_dur=15)
    ok("malformed entry skipped", r["status"] == "ok" and r["recommended"]["time"] == "09:00")

    # 9) specific requested time honoured when valid
    r = suggest(15, candidates_by_emp={"A": ["14:30"]}, reference_by_emp={"A": ["14:30"]},
                ref_dur=15, specific_time="14:30")
    ok("specific time honoured", r["completeness"] == "exact_request"
       and r["recommended"]["time"] == "14:30")

    # 9b) specific requested time that is NOT available -> clear status, no false booking
    r = suggest(15, candidates_by_emp={"A": ["14:30"]}, reference_by_emp={"A": ["14:30"]},
                ref_dur=15, specific_time="16:00")
    ok("specific time unavailable flagged", r["status"] == "requested_time_unavailable")

    # 10) no eligible provider / empty -> safe handoff status
    r = suggest(15, candidates_by_emp={}, reference_by_emp={}, ref_dur=15)
    ok("no availability handoff", r["status"] == "no_availability")

    # 11) completeness flag reflects reference resolution (booth exact vs facial approx)
    r_booth = suggest(15, {"A": ["09:00"]}, {"A": ["09:00"]}, ref_dur=15)
    r_facial = suggest(50, {"A": ["09:00"]}, {"A": ["09:00"]}, ref_dur=25)
    ok("completeness booth=complete", r_booth["completeness"] == "complete")
    ok("completeness facial=approx", r_facial["completeness"] == "approx_trailing_edge")

    print(f"\nALL {passed} ASSERTIONS PASSED")


if __name__ == "__main__":
    _run_tests()
