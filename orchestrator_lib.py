"""
Shared logic used by all six cron scripts (cron_*.py). No scheduling code
lives here — Render's own Cron Jobs handle timing now, not an internal
Python scheduler. This file just holds the actual business logic so it's
not duplicated six times.
"""

import os
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests

from rentcast_lookup import (
    get_sold_comps, get_property_valuation, analyze_sold_comps, get_recommended_arv
)
from zillapi_lookup import get_zillow_valuation, extract_zestimate

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")

TARGET_CONCURRENT_CALLS = 3  # start conservative for real-world testing; raise
                             # toward your account's actual concurrency limit
                             # (10, per Vapi's subscriptionLimits) once proven out


# ---------------------------------------------------------------------------
# Retry cadence — a decaying schedule instead of a flat attempt cap.
# Cumulative schedule: index = attempt number (0-indexed), value = earliest
# day-offset from lead creation that attempt is allowed. Attempts 1-2 same
# day, then day 2, then roughly weekly through day 30 — after that, or after
# MAX_ATTEMPTS total, the lead is Exhausted regardless of days elapsed.
# ---------------------------------------------------------------------------

CALL_SCHEDULE_DAYS = [0, 0, 2, 9, 16, 23, 30]
MAX_ATTEMPTS = len(CALL_SCHEDULE_DAYS)


def _parse_date_safely(date_string: str) -> date:
    """
    Handles both a plain date string ("2026-08-27") and a full ISO
    timestamp with time/timezone ("2026-08-27T15:30:00.000Z") — Airtable's
    "Created time" field type returns the latter, which date.fromisoformat()
    alone can't parse directly.
    """
    return date.fromisoformat(date_string[:10])


def is_lead_exhausted(date_created: str, call_count: int, next_contact_date: str = None) -> bool:
    """
    True if this lead has used up its full retry schedule — either hit the
    attempt cap, or run past the final cutoff day.

    next_contact_date: if a seller gave a real future timeframe ("check back
    in 6 months"), this OVERRIDES the normal 30-day cutoff entirely — without
    this, a genuinely promising lead would get silently marked Exhausted and
    dropped forever, months before the scheduled callback ever happens. Set
    via the "next_contact_date" field on the Airtable record.
    """
    if next_contact_date:
        # A real scheduled date means this lead is deliberately being held,
        # not abandoned — never exhaust it while a future date is pending.
        return False
    if call_count >= MAX_ATTEMPTS:
        return True
    created = _parse_date_safely(date_created)
    days_elapsed = (date.today() - created).days
    return days_elapsed > CALL_SCHEDULE_DAYS[-1]


def is_retry_due(date_created: str, call_count: int, next_contact_date: str = None) -> bool:
    """
    True if enough time has passed since lead creation to allow the NEXT
    attempt (call_count is how many attempts have happened so far).

    next_contact_date: if set, this REPLACES the normal decaying schedule —
    the lead is due exactly on that date, not before, regardless of
    call_count or the default cadence. This is what makes "check back in 6
    months" actually mean something instead of either being retried every
    few days by the normal schedule or dropped as Exhausted.
    """
    if next_contact_date:
        scheduled = _parse_date_safely(next_contact_date)
        return date.today() >= scheduled
    if is_lead_exhausted(date_created, call_count):
        return False  # never due if already exhausted, regardless of schedule index
    created = _parse_date_safely(date_created)
    days_elapsed = (date.today() - created).days
    earliest_allowed = CALL_SCHEDULE_DAYS[call_count]
    return days_elapsed >= earliest_allowed


def enrich_lead_with_valuation(address: str, city: str, state: str, zip_code: str) -> dict:
    """Runs the full RentCast + Zillow analysis for one lead."""
    full_address = f"{address}, {city}, {state} {zip_code}"
    avm_result = get_property_valuation(full_address)
    subject = avm_result.get("subjectProperty", {})
    sold_result = get_sold_comps(full_address, subject_property=subject)
    sold_properties = sold_result if isinstance(sold_result, list) else sold_result.get("properties", [])
    analysis = analyze_sold_comps(sold_properties, subject_property=subject)

    zillow_estimate = None
    try:
        zillow_result = get_zillow_valuation(full_address)
        zillow_estimate = extract_zestimate(zillow_result)
    except Exception as e:
        print(f"Zillow lookup failed for {address}, proceeding without it: {e}")

    return get_recommended_arv(analysis, avm_result, zillow_estimate=zillow_estimate)


# Predominant timezone per US state — see original orchestrator.py comment
# for the border-state caveat, unchanged from before.
STATE_TIMEZONES = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix",
    "AR": "America/Chicago", "CA": "America/Los_Angeles", "CO": "America/Denver",
    "CT": "America/New_York", "DE": "America/New_York", "FL": "America/New_York",
    "GA": "America/New_York", "HI": "Pacific/Honolulu", "ID": "America/Boise",
    "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis", "IA": "America/Chicago",
    "KS": "America/Chicago", "KY": "America/New_York", "LA": "America/Chicago",
    "ME": "America/New_York", "MD": "America/New_York", "MA": "America/New_York",
    "MI": "America/Detroit", "MN": "America/Chicago", "MS": "America/Chicago",
    "MO": "America/Chicago", "MT": "America/Denver", "NE": "America/Chicago",
    "NV": "America/Los_Angeles", "NH": "America/New_York", "NJ": "America/New_York",
    "NM": "America/Denver", "NY": "America/New_York", "NC": "America/New_York",
    "ND": "America/North_Dakota/Center", "OH": "America/New_York", "OK": "America/Chicago",
    "OR": "America/Los_Angeles", "PA": "America/New_York", "RI": "America/New_York",
    "SC": "America/New_York", "SD": "America/Chicago", "TN": "America/Chicago",
    "TX": "America/Chicago", "UT": "America/Denver", "VT": "America/New_York",
    "VA": "America/New_York", "WA": "America/Los_Angeles", "WV": "America/New_York",
    "WI": "America/Chicago", "WY": "America/Denver", "DC": "America/New_York",
}


def get_lead_local_hour(state: str) -> int:
    tz_name = STATE_TIMEZONES.get(state.upper())
    if not tz_name:
        raise ValueError(f"Unknown state code: {state}. Cannot determine calling window safely.")
    local_time = datetime.now(ZoneInfo(tz_name))
    return local_time.hour


def is_within_calling_hours(state: str, start_hour: int = 8, end_hour: int = 21) -> bool:
    try:
        current_hour = get_lead_local_hour(state)
    except ValueError:
        return False  # unknown state — fail safe, don't call
    return start_hour <= current_hour < end_hour


def trigger_vapi_call(phone_number: str, lead_context: dict):
    if not VAPI_API_KEY:
        raise RuntimeError("VAPI_API_KEY not set")

    headers = {"Authorization": f"Bearer {VAPI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "assistantId": os.environ.get("VAPI_ASSISTANT_ID"),
        "phoneNumberId": os.environ.get("VAPI_PHONE_NUMBER_ID"),
        "customer": {"number": phone_number},
        "assistantOverrides": {"variableValues": lead_context},
    }
    response = requests.post("https://api.vapi.ai/call/phone", headers=headers, json=payload, timeout=15)
    if not response.ok:
        print(f"Vapi call trigger failed ({response.status_code}): {response.text}")
    response.raise_for_status()
    return response.json()


def send_sms(phone_number: str, message: str):
    """TODO: Twilio integration — not yet built."""
    raise NotImplementedError("SMS integration not yet built")


def send_email(to_address: str, subject: str, body: str):
    """TODO: email service integration — not yet built."""
    raise NotImplementedError("Email integration not yet built")
