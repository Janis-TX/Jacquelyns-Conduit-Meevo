"""
rec_cache.py — pure, network-free store for suggest_best_slot recommendation sets.

Why: the decline turn ("that time doesn't work") must be answered by a LIGHTWEIGHT tool
(get_next_recommended_slot) that reads a pre-computed alternative from here, instead of
re-running the whole optimizer (service lookup, provider scans, scoring, KB search) and
exhausting the agent's per-turn orchestration budget.

Privacy: stores ONLY service / date / provider / window + the alternative slots and
timestamps. No client names, phone numbers, or other PII ever enter this store — put()
copies just {time, employee_id, reason_codes} out of each alternative, and the record
shape is asserted against _ALLOWED.

Durability: this reference implementation is an in-process dict, safe across turns within
ONE running instance. If the instance restarts, or requests are split across instances, a
lookup MISS is returned as status 'cache_miss' — callers MUST treat that as "recommendation
not found" and safely re-run suggest_best_slot. A shared store (e.g. Redis) can be dropped
in behind put() / get_next() / peek() / cleanup() without changing callers.
"""
import secrets
import threading
import time as _time

TTL_SECONDS = 900  # ~15 minutes

_LOCK = threading.RLock()
_STORE = {}  # recommendation_id -> record dict

# The ONLY keys a stored record may contain. Guards against caching PII by construction.
_ALLOWED = {"service_id", "date", "employee", "window",
            "alternatives", "created_at", "expires_at"}


def _clock(now):
    return _time.time() if now is None else now


def cleanup(now=None):
    """Drop every expired record. Returns the count removed."""
    t = _clock(now)
    with _LOCK:
        dead = [k for k, v in _STORE.items() if v["expires_at"] <= t]
        for k in dead:
            _STORE.pop(k, None)
    return len(dead)


def put(service_id, date, employee, window, alternatives, ttl=TTL_SECONDS, now=None):
    """Store a recommendation set; return (recommendation_id, expires_at_epoch).
    `alternatives` = the not-yet-offered slots. Only {time, employee_id, reason_codes} are
    copied out of each — anything else (incl. PII) is dropped."""
    t = _clock(now)
    cleanup(t)
    rid = "rec_" + secrets.token_urlsafe(9)
    rec = {
        "service_id": service_id or "",
        "date": date or "",
        "employee": employee or "",
        "window": list(window) if window else None,
        "alternatives": [{"time": a.get("time"),
                          "employee_id": a.get("employee_id"),
                          "reason_codes": list(a.get("reason_codes") or [])}
                         for a in (alternatives or [])],
        "created_at": t,
        "expires_at": t + ttl,
    }
    assert set(rec.keys()) <= _ALLOWED, "rec_cache would store a disallowed key"
    with _LOCK:
        _STORE[rid] = rec
    return rid, rec["expires_at"]


def peek(rid):
    """Return a shallow copy of the stored record (tests/inspection), or None."""
    with _LOCK:
        r = _STORE.get(rid)
        if not r:
            return None
        return {k: (list(v) if isinstance(v, list) else v) for k, v in r.items()}


def get_next(rid, revalidate_fn=None, now=None):
    """Advance to the next still-valid alternative for a recommendation.

    revalidate_fn(service_id, date, time, employee) -> bool checks the exact slot against
    live availability. If None, no revalidation is done. Any alternative that fails
    revalidation is dropped (advanced past) and the next is tried.

    Returns a dict with status:
      ok                     -> 'recommended' holds the next time to offer
      no_more_alternatives   -> cache emptied (see 'skipped' for any dropped times)
      recommendation_expired -> the set aged out of the cache
      cache_miss             -> unknown/evicted id; caller should re-run suggest_best_slot
    """
    t = _clock(now)
    with _LOCK:
        rec = _STORE.get(rid)
        if rec is None:
            return {"status": "cache_miss", "recommendation_id": rid}
        if rec["expires_at"] <= t:
            _STORE.pop(rid, None)
            return {"status": "recommendation_expired", "recommendation_id": rid}

    skipped = []
    while True:
        with _LOCK:
            rec = _STORE.get(rid)
            if rec is None:
                return {"status": "cache_miss", "recommendation_id": rid}
            if rec["expires_at"] <= _clock(now):
                _STORE.pop(rid, None)
                return {"status": "recommendation_expired", "recommendation_id": rid}
            alts = rec["alternatives"]
            if not alts:
                return {"status": "no_more_alternatives",
                        "recommendation_id": rid, "skipped": skipped}
            cand = alts.pop(0)  # advance/remove regardless of the revalidation result
        ok = True
        if revalidate_fn is not None:
            try:
                ok = bool(revalidate_fn(rec["service_id"], rec["date"], cand.get("time"),
                                        cand.get("employee_id") or rec.get("employee", "")))
            except Exception:
                ok = False
        if ok:
            return {"status": "ok", "recommendation_id": rid,
                    "recommended": {"time": cand.get("time"),
                                    "employee_id": cand.get("employee_id"),
                                    "reason_codes": cand.get("reason_codes") or []},
                    "revalidated": revalidate_fn is not None,
                    "skipped": skipped}
        skipped.append(cand.get("time"))


