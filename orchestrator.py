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
        if not lead.get("address"):
            continue
        valuation = enrich_lead_with_valuation(
            lead["address"], lead["city"], lead["state"], lead["zip"]
        )
        # TODO: write lead + valuation into Airtable as a new "New" status record
        print(f"  Enriched: {lead['address']} — recommended ARV: {valuation['recommended_arv']}")


@scheduler.scheduled_job("interval", minutes=30, start_date=f"{datetime.now().date()} 07:30:00")
def continuous_enrichment():
    """Re-checks for leads still missing valuation data throughout the day —
    covers leads added mid-day or where the morning pull failed."""
    print(f"[{datetime.now()}] Running periodic enrichment check...")
    # TODO: query Airtable for leads with status=New and no arv value yet,
    # run enrich_lead_with_valuation() on each


# ---------------------------------------------------------------------------
# 8:00 AM - 9:00 PM — calling window
# ---------------------------------------------------------------------------

def get_lead_local_hour(state: str) -> int:
    """TODO: map state -> timezone (e.g. via a state-to-tz lookup dict or
    the `pytz`/`zoneinfo` library) and return the CURRENT local hour for
    that lead. Calls must only dispatch when this falls within 8-21."""
    raise NotImplementedError("Timezone mapping not yet built")


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


@scheduler.scheduled_job("interval", minutes=15, start_date=f"{datetime.now().date()} 08:00:00",
                          end_date=f"{datetime.now().date()} 21:00:00")
def dispatch_calls():
    print(f"[{datetime.now()}] Checking for leads ready to call...")
    # TODO: query Airtable for status=New leads with valuation data already
    # populated, filter to only those within their LOCAL calling hours
    # (get_lead_local_hour), respect a concurrency cap (e.g. max 3 active
    # calls at once), then trigger_vapi_call() for each ready lead
    pass


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
