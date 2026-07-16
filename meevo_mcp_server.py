"""
Meevo -> Conduit MCP Server  (V11)
==================================
Clean rebuild of the proven v10 server, now built on ENDPOINTS VERIFIED against
the live Meevo Public API docs (docs.meevoapi.com collection, read 2026-07-15).

Key findings that shaped this build:
  * Availability is a PUBLIC API call: POST /publicapi/v2/scan/openings (OAuth
    Bearer, same token as everything else). The separate Online Booking (OB) API
    that v10 used is NO LONGER NEEDED — it was a workaround for the scan returning
    empty, which was actually caused by omitting required fields (ScanDateType /
    ScanTimeType). So v11 drops the OB session / User-Agent spoof entirely.
  * EVERY call requires TenantId + LocationId as QUERY params (capitalized).
  * Auth: POST appId/appSecret as client_id/client_secret in the body; Bearer JWT
    valid 3600s, scope "meevo:scope".

Verified paths are marked ✅. A few request-body shapes weren't populated in the
docs collection (client lookup filter; booking body) — marked ⚠ CONFIRM VIA HARNESS.

Secrets come ONLY from env vars. Never commit real credentials.
"""

import os
import re
import time
import json
import uuid
import logging

import requests
from fastmcp import FastMCP
from starlette.responses import PlainTextResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [v11] %(levelname)s: %(message)s")
log = logging.getLogger("meevo-v11")
SERVER_VERSION = "v11"

# ------------------------------------------------------------------ config
def _normalize_host(url: str) -> str:
    """Accept host or full base; return scheme+host only (strip /publicapi/... )."""
    url = (url or "").rstrip("/")
    return re.sub(r"/publicapi(/v\d+)?$", "", url)

APP_ID      = os.environ.get("MEEVO_APP_ID", "")
APP_SECRET  = os.environ.get("MEEVO_APP_SECRET", "")
AUTH_URL    = os.environ.get("MEEVO_AUTH_URL", "https://marketplace.meevo.com/oauth2/token")
BASE_URL    = _normalize_host(os.environ.get("MEEVO_BASE_URL", "https://na2pub.meevo.com"))
TENANT_ID   = os.environ.get("MEEVO_TENANT_ID", "502388")     # ✅ Jacquelyns Spa
LOCATION_ID = os.environ.get("MEEVO_LOCATION_ID", "503369")   # ✅ Jacquelyns Spa
PORT        = int(os.environ.get("PORT", "10000"))

# ================= VERIFIED ENDPOINTS (paths relative to BASE_URL) ==========
EP = {
    "services":          "/publicapi/v1/services",                      # ✅ GET list
    "service_get":       "/publicapi/v1/service/{id}",                  # ✅ GET
    "employees":         "/publicapi/v1/employees",                     # ✅ GET list
    "clients_lookup":    "/publicapi/v1/clients/lookup",                # ✅ POST (filter body ⚠ confirm)
    "client_get":        "/publicapi/v1/client/{id}",                   # ✅ GET
    "client_services":   "/publicapi/v1/book/client/{id}/services",     # ✅ GET client's booked services
    "scan_openings":     "/publicapi/v2/scan/openings",                 # ✅ POST availability
    "book_create":       "/publicapi/v1/book/service",                  # ✅ POST [WRITE] body ⚠ confirm
    "book_update":       "/publicapi/v1/book/service/{id}",             # ✅ PUT  [WRITE]
    "book_cancel":       "/publicapi/v1/book/service/{id}",             # ✅ DELETE [WRITE]
    "client_memberships":"/publicapi/v1/clientmemberships",             # ✅ GET (Phase 2 bonus)
    "client_giftcards":  "/publicapi/v1/client/{id}/giftCards",         # ✅ GET (Phase 2 bonus)
    "enum_lookup":       "/publicapi/v1/system/enum/{name}",            # ✅ GET enum values
}


def config_status() -> dict:
    return {"app_id_set": bool(APP_ID), "app_secret_set": bool(APP_SECRET),
            "tenant_id": TENANT_ID, "location_id": LOCATION_ID, "base_url": BASE_URL}


# ------------------------------------------------------------------ auth
_tok = {"value": None, "exp": 0.0}


