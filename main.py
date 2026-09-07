"""
Mello Acquisitions — Agent Tool Server

The "toolbox" Vapi calls into during a live call. The voice model decides
WHEN to call these; this server does the work (math, writing to Airtable)
and hands the result back.

The three custom Vapi tools are type `apiRequest`, which sends a FLAT JSON
body and accepts any 2xx JSON response. The endpoints below are therefore
the correct shape — no toolCallList envelope parsing is needed. (A
`vapi_tools_router.py` written before the tool type was known spoke the
`function`-tool protocol instead; it has been deleted.)

RUN LOCALLY:  uvicorn main:app --reload   ->  http://127.0.0.1:8000/docs

REQUIRED ENV (web service):
  AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME,
  RESEND_API_KEY, OWNER_EMAIL, BUYER_NAME, BUYER_PHONE,
  DEFAULT_TITLE_COMPANY, DASHBOARD_PASSWORD
STRONGLY RECOMMENDED:
  MELLO_TOOL_SECRET  — see require_tool_auth() below.

CHANGES IN THIS CLEANUP
-----------------------
- Tool endpoints are no longer unauthenticated (see require_tool_auth).
- Empty-string numerics from Vapi are coerced instead of 422-ing the call.
- Status values are validated against the Airtable single-select options,
  so one bad status string can't lose the whole call outcome.
- Notes are APPENDED, matching the two fallback layers, instead of
  overwriting the lead's entire call history.
- The end-of-call webhook dedupes on the Vapi CALL ID, not on today's date.
- REMOVED: /get_property_analysis and /calculate_final_fee (no Vapi tool
  called either; valuation now happens in the enrichment crons before the
  call, per system prompt v13 "do not fetch mid-call"), and /inbound_email
  and /inbound_sms (both were TODO stubs that parsed a payload, printed a
  line and threw it away — they looked like working integrations and were
  not).
"""

import os
from typing import Any, Optional, Union

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from airtable_helpers import (
    AirtableError, append_notes, find_lead_by_phone, find_lead_flexible,
    increment_daily_log_field, resolve_address_for_write, upsert_lead,
)
from calculator import flip_mao
from dashboard import router as dashboard_router
from deal_dispatch import dispatch_agreed_deal, notify_attention_needed
from mello_time import today_iso

app = FastAPI(title="Mello Acquisitions Agent Tools")
app.include_router(dashboard_router)


