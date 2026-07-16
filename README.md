# Meevo → Conduit MCP Server — V11 (Jacquelyn's Spa)

Clean rebuild of the proven v10 Python/FastMCP server. Connects Meevo to Conduit
so Hazel can look up clients, check availability, and book/reschedule/cancel.

**Architecture:** `Hazel → Conduit (MCP connection) → this server (always-on host) → Meevo (Public API + OB API)`

## What's in this repo
| File | Purpose |
|---|---|
| `meevo_mcp_server.py` | The V11 MCP server |
| `test_harness.py` | Run Meevo calls **without Conduit** to localize failures |
| `requirements.txt` | `fastmcp`, `requests` |
| `render.yaml` | Render blueprint (Starter = always-on) |
| `.env.example` | Env var template (copy to `.env` locally) |
| `.gitignore` | Keeps `.env` out of git |

## What V11 changes vs v10
- **Confirmation tokens on writes** — book/reschedule/cancel return a token first; they
  execute only when re-called with `confirm=true` + the token. Hazel can't act without approval.
- **Idempotency keys on writes** — a duplicate call returns the original result, so a flaky
  Conduit retry can't double-book or double-cancel.
- **One `ENDPOINTS` block** — all Meevo paths in one place, each marked `# VERIFY`.
- **Structured logging + `test_harness.py`** — decide Conduit-vs-Meevo faults with evidence.
- Read-only vs write tools clearly separated (supports a two-connection split in Conduit).

## Confirmed config (from Welcome Kit + live docs)
- TenantId **502388**, LocationId **503369**, cluster **NA2** → base `https://na2pub.meevo.com`
- Auth: POST `client_id`/`client_secret` to the Marketplace token URL → Bearer JWT (1 hr)
- Every call sends **TenantId + LocationId as query params**

## Verified endpoints (read from docs.meevoapi.com, 2026-07-15)
| Tool | Method + Path | Status |
|---|---|---|
| list_services | GET `/publicapi/v1/services` | ✅ |
| list_staff | GET `/publicapi/v1/employees` | ✅ |
| lookup_client | GET `/publicapi/v1/client/{id}` | ✅ |
| get_client_appointments | GET `/publicapi/v1/book/client/{id}/services` | ✅ |
| check_availability | POST `/publicapi/v2/scan/openings` | ✅ (needs ScanDateType/ScanTimeType) |
| search_clients | POST `/publicapi/v1/clients/lookup` | ✅ path; ⚠ filter body |
| get_client_memberships | GET `/publicapi/v1/clientmemberships` | ✅ |
| get_client_gift_cards | GET `/publicapi/v1/client/{id}/giftCards` | ✅ |
| book_appointment | POST `/publicapi/v1/book/service` | ✅ path; ⚠ body |
| reschedule_appointment | PUT `/publicapi/v1/book/service/{id}` | ✅ |
| cancel_appointment | DELETE `/publicapi/v1/book/service/{id}` | ✅ |

Two items still to confirm on the first live run: the **clients/lookup filter body** and the
**book/service create body** (both were empty placeholders in the docs collection).

## Verify calls locally — READ-ONLY, no Conduit (do this first)
```bash
pip install -r requirements.txt
cp .env.example .env          # fill in App ID + App Secret (never commit)
export $(grep -v '^#' .env | xargs)
python test_harness.py auth                      # 1 token works
python test_harness.py services                  # 2 list services
python test_harness.py staff                     # 3 list staff
python test_harness.py client "SomeLastName"     # 4 client search (confirms filter body)
python test_harness.py lookup <CLIENT_ID>        #   client by id (id from step 4)
python test_harness.py appts <CLIENT_ID>         # 5 client appointments
python test_harness.py enum ScanDateType         #   get the required enum values...
python test_harness.py enum ScanTimeType
python test_harness.py avail <SERVICE_ID> 2026-07-20 2026-07-27 "" <DATETYPE> <TIMETYPE>  # 6 availability
```
Failing HERE = Meevo/our code (check the `EP` path/body). Works here but fails via Conduit = Conduit's fault.
**No write tests (book/reschedule/cancel) until you approve them.**

## Deploy (Render, always-on)
1. Push this folder to a **public** GitHub repo.
2. Render → **New → Blueprint** (uses `render.yaml`) — or New → Web Service:
   - Build: `pip install -r requirements.txt`
   - Start: `python meevo_mcp_server.py`
   - Plan: **Starter** (always-on; avoids the free-tier 30–50s cold start)
3. Set env vars (App ID, App Secret, Location ID) as **secrets** in the dashboard.
4. Wait for **Live**, then open `https://YOUR-SERVICE.onrender.com/health` → should show `OK v11`.

## Connect to Conduit
1. Conduit → Connections → **Add MCP Server**.
2. URL: `https://YOUR-SERVICE.onrender.com/mcp`
3. Conduit discovers the tools. **Do not delete the old MCP connection yet.**
4. Recommended: add it **twice** as two connections and mentally treat them as
   Read-Only (lookups/availability) vs Actions (book/reschedule/cancel), enabling
   Actions only when you want Hazel to be able to write.

> ⚠ Every Render redeploy: **remove and re-add** the MCP URL in Conduit, or tool calls fail
> on a stale connection. This is Conduit/MCP behavior, not a hosting issue.

## Staged testing (do NOT write to real clients without explicit approval)
1. list_services → 2. list_staff → 3. lookup_client → 4. get_client_appointments →
5. check_availability → 6. book/reschedule/cancel **only** with your explicit go-ahead.

## Phase 2 (mapped, not the focus yet)
- `get_client_memberships` / `get_client_gift_cards` — endpoints are now confirmed and already
  included above as read-only bonuses.
- **Meevo → Conduit communication mirroring:** the docs confirm **MeevoEvents webhooks**
  (Appointment Created/Updated return client email + phone; Client events too). Webhooks are the
  clean one-way mirroring path — no polling needed. Map/build later.
