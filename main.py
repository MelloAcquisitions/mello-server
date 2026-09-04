"""
Mello Acquisitions — Agent Tool Server

This is the "toolbox" Vapi calls into during a live call. Claude decides
WHEN to call these; this server does the actual work (property lookups,
math, writing to Airtable) and hands the result back.

SETUP:
1. pip install -r requirements.txt
2. Set these environment variables (locally for testing, and on your hosting
   platform once deployed):
     RENTCAST_API_KEY
     AIRTABLE_API_KEY
     AIRTABLE_BASE_ID       (from your Airtable base's URL or API docs page)
     AIRTABLE_TABLE_NAME    (e.g. "Leads")
3. Run locally to test: uvicorn main:app --reload
   Then visit http://127.0.0.1:8000/docs for an interactive test page —
   FastAPI builds this automatically, no extra work needed.
4. Deploy (see deployment steps provided separately) to get a public URL.
"""

import os
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from calculator import flip_mao, calculate_final_fee
from rentcast_lookup import (
    get_sold_comps,
    get_property_valuation,
    analyze_sold_comps,
    get_recommended_arv,
)
from zillapi_lookup import get_zillow_valuation, extract_zestimate
from dashboard import router as dashboard_router
from airtable_helpers import (
    find_lead_record, find_lead_flexible, resolve_address_for_write,
    upsert_lead, query_leads, AirtableError, increment_daily_log_field,
)
from deal_dispatch import dispatch_agreed_deal, notify_attention_needed

app = FastAPI(title="Mello Acquisitions Agent Tools")
app.include_router(dashboard_router)


