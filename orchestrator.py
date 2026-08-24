"""
The "central brain" — a persistent Background Worker implementing your
exact daily schedule. Deploy this as a SEPARATE Render service from your
existing web app (Render service type: Background Worker).

This is a skeleton: functions that call into modules you already have are
wired up for real. Functions for pieces that don't exist yet (SMS, email,
contract e-sign, appointment scheduling) are clearly stubbed with TODO —
fill these in as you build each integration, one at a time.

SETUP:
1. pip install apscheduler (in addition to your existing requirements)
2. Same environment variables as your web server, PLUS:
     VAPI_API_KEY (for triggering outbound calls)
3. Deploy as a Render Background Worker, pointing at this file
"""

import os
import requests
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler

from lead_sourcing import get_daily_leads, extract_lead_summary
from airtable_helpers import upsert_lead, query_leads, AirtableError
from rentcast_lookup import (
    get_sold_comps, get_property_valuation, analyze_sold_comps, get_recommended_arv
)
from zillapi_lookup import get_zillow_valuation, extract_zestimate
from calculator import flip_mao

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Leads")

scheduler = BlockingScheduler(timezone="America/Chicago")  # set to YOUR timezone


# ---------------------------------------------------------------------------
# 7:00 AM — apply approved improvements
# ---------------------------------------------------------------------------

@scheduler.scheduled_job("cron", hour=7, minute=0)
def apply_approved_updates():
    print(f"[{datetime.now()}] Checking for approved prompt updates...")
    # TODO: query a "Proposed Updates" Airtable table for records where
    # you've checked an "approved" box, then PATCH Vapi's assistant with
    # the new system prompt via their API. Skip if nothing's approved.
    pass


# ---------------------------------------------------------------------------
# 7:30 AM (and ongoing) — confirm leads, source new ones, run analysis
# ---------------------------------------------------------------------------

def enrich_lead_with_valuation(address: str, city: str, state: str, zip_code: str) -> dict:
    """Runs the full RentCast + Zillow analysis for one lead — reused logic
    from your web server's get_property_analysis endpoint."""
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


@scheduler.scheduled_job("cron", hour=7, minute=30)
def morning_lead_prep():
    print(f"[{datetime.now()}] Sourcing new leads and enriching data...")
    raw_leads = get_daily_leads(city="Austin", state="TX", limit=15)  # adjust market
    new_leads = extract_lead_summary(raw_leads)

    for lead in new_leads:
        full_address = lead.get("address")
        if not full_address:
            continue

        try:
            valuation = enrich_lead_with_valuation(
                full_address, lead["city"], lead["state"], lead["zip"]
            )
            fields = {
                "owner_name": lead.get("owner_name", ""),
                "phone": lead.get("phone", ""),
                "source": lead.get("source", ""),
                "status": "New",
                "arv": valuation["recommended_arv"],
                "state": lead["state"],  # used later for timezone-aware dispatch
            }
            upsert_lead(full_address, fields)
            print(f"  Saved to Airtable: {full_address} — ARV: {valuation['recommended_arv']}")
        except (AirtableError, Exception) as e:
            print(f"  FAILED to process {full_address}: {e}")
            continue


@scheduler.scheduled_job("interval", minutes=30, start_date=f"{datetime.now().date()} 07:30:00")
def continuous_enrichment():
    """Re-checks for leads still missing valuation data throughout the day —
    covers leads added mid-day or where the morning pull failed."""
    print(f"[{datetime.now()}] Running periodic enrichment check...")

    unenriched = query_leads("AND({status}='New', {arv}=BLANK())")
    print(f"  Found {len(unenriched)} leads needing enrichment")

    for record in unenriched:
        fields = record["fields"]
        address = fields.get("address")
        if not address:
            continue
        # address is stored as one combined string — this assumes city/state/zip
        # were captured separately at intake; adjust if your lead source stores
        # them differently.
        try:
            valuation = enrich_lead_with_valuation(
                address, fields.get("city", ""), fields.get("state", ""), fields.get("zip", "")
            )
            upsert_lead(address, {"arv": valuation["recommended_arv"]})
            print(f"  Enriched: {address} — ARV: {valuation['recommended_arv']}")
        except Exception as e:
            print(f"  FAILED to enrich {address}: {e}")
            continue


# ---------------------------------------------------------------------------
# 8:00 AM - 9:00 PM — calling window
# ---------------------------------------------------------------------------

from datetime import datetime
from zoneinfo import ZoneInfo

# Predominant timezone per US state. A few large states genuinely span two
# zones (TX, FL, etc.) — this uses each state's majority-population zone as
# a reasonable approximation. A more precise version would look up by city/
# zip instead of state, worth upgrading to once you have real call volume
# and want to be exact about border-zone leads.
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
    """
    Returns the current hour (0-23) in the lead's local timezone, based on
    their state. Calls must only dispatch when this falls within legal
    calling hours (8am-9pm in the LEAD's timezone, not yours — a real TCPA
    requirement, not a suggestion).
    """
    tz_name = STATE_TIMEZONES.get(state.upper())
    if not tz_name:
        raise ValueError(f"Unknown state code: {state}. Cannot determine calling window safely.")
    local_time = datetime.now(ZoneInfo(tz_name))
    return local_time.hour


