"""
date_labels.py — pure, network-free date-relativity helpers.

Purpose: stop the agent from guessing "today" vs "tomorrow" or parroting a relative
word from an earlier message. Given an authoritative anchor ("today"), any date is
labeled deterministically here, on the server, so the agent can just read the correct
day back to the client.

This directly addresses the failure where a confirmation sent last night ("...tomorrow
at 1:15") was echoed the next morning as "tomorrow" when the appointment was actually
TODAY. With a server-computed label the agent states "today (Tuesday, Jul 28)" instead.

No network, no server deps. Unit-tested in isolation.
"""
from datetime import date, datetime

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _to_date(d):
    """Accept a date, datetime, or 'YYYY-MM-DD...' string; return a date. Raise on junk."""
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def relative_label(target, today):
    """'today' | 'tomorrow' | 'yesterday' | 'in N days' | 'N days ago'."""
    delta = (_to_date(target) - _to_date(today)).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if delta > 1:
        return "in %d days" % delta
    return "%d days ago" % (-delta)


def pretty(target):
    """'Tuesday, Jul 28' — absolute, unambiguous."""
    t = _to_date(target)
    return "%s, %s %d" % (_WEEKDAYS[t.weekday()], _MONTHS[t.month - 1], t.day)


def date_label(target, today):
    """Combined, safe-to-read-aloud label: 'today (Tuesday, Jul 28)'.
    Always pairs the relative word with the absolute date so a stale relative word
    from an earlier message can never be repeated blindly."""
    return "%s (%s)" % (relative_label(target, today), pretty(target))


def is_past(target, today):
    """True if target date is before today (already passed)."""
    return _to_date(target) < _to_date(today)


# =============================== UNIT TESTS ===============================
def _run_tests():
    passed = 0

    def ok(name, cond):
        nonlocal passed
        assert cond, "FAIL: " + name
        passed += 1
        print("  ok  " + name)

    today = "2026-07-28"  # a Tuesday

    # THE Katie case: confirmation sent 2026-07-27 said "tomorrow at 1:15".
    # The next morning (today = 07-28) the appointment is 07-28 => must read "today", not "tomorrow".
    ok("katie: appt 07-28 relative to today 07-28 = today",
       relative_label("2026-07-28", today) == "today")
    ok("katie: appt 07-28 is NOT tomorrow", relative_label("2026-07-28", today) != "tomorrow")
    ok("katie: full label", date_label("2026-07-28", today) == "today (Tuesday, Jul 28)")

    # Basic relatives
    ok("tomorrow", relative_label("2026-07-29", today) == "tomorrow")
    ok("yesterday", relative_label("2026-07-27", today) == "yesterday")
    ok("in 3 days", relative_label("2026-07-31", today) == "in 3 days")
    ok("4 days ago", relative_label("2026-07-24", today) == "4 days ago")

    # pretty / weekday correctness
    ok("pretty tue", pretty("2026-07-28") == "Tuesday, Jul 28")
    ok("pretty wed", pretty("2026-07-29") == "Wednesday, Jul 29")
    ok("pretty month boundary", pretty("2026-08-01") == "Saturday, Aug 1")

    # accepts datetime strings and datetime/date objects
    ok("accepts full datetime string", relative_label("2026-07-29T13:15:00", today) == "tomorrow")
    ok("accepts date object", relative_label(date(2026, 7, 28), date(2026, 7, 28)) == "today")
    ok("accepts datetime object", relative_label(datetime(2026, 7, 29, 13, 15), today) == "tomorrow")

    # is_past
    ok("is_past true", is_past("2026-07-27", today) is True)
    ok("is_past false today", is_past("2026-07-28", today) is False)
    ok("is_past false future", is_past("2026-07-29", today) is False)

    # year boundary
    ok("year boundary tomorrow", relative_label("2027-01-01", "2026-12-31") == "tomorrow")

    print("\nALL %d ASSERTIONS PASSED" % passed)


if __name__ == "__main__":
    _run_tests()
