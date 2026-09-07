"""
Shared Airtable read/write logic — used by main.py (the web server) and by
every cron_*.py script, so there is exactly one place this logic lives
instead of copies drifting apart.

CHANGES IN THIS CLEANUP
-----------------------
- Every HTTP call now goes through `_request()`, which retries on 429 and
  5xx with backoff. Airtable rate-limits at 5 requests/second PER BASE.
  Six crons plus live in-call tool writes share that budget, and a single
  429 previously turned into a hard failure — during a live call that means
  the seller's outcome is never recorded.
- `append_notes()` added. Notes were being APPENDED by the webhook and the
  reconcile cron but OVERWRITTEN by log_call_outcome — the most common path
  was destroying the call history the other two layers work to protect.
- `increment_todays_call_count()` is now a thin alias of the generic
  `increment_daily_log_field()`; they were two copies of the same code.
- All date logic routed through mello_time (see that module for why).
- `require_config()` added — Render crons do NOT inherit env vars from the
  web service, which has caused repeated silent failures. Now a cron dies
  immediately with a readable message naming the missing variable instead
  of producing a confusing 404 against ".../None/Leads".
"""

import os
import time
from typing import Optional

import requests

from mello_time import today_iso

AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Leads")
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"

DAILY_LOG_TABLE = os.environ.get("DAILY_LOG_TABLE", "Daily Log")
DAILY_LOG_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{DAILY_LOG_TABLE}"

# Airtable's own hard limit on a Long Text field.
MAX_NOTES_CHARS = 99000

_TIMEOUT = 15
_MAX_RETRIES = 3