@app.exception_handler(AirtableError)
async def airtable_error_handler(request: Request, exc: AirtableError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Leads")
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"


# ---------------------------------------------------------------------------
# Request/response schemas — FastAPI uses these to validate incoming calls
# and auto-generate the /docs test page.
# ---------------------------------------------------------------------------

class PropertyAnalysisRequest(BaseModel):
    address: str
    city: str
    state: str
    zip_code: str


class MaoRequest(BaseModel):
    arv: float
    repair_cost: float
    wholesale_fee_min: float = 10000
    buyer_profit_pct: float = 0.10


class FinalFeeRequest(BaseModel):
    arv: float
    repair_cost: float
    agreed_price: float
    buyer_profit_pct: float = 0.10


class LogCallRequest(BaseModel):
    address: str
    status: str  # Must exactly match your Airtable single-select options (case-sensitive):
                 # New | Contacted | Qualified | Offer Made | Agreed | Rejected | Opt Out | Human Call | Priority Follow-up | Exhausted | Closed
                 # NOTE: "Closed" is never set by the AI or this API — it's set manually
                 # by you once a deal actually funds. Listed here only for completeness.
    notes: Optional[str] = ""
    offer_amount: Optional[float] = None
    arv: Optional[float] = None
    repair_estimate: Optional[float] = None
    mao_floor: Optional[float] = None
    email: Optional[str] = None
    next_contact_date: Optional[str] = None  # ISO date "2027-02-15" — set when the seller gave a real future timeframe ("check back in 6 months")


class FlagReviewRequest(BaseModel):
    address: str
    agreed_price: float
    call_transcript_summary: str
    email: Optional[str] = None
    repair_estimate: Optional[float] = None
    mao_floor: Optional[float] = None


# ---------------------------------------------------------------------------
# Airtable helpers — imported at the top of the file
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Endpoints — these URLs are what you paste into Vapi's tool configuration
# ---------------------------------------------------------------------------

@app.get("/")
def health_check():
    """Quick check that the server is alive — visit this URL in a browser after deploying."""
    return {"status": "Mello agent server is running"}


@app.post("/get_property_analysis")
def get_property_analysis(req: PropertyAnalysisRequest):
    """
    The main research call. Pulls sold comps + RentCast AVM + Zillow Zestimate,
    reconciles all available sources, and returns a conservative recommended
    ARV plus supporting data (investor buyer activity, comp count, etc.).
    """
    full_address = f"{req.address}, {req.city}, {req.state} {req.zip_code}"

    avm_result = get_property_valuation(full_address)
    subject = avm_result.get("subjectProperty", {})

    sold_result = get_sold_comps(full_address, subject_property=subject)
    sold_properties = sold_result if isinstance(sold_result, list) else sold_result.get("properties", [])

    analysis = analyze_sold_comps(sold_properties, subject_property=subject)

    # Zillow is a genuinely independent third source — but it's a separate
    # vendor with its own possible outages, so it must never take down the
    # whole endpoint. If it fails, we just proceed with the two RentCast
    # candidates, same as before Zillow existed.
    zillow_estimate = None
    try:
        zillow_result = get_zillow_valuation(full_address)
        zillow_estimate = extract_zestimate(zillow_result)
    except Exception as e:
        print(f"Zillow lookup failed, proceeding without it: {e}")

    recommendation = get_recommended_arv(analysis, avm_result, zillow_estimate=zillow_estimate)

    return {
        "recommended_arv": recommendation["recommended_arv"],
        "source": recommendation["source"],
        "spread_pct": recommendation["spread_pct"],
        "all_candidates": recommendation["all_candidates"],
        "comp_count": analysis["comp_count"],
        "investor_buyer_pct": analysis["investor_buyer_pct"],
        "subject_property": {
            "squareFootage": subject.get("squareFootage"),
            "yearBuilt": subject.get("yearBuilt"),
            "bedrooms": subject.get("bedrooms"),
            "bathrooms": subject.get("bathrooms"),
        },
    }


@app.post("/calculate_mao")
def calculate_mao_endpoint(req: MaoRequest):
    """Standard flip-deal MAO calculation — returns ceiling, opening offer,
    and the scaled wholesale fee. Can return no_deal=True if the numbers
    can't support the $10K minimum fee while protecting buyer margin."""
    return flip_mao(
        arv=req.arv,
        repair_cost=req.repair_cost,
        wholesale_fee_min=req.wholesale_fee_min,
        buyer_profit_pct=req.buyer_profit_pct,
    )


@app.post("/calculate_final_fee")
def calculate_final_fee_endpoint(req: FinalFeeRequest):
    """
    Call this ONCE a real price has been agreed with the seller — not
    during the initial offer. Your actual fee can be higher than the
    minimum used to set the ceiling if you negotiated a lower price than
    the max — that's expected and good, not something to cap.
    """
    return calculate_final_fee(
        arv=req.arv,
        repair_cost=req.repair_cost,
        agreed_price=req.agreed_price,
        buyer_profit_pct=req.buyer_profit_pct,
    )


@app.post("/log_call_outcome")
def log_call_outcome(req: LogCallRequest, background_tasks: BackgroundTasks):
    """
    Writes (or updates) the lead's record in Airtable. Called at the end of
    EVERY call, regardless of outcome — including opt-outs and rejections.

    Automatically increments the #_calls column by 1 each time this fires,
    since one call outcome logged = one call made.

    Email notification fires ONLY for statuses that genuinely need your
    attention — "Human Call" (seller asked for a person), "Offer Made" (a
    real number is on the table, close to a deal), and "Priority Follow-up"
    (weak number but strong, specific reason to sell — worth your personal
    touch). Every other outcome (New, Contacted, Qualified, Rejected, Opt
    Out, Exhausted) is logged silently — deliberately not emailed, so you
    only ever see leads that actually warrant your time.

    NOTE: writes to an "offer_amount" field — this column must exist in
    your Airtable table (currency type) or this will fail. If you haven't
    added it yet, add it before testing this endpoint.
    """
    # find_lead_flexible (not find_lead_record) because the live agent sends
    # the FULL address ("123 Main St, Austin, TX 78745") while Airtable's
    # address column only stores the street portion. An exact match alone
    # would miss on every real call and create a duplicate orphan record.
    existing_record = find_lead_flexible(req.address)
    write_address = existing_record["fields"]["address"] if existing_record else req.address.split(",")[0].strip()
    current_call_count = existing_record["fields"].get("#_calls", 0) if existing_record else 0

    # DO NOT increment #_calls here when the record already exists.
    # cron_dispatch_calls.py already incremented it at the moment the call
    # was triggered. Incrementing again here would count every ANSWERED call
    # as two attempts — burning through the retry schedule twice as fast as
    # intended and inflating every call statistic on the dashboard.
    # Only set it for a brand-new record (e.g. a manually-triggered test
    # call for an address that isn't in the table yet), where nothing
    # incremented it beforehand.
    fields = {
        "status": req.status,
        "last_call_date": __import__("datetime").date.today().isoformat(),
    }
    # Only overwrite the summary if the agent actually sent one — req.notes
    # defaults to "", and an unconditional assignment here would silently
    # WIPE a previous call's notes on any call where the agent skipped this
    # optional field.
    if req.notes:
        fields["call_transcript_summary"] = req.notes
    if not existing_record:
        fields["#_calls"] = 1
    if req.offer_amount is not None:
        fields["offer_amount"] = req.offer_amount
    if req.arv is not None:
        fields["arv"] = req.arv
    if req.repair_estimate is not None:
        fields["repair_estimate"] = req.repair_estimate
    if req.mao_floor is not None:
        fields["mao_floor"] = req.mao_floor
    if req.email is not None:
        fields["email"] = req.email
    if req.next_contact_date is not None:
        fields["next_contact_date"] = req.next_contact_date

    result = upsert_lead(write_address, fields)

    NOTIFY_STATUSES = {"Human Call", "Offer Made", "Priority Follow-up"}
    if req.status in NOTIFY_STATUSES:
        lead_fields_for_notify = {**(existing_record["fields"] if existing_record else {}), **fields, "address": write_address}
        background_tasks.add_task(_safe_notify_attention_needed, write_address, lead_fields_for_notify, req.status)

    true_call_count = current_call_count if existing_record else 1
    return {"success": True, "airtable_record": result.get("id"), "call_count": true_call_count}


def _safe_notify_attention_needed(address: str, lead_fields: dict, status: str):
    try:
        notify_attention_needed(lead_fields, status)
        print(f"Attention-needed notification sent for {address} (status: {status})")
    except Exception as e:
        print(f"Failed to send human-call notification for {address} (status still saved): {e}")



@app.post("/inbound_email")
async def inbound_email(request: Request):
    """
    Mailgun calls this the moment a seller replies to an email, attachments
    included. Extracts any image/video attachments and stores their URLs
    directly on the matching lead's Airtable record.
    """
    form = await request.form()
    sender_email = form.get("sender", "")
    attachment_count = int(form.get("attachment-count", 0))

    attachment_urls = []
    for i in range(1, attachment_count + 1):
        # Mailgun includes a direct URL for each attachment in the webhook payload
        key = f"attachment-{i}"
        if key in form:
            file = form[key]
            attachment_urls.append({"url": file.url if hasattr(file, "url") else str(file)})

    # TODO: look up the lead by matching sender_email against an "email"
    # field on the Leads table (needs adding — currently no email column),
    # then append attachment_urls to an Airtable Attachment-type field.
    print(f"Received {len(attachment_urls)} attachment(s) from {sender_email}")

    return {"received": True, "attachment_count": len(attachment_urls)}


@app.post("/inbound_sms")
async def inbound_sms(request: Request):
    """
    Twilio calls this the moment a seller texts back, MMS photos included.
    Extracts any media URLs and stores them directly on the matching lead's
    Airtable record.
    """
    form = await request.form()
    from_number = form.get("From", "")
    num_media = int(form.get("NumMedia", 0))

    media_urls = []
    for i in range(num_media):
        media_url = form.get(f"MediaUrl{i}")
        if media_url:
            media_urls.append({"url": media_url})

    # TODO: look up the lead by matching from_number against the "phone"
    # field already on your Leads table, then append media_urls to an
    # Airtable Attachment-type field (add one if it doesn't exist yet —
    # Airtable's Attachment field type accepts external URLs directly and
    # will fetch/store the file itself, no separate upload step needed).
    print(f"Received {len(media_urls)} MMS attachment(s) from {from_number}")

    return {"received": True, "media_count": len(media_urls)}



@app.post("/vapi_call_ended")
async def vapi_call_ended(request: Request):
    """
    Vapi's end-of-call-report webhook — fires automatically when a call
    that actually CONNECTED ends, independent of whether the live agent's
    own log_call_outcome tool call succeeded. Best-effort enrichment, not
    a guarantee:
      - Vapi does NOT send this for unanswered calls (confirmed by their
        own support) — fine, the retry cadence already handles no-answer
        leads correctly without this.
      - There's a known intermittent bug where this occasionally doesn't
        fire even for connected calls.

    Configure this as your assistant's Server URL in Vapi, with
    "end-of-call-report" included in serverMessages.
    """
    body = await request.json()
    message = body.get("message", {})

    if message.get("type") != "end-of-call-report":
        return {"received": True, "ignored": "not an end-of-call-report"}

    duration = message.get("durationSeconds")
    ended_reason = message.get("endedReason")
    phone = message.get("call", {}).get("customer", {}).get("number")
    ai_summary = message.get("analysis", {}).get("summary", "")

    print(f"End-of-call report: phone={phone}, duration={duration}s, reason={ended_reason}")

    # This was previously only printed and then discarded — now it accumulates
    # into today's Daily Log record so the dashboard can compute a REAL,
    # duration-based Vapi cost estimate instead of a flat per-call guess.
    if duration is not None:
        try:
            increment_daily_log_field("call_seconds_today", duration)
        except Exception as e:
            print(f"Failed to record call duration for cost tracking: {e}")

    if not phone:
        return {"received": True, "warning": "no phone number in payload"}

    # find_lead_record() filters by the "address" field, so it can never
    # match a phone number — this webhook needs a phone-based lookup instead.
    # Escape any single quotes so a stray character can't break the Airtable
    # formula (matches the same precaution used elsewhere on user-supplied data).
    try:
        safe_phone = phone.replace("'", "\\'")
        matches = query_leads(f"{{phone}}='{safe_phone}'", max_records=1)
        record = matches[0] if matches else None
    except Exception:
        record = None

    SHORT_CALL_THRESHOLD_SECONDS = 15
    if duration is not None and duration < SHORT_CALL_THRESHOLD_SECONDS and record:
        current_status = record["fields"].get("status")
        if current_status in ("New", "Contacted", None):
            note = f"Short call ({duration}s, ended: {ended_reason}). AI summary: {ai_summary}"
            print(f"  Flagging as short/low-engagement call: {note}")
            # Logged as a note, not an automatic status change — a human
            # glance at "short call, no engagement" is safer than the
            # system unilaterally deciding this lead is dead from one
            # data point alone.

    return {"received": True}


def _dispatch_and_record(address: str, lead_fields: dict, agreed_price: float):
    """
    Runs after the HTTP response has already gone back to Vapi — the call
    keeps moving instead of sitting on a multi-second pause for docx
    generation + SMTP login. Exceptions here can't be surfaced back to the
    call anymore, so they're logged loudly and left visible on the record
    for you to notice from the dashboard instead.
    """
    try:
        dispatch_agreed_deal(lead_fields, agreed_price)
        current_emails = lead_fields.get("#_emails", 0)
        upsert_lead(address, {"#_emails": current_emails + 1})
        print(f"Contract emailed successfully for {address}")
    except Exception as e:
        print(f"Contract dispatch FAILED for {address} (Agreed status still saved — handle manually): {e}")
        try:
            upsert_lead(address, {
                "call_transcript_summary": (lead_fields.get("call_transcript_summary") or "")
                + f"\n[Contract email failed to send: {e}]"
            })
        except Exception as inner_e:
            print(f"Also failed to record the dispatch failure on the record: {inner_e}")


@app.post("/flag_for_human_review")
def flag_for_human_review(req: FlagReviewRequest, background_tasks: BackgroundTasks):
    """
    Called ONLY when a seller verbally agrees to a price. Marks the lead as
    Agreed, then queues contract generation + emailing it to YOU (not the
    seller) as a BACKGROUND task — this endpoint returns to Vapi immediately
    rather than making the live call wait several seconds for a docx render
    and an SMTP login. See deal_dispatch.py. This is the interim workflow
    while Box Sign's paid tier is on hold: it does NOT send anything to the
    seller itself. You still review the attached contract, add a signature
    field, and send it on yourself.
    """
    fields = {
        "status": "Agreed",
        "offer_amount": req.agreed_price,
        "call_transcript_summary": req.call_transcript_summary,
        "last_call_date": __import__("datetime").date.today().isoformat(),
    }
    if req.email is not None:
        fields["email"] = req.email
    if req.repair_estimate is not None:
        fields["repair_estimate"] = req.repair_estimate
    if req.mao_floor is not None:
        fields["mao_floor"] = req.mao_floor

    # Same full-address-vs-street-only mismatch as log_call_outcome — resolve
    # to the existing record's real stored address before writing, so this
    # updates the real lead instead of creating a duplicate.
    write_address = resolve_address_for_write(req.address)
    result = upsert_lead(write_address, fields)

    full_record = find_lead_record(write_address)
    lead_fields = full_record["fields"] if full_record else {**fields, "address": write_address}
    background_tasks.add_task(_dispatch_and_record, write_address, lead_fields, req.agreed_price)

    return {
        "success": True,
        "airtable_record": result.get("id"),
        "needs_human_review": True,
        "contract_dispatch": "queued",
    }
