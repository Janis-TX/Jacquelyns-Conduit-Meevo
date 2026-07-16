"""
Conduit-independent read-only test harness (requirement R1).
Runs Meevo calls DIRECTLY — no Conduit, no MCP — so a failure here means Meevo/our
code, while "works here but fails in Conduit" means the fault is Conduit.

Load env first (creds + IDs), e.g.:  export $(grep -v '^#' .env | xargs)

READ-ONLY commands (safe to run):
    python test_harness.py auth
    python test_harness.py services
    python test_harness.py staff
    python test_harness.py client "LastName"
    python test_harness.py lookup <CLIENT_ID>
    python test_harness.py appts <CLIENT_ID>
    python test_harness.py enum ScanDateType        # then enum ScanTimeType
    python test_harness.py avail <SERVICE_ID> 2026-07-20 2026-07-27 [EMPLOYEE_ID] [DATETYPE] [TIMETYPE]
    python test_harness.py memberships <CLIENT_ID>
    python test_harness.py giftcards <CLIENT_ID>

Write actions (book/reschedule/cancel) are intentionally NOT runnable here.
"""

import sys
import json

import meevo_mcp_server as srv


def call(tool, *args):
    """Works whether FastMCP wraps the tool (.fn) or exposes a plain function."""
    return getattr(tool, "fn", tool)(*args)


def show(label, obj):
    print(f"\n=== {label} ===")
    print(json.dumps(obj, indent=2, default=str)[:4000])


def main(argv):
    if not argv:
        print(__doc__); return
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "auth":
            srv.get_token(); print("Auth token: OK")
        elif cmd == "services":
            show("services", call(srv.list_services))
        elif cmd == "staff":
            show("staff", call(srv.list_staff))
        elif cmd == "client":
            show("client search", call(srv.search_clients, rest[0]))
        elif cmd == "lookup":
            show("client", call(srv.lookup_client, rest[0]))
        elif cmd == "appts":
            show("client appointments", call(srv.get_client_appointments, rest[0]))
        elif cmd == "enum":
            show(f"enum {rest[0]}", call(srv.lookup_enum, rest[0]))
        elif cmd == "avail":
            emp = rest[3] if len(rest) > 3 else ""
            dt = int(rest[4]) if len(rest) > 4 else 0
            tt = int(rest[5]) if len(rest) > 5 else 0
            show("availability", call(srv.check_availability, rest[0], rest[1], rest[2], emp, dt, tt))
        elif cmd == "memberships":
            show("memberships", call(srv.get_client_memberships, rest[0]))
        elif cmd == "giftcards":
            show("gift cards", call(srv.get_client_gift_cards, rest[0]))
        else:
            print(f"Unknown command: {cmd}\n"); print(__doc__)
    except Exception as e:  # noqa: BLE001
        print(f"\nFAILED ({cmd}): {type(e).__name__}: {e}")
        print("Failure HERE = Meevo/our code (not Conduit). Check the matching EP path/body/params.")


if __name__ == "__main__":
    main(sys.argv[1:])