class AirtableError(Exception):
    """Raised on any Airtable API failure — a plain Python exception, not
    tied to FastAPI, so this module works identically from the web server
    and from a standalone cron script."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Airtable error ({status_code}): {detail}")


def require_config(*extra_vars: str) -> None:
    """
    Fail fast and readably if required environment variables are missing.

    Call this at the top of every cron script. Render cron jobs do not
    inherit environment variables from the web service — each needs its own
    full set — and forgetting one previously produced a 404 against a URL
    containing the literal string "None", which reads like an Airtable
    problem instead of a config problem.
    """
    required = ["AIRTABLE_API_KEY", "AIRTABLE_BASE_ID"] + list(extra_vars)
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Render cron jobs do NOT inherit env vars from the web service — "
            "set them on this job directly."
        )


def _escape_formula_value(value: str) -> str:
    """
    Escapes a value for safe interpolation into an Airtable filterByFormula
    string literal. Without this an address containing an apostrophe
    ("O'Brien St") breaks the formula outright, and in principle a crafted
    value could alter the filter logic.
    """
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _airtable_headers():
    if not AIRTABLE_API_KEY:
        raise AirtableError(500, "AIRTABLE_API_KEY not set")
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }


def _request(method: str, url: str, *, context: str, **kwargs) -> dict:
    """
    One HTTP call to Airtable, with retry on 429 (rate limit) and 5xx.

    Airtable allows 5 requests/second per base. This project has six cron
    jobs, a dashboard that scans the full table, and live in-call tool
    writes all sharing that budget. A 429 during a live call previously
    meant the call outcome was simply lost, which is exactly the failure
    the three-layer logging architecture exists to prevent — so it is worth
    the few hundred milliseconds to retry here.
    """
    last_detail = ""
    for attempt in range(_MAX_RETRIES):
        try:
            response = requests.request(
                method, url, headers=_airtable_headers(), timeout=_TIMEOUT, **kwargs
            )
        except requests.RequestException as e:
            last_detail = f"{context} — network error: {e}"
            if attempt == _MAX_RETRIES - 1:
                raise AirtableError(503, last_detail)
            time.sleep(0.4 * (2 ** attempt))
            continue

        if response.ok:
            return response.json()

        last_detail = f"{context} failed: {response.text[:400]}"
        # 429 = rate limited, 5xx = Airtable's problem. Both are worth
        # retrying. 4xx anything else (bad field name, invalid select
        # option) will fail identically forever — surface it immediately.
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < _MAX_RETRIES - 1:
                time.sleep(0.5 * (2 ** attempt))
                continue
        raise AirtableError(response.status_code, last_detail)

    raise AirtableError(503, last_detail or f"{context} failed after retries")


# ---------------------------------------------------------------------------
# Lead lookup
# ---------------------------------------------------------------------------

def find_lead_record(address: str) -> Optional[dict]:
    """Returns the full Airtable record (id + fields) matching this exact
    address, or None."""
    if not address:
        return None
    params = {"filterByFormula": f"{{address}} = '{_escape_formula_value(address)}'"}
    payload = _request("GET", AIRTABLE_URL, context="lookup", params=params)
    records = payload.get("records", [])
    return records[0] if records else None


def find_lead_flexible(address: str) -> Optional[dict]:
    """
    Same as find_lead_record(), but also tries the street-only portion of
    the address before giving up.

    Why: the live agent is given {{property_address}}, which is the FULL
    address ("6506 Clubway Ln, Austin, TX 78745"). The `address` column in
    Airtable only holds the street portion, because that is what
    lead_sourcing.py wrote at intake. An exact match therefore misses on
    every real call, and upsert_lead() would silently CREATE A DUPLICATE
    instead of updating the real lead — splitting status, ARV and call
    history across two rows for one property.

    Use this, not find_lead_record(), anywhere a lookup may be seeded from
    the agent's own address variable.
    """
    record = find_lead_record(address)
    if record:
        return record

    street = (address or "").split(",")[0].strip()
    if street and street != address:
        return find_lead_record(street)

    return None


def phone_variants(phone: str) -> set:
    """
    Every plausible written form of the same 10-digit US number.

    Vapi reports E.164 ("+15125551234"); lead_sourcing.py saved whatever
    BatchData returned, which may be "(512) 555-1234", "512-555-1234" or
    "5125551234". Airtable's formula language cannot strip punctuation from
    a stored field, so rather than migrating the column we generate the
    variants and match any of them.

    Exposed separately from find_lead_by_phone() so the opt-out suppression
    list in cron_dispatch_calls.py can normalize on the same rules — it
    previously compared raw strings, which meant the same person stored in
    two formats could be suppressed under one and dialed under the other.
    """
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) < 10:
        return set()
    last10 = digits[-10:]
    area, prefix, line = last10[0:3], last10[3:6], last10[6:10]
    return {
        phone,
        last10,
        f"+1{last10}",
        f"1{last10}",
        f"({area}) {prefix}-{line}",
        f"({area}){prefix}-{line}",
        f"{area}-{prefix}-{line}",
        f"{area}.{prefix}.{line}",
        f"{area} {prefix} {line}",
        f"+1 ({area}) {prefix}-{line}",
    }


def normalize_phone(phone: str) -> Optional[str]:
    """
    Reduces any written phone format to its last 10 digits, so two records
    holding the same number in different formats compare equal. Returns
    None if there aren't 10 digits to work with.
    """
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else None


def find_lead_by_phone(phone: str) -> Optional[dict]:
    """Finds a lead by phone number, tolerating format mismatches."""
    variants = phone_variants(phone)
    if not variants:
        return None

    clauses = ", ".join(f"{{phone}}='{_escape_formula_value(v)}'" for v in variants)
    try:
        matches = query_leads(f"OR({clauses})", max_records=1)
        return matches[0] if matches else None
    except AirtableError:
        return None


def resolve_address_for_write(address: str) -> str:
    """
    Returns the address string an upsert_lead() call should actually use, so
    the write lands on the existing record instead of creating a duplicate.

    If a lead is found, returns THAT record's own stored address value,
    guaranteeing upsert_lead()'s internal exact-match lookup hits it. If not
    found (a genuinely new lead), falls back to the street-only portion,
    since that is the format every other lead in the table uses.
    """
    record = find_lead_flexible(address)
    if record:
        return record["fields"].get("address") or address
    street = (address or "").split(",")[0].strip()
    return street or address


# ---------------------------------------------------------------------------
# Lead writes
# ---------------------------------------------------------------------------

def upsert_lead(address: str, fields: dict) -> dict:
    """Creates a new lead record, or updates the existing one for this address."""
    if not address:
        raise AirtableError(400, "upsert_lead called with an empty address")

    existing_record = find_lead_record(address)
    payload = {"fields": dict(fields)}

    if existing_record:
        return _request(
            "PATCH",
            f"{AIRTABLE_URL}/{existing_record['id']}",
            context="write",
            json=payload,
        )

    payload["fields"]["address"] = address
    return _request("POST", AIRTABLE_URL, context="write", json=payload)


def append_notes(existing_notes: str, addition: str) -> str:
    """
    Builds a call_transcript_summary value that ADDS to the history instead
    of replacing it.

    The record has one notes field and a lead can be called many times.
    Overwriting destroys the history of every prior call — including
    opt-out language a human would need to see, and including the
    "[AUTO-LOGGED]" and "[RECONCILED]" markers the fallback layers rely on
    to avoid double-writing. The webhook and the reconcile cron were
    already appending; log_call_outcome was overwriting, so the most common
    path was the one losing data.

    Truncates from the FRONT, not the back, when the field is full: the
    most recent call is the one worth keeping.
    """
    existing = (existing_notes or "").strip()
    addition = (addition or "").strip()
    if not addition:
        return existing
    combined = f"{existing}\n\n{addition}".strip() if existing else addition
    if len(combined) > MAX_NOTES_CHARS:
        combined = "[...older call history truncated...]\n\n" + combined[-(MAX_NOTES_CHARS - 45):]
    return combined


def query_leads(filter_formula: str, max_records: int = None) -> list:
    """
    Returns ALL full records matching an Airtable filter formula, following
    Airtable's pagination automatically.

    max_records: optional hard cap. Leave as None (the default) to get every
    matching record — this matters for correctness. Airtable returns at most
    100 records per page, and a silent cap meant:
      - the opt-out suppression list in cron_dispatch_calls.py stopped
        including people past the cap, so someone who explicitly asked not
        to be called could be called again. That is a compliance problem.
      - all-lead counts under-reported once the table grew.
    Only pass max_records when you genuinely want a small sample.
    """
    records = []
    params = {"filterByFormula": filter_formula, "pageSize": 100}
    if max_records is not None:
        params["maxRecords"] = max_records

    while True:
        payload = _request("GET", AIRTABLE_URL, context="query", params=params)
        records.extend(payload.get("records", []))

        offset = payload.get("offset")
        if not offset:
            break
        if max_records is not None and len(records) >= max_records:
            break
        params["offset"] = offset

    return records[:max_records] if max_records is not None else records


# ---------------------------------------------------------------------------
# Daily Log — the spend circuit-breaker
#
# A real, hard daily call cap, so a bug that causes runaway dialing is
# stopped automatically instead of running unchecked until the bill arrives.
#
# NOTE ON ATOMICITY: this is a read-modify-write against Airtable, which has
# no atomic increment. Two writers in the same instant can lose a count.
# In practice the only concurrent writers are the dispatch cron (serial
# within a run, every 15 min) and the end-of-call webhook (writes a
# different field), so drift is rare and always in the direction of
# UNDER-counting. The cap is therefore a safety net, not an exact meter —
# treat MAX_CALLS_PER_DAY as approximate and leave headroom.
# ---------------------------------------------------------------------------

def _todays_daily_log_record() -> Optional[dict]:
    params = {"filterByFormula": f"{{date}}='{today_iso()}'"}
    payload = _request("GET", DAILY_LOG_URL, context="daily log read", params=params)
    records = payload.get("records", [])
    return records[0] if records else None


def get_daily_log_value(field_name: str, default: float = 0) -> float:
    """Reads one running total off today's Daily Log row."""
    record = _todays_daily_log_record()
    if not record:
        return default
    value = record["fields"].get(field_name)
    return default if value is None else value


def get_todays_call_count() -> int:
    """Reads today's call count (business timezone) from the Daily Log table."""
    return int(get_daily_log_value("calls_today", 0))


def increment_daily_log_field(field_name: str, by: float = 1):
    """
    Adds `by` to a running daily total on today's Daily Log row, creating
    the row if it does not exist yet.

    Used for calls_today (the spend cap), call_seconds_today (a real,
    duration-based Vapi cost estimate rather than a flat per-call guess),
    and the BatchData usage counters.
    """
    record = _todays_daily_log_record()

    if record:
        current = record["fields"].get(field_name, 0) or 0
        _request(
            "PATCH",
            f"{DAILY_LOG_URL}/{record['id']}",
            context=f"daily log write ({field_name})",
            json={"fields": {field_name: current + by}},
        )
    else:
        _request(
            "POST",
            DAILY_LOG_URL,
            context=f"daily log create ({field_name})",
            json={"fields": {"date": today_iso(), field_name: by}},
        )


def increment_todays_call_count(by: int = 1):
    """Call once for every real call placed — enforces the daily cap."""
    increment_daily_log_field("calls_today", by)