def is_within_calling_hours(state: str, start_hour: int = 8, end_hour: int = 21) -> bool:
    """True if it's currently within legal calling hours in the lead's timezone."""
    try:
        current_hour = get_lead_local_hour(state)
    except ValueError:
        return False  # unknown state — fail safe, don't call
    return start_hour <= current_hour < end_hour


def trigger_vapi_call(phone_number: str, lead_context: dict):
    """
    Places a real outbound call via Vapi's API, passing pre-fetched
    valuation data as call variables. The agent's system prompt should
    reference these directly via {{recommended_arv}}, {{mao_ceiling}}, etc.
    (Liquid template syntax) instead of calling get_property_analysis live —
    this is the whole point of the pre-fetch architecture: zero live
    dependency on RentCast/Zillow during the actual conversation.

    Endpoint confirmed from Vapi's own docs, though their docs reference
    both "/call" and "/call/phone" in different places — verify against
    your live account before trusting this at volume.
    """
    if not VAPI_API_KEY:
        raise RuntimeError("VAPI_API_KEY not set")

    headers = {"Authorization": f"Bearer {VAPI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "assistantId": os.environ.get("VAPI_ASSISTANT_ID"),
        "phoneNumberId": os.environ.get("VAPI_PHONE_NUMBER_ID"),
        "customer": {"number": phone_number},
        "assistantOverrides": {
            "variableValues": lead_context
        },
    }
    response = requests.post("https://api.vapi.ai/call/phone", headers=headers, json=payload, timeout=15)
    if not response.ok:
        print(f"Vapi call trigger failed ({response.status_code}): {response.text}")
    response.raise_for_status()
    return response.json()


MAX_CALLS_PER_DISPATCH_RUN = 3  # throttle per run — simple, honest concurrency
                                 # control rather than real-time tracking, since
                                 # this job runs every 15 min anyway


@scheduler.scheduled_job("interval", minutes=15, start_date=f"{datetime.now().date()} 08:00:00",
                          end_date=f"{datetime.now().date()} 21:00:00")
def dispatch_calls():
    print(f"[{datetime.now()}] Checking for leads ready to call...")

    ready_leads = query_leads("AND({status}='New', {arv}!=BLANK())")
    print(f"  Found {len(ready_leads)} enriched leads with status New")

    calls_made = 0
    for record in ready_leads:
        if calls_made >= MAX_CALLS_PER_DISPATCH_RUN:
            print(f"  Reached batch limit ({MAX_CALLS_PER_DISPATCH_RUN}) for this run — remaining leads wait for next cycle")
            break

        fields = record["fields"]
        address = fields.get("address")
        state = fields.get("state")
        phone = fields.get("phone")

        if not (address and state and phone):
            print(f"  Skipping incomplete record: {address}")
            continue

        if not is_within_calling_hours(state):
            print(f"  Skipping {address} — outside calling hours in {state} right now")
            continue

        try:
            call_context = {
                "seller_name": fields.get("owner_name", "there"),
                "property_address": address,
                "recommended_arv": str(fields.get("arv")),
            }
            result = trigger_vapi_call(phone, call_context)
            upsert_lead(address, {"status": "Contacted"})
            print(f"  Called {address} — Vapi call ID: {result.get('id')}")
            calls_made += 1
        except Exception as e:
            print(f"  FAILED to call {address}: {e}")
            continue


def send_sms(phone_number: str, message: str):
    """TODO: Twilio integration — not yet built."""
    raise NotImplementedError("SMS integration not yet built")


def send_email(to_address: str, subject: str, body: str):
    """TODO: email service integration (e.g. SendGrid/Postmark) — not yet built."""
    raise NotImplementedError("Email integration not yet built")


# ---------------------------------------------------------------------------
# 9:00 PM — close out the day
# ---------------------------------------------------------------------------

@scheduler.scheduled_job("cron", hour=21, minute=0)
def evening_wrap_up():
    print(f"[{datetime.now()}] Wrapping up the day...")
    # TODO: confirm all pending SMS/emails sent, compile a summary of the
    # day's calls (counts by outcome), and flag anything ambiguous for
    # your attention — e.g. write a "Daily Summary" record to Airtable
    pass


# ---------------------------------------------------------------------------
# 10:00 PM — draft improvements for tomorrow
# ---------------------------------------------------------------------------

@scheduler.scheduled_job("cron", hour=22, minute=0)
def draft_improvements():
    print(f"[{datetime.now()}] Analyzing today's calls for improvement ideas...")
    # TODO: pull today's call transcripts from Vapi's API, send them to
    # Claude via the Anthropic API asking it to identify patterns and
    # draft a proposed system prompt change, then write that proposal to
    # a "Proposed Updates" Airtable table for your 7am review — NEVER
    # apply automatically
    pass


if __name__ == "__main__":
    print("Central brain orchestrator starting...")
    scheduler.start()
