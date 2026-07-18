"""
Meevo MCP Server
================
Exposes Meevo API endpoints as MCP tools so Conduit's AI agent can look up
clients, appointments, and services in real-time, and book/reschedule/cancel.

Version: v25 - built on v24, fixed against the VERIFIED Meevo Public API
(docs.meevoapi.com collection, 2026-07-15). Changes vs v24:

  1. CLIENT LOOKUP: use documented POST /publicapi/v1/clients/lookup (server-side
     filter) instead of paging the entire client list (which was unreliable AND
     burned the 1000/day API budget). Falls back to paging only if lookup fails.
  2. APPOINTMENTS: primary source is now GET /publicapi/v1/book/client/{id}/services
     (verified). SFTP/DDS CSV is demoted to last-resort fallback.
  3. QUERY PARAMS: TenantId/LocationId now sent with documented capitalized keys.
  4. AVAILABILITY: keeps the OB scanforopenings path (rich booking data) but adds a
     Public API /publicapi/v2/scan/openings fallback, makes ScanDateType/ScanTimeType
     overridable, and adds lookup_enum() so the correct enum values can be verified.
  5. WRITE SAFETY: book/reschedule/cancel now require a confirmation token and support
     an idempotency key (a flaky/retrying caller can't double-book or double-cancel).
  6. SECURITY: removed hardcoded App ID / App Secret defaults (they must come from env).

Secrets come ONLY from environment variables. Never commit real credentials.
"""

import base64
import csv
import io
import json
import os
import re
import time
import uuid
import requests
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from mcp.server.fastmcp import FastMCP

# ---- config (NO secrets hardcoded; all from env) --------------------------
APP_ID = os.environ.get("MEEVO_APP_ID", "")
APP_SECRET = os.environ.get("MEEVO_APP_SECRET", "")
AUTH_URL = os.environ.get("MEEVO_AUTH_URL", "https://marketplace.meevo.com/oauth2/token")
BASE_URL = os.environ.get("MEEVO_BASE_URL", "https://na2pub.meevo.com")
TENANT_ID = os.environ.get("MEEVO_TENANT_ID", "502388")
LOCATION_ID = os.environ.get("MEEVO_LOCATION_ID", "503369")

SFTP_HOST = os.environ.get("MEEVO_SFTP_HOST", "cdcsftp.meevo.com")
SFTP_USER = os.environ.get("MEEVO_SFTP_USER", "JacquelynsSpa")
SFTP_KEY_B64 = os.environ.get("MEEVO_SFTP_KEY_B64", "")
SFTP_PATH = os.environ.get("MEEVO_SFTP_PATH", "/pmvo2-cdcsftp-storage01/MeevoTemp/SFTP/JacquelynsSpa")


def _ob_base():
    host = BASE_URL.rstrip("/")
    host = re.sub(r'pub\.meevo\.com', '.meevo.com', host)
    host = re.sub(r'devpub\.meevodev\.com', '.meevodev.com', host)
    return host + "/onlinebooking/api/ob"


OB_BASE = _ob_base()

# Spa's local timezone — dates ("today", availability windows) are computed here,
# NOT in the server's UTC, so "today/tomorrow" never shifts by one near midnight.
SPA_TZ = ZoneInfo(os.environ.get("MEEVO_TZ", "America/Chicago"))


def _today():
    return datetime.now(SPA_TZ).date()


_token = None
_token_expiry = 0.0
_ob_token = None
_ob_token_expiry = 0.0


def get_token():
    global _token, _token_expiry
    if _token and time.time() < _token_expiry:
        return _token
    r = requests.post(AUTH_URL,
                      data={"client_id": APP_ID, "client_secret": APP_SECRET,
                            "grant_type": "client_credentials"},
                      headers={"Accept": "application/json"}, timeout=10)
    r.raise_for_status()
    d = r.json()
    _token = d["access_token"]
    _token_expiry = time.time() + d.get("expires_in", 3600) - 60
    return _token


def _auth_headers():
    return {"Authorization": f"Bearer {get_token()}", "Accept": "application/json",
            "Content-Type": "application/json"}


def get_ob_token():
    global _ob_token, _ob_token_expiry
    if _ob_token and time.time() < _ob_token_expiry:
        return _ob_token
    r = requests.patch(
        f"{OB_BASE}/session",
        json={"TenantId": int(TENANT_ID), "LocationId": int(LOCATION_ID)},
        headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": f"https://na2.meevo.com/CustomerPortal/onlinebooking/booking/services?tenantId={TENANT_ID}&locationId={LOCATION_ID}",
            "Origin": "https://na2.meevo.com",
        },
        timeout=10,
    )
    r.raise_for_status()
    d = r.json()
    _ob_token = d.get("bearerToken") or d.get("BearerToken")
    _ob_token_expiry = time.time() + 1800
    return _ob_token


