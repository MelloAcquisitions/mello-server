"""
Shared Airtable read/write logic — used by BOTH main.py (the web server)
and orchestrator.py (the background worker), so there's exactly one place
this logic lives instead of two copies drifting apart over time.
"""

import os
from typing import Optional

import requests

AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Leads")
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"


class AirtableError(Exception):
    """Raised on any Airtable API failure — plain Python exception, not tied
    to FastAPI, so this module works the same whether it's called from the
    web server or the standalone background worker."""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Airtable error ({status_code}): {detail}")


def _escape_formula_value(value: str) -> str:
    """
    Escapes a value for safe interpolation inside an Airtable filterByFormula
    string literal. Without this, an address or name containing a single
    quote (e.g. "O'Brien St") breaks the formula syntax outright — and in
    principle a crafted value could alter the filter logic entirely.
    """
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _airtable_headers():
    if not AIRTABLE_API_KEY:
        raise AirtableError(500, "AIRTABLE_API_KEY not set")
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }


def find_lead_record(address: str) -> Optional[dict]:
    """Returns the full Airtable record (id + fields) matching this address, or None."""
    params = {"filterByFormula": f"{{address}} = '{_escape_formula_value(address)}'"}
    response = requests.get(AIRTABLE_URL, headers=_airtable_headers(), params=params, timeout=15)
    if not response.ok:
        raise AirtableError(response.status_code, f"lookup failed: {response.text}")
    records = response.json().get("records", [])
    return records[0] if records else None


def find_lead_flexible(address: str) -> Optional[dict]:
    """
    Same as find_lead_record(), but also tries the street-only portion of
    the address before giving up.

    Why this exists: the live agent is given {{property_address}}, which is
    the FULL address ("6506 Clubway Ln, Austin, TX 78745"). But the address
    column in Airtable only ever holds the street portion ("6506 Clubway
    Ln"), because that's what lead_sourcing.py wrote at intake time. An
    exact match on the full string therefore always misses for a real call,
    and upsert_lead() would silently CREATE A DUPLICATE record instead of
    updating the real lead — splitting status/ARV/call-count history across
    two rows for the same property. Use this (not find_lead_record) anywhere
    a lookup might be seeded from the agent's own address variable.
    """
    record = find_lead_record(address)
    if record:
        return record

    street = address.split(",")[0].strip()
    if street and street != address:
        return find_lead_record(street)

    return None


def resolve_address_for_write(address: str) -> str:
    """
    Returns the address string an upsert_lead() call should actually use, so
    it lands on an existing record instead of creating a duplicate.

    If a matching lead is found (by find_lead_flexible), returns THAT
    record's own stored address value — guaranteeing upsert_lead()'s own
    internal find_lead_record() call matches it exactly. If no match is
    found (a genuinely new lead), falls back to the street-only portion of
    whatever was passed in, since that's the format every other lead in the
    table is stored in.
    """
    record = find_lead_flexible(address)
    if record:
        return record["fields"].get("address", address)
    street = address.split(",")[0].strip()
    return street or address


def upsert_lead(address: str, fields: dict) -> dict:
    """Creates a new lead record, or updates the existing one for this address."""
    existing_record = find_lead_record(address)
    payload = {"fields": fields}

    if existing_record:
        response = requests.patch(
            f"{AIRTABLE_URL}/{existing_record['id']}", headers=_airtable_headers(), json=payload, timeout=15
        )
    else:
        payload["fields"]["address"] = address
        response = requests.post(AIRTABLE_URL, headers=_airtable_headers(), json=payload, timeout=15)

    if not response.ok:
        raise AirtableError(response.status_code, f"write failed: {response.text}")
    return response.json()