def _reset_for_tests():
    with _LOCK:
        _STORE.clear()


# =============================== UNIT TESTS ===============================
def _run_tests():
    passed = 0

    def ok(name, cond):
        nonlocal passed
        assert cond, "FAIL: " + name
        passed += 1
        print("  ok  " + name)

    A = [{"time": "12:45", "employee_id": "E1", "reason_codes": ["ADJACENT"]},
         {"time": "15:30", "employee_id": "E2", "reason_codes": ["EDGE"]}]
    yes = lambda *a: True
    no = lambda *a: False

    # 1-3) sequential declines exhaust the two alternatives in order
    _reset_for_tests()
    rid, _ = put("svc", "2026-07-25", "", None, A, now=1000)
    r1 = get_next(rid, revalidate_fn=yes, now=1001)
    ok("first decline -> alternative one", r1["status"] == "ok" and r1["recommended"]["time"] == "12:45")
    r2 = get_next(rid, revalidate_fn=yes, now=1002)
    ok("second decline -> alternative two", r2["status"] == "ok" and r2["recommended"]["time"] == "15:30")
    r3 = get_next(rid, revalidate_fn=yes, now=1003)
    ok("third decline -> no more alternatives", r3["status"] == "no_more_alternatives")

    # 4) cached alternative unavailable at revalidation -> skip it, return the next valid one
    _reset_for_tests()
    rid, _ = put("svc", "2026-07-25", "", None, A, now=1000)
    gone_first = lambda svc, d, tm, emp: tm != "12:45"
    r = get_next(rid, revalidate_fn=gone_first, now=1001)
    ok("stale alt skipped, next valid returned",
       r["status"] == "ok" and r["recommended"]["time"] == "15:30" and r["skipped"] == ["12:45"])

    # 4b) every remaining alternative unavailable -> no_more_alternatives
    _reset_for_tests()
    rid, _ = put("svc", "2026-07-25", "", None, A, now=1000)
    r = get_next(rid, revalidate_fn=no, now=1001)
    ok("all stale -> no more alternatives",
       r["status"] == "no_more_alternatives" and r["skipped"] == ["12:45", "15:30"])

    # 5) expired recommendation id
    _reset_for_tests()
    rid, _ = put("svc", "2026-07-25", "", None, A, ttl=900, now=1000)
    r = get_next(rid, revalidate_fn=yes, now=1000 + 901)
    ok("expired recommendation id", r["status"] == "recommendation_expired")

    # 6) invalid / unknown recommendation id
    _reset_for_tests()
    r = get_next("rec_does_not_exist", revalidate_fn=yes, now=1000)
    ok("invalid recommendation id -> cache_miss", r["status"] == "cache_miss")

    # 7) two simultaneous conversations stay isolated
    _reset_for_tests()
    ridA, _ = put("svcA", "2026-07-25", "", None,
                  [{"time": "09:00", "employee_id": "EA", "reason_codes": []}], now=1000)
    ridB, _ = put("svcB", "2026-07-26", "", None,
                  [{"time": "17:00", "employee_id": "EB", "reason_codes": []}], now=1000)
    rA = get_next(ridA, revalidate_fn=yes, now=1001)
    ok("conversation A isolated", rA["recommended"]["time"] == "09:00")
    pB = peek(ridB)
    ok("conversation B untouched by A",
       len(pB["alternatives"]) == 1 and pB["alternatives"][0]["time"] == "17:00")
    rB = get_next(ridB, revalidate_fn=yes, now=1002)
    ok("conversation B independent", rB["recommended"]["time"] == "17:00")

    # 8) no personal data stored (PII passed in is dropped)
    _reset_for_tests()
    rid, _ = put("svc", "2026-07-25", "E9", ("12:00", "17:00"),
                 [{"time": "12:45", "employee_id": "E1", "reason_codes": ["ADJACENT"],
                   "client_name": "Jane Doe", "phone": "5125551234"}], now=1000)
    rec = peek(rid)
    ok("record has only allowed keys", set(rec.keys()) <= _ALLOWED)
    alt0 = rec["alternatives"][0]
    ok("no PII leaked into cached alternative",
       set(alt0.keys()) == {"time", "employee_id", "reason_codes"}
       and "client_name" not in alt0 and "phone" not in alt0)

    # 9) cache cleanup after expiration
    _reset_for_tests()
    rid, _ = put("svc", "2026-07-25", "", None, A, ttl=900, now=1000)
    removed = cleanup(now=1000 + 901)
    ok("expired record cleaned up", removed == 1 and peek(rid) is None)
    ok("store empty after cleanup", len(_STORE) == 0)

    print("\nALL %d ASSERTIONS PASSED" % passed)


if __name__ == "__main__":
    _run_tests()