def _ob_headers():
    return {"Authorization": f"Bearer {get_ob_token()}", "Content-Type": "application/json",
            "Accept": "application/json"}


# ---- documented query params: TenantId / LocationId (capitalized) ---------
def _cap_params(extra=None):
    p = {"TenantId": int(TENANT_ID), "LocationId": int(LOCATION_ID)}
    if extra:
        p.update(extra)
    return p


def meevo_get(path, params=None):
    base = {"TenantId": TENANT_ID, "LocationId": LOCATION_ID}   # was lowercase in v24
    if params:
        base.update(params)
    r = requests.get(f"{BASE_URL}{path}", params=base, headers=_auth_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def meevo_post(path, body, extra_params=None):
    r = requests.post(f"{BASE_URL}{path}", params=_cap_params(extra_params), json=body,
                      headers=_auth_headers(), timeout=15)
    r.raise_for_status()
    return r.json() if r.content else {}


def meevo_put(path, body, extra_params=None):
    r = requests.put(f"{BASE_URL}{path}", params=_cap_params(extra_params), json=body,
                     headers=_auth_headers(), timeout=15)
    r.raise_for_status()
    return r.json() if r.content else {"success": True}


def _items(data):
    for key in ("Clients", "Appointments", "Services", "Employees", "Data", "Items",
                "Results", "Records", "clients", "appointments", "services",
                "employees", "data", "items", "results", "records"):
        if isinstance(data, dict) and key in data:
            return data[key]
    if isinstance(data, list):
        return data
    return []


def _get(obj, *keys, default=""):
    for k in keys:
        v = obj.get(k)
        if v is not None:
            return v
    return default


def _str(v):
    return "" if v is None else str(v)


# ---- V25 write-safety: confirmation tokens + idempotency ------------------
_pending = {}     # confirmation_token -> {"sig":..., "t":...}
_idem = {}        # idempotency_key    -> cached result
_CONFIRM_TTL = 600


def _sig(action, params):
    return action + "|" + json.dumps(params, sort_keys=True, default=str)


def _begin_write(action, params, summary):
    tok = uuid.uuid4().hex
    _pending[tok] = {"sig": _sig(action, params), "t": time.time()}
    return {"status": "confirmation_required", "action": action, "summary": summary,
            "confirmation_token": tok,
            "instructions": "Re-call with confirm=true and this confirmation_token ONLY "
                            "after the client explicitly approves the exact action above."}


def _confirm_ok(action, params, token):
    if not token:
        return "confirmation_token missing - call once without confirm to obtain one."
    rec = _pending.get(token)
    if not rec:
        return "confirmation_token unknown or already used."
    if time.time() - rec["t"] > _CONFIRM_TTL:
        _pending.pop(token, None)
        return "confirmation_token expired - request a new one."
    if rec["sig"] != _sig(action, params):
        return "confirmation_token does not match this exact action."
    return None


mcp = FastMCP("Meevo", host="0.0.0.0", stateless_http=True)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import PlainTextResponse
    return PlainTextResponse("OK v31")


# ======================= READ-ONLY TOOLS ===================================
@mcp.tool()
def debug_api(path: str) -> dict:
    """Call any Meevo Public API path (GET) and return the raw response."""
    try:
        data = meevo_get(path)
        return {"path": path, "keys": list(data.keys()) if isinstance(data, dict) else None,
                "sample": str(data)[:3000]}
    except requests.HTTPError as e:
        return {"error": str(e), "status": e.response.status_code if e.response else None,
                "body": e.response.text[:500] if e.response else ""}


def lookup_enum(enum_name: str) -> dict:
    """Internal helper only - NOT exposed as an agent tool. (It was causing the agent to loop
    trying to resolve scan enums instead of just calling check_availability.)"""
    try:
        return {"enum": enum_name, "values": meevo_get(f"/publicapi/v1/system/enum/{enum_name}")}
    except requests.HTTPError as e:
        return {"error": str(e), "body": e.response.text[:500] if e.response else ""}


def _client_candidates(phone="", email="", last_name=""):
    """Prefer documented server-side lookup, fall back to paging only if needed.
    Per Meevo docs, POST /publicapi/v1/clients/lookup takes a LIST of ONE attribute type
    (ClientIds OR EmailAddresses OR PhoneNumbers) — only one type at a time. We try a few
    likely body encodings and use the first that returns matches (name-only search can't use
    this endpoint, so it falls through to paging)."""
    bodies = []
    if phone:
        d = re.sub(r'\D', '', phone)
        d10 = d.lstrip('1')
        for v in dict.fromkeys([d10, d, phone]):   # ordered, de-duped
            bodies += [[v], {"PhoneNumbers": [v]}, {"phoneNumbers": [v]}]
    elif email:
        e = email.strip()
        bodies += [[e], {"EmailAddresses": [e]}, {"emailAddresses": [e]}]
    for b in bodies:
        try:
            items = _items(meevo_post("/publicapi/v1/clients/lookup", b))
            if items:
                return items, "clients/lookup"
        except requests.HTTPError:
            continue
    # FALLBACK: page GET /publicapi/v1/clients (v24 behavior; capped to protect API budget)
    all_clients = []
    page_size = None
    for page_num in range(1, 51):   # cap 50 pages so we never exhaust the daily budget
        try:
            params = {"pageNumber": page_num}
            if last_name:
                params["lastName"] = last_name
            data = meevo_get("/publicapi/v1/clients", params)
        except requests.HTTPError:
            if page_num > 1:
                break
            raise
        batch = _items(data)
        if not batch:
            break
        all_clients.extend(batch)
        if page_size is None:
            page_size = len(batch)
        if len(batch) < (page_size or 1):
            break
    return all_clients, "clients/paged"


def _phone_digits(p):
    return re.sub(r'\D', '', _str(p)).lstrip('1')[-10:]


def _shape_client(c):
    phones = c.get("phoneNumbers") or c.get("PhoneNumbers") or []
    return {
        "client_id": _get(c, "ClientId", "clientId", "Id", "id"),
        "name": f"{_get(c, 'FirstName', 'firstName')} {_get(c, 'LastName', 'lastName')}".strip(),
        "email": _get(c, "Email", "email", "EmailAddress", "emailAddress"),
        "phones": [_get(p, "Number", "number", "PhoneNumber", "phoneNumber") for p in phones],
        "birth_date": _get(c, "BirthDate", "birthDate"),
        "notes": _get(c, "Notes", "notes"),
    }


@mcp.tool()
def lookup_client(phone: str = "", email: str = "") -> dict:
    """Look up a Meevo client by phone or email. Uses Meevo's server-side lookup, then
    verifies an EXACT match locally (last 10 phone digits / exact email) so unknown
    numbers return found=False instead of resolving to the wrong client."""
    if not phone and not email:
        return {"error": "Provide a phone number or email."}
    target_phone = _phone_digits(phone) if phone else ""
    target_email = email.strip().lower()
    try:
        clients, source = _client_candidates(phone=phone, email=email)
    except requests.HTTPError as e:
        return {"error": str(e), "body": e.response.text[:500] if e.response else ""}

    def _phone_hit(c):
        if not target_phone:
            return False
        for p in (c.get("phoneNumbers") or c.get("PhoneNumbers") or []):
            if _phone_digits(_get(p, "number", "Number", "phoneNumber", "PhoneNumber")) == target_phone:
                return True
        return False

    def _email_hit(c):
        return bool(target_email) and _str(_get(c, "emailAddress", "email", "Email", "EmailAddress")).lower() == target_email

    matches = [c for c in clients if (_phone_hit(c) or _email_hit(c))]
    if not matches:
        return {"found": False, "searched": len(clients), "source": source,
                "message": f"No client found for {phone or email}. Create a new client or ask "
                           f"for their name - do NOT book under an existing client."}
    if len(matches) > 1:
        return {"found": True, "ambiguous": True, "match_count": len(matches),
                "clients": [_shape_client(c) for c in matches[:5]],
                "message": "Multiple clients share this contact info. Confirm the name before booking."}
    return {"found": True, "source": source, **_shape_client(matches[0])}


@mcp.tool()
def search_clients(last_name: str = "", first_name: str = "", phone: str = "", email: str = "") -> dict:
    """Search for Meevo clients by name, phone, or email."""
    target_phone = _phone_digits(phone) if phone else ""
    try:
        clients, source = _client_candidates(phone=phone, email=email, last_name=last_name)
    except requests.HTTPError as e:
        return {"error": str(e), "body": e.response.text[:500] if e.response else ""}
    matches = []
    for c in clients:
        c_last = _str(_get(c, "lastName", "LastName")).lower()
        c_first = _str(_get(c, "firstName", "FirstName")).lower()
        c_email = _str(_get(c, "emailAddress", "email", "Email", "EmailAddress")).lower()
        c_phones = [_phone_digits(_get(p, "number", "Number", "phoneNumber", "PhoneNumber"))
                    for p in (c.get("phoneNumbers") or c.get("PhoneNumbers") or [])]
        if last_name and last_name.lower() not in c_last:
            continue
        if first_name and first_name.lower() not in c_first:
            continue
        if email and email.lower() not in c_email:
            continue
        if target_phone and not any(target_phone == p for p in c_phones if p):
            continue
        matches.append(c)
    if not matches:
        return {"found": False, "searched": len(clients), "source": source,
                "message": f"No clients matching in {len(clients)} records."}
    return {"found": True, "source": source, "total_matches": len(matches),
            "clients": [_shape_client(c) for c in matches[:5]]}


def _appt_status(a):
    """This endpoint reports status as booleans, not a string — derive a readable one."""
    if a.get("IsCancelled") or a.get("isCancelled"):
        return "Cancelled"
    if a.get("IsCheckedOut") or a.get("isCheckedOut"):
        return "Completed"
    if a.get("IsCheckedIn") or a.get("isCheckedIn"):
        return "Checked In"
    if a.get("IsNoShow") or a.get("isNoShow"):
        return "No Show"
    return "Booked"


# id->name maps for services/employees (cached ~10 min; Meevo recommends caching these)
_idmaps = {"s": None, "e": None, "t": 0.0}


def _id_name_maps():
    if _idmaps["s"] is not None and time.time() - _idmaps["t"] < 600:
        return _idmaps["s"], _idmaps["e"]
    smap, emap = {}, {}
    try:
        for s in (list_services().get("services") or []):
            if s.get("id"):
                smap[s["id"]] = s.get("name", "")
    except Exception:
        pass
    try:
        for e in (list_staff().get("staff") or []):
            if e.get("id"):
                emap[e["id"]] = e.get("name", "")
    except Exception:
        pass
    _idmaps.update({"s": smap, "e": emap, "t": time.time()})
    return smap, emap


def _parse_client_services(data):
    out = []
    for svc in _items(data):
        out.append({
            "appointment_id": _str(_get(svc, "AppointmentId", "appointmentId")),
            "appointment_service_id": _str(_get(svc, "AppointmentServiceId", "appointmentServiceId", "id", "Id")),
            "service_id": _str(_get(svc, "ServiceId", "serviceId")),
            "service_name": "",   # filled by _enrich_names (endpoint returns only ServiceId)
            "employee_id": _str(_get(svc, "EmployeeId", "employeeId")),
            "employee_name": "",  # filled by _enrich_names
            "resource_id": _str(_get(svc, "ResourceId", "resourceId")),
            "start_time": _str(_get(svc, "StartTime", "startTime", "startDateTime", "StartDateTime")),
            "status": _appt_status(svc),
            "concurrency_check_digits": _str(_get(svc, "ConcurrencyCheckDigits", "concurrencyCheckDigits",
                                                   "RowVersion", "rowVersion")),
        })
    return out


def _enrich_names(appts):
    """Fill service_name/employee_name from their IDs (cached lookups)."""
    if not appts:
        return appts
    smap, emap = _id_name_maps()
    for a in appts:
        a["service_name"] = smap.get(a.get("service_id"), a.get("service_name", ""))
        a["employee_name"] = emap.get(a.get("employee_id"), a.get("employee_name", ""))
    return appts


def _appts_api_fallback(client_id, sd, ed):
    tried = {}
    for path, params in [
        (f"/publicapi/v1/clients/{client_id}/appointments", {"startDate": sd, "endDate": ed}),
        ("/publicapi/v1/appointments", {"ClientId": client_id, "StartDate": sd, "EndDate": ed}),
    ]:
        try:
            parsed = _parse_client_services(meevo_get(path, params))
            tried[path] = f"ok ({len(parsed)})" if parsed else "empty"
            if parsed:
                return parsed, tried
        except requests.HTTPError as e:
            tried[path] = f"{e.response.status_code}" if e.response else "err"
    return [], tried


def _appts_sftp_fallback(client_id, sd, ed):
    if not SFTP_KEY_B64:
        return {"error": "SFTP key not set; skipped SFTP fallback", "appointments": [], "count": 0}
    try:
        import paramiko
        key = paramiko.RSAKey.from_private_key(io.StringIO(base64.b64decode(SFTP_KEY_B64).decode()))
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(SFTP_HOST, username=SFTP_USER, pkey=key, timeout=20)
        sftp = ssh.open_sftp()
    except Exception as e:
        return {"error": f"SFTP connect failed: {e}", "appointments": [], "count": 0}
    try:
        files = sftp.listdir(SFTP_PATH)
        appt_files = sorted([f for f in files if any(k in f.lower() for k in ("appoint", "appt", "booking"))],
                            reverse=True)
        results = []
        for fname in appt_files[:3]:
            with sftp.open(f"{SFTP_PATH}/{fname}", "r") as f:
                reader = csv.DictReader(io.StringIO(f.read().decode("utf-8", errors="replace")))
                for row in reader:
                    rc = (row.get("ClientId") or row.get("clientId") or row.get("ClientGuid") or "")
                    if rc.lower() != client_id.lower():
                        continue
                    results.append({
                        "appointment_service_id": row.get("AppointmentServiceId") or row.get("appointmentServiceId") or "",
                        "service_name": row.get("ServiceName") or "",
                        "start_time": row.get("StartTime") or row.get("StartDate") or "",
                        "status": row.get("Status") or "",
                        "concurrency_check_digits": row.get("ConcurrencyCheckDigits") or row.get("RowVersion") or "",
                    })
            if results:
                break
        return {"appointments": results, "count": len(results)}
    finally:
        sftp.close(); ssh.close()


@mcp.tool()
def get_client_appointments(client_id: str, start_date: str = "", end_date: str = "") -> dict:
    """Get a client's upcoming appointments. Returns appointment_service_id and
    concurrency_check_digits needed for cancel/reschedule. Dates: YYYY-MM-DD
    (defaults today .. +90 days)."""
    sd = start_date or _today().isoformat()
    ed = end_date or (_today() + timedelta(days=90)).isoformat()
    tried = {}
    # PRIMARY (verified): a client's booked services
    try:
        parsed = _parse_client_services(meevo_get(f"/publicapi/v1/book/client/{client_id}/services",
                                                  {"startDate": sd, "endDate": ed}))
        tried["book/client/{id}/services"] = f"ok ({len(parsed)})" if parsed else "empty"
        if parsed:
            _enrich_names(parsed)
            return {"appointments": parsed, "count": len(parsed), "date_range": f"{sd} to {ed}",
                    "source": "book/client/services"}
    except requests.HTTPError as e:
        tried["book/client/{id}/services"] = f"{e.response.status_code}" if e.response else "err"
    # FALLBACK: other public api paths
    parsed, api_tried = _appts_api_fallback(client_id, sd, ed)
    tried.update(api_tried)
    if parsed:
        _enrich_names(parsed)
        return {"appointments": parsed, "count": len(parsed), "date_range": f"{sd} to {ed}",
                "source": "api-fallback"}
    # LAST RESORT: SFTP/DDS feed
    sftp_res = _appts_sftp_fallback(client_id, sd, ed)
    sftp_res.update({"date_range": f"{sd} to {ed}", "source": "sftp", "tried": tried})
    return sftp_res


def _scan_body(service_id, start, end, employee_id, scan_date_type, scan_time_type):
    scan_svc = {
        "clientId": "00000000-0000-0000-0000-000000000000", "serviceId": service_id,
        "employeeId": employee_id if employee_id else None, "genderPreferenceEnum": 105,
        "clientFirstName": "Guest", "clientPhoneNumber": "0000000000", "clientCountryCode": "1",
        "isGuest": True, "customServiceStepTimings": None,
    }
    return {
        "scanServices": [scan_svc], "payingClientId": None, "isRescan": False, "scanOrigin": 1,
        "maxOpeningsPerDay": 100, "appointmentBufferMinutes": 0, "maxStartTimeWait": 0,
        "maxWaitTimeBetweenServices": 0, "requireSameStartTime": True, "requireSameResource": False,
        "scanDateType": scan_date_type, "scanTimeType": scan_time_type,
        "startDate": f"{start}T00:00:00", "endDate": f"{end}T23:59:59",
        "isCouplesScan": False, "isRestrictedToBookableOnline": True,
    }


def _compact_openings(openings, per_day=4, cap=15):
    """Return a small, day-spread subset so the tool payload stays light for the agent.
    A huge openings list (100-200 slots) can stall the agent; a handful per day is plenty
    to answer 'what's available' and still carries booking fields for the chosen slot."""
    by_day, out = {}, []
    for o in openings:
        d = o.get("date", "")
        if by_day.get(d, 0) >= per_day:
            continue
        by_day[d] = by_day.get(d, 0) + 1
        out.append(o)
        if len(out) >= cap:
            break
    return out


@mcp.tool()
def check_availability(service_id: str, check_date: str = "", days_ahead: int = 7,
                       employee_id: str = "") -> dict:
    """Check open appointment slots for a service. This is THE tool for availability/openings -
    call it directly, no setup needed. service_id comes from list_services. check_date is
    YYYY-MM-DD (defaults today). Returns up to ~15 openings spread across the days."""
    scan_date_type = 2094   # working defaults for this location (do not require the agent to set)
    scan_time_type = 2095
    start = check_date or _today().isoformat()
    end = (date.fromisoformat(start) + timedelta(days=days_ahead)).isoformat()
    body = _scan_body(service_id, start, end, employee_id, scan_date_type, scan_time_type)

    def _parse_groups(groups):
        out = []
        for group in (groups or []):
            for o in (group.get("serviceOpenings") or []):
                out.append({
                    "date": (o.get("date") or "")[:10],
                    "start_time": (o.get("startTime") or "")[11:16],
                    "end_time": (o.get("endTime") or "")[11:16],
                    "employee_id": o.get("employeeId"),
                    "employee_name": o.get("employeeDisplayName") or o.get("employeeName") or "",
                    "resource_id": o.get("resourceId") or o.get("ResourceId") or "",
                    "resource_name": o.get("resourceName") or o.get("ResourceName") or "",
                    "concurrency_check_digits": o.get("concurrencyCheckDigits") or o.get("ConcurrencyCheckDigits") or "",
                    "service_name": o.get("serviceName"), "price": o.get("serviceBasePrice"),
                })
        return out

    # PRIMARY: OB scanforopenings (rich data incl. resource + concurrency digits)
    try:
        r = requests.post(f"{OB_BASE}/scanforopenings", json=body, headers=_ob_headers(), timeout=20)
        r.raise_for_status()
        openings = _parse_groups(r.json())
        if openings:
            return {"service_id": service_id, "start": start, "end": end,
                    "openings": _compact_openings(openings), "total": len(openings), "source": "ob"}
        ob_note = "ob scan returned 0 openings"
    except requests.HTTPError as e:
        ob_note = f"ob scan error: {e.response.text[:200] if e.response is not None else e}"
    except Exception as e:
        ob_note = f"ob scan error: {e}"

    # FALLBACK: Public API scan/openings (OAuth) - simpler body, same enums
    try:
        pub_body = {"ScanDateType": scan_date_type, "ScanTimeType": scan_time_type,
                    "StartDate": f"{start}T00:00:00", "EndDate": f"{end}T23:59:59",
                    "ScanServices": [{"ServiceId": service_id,
                                      **({"EmployeeIds": [employee_id]} if employee_id else {})}]}
        data = meevo_post("/publicapi/v2/scan/openings", pub_body)
        return {"service_id": service_id, "start": start, "end": end, "source": "publicapi-scan",
                "note": ob_note, "raw": str(data)[:3000]}
    except requests.HTTPError as e:
        return {"error": f"both scan paths failed", "ob": ob_note,
                "publicapi": e.response.text[:300] if e.response is not None else str(e),
                "hint": "check ScanDateType/ScanTimeType via lookup_enum"}


@mcp.tool()
def list_services() -> dict:
    """List all services at the spa with IDs, durations, and prices."""
    try:
        all_services = []
        for page_num in range(1, 20):
            data = meevo_get("/publicapi/v1/services", {"pageNumber": page_num})
            batch = _items(data)
            if not batch:
                break
            all_services.extend(batch)
            if len(batch) < 20:
                break
        result = [{"id": _str(s.get("id") or s.get("serviceId")),
                   "name": _str(s.get("displayName") or s.get("serviceDisplayName") or s.get("name")),
                   "category": _str(s.get("categoryName") or s.get("category")),
                   "duration_minutes": _str(s.get("duration") or s.get("durationMinutes")),
                   "price": _str(s.get("price") or s.get("retailPrice"))} for s in all_services]
        return {"services": result, "total": str(len(result))}
    except requests.HTTPError as e:
        return {"error": str(e), "body": e.response.text[:500] if e.response else ""}


@mcp.tool()
def list_staff(page: int = 1) -> dict:
    """List all staff/employees at the spa."""
    data = meevo_get("/publicapi/v1/employees")
    staff = _items(data)
    result = []
    for e in staff:
        cats = e.get("employeeCategories")
        title = _str(cats[0].get("employeeCategoryDisplayName")) if (isinstance(cats, list) and cats and isinstance(cats[0], dict)) else ""
        result.append({"id": _str(e.get("id") or e.get("employeeId")),
                       "name": (_str(e.get("firstName")) + " " + _str(e.get("lastName"))).strip(),
                       "title": title})
    return {"staff": result, "total": str(len(staff))}


@mcp.tool()
def get_client_memberships(client_id: str) -> dict:
    """List a client's memberships (name, status, dates). READ-ONLY."""
    try:
        items = _items(meevo_get("/publicapi/v1/clientmemberships", {"ClientId": client_id}))
        out = [{"membership_id": _str(_get(m, "id", "Id", "clientMembershipId", "ClientMembershipId")),
                "name": _str(_get(m, "membershipName", "MembershipName", "name", "Name", "displayName")),
                "status": _str(_get(m, "status", "Status", "state", "State")),
                "start_date": _str(_get(m, "startDate", "StartDate", "effectiveDate")),
                "end_date": _str(_get(m, "endDate", "EndDate", "expirationDate", "ExpirationDate"))}
               for m in items]
        return {"memberships": out, "count": len(out)}
    except requests.HTTPError as e:
        return {"error": str(e), "body": e.response.text[:500] if e.response else ""}


@mcp.tool()
def get_client_gift_cards(client_id: str) -> dict:
    """List a client's gift cards and balances (card number masked). READ-ONLY."""
    try:
        items = _items(meevo_get(f"/publicapi/v1/client/{client_id}/giftCards"))
        out = [{"gift_card_id": _str(_get(g, "id", "Id", "giftCardId", "GiftCardId")),
                "number_last4": _str(_get(g, "lastFour", "LastFour", "maskedNumber", "MaskedNumber", "number", "Number"))[-4:],
                "balance": _str(_get(g, "balance", "Balance", "currentBalance", "CurrentBalance", "remainingValue")),
                "status": _str(_get(g, "status", "Status"))}
               for g in items]
        return {"gift_cards": out, "count": len(out)}
    except requests.HTTPError as e:
        return {"error": str(e), "body": e.response.text[:500] if e.response else ""}


@mcp.tool()
def list_resources(service_id: str = "") -> dict:
    """List bookable resources (rooms/booths) needed for services that require one
    (spray tan, facial room, laser room, etc.). Meevo's Public API has no documented
    resources list, so the reliable source is the resource_id/resource_name returned by
    check_availability. If service_id is given, this derives distinct resources from that
    service's openings; it also best-effort-tries known endpoints. READ-ONLY.
    Known rooms (from onboarding): Brow Chair, Facial Room, Laser Room, Lash Lounge, Spray Tan."""
    found = {}
    notes = {}
    if service_id:
        try:
            start = _today().isoformat()
            end = (_today() + timedelta(days=7)).isoformat()
            body = _scan_body(service_id, start, end, "", 2094, 2095)
            r = requests.post(f"{OB_BASE}/scanforopenings", json=body, headers=_ob_headers(), timeout=20)
            r.raise_for_status()
            for group in (r.json() or []):
                for o in (group.get("serviceOpenings") or []):
                    rid = o.get("resourceId") or o.get("ResourceId")
                    if rid:
                        found[rid] = o.get("resourceName") or o.get("ResourceName") or ""
            notes["from_openings"] = f"{len(found)} resource(s) seen in openings"
        except Exception as e:
            notes["from_openings"] = f"error: {str(e)[:150]}"
    for path in ["/publicapi/v1/resources", "/publicapi/v1/resource"]:
        try:
            for rsc in _items(meevo_get(path)):
                rid = _get(rsc, "id", "Id", "resourceId", "ResourceId")
                if rid:
                    found[rid] = _get(rsc, "name", "Name", "displayName", "DisplayName")
            notes[path] = "ok"
        except requests.HTTPError as e:
            notes[path] = e.response.status_code if e.response else "err"
    return {"resources": [{"resource_id": k, "resource_name": v} for k, v in found.items()],
            "count": len(found), "attempts": notes,
            "note": "resource_id is also returned directly by check_availability openings"}


# ======================= WRITE TOOLS (confirmation + idempotency) ==========
@mcp.tool()
def book_appointment(client_id: str, service_id: str, start_datetime: str, employee_id: str = "",
                     resource_id: str = "", concurrency_check_digits: str = "", notes: str = "",
                     confirm: bool = False, confirmation_token: str = "", idempotency_key: str = "") -> dict:
    """Book a new appointment. start_datetime: YYYY-MM-DDTHH:MM:SS. Pass resource_id and
    concurrency_check_digits from check_availability. WRITE - requires confirmation."""
    params = {"client_id": client_id, "service_id": service_id, "start_datetime": start_datetime,
              "employee_id": employee_id, "resource_id": resource_id}
    if not confirm:
        return _begin_write("book_appointment", params,
                            f"Book service {service_id} for client {client_id} at {start_datetime} "
                            f"(staff {employee_id or 'any'}, resource {resource_id or 'auto'}).")
    if idempotency_key and idempotency_key in _idem:
        return _idem[idempotency_key]
    err = _confirm_ok("book_appointment", params, confirmation_token)
    if err:
        return {"success": False, "error": err}
    body = {"ClientId": client_id, "ServiceId": service_id, "StartTime": start_datetime,
            "SendConfirmation": True, "SendClientNotification": True, "NotifyClient": True,
            "BookingSource": 2}
    if employee_id:
        body["EmployeeId"] = employee_id
    if resource_id:
        body["ResourceId"] = resource_id
    if concurrency_check_digits:
        body["ConcurrencyCheckDigits"] = concurrency_check_digits
    if notes:
        body["Notes"] = notes
    try:
        r = requests.post(f"{BASE_URL}/publicapi/v1/book/service", params=_cap_params(), json=body,
                          headers=_auth_headers(), timeout=15)
        r.raise_for_status()
        result = r.json() if r.content else {"success": True}
        out = {"success": True,
               "appointment_service_id": _get(result, "AppointmentServiceId", "appointmentServiceId", "Id", "id"),
               "appointment_id": _get(result, "AppointmentId", "appointmentId"), "raw": result}
    except requests.HTTPError as e:
        out = {"success": False, "error": str(e), "response_body": e.response.text if e.response is not None else ""}
    if idempotency_key and out.get("success"):
        _idem[idempotency_key] = out
    _pending.pop(confirmation_token, None)
    return out


@mcp.tool()
def reschedule_appointment(appointment_service_id: str, new_start_datetime: str, employee_id: str = "",
                           concurrency_check_digits: str = "", confirm: bool = False,
                           confirmation_token: str = "", idempotency_key: str = "") -> dict:
    """Reschedule an appointment. new_start_datetime: YYYY-MM-DDTHH:MM:SS. WRITE - requires confirmation."""
    params = {"appointment_service_id": appointment_service_id, "new_start_datetime": new_start_datetime}
    if not confirm:
        return _begin_write("reschedule_appointment", params,
                            f"Reschedule booked service {appointment_service_id} to {new_start_datetime}.")
    if idempotency_key and idempotency_key in _idem:
        return _idem[idempotency_key]
    err = _confirm_ok("reschedule_appointment", params, confirmation_token)
    if err:
        return {"success": False, "error": err}
    concurrency = concurrency_check_digits
    if not concurrency:
        try:
            svc = meevo_get(f"/publicapi/v1/book/service/{appointment_service_id}")
            concurrency = _get(svc, "ConcurrencyCheckDigits", "concurrencyCheckDigits", "RowVersion", "rowVersion")
        except requests.HTTPError as e:
            return {"success": False, "error": f"Could not fetch service: {e}",
                    "response_body": e.response.text if e.response else ""}
    body = {"StartTime": new_start_datetime}
    if employee_id:
        body["EmployeeId"] = employee_id
    if concurrency:
        body["ConcurrencyCheckDigits"] = concurrency
    try:
        result = meevo_put(f"/publicapi/v1/book/service/{appointment_service_id}", body)
        out = {"success": True, "appointment_service_id": appointment_service_id,
               "new_start_datetime": new_start_datetime, "raw": result}
    except requests.HTTPError as e:
        out = {"success": False, "error": str(e), "response_body": e.response.text if e.response is not None else ""}
    if idempotency_key and out.get("success"):
        _idem[idempotency_key] = out
    _pending.pop(confirmation_token, None)
    return out


@mcp.tool()
def cancel_appointment(appointment_service_id: str, cancellation_reason_id: str = "",
                       concurrency_check_digits: str = "", confirm: bool = False,
                       confirmation_token: str = "", idempotency_key: str = "") -> dict:
    """Cancel an appointment. WRITE - requires confirmation. Always confirm with the client first."""
    params = {"appointment_service_id": appointment_service_id}
    if not confirm:
        return _begin_write("cancel_appointment", params,
                            f"Cancel booked service {appointment_service_id}.")
    if idempotency_key and idempotency_key in _idem:
        return _idem[idempotency_key]
    err = _confirm_ok("cancel_appointment", params, confirmation_token)
    if err:
        return {"success": False, "error": err}
    concurrency = concurrency_check_digits
    if not concurrency:
        try:
            svc = meevo_get(f"/publicapi/v1/book/service/{appointment_service_id}")
            concurrency = _get(svc, "ConcurrencyCheckDigits", "concurrencyCheckDigits", "RowVersion", "rowVersion")
        except requests.HTTPError as e:
            return {"success": False, "error": f"Could not fetch service: {e}",
                    "response_body": e.response.text if e.response else ""}
    cparams = {"TenantId": int(TENANT_ID), "LocationId": int(LOCATION_ID)}
    if concurrency:
        cparams["ConcurrencyCheckDigits"] = concurrency
    if cancellation_reason_id:
        cparams["CancellationReasonId"] = cancellation_reason_id
    try:
        r = requests.delete(f"{BASE_URL}/publicapi/v1/book/service/{appointment_service_id}",
                            params=cparams, headers=_auth_headers(), timeout=15)
        r.raise_for_status()
        out = {"success": True, "appointment_service_id": appointment_service_id, "cancelled": True}
    except requests.HTTPError as e:
        out = {"success": False, "error": str(e), "response_body": e.response.text if e.response is not None else ""}
    if idempotency_key and out.get("success"):
        _idem[idempotency_key] = out
    _pending.pop(confirmation_token, None)
    return out


if __name__ == "__main__":
    mcp.settings.port = int(os.environ.get("PORT", 10000))
    mcp.run(transport="streamable-http")