def get_token() -> str:
    now = time.time()
    if _tok["value"] and now < _tok["exp"] - 60:
        return _tok["value"]
    # Docs: POST client_id/client_secret in the body; Accept: application/json.
    # (grant_type included as it's standard; harness `auth` confirms this works.)
    data = {"grant_type": "client_credentials", "client_id": APP_ID, "client_secret": APP_SECRET}
    r = requests.post(AUTH_URL, data=data, headers={"Accept": "application/json"}, timeout=30)
    r.raise_for_status()
    d = r.json()
    _tok["value"] = d["access_token"]
    _tok["exp"] = now + int(d.get("expires_in", 3600))
    log.info("Token acquired (expires_in=%s).", d.get("expires_in"))
    return _tok["value"]


def api(method: str, ep_key: str, *, id: str = "", name: str = "",
        params=None, json_body=None) -> dict:
    """Authenticated Public API call. Always sends TenantId + LocationId query params."""
    path = EP[ep_key].format(id=id, name=name)
    q = {"TenantId": TENANT_ID, "LocationId": LOCATION_ID}
    q.update(params or {})
    headers = {"Authorization": f"Bearer {get_token()}", "Accept": "application/json"}
    url = f"{BASE_URL}{path}"
    log.info("%s %s", method, path)
    r = requests.request(method, url, headers=headers, params=q, json=json_body, timeout=45)
    r.raise_for_status()
    return r.json() if r.content else {"status": "ok", "http": r.status_code}


# --------------------------------------------- V11 write-safety primitives
_pending: dict[str, dict] = {}
_idem: dict[str, dict] = {}
_CONFIRM_TTL = 600


def _sig(a, p): return a + "|" + json.dumps(p, sort_keys=True, default=str)


def begin_write(action, params, summary):
    tok = uuid.uuid4().hex
    _pending[tok] = {"sig": _sig(action, params), "t": time.time()}
    return {"status": "confirmation_required", "action": action, "summary": summary,
            "confirmation_token": tok,
            "instructions": "Re-call with confirm=true and this confirmation_token ONLY after "
                            "the client explicitly approves the exact action above."}


def run_write(action, params, token, idem_key, executor):
    if idem_key and idem_key in _idem:
        log.info("Idempotency hit %s", idem_key)
        return _idem[idem_key]
    rec = _pending.get(token or "")
    if not token or not rec:
        return {"status": "error", "error": "confirmation_token missing/unknown — call once without confirm first."}
    if time.time() - rec["t"] > _CONFIRM_TTL:
        _pending.pop(token, None)
        return {"status": "error", "error": "confirmation_token expired — request a new one."}
    if rec["sig"] != _sig(action, params):
        return {"status": "error", "error": "confirmation_token does not match this exact action."}
    result = executor()
    result = result if isinstance(result, dict) else {"result": result}
    result.setdefault("status", "ok")
    if idem_key:
        _idem[idem_key] = result
    _pending.pop(token, None)
    return result


# ================================================================== MCP
mcp = FastMCP("Meevo Direct — Jacquelyn's Spa (v11)")


# ---- READ-ONLY ------------------------------------------------------------
@mcp.tool
def list_services(page_number: int = 1, items_per_page: int = 200) -> dict:
    """List bookable services. READ-ONLY."""
    return api("GET", "services", params={"PageNumber": page_number, "ItemsPerPage": items_per_page})


@mcp.tool
def list_staff(page_number: int = 1, items_per_page: int = 200) -> dict:
    """List employees/providers (EmployeeId UUIDs). READ-ONLY."""
    return api("GET", "employees", params={"PageNumber": page_number, "ItemsPerPage": items_per_page})


@mcp.tool
def search_clients(query: str) -> dict:
    """Search clients by name/phone/email via the lookup filter. READ-ONLY.
    ⚠ Filter body shape not in docs — confirm exact field names on first live run."""
    body = {"SearchText": query}   # ⚠ CONFIRM: may need FirstName/LastName/PhoneNumber/EmailAddress
    return api("POST", "clients_lookup", json_body=body)


@mcp.tool
def lookup_client(client_id: str) -> dict:
    """Get one client by ClientId. READ-ONLY."""
    return api("GET", "client_get", id=client_id)


@mcp.tool
def get_client_appointments(client_id: str) -> dict:
    """Get a client's booked services/appointments. READ-ONLY."""
    return api("GET", "client_services", id=client_id)


