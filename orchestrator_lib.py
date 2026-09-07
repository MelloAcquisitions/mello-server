"""
Shared logic used by the cron scripts (cron_*.py). No scheduling code lives
here — Render's own Cron Jobs handle timing. This file holds the business
logic so it is not duplicated six times.

CHANGES IN THIS CLEANUP
-----------------------
- All date logic routed through mello_time (Render runs UTC, the business
  runs Monterrey — see mello_time.py for the three bugs that caused).
- is_within_calling_hours() now catches ZoneInfoNotFoundError. It only
  caught ValueError, but ZoneInfoNotFoundError subclasses KeyError — on an
  image without the tzdata package the dispatch cron would have crashed on
  the first lead instead of failing safe. (tzdata is now pinned in
  requirements.txt so this should never fire, but failing safe matters more
  here than anywhere else in the system: the alternative to "don't call" is
  "call someone at 3 AM".)
- CALL_SCHEDULE_DAYS is now indexed defensively — a #_calls value edited by
  hand in Airtable to something past the end of the schedule used to be an
  IndexError that killed the whole dispatch run mid-loop.
- trigger_vapi_call() validates its config up front, so a missing
  VAPI_PHONE_NUMBER_ID reads as a config error instead of an opaque Vapi 400.
- REMOVED: send_sms() and send_email(), which raised NotImplementedError and
  were called by nothing.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from mello_time import parse_date_safely, today_local
from rentcast_lookup import (
    get_sold_comps, get_property_valuation, analyze_sold_comps, get_recommended_arv
)
from zillapi_lookup import get_zillow_valuation, extract_zestimate

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")

TARGET_CONCURRENT_CALLS = int(os.environ.get("TARGET_CONCURRENT_CALLS", 3))
# Start conservative for real-world testing; raise toward the account's
# actual concurrency limit (10, per Vapi's subscriptionLimits) once proven.


# ---------------------------------------------------------------------------
# Retry cadence — a decaying schedule instead of a flat attempt cap.
# index = attempt number (0-indexed), value = earliest day-offset from lead
# creation that attempt is allowed. Attempts 1-2 same day, then day 2, then
# roughly weekly through day 30 — after that, or after MAX_ATTEMPTS total,
# the lead is Exhausted regardless of days elapsed.
# ---------------------------------------------------------------------------

CALL_SCHEDULE_DAYS = [0, 0, 2, 9, 16, 23, 30]
MAX_ATTEMPTS = len(CALL_SCHEDULE_DAYS)

# Separate, more generous ceiling for leads with a real scheduled callback
# date. These are deliberately long-lived (a seller saying "check back in 6
# months" should not be dropped at 30 days) but still need a hard stop so an
# unreachable one is not redialed indefinitely.
MAX_SCHEDULED_ATTEMPTS = 15

# "Offer Made" is the warmest bucket in the system — a real number is on the
# table and the seller has not rejected it. Letting those sit on the standard
# 9/16/23-day spacing wastes the momentum that makes them valuable, so they
# get their own tight cadence measured from the LAST call rather than from
# lead creation.
OFFER_MADE_RETRY_DAYS = 2


def is_lead_exhausted(date_created: str, call_count: int, next_contact_date: str = None) -> bool:
    """
    True if this lead has used up its retry schedule — hit the attempt cap,
    or run past the final cutoff day.

    next_contact_date: a real scheduled callback OVERRIDES the 30-day
    cutoff. Without that, a genuinely promising lead ("check back in 6
    months") would be marked Exhausted and dropped months before the
    callback ever happened. It still gets a generous attempt ceiling so an
    unanswerable scheduled lead eventually stops.
    """
    call_count = call_count or 0

    if next_contact_date:
        return call_count >= MAX_SCHEDULED_ATTEMPTS

    if call_count >= MAX_ATTEMPTS:
        return True

    created = parse_date_safely(date_created)
    if created is None:
        # Unparseable creation date. Do NOT treat as exhausted (that would
        # silently kill leads over a formatting problem); is_retry_due()
        # below refuses to dial it, so it stays visible and inert until a
        # human looks at it.
        return False

    return (today_local() - created).days > CALL_SCHEDULE_DAYS[-1]


def is_retry_due(date_created: str, call_count: int, next_contact_date: str = None,
                 status: str = None, last_call_date: str = None) -> bool:
    """
    True if enough time has passed to allow the NEXT attempt.

    Priority order:
      1. next_contact_date — an explicit scheduled callback REPLACES the
         normal schedule entirely.
      2. status == "Offer Made" — the warmest bucket, uses
         OFFER_MADE_RETRY_DAYS measured from the last call.
      3. Everything else — the decaying schedule from lead creation.
    """
    call_count = call_count or 0

    if next_contact_date:
        scheduled = parse_date_safely(next_contact_date)
        if scheduled is None:
            return False  # malformed date — never dial on a guess
        return today_local() >= scheduled

    if is_lead_exhausted(date_created, call_count):
        return False

    if status == "Offer Made" and last_call_date:
        last = parse_date_safely(last_call_date)
        if last is not None:
            return (today_local() - last).days >= OFFER_MADE_RETRY_DAYS

    created = parse_date_safely(date_created)
    if created is None:
        return False  # no usable creation date — cannot place it on the schedule

    # Defensive index. A #_calls value edited by hand in Airtable to
    # something past the end of the schedule used to raise IndexError and
    # kill the entire dispatch run mid-loop, leaving the remaining leads
    # uncalled with no obvious cause.
    if call_count >= len(CALL_SCHEDULE_DAYS):
        return False

    return (today_local() - created).days >= CALL_SCHEDULE_DAYS[call_count]


def enrich_lead_with_valuation(address: str, city: str, state: str, zip_code: str) -> dict:
    """
    Runs the full RentCast + Zillow analysis for one lead.

    COST WARNING: one call to this function is 2 RentCast requests (AVM +
    sold comps) and 1 Zillapi request. RentCast's free tier is 50
    requests/month total. See the note in cron_morning_lead_prep.py — at 15
    leads a day this exhausts the free tier in under two days.
    """
    if not (address and state):
        raise ValueError(f"Cannot value a property without at least address and state (got {address!r}, {state!r})")

    full_address = ", ".join(p for p in [address, city, " ".join(x for x in [state, zip_code] if x)] if p)

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


# Predominant timezone per US state. Border-state caveat stands: a lead in
# far-west Texas (El Paso, Mountain time) is treated as Central, which makes
# the calling window one hour EARLIER than it should be there — i.e. it errs
# toward calling too early in the morning. Worth a per-zip lookup before
# expanding beyond Texas metros.
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
    tz_name = STATE_TIMEZONES.get((state or "").upper())
    if not tz_name:
        raise ValueError(f"Unknown state code: {state!r}. Cannot determine calling window safely.")
    return datetime.now(ZoneInfo(tz_name)).hour


def is_within_calling_hours(state: str, start_hour: int = 8, end_hour: int = 21) -> bool:
    """
    TCPA allows 8 AM - 9 PM in the CALLED PARTY's local time. Fails safe:
    anything it cannot resolve returns False, because the cost of not
    calling is one skipped lead and the cost of calling is a statutory
    violation per call.
    """
    try:
        current_hour = get_lead_local_hour(state)
    except (ValueError, ZoneInfoNotFoundError, KeyError) as e:
        print(f"  Cannot determine local time for state {state!r} ({e}) — not calling.")
        return False
    return start_hour <= current_hour < end_hour


def trigger_vapi_call(phone_number: str, lead_context: dict):
    """Places one outbound call through Vapi. Raises on any non-2xx."""
    assistant_id = os.environ.get("VAPI_ASSISTANT_ID")
    phone_number_id = os.environ.get("VAPI_PHONE_NUMBER_ID")

    missing = [
        name for name, value in (
            ("VAPI_API_KEY", VAPI_API_KEY),
            ("VAPI_ASSISTANT_ID", assistant_id),
            ("VAPI_PHONE_NUMBER_ID", phone_number_id),
        ) if not value
    ]
    if missing:
        raise RuntimeError(
            f"Cannot place a call — missing {', '.join(missing)} on this job. "
            f"(VAPI_PHONE_NUMBER_ID must be the Telnyx number ID, not a stale Twilio one.)"
        )

    headers = {"Authorization": f"Bearer {VAPI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "assistantId": assistant_id,
        "phoneNumberId": phone_number_id,
        "customer": {"number": phone_number},
        "assistantOverrides": {"variableValues": lead_context},
    }
    response = requests.post("https://api.vapi.ai/call/phone", headers=headers, json=payload, timeout=15)
    if not response.ok:
        print(f"Vapi call trigger failed ({response.status_code}): {response.text[:400]}")
    response.raise_for_status()
    return response.json()