@app.exception_handler(AirtableError)
async def airtable_error_handler(request: Request, exc: AirtableError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ---------------------------------------------------------------------------
# Authentication
#
# These endpoints previously had NONE. Anyone who found the Render URL could
# write to the Leads table, mark a lead Agreed, and trigger a contract
# generation + email — no key, no signature, no rate limit. The URL is not
# secret: it is pasted into Vapi's dashboard, it appears in call logs, and
# it was published in this project's own handoff docs.
#
# Vapi apiRequest tools support custom headers. Set MELLO_TOOL_SECRET here
# and add the matching `X-Mello-Token` header to each of the three tools in
# Vapi. Until it is set, requests are allowed and a warning is logged — so
# deploying this file cannot take the live call path down. Set it, add the
# headers, then confirm the warning stops appearing.
# ---------------------------------------------------------------------------

MELLO_TOOL_SECRET = os.environ.get("MELLO_TOOL_SECRET")


def require_tool_auth(x_mello_token: Optional[str]) -> None:
    if not MELLO_TOOL_SECRET:
        print("WARNING: MELLO_TOOL_SECRET is not set — tool endpoints are OPEN to "
              "anyone with the URL. Set it and add an X-Mello-Token header to each "
              "Vapi tool.")
        return
    import secrets as _secrets
    if not x_mello_token or not _secrets.compare_digest(x_mello_token, MELLO_TOOL_SECRET):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Mello-Token")


# ---------------------------------------------------------------------------
# Airtable single-select options for `status`.
#
# Writing a value Airtable does not recognise returns 422 and the ENTIRE
# write fails — meaning a call outcome is lost because the model produced
# "Follow Up" instead of "Priority Follow-up". Validating here converts a
# total loss into a slightly-less-precise save.
#
# "Closed" is set manually by the owner once a deal funds. "New" and
# "Exhausted" are set by the system. The agent should never send any of
# those three, but they are listed so a stray one is not rejected outright.
# ---------------------------------------------------------------------------

VALID_STATUSES = {
    "New", "Contacted", "Qualified", "Offer Made", "Agreed", "Rejected",
    "Opt Out", "Human Call", "Priority Follow-up", "Exhausted", "Closed",
}

# Common near-misses a voice model produces, mapped to the real option.
STATUS_ALIASES = {
    "optout": "Opt Out", "opt-out": "Opt Out", "do not call": "Opt Out",
    "priority followup": "Priority Follow-up", "priority follow up": "Priority Follow-up",
    "follow up": "Priority Follow-up", "followup": "Priority Follow-up",
    "human": "Human Call", "human callback": "Human Call", "callback": "Human Call",
    "offer": "Offer Made", "offermade": "Offer Made",
    "not interested": "Rejected", "declined": "Rejected",
}


def normalize_status(raw: str) -> str:
    """Maps whatever the model sent onto a real Airtable option. Falls back
    to 'Contacted' — the honest, conservative meaning of "a human was
    reached, outcome unclear" — rather than failing the write."""
    if not raw:
        return "Contacted"
    candidate = str(raw).strip()
    if candidate in VALID_STATUSES:
        return candidate
    lowered = candidate.lower()
    for option in VALID_STATUSES:
        if option.lower() == lowered:
            return option
    if lowered in STATUS_ALIASES:
        return STATUS_ALIASES[lowered]
    print(f"  Unrecognised status {raw!r} from the agent — saving as 'Contacted' "
          f"so the outcome is not lost. Check the tool's status description in Vapi.")
    return "Contacted"


def _num(value: Any) -> Optional[float]:
    """
    Coerces a Vapi-supplied numeric argument, tolerating an empty string.

    The Vapi tool schemas define numeric parameters with `"default": ""` —
    an empty string default on a `number` type. If Vapi injects defaults for
    omitted optional fields, the body arrives as {"arv": ""}, which a strict
    Optional[float] rejects with a 422. The model then sees the tool fail
    mid-call and the outcome is never written.

    Rather than depending on whether Vapi does that, accept both and coerce.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


Number = Optional[Union[float, str]]


class MaoRequest(BaseModel):
    arv: Union[float, str]
    repair_cost: Union[float, str]
    wholesale_fee_min: Number = 10000
    buyer_profit_pct: Number = 0.10


class LogCallRequest(BaseModel):
    address: str
    status: str
    notes: Optional[str] = ""
    offer_amount: Number = None
    arv: Number = None
    repair_estimate: Number = None
    mao_floor: Number = None
    email: Optional[str] = None
    # ISO date "2027-02-15" — set when the seller gave a real future timeframe.
    next_contact_date: Optional[str] = None


class FlagReviewRequest(BaseModel):
    address: str
    agreed_price: Union[float, str]
    call_transcript_summary: str
    email: Optional[str] = None
    repair_estimate: Number = None
    mao_floor: Number = None


# ---------------------------------------------------------------------------
# Endpoints — these URLs go into Vapi's tool configuration.
#
# The hostname in Vapi must be the one from Render's dashboard, suffix and
# all (https://mello-server-hqfi.onrender.com), not the service name. The
# old suffix-less hostname still resolves but has nothing behind it, so
# requests hang forever instead of failing — that cost this project weeks.
# ---------------------------------------------------------------------------

@app.get("/")
def health_check():
    """Quick liveness check. Also reports whether tool auth is configured,
    so you can confirm the secret took effect without reading logs."""
    return {
        "status": "Mello agent server is running",
        "tool_auth": "enabled" if MELLO_TOOL_SECRET else "OPEN — set MELLO_TOOL_SECRET",
    }


@app.post("/calculate_mao")
def calculate_mao_endpoint(req: MaoRequest, x_mello_token: Optional[str] = Header(default=None)):
    """
    Standard flip-deal MAO calculation. Returns the ceiling (mao_floor), an
    opening offer, and the scaled wholesale fee — or no_deal=True with a
    plain-language reason when the numbers cannot support a real offer to
    the seller while protecting the end buyer's margin.
    """
    require_tool_auth(x_mello_token)
    return flip_mao(
        arv=_num(req.arv) or 0,
        repair_cost=_num(req.repair_cost) or 0,
        wholesale_fee_min=_num(req.wholesale_fee_min) or 10000,
        buyer_profit_pct=_num(req.buyer_profit_pct) or 0.10,
    )


@app.post("/log_call_outcome")
def log_call_outcome(req: LogCallRequest, background_tasks: BackgroundTasks,
                     x_mello_token: Optional[str] = Header(default=None)):
    """
    Writes (or updates) the lead's record. Called at the end of EVERY call,
    regardless of outcome — including opt-outs and rejections.

    Email notification fires ONLY for the three statuses that genuinely need
    a human: "Human Call", "Offer Made" and "Priority Follow-up". Everything
    else is logged silently and deliberately, so the inbox only ever
    contains leads that warrant real time.

    NOTE: writes to an `offer_amount` field — that column must exist on the
    Leads table (currency type) or this fails.
    """
    require_tool_auth(x_mello_token)

    # find_lead_flexible, not find_lead_record: the live agent sends the FULL
    # address ("123 Main St, Austin, TX 78745") while Airtable's address
    # column holds only the street portion. An exact match misses on every
    # real call and upsert_lead would create a duplicate orphan record.
    existing_record = find_lead_flexible(req.address)
    existing_fields = existing_record["fields"] if existing_record else {}
    write_address = existing_fields.get("address") or req.address.split(",")[0].strip()
    current_call_count = existing_fields.get("#_calls", 0) or 0

    fields = {
        "status": normalize_status(req.status),
        "last_call_date": today_iso(),
    }

    # APPEND, never replace. A lead can be called many times and there is one
    # notes field. The end-of-call webhook and the reconcile cron both append;
    # this endpoint used to overwrite, so the richest and most common path was
    # the one destroying prior call history — including opt-out language a
    # human would need to see.
    note = _text(req.notes)
    if note:
        fields["call_transcript_summary"] = append_notes(
            existing_fields.get("call_transcript_summary"),
            f"[{today_iso()}] {note}",
        )

    # DO NOT increment #_calls for an existing record. cron_dispatch_calls.py
    # already incremented it when the call was triggered. Incrementing again
    # would count every ANSWERED call as two attempts — burning the retry
    # schedule twice as fast and inflating every dashboard statistic. Only
    # set it for a brand-new record (e.g. a manual test call for an address
    # not yet in the table), where nothing incremented it beforehand.
    if not existing_record:
        fields["#_calls"] = 1

    for key, value in (
        ("offer_amount", _num(req.offer_amount)),
        ("arv", _num(req.arv)),
        ("repair_estimate", _num(req.repair_estimate)),
        ("mao_floor", _num(req.mao_floor)),
    ):
        if value is not None:
            fields[key] = value

    for key, value in (
        ("email", _text(req.email)),
        ("next_contact_date", _text(req.next_contact_date)),
    ):
        if value is not None:
            fields[key] = value

    result = upsert_lead(write_address, fields)

    NOTIFY_STATUSES = {"Human Call", "Offer Made", "Priority Follow-up"}
    if fields["status"] in NOTIFY_STATUSES:
        lead_fields_for_notify = {**existing_fields, **fields, "address": write_address}
        background_tasks.add_task(
            _safe_notify_attention_needed, write_address, lead_fields_for_notify, fields["status"]
        )

    return {
        "success": True,
        "airtable_record": result.get("id"),
        "call_count": current_call_count if existing_record else 1,
        "status_saved": fields["status"],
    }


def _safe_notify_attention_needed(address: str, lead_fields: dict, status: str):
    try:
        notify_attention_needed(lead_fields, status)
        print(f"Attention-needed notification sent for {address} (status: {status})")
    except Exception as e:
        print(f"Failed to send notification for {address} (status still saved): {e}")


@app.post("/flag_for_human_review")
def flag_for_human_review(req: FlagReviewRequest, background_tasks: BackgroundTasks,
                          x_mello_token: Optional[str] = Header(default=None)):
    """
    Called ONLY when a seller verbally agrees to a price. Marks the lead
    Agreed, then queues contract generation + email to YOU (not the seller)
    as a BACKGROUND task, so this returns to Vapi immediately instead of
    making the live call wait on a docx render and an API send.

    Interim workflow while Box Sign is on hold: this sends nothing to the
    seller. You review the attached contract, add a signature field, and
    send it on yourself.
    """
    require_tool_auth(x_mello_token)

    # Same full-address-vs-street-only mismatch as log_call_outcome. Resolve
    # once and reuse the record we already fetched, instead of the previous
    # resolve -> upsert -> re-fetch sequence (up to five Airtable round trips
    # on a live call, against a 5 req/sec per-base rate limit).
    existing_record = find_lead_flexible(req.address)
    existing_fields = existing_record["fields"] if existing_record else {}
    write_address = existing_fields.get("address") or resolve_address_for_write(req.address)

    agreed_price = _num(req.agreed_price)
    if agreed_price is None:
        raise HTTPException(status_code=400, detail="agreed_price is required and must be a number")

    fields = {
        "status": "Agreed",
        "offer_amount": agreed_price,
        "last_call_date": today_iso(),
        "call_transcript_summary": append_notes(
            existing_fields.get("call_transcript_summary"),
            f"[{today_iso()} AGREED at ${agreed_price:,.0f}] {req.call_transcript_summary}",
        ),
    }
    if _text(req.email):
        fields["email"] = _text(req.email)
    for key, value in (("repair_estimate", _num(req.repair_estimate)),
                       ("mao_floor", _num(req.mao_floor))):
        if value is not None:
            fields[key] = value

    result = upsert_lead(write_address, fields)

    lead_fields = {**existing_fields, **fields, "address": write_address}
    background_tasks.add_task(_dispatch_and_record, write_address, lead_fields, agreed_price)

    return {
        "success": True,
        "airtable_record": result.get("id"),
        "needs_human_review": True,
        "contract_dispatch": "queued",
    }


def _dispatch_and_record(address: str, lead_fields: dict, agreed_price: float):
    """
    Runs after the HTTP response has gone back to Vapi, so the call keeps
    moving instead of pausing for docx generation and an email send.
    Exceptions here cannot be surfaced to the call, so they are logged
    loudly AND written onto the record, where the dashboard will show them.
    """
    try:
        dispatch_agreed_deal(lead_fields, agreed_price)
        upsert_lead(address, {"#_emails": (lead_fields.get("#_emails", 0) or 0) + 1})
        print(f"Contract emailed successfully for {address}")
    except Exception as e:
        print(f"Contract dispatch FAILED for {address} "
              f"(Agreed status still saved — handle manually): {e}")
        try:
            upsert_lead(address, {
                "call_transcript_summary": append_notes(
                    lead_fields.get("call_transcript_summary"),
                    f"[{today_iso()}] CONTRACT EMAIL FAILED — send it manually: {e}",
                )
            })
        except Exception as inner_e:
            print(f"Also failed to record the dispatch failure on the record: {inner_e}")


@app.post("/vapi_call_ended")
async def vapi_call_ended(request: Request):
    """
    Vapi's end-of-call-report webhook — LAYER 2 of the three-layer logging
    architecture. Fires from Vapi's side when a connected call ends,
    independent of whether the model called log_call_outcome.

    Best-effort, not a guarantee:
      - Vapi does not send this for unanswered calls (confirmed by their
        support). Fine — the retry cadence handles no-answer leads already.
      - There is a known intermittent bug where it does not fire even for
        connected calls. That is why cron_reconcile_calls.py (layer 3, a
        pull) exists.

    Configure as the assistant's Server URL in Vapi, with
    "end-of-call-report" in serverMessages. Note this endpoint is
    deliberately NOT behind the tool secret: Vapi posts it from its own
    infrastructure without the custom headers attached to the tools. It
    writes nothing an attacker gains from and only ever moves New ->
    Contacted, but if that changes, sign it instead.
    """
    body = await request.json()
    message = body.get("message", {})

    if message.get("type") != "end-of-call-report":
        return {"received": True, "ignored": "not an end-of-call-report"}

    call = message.get("call") or {}
    call_id = call.get("id") or message.get("callId")
    duration = message.get("durationSeconds")
    ended_reason = message.get("endedReason")
    phone = (call.get("customer") or {}).get("number")
    ai_summary = (message.get("analysis") or {}).get("summary", "")

    print(f"End-of-call report: call={call_id} phone={phone} "
          f"duration={duration}s reason={ended_reason}")

    # Accumulate into today's Daily Log so the dashboard can compute a real,
    # duration-based Vapi cost instead of a flat per-call guess.
    if duration is not None:
        try:
            increment_daily_log_field("call_seconds_today", duration)
        except Exception as e:
            print(f"Failed to record call duration for cost tracking: {e}")

    if not phone:
        return {"received": True, "warning": "no phone number in payload"}

    # Phone formats never match exactly — Vapi sends E.164, Airtable stores
    # whatever BatchData returned.
    record = find_lead_by_phone(phone)
    if not record:
        print(f"  No Airtable lead matches {phone} — nothing to record against.")
        return {"received": True, "warning": "no matching lead"}

    fields = record["fields"]
    address = fields.get("address")
    if not address:
        print(f"  Lead {record.get('id')} has no address value — cannot write to it.")
        return {"received": True, "warning": "matched lead has no address"}

    today = today_iso()
    existing_notes = fields.get("call_transcript_summary") or ""

    # DEDUPE ON THE VAPI CALL ID, not on today's date.
    #
    # The old check was `last_call_date == today`. That breaks the moment a
    # lead is dialled twice in one day: the first call stamps today's date,
    # and the second call then looks already-handled — so a second call the
    # agent failed to log was silently dropped by the layer whose whole job
    # is catching exactly that. cron_reconcile_calls.py already fixed this
    # for layer 3; layer 2 still had the bug.
    if call_id and call_id in existing_notes:
        return {"received": True, "already_recorded": True}

    agent_logged_this_call = fields.get("last_call_date") == today

    if agent_logged_this_call:
        # The agent logged it. Don't touch its richer notes — attach Vapi's
        # metadata alongside, tagged with the call id so a later run of this
        # webhook (or the reconcile cron) recognises it.
        addition = f"[Call {call_id}: {duration}s, ended: {ended_reason}]"
        if ai_summary and ai_summary not in existing_notes:
            addition = (f"[Call {call_id}: {duration}s, ended: {ended_reason}. "
                        f"Vapi summary: {ai_summary}]")
        try:
            upsert_lead(address, {
                "call_transcript_summary": append_notes(existing_notes, addition)
            })
            print(f"  Appended call metadata to already-logged lead {address}")
        except Exception as e:
            print(f"  Could not append call metadata for {address}: {e}")
        return {"received": True, "logged_by_agent": True}

    # The agent did NOT log this call. Record it ourselves.
    auto_note = (
        f"[AUTO-LOGGED by end-of-call webhook — the agent did not call "
        f"log_call_outcome. vapi_call_id: {call_id}] Date: {today}. "
        f"Duration: {duration}s. Ended: {ended_reason}. "
        f"Vapi summary: {ai_summary or 'none available'}"
    )
    fallback = {
        "last_call_date": today,
        "call_transcript_summary": append_notes(existing_notes, auto_note),
    }

    # Status is deliberately conservative: only ever move "New" ->
    # "Contacted", meaning "a human was reached, outcome unknown". Guessing
    # a richer status from a duration and an AI summary risks writing
    # something wrong into the record that drives future dialling. A human
    # reading "Contacted + auto-logged" can tell what happened; a wrongly
    # inferred "Rejected" quietly kills a live lead, and a missed "Opt Out"
    # leaves someone who asked to be removed looking dialable.
    if fields.get("status") in ("New", None):
        fallback["status"] = "Contacted"

    try:
        upsert_lead(address, fallback)
        print(f"  AUTO-LOGGED unlogged call for {address} ({duration}s, {ended_reason})")
    except Exception as e:
        print(f"  FAILED to auto-log call for {address}: {e}")
        return {"received": True, "error": str(e)[:200]}

    return {"received": True, "logged_by_agent": False, "auto_logged": True}