@mcp.tool
def check_availability(service_id: str, start_date: str, end_date: str,
                       employee_id: str = "",
                       scan_date_type: int = 0, scan_time_type: int = 0) -> dict:
    """
    Appointment openings for a service. READ-ONLY. POST /publicapi/v2/scan/openings.
    ScanDateType / ScanTimeType are REQUIRED enums — omitting them was the cause of the
    old empty-array bug. Use lookup_enum('ScanDateType') / lookup_enum('ScanTimeType')
    to get the correct integer values for this location, then pass them here.
    """
    svc = {"ServiceId": service_id}
    if employee_id:
        svc["EmployeeIds"] = [employee_id]
    body = {"ScanDateType": scan_date_type, "ScanTimeType": scan_time_type,
            "StartDate": start_date, "EndDate": end_date, "ScanServices": [svc]}
    return api("POST", "scan_openings", json_body=body)


@mcp.tool
def get_client_memberships(client_id: str) -> dict:
    """Client memberships. READ-ONLY. (Phase 2 bonus — endpoint verified.)"""
    return api("GET", "client_memberships", params={"ClientId": client_id})


@mcp.tool
def get_client_gift_cards(client_id: str) -> dict:
    """Client gift cards. READ-ONLY. (Phase 2 bonus — endpoint verified.)"""
    return api("GET", "client_giftcards", id=client_id)


@mcp.tool
def lookup_enum(enum_name: str) -> dict:
    """Get allowed values for a Meevo enum (e.g. 'ScanDateType'). READ-ONLY."""
    return api("GET", "enum_lookup", name=enum_name)


@mcp.tool
def debug_api() -> dict:
    """Diagnostics. READ-ONLY. Consider NOT exposing to Hazel in production."""
    return {"server_version": SERVER_VERSION, "config": config_status(), "endpoints": EP}


# ---- WRITE / ACTION (confirmation + idempotency gated) --------------------
@mcp.tool
def book_appointment(client_id: str, service_id: str, start_datetime: str,
                     employee_id: str = "", confirm: bool = False,
                     confirmation_token: str = "", idempotency_key: str = "") -> dict:
    """Book an appointment (POST /book/service). WRITE.
    ⚠ Request body shape not in docs — CONFIRM exact fields before enabling live writes."""
    params = {"client_id": client_id, "service_id": service_id,
              "start_datetime": start_datetime, "employee_id": employee_id}
    if not confirm:
        return begin_write("book_appointment", params,
                           f"Book service {service_id} for client {client_id} at "
                           f"{start_datetime} (staff {employee_id or 'any'}).")

    def _do():
        body = {"ClientId": client_id, "ServiceId": service_id,   # ⚠ CONFIRM field names
                "StartDateTime": start_datetime}
        if employee_id:
            body["EmployeeId"] = employee_id
        return api("POST", "book_create", json_body=body)

    return run_write("book_appointment", params, confirmation_token, idempotency_key, _do)


@mcp.tool
def reschedule_appointment(appointment_service_id: str, new_start_datetime: str,
                           confirm: bool = False, confirmation_token: str = "",
                           idempotency_key: str = "") -> dict:
    """Move a booked service (PUT /book/service/{id}). WRITE."""
    params = {"appointment_service_id": appointment_service_id,
              "new_start_datetime": new_start_datetime}
    if not confirm:
        return begin_write("reschedule_appointment", params,
                           f"Reschedule booked service {appointment_service_id} to {new_start_datetime}.")

    def _do():
        body = {"StartDateTime": new_start_datetime}   # ⚠ CONFIRM field names
        return api("PUT", "book_update", id=appointment_service_id, json_body=body)

    return run_write("reschedule_appointment", params, confirmation_token, idempotency_key, _do)


@mcp.tool
def cancel_appointment(appointment_service_id: str, confirm: bool = False,
                       confirmation_token: str = "", idempotency_key: str = "") -> dict:
    """Cancel a booked service (DELETE /book/service/{id}). WRITE."""
    params = {"appointment_service_id": appointment_service_id}
    if not confirm:
        return begin_write("cancel_appointment", params,
                           f"Cancel booked service {appointment_service_id}.")

    def _do():
        return api("DELETE", "book_cancel", id=appointment_service_id)

    return run_write("cancel_appointment", params, confirmation_token, idempotency_key, _do)


# ---- health + entrypoint --------------------------------------------------
@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    return PlainTextResponse(f"OK {SERVER_VERSION}")


if __name__ == "__main__":
    log.info("Starting Meevo MCP %s on :%s", SERVER_VERSION, PORT)
    for k, v in config_status().items():
        log.info("  %s = %s", k, v)
    mcp.run(transport="http", host="0.0.0.0", port=PORT)