def query_leads(filter_formula: str, max_records: int = None) -> list:
    """
    Returns ALL full records matching an Airtable filter formula, following
    Airtable's pagination automatically.

    max_records: optional hard cap. Leave as None (the default) to get every
    matching record — this matters a lot for correctness. Airtable returns
    at most 100 records per page, and this previously defaulted to a silent
    50-record cap, which meant:
      - the opt-out suppression list in cron_dispatch_calls.py would stop
        including people past the 50th opt-out, so someone who explicitly
        asked not to be called could be called again. That's a real
        compliance problem, not just a stats one.
      - all-lead counts (evening wrap, dashboard stats) would silently
        under-report once the table grew past 50 rows.
    Only pass max_records when you genuinely want a small sample (e.g. a
    single lookup by phone).
    """
    records = []
    params = {"filterByFormula": filter_formula, "pageSize": 100}
    if max_records is not None:
        params["maxRecords"] = max_records

    while True:
        response = requests.get(AIRTABLE_URL, headers=_airtable_headers(), params=params, timeout=15)
        if not response.ok:
            raise AirtableError(response.status_code, f"query failed: {response.text}")
        payload = response.json()
        records.extend(payload.get("records", []))

        offset = payload.get("offset")
        if not offset:
            break
        if max_records is not None and len(records) >= max_records:
            break
        params["offset"] = offset

    return records[:max_records] if max_records is not None else records


# ---------------------------------------------------------------------------
# Spend circuit-breaker — a real, hard daily call cap so a bug that causes
# runaway dialing gets stopped automatically instead of running unchecked
# until someone notices the bill.
# ---------------------------------------------------------------------------

DAILY_LOG_TABLE = os.environ.get("DAILY_LOG_TABLE", "Daily Log")
DAILY_LOG_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{DAILY_LOG_TABLE}"


def get_todays_call_count() -> int:
    """Reads today's call count from a dedicated Airtable table."""
    from datetime import date
    today = date.today().isoformat()
    headers = _airtable_headers()
    params = {"filterByFormula": f"{{date}}='{today}'"}
    response = requests.get(DAILY_LOG_URL, headers=headers, params=params, timeout=15)
    if not response.ok:
        raise AirtableError(response.status_code, f"daily log read failed: {response.text}")
    records = response.json().get("records", [])
    return records[0]["fields"].get("calls_today", 0) if records else 0


def increment_todays_call_count(by: int = 1):
    """Call this once for every real call placed — used to enforce the daily cap."""
    from datetime import date
    today = date.today().isoformat()
    headers = _airtable_headers()
    params = {"filterByFormula": f"{{date}}='{today}'"}
    response = requests.get(DAILY_LOG_URL, headers=headers, params=params, timeout=15)
    if not response.ok:
        raise AirtableError(response.status_code, f"daily log read failed: {response.text}")
    records = response.json().get("records", [])

    if records:
        record_id = records[0]["id"]
        current = records[0]["fields"].get("calls_today", 0)
        patch_response = requests.patch(
            f"{DAILY_LOG_URL}/{record_id}", headers=headers,
            json={"fields": {"calls_today": current + by}}, timeout=15,
        )
    else:
        patch_response = requests.post(
            DAILY_LOG_URL, headers=headers,
            json={"fields": {"date": today, "calls_today": by}}, timeout=15,
        )
    if not patch_response.ok:
        raise AirtableError(patch_response.status_code, f"daily log write failed: {patch_response.text}")


def increment_daily_log_field(field_name: str, by: float = 1):
    """
    Generic version of increment_todays_call_count() for any other running
    daily total you want tracked — e.g. total call seconds (for a real,
    duration-based Vapi cost estimate) or BatchData request count (for a
    real, usage-based BatchData cost estimate), instead of flat per-unit
    guesses. Same day-record-per-row pattern as calls_today.
    """
    from datetime import date
    today = date.today().isoformat()
    headers = _airtable_headers()
    params = {"filterByFormula": f"{{date}}='{today}'"}
    response = requests.get(DAILY_LOG_URL, headers=headers, params=params, timeout=15)
    if not response.ok:
        raise AirtableError(response.status_code, f"daily log read failed: {response.text}")
    records = response.json().get("records", [])

    if records:
        record_id = records[0]["id"]
        current = records[0]["fields"].get(field_name, 0)
        patch_response = requests.patch(
            f"{DAILY_LOG_URL}/{record_id}", headers=headers,
            json={"fields": {field_name: current + by}}, timeout=15,
        )
    else:
        patch_response = requests.post(
            DAILY_LOG_URL, headers=headers,
            json={"fields": {"date": today, field_name: by}}, timeout=15,
        )
    if not patch_response.ok:
        raise AirtableError(patch_response.status_code, f"daily log write failed ({field_name}): {patch_response.text}")
