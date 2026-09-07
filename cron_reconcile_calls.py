"""
Render Cron Job: run ~30 min after calling stops. Suggested UTC: 0 2 * * *
(8:00 PM Monterrey). Safe to run more often — it's idempotent.

WHY THIS EXISTS — the guarantee layer.

Three things record a call outcome, in descending order of data quality and
ASCENDING order of reliability:

  1. log_call_outcome (in-call tool)     — richest data, LEAST reliable.
     The model has to choose to call it. If the seller hangs up, if the
     agent ends the call for abuse, if the tool 422s, if the model simply
     doesn't — nothing is written and you never find out.

  2. /vapi_call_ended (webhook)          — fires from Vapi's side regardless
     of what the model did. Much better. But it's a PUSH: if Vapi's webhook
     drops (they have a known intermittent bug), if your service is
     cold-starting, if a deploy is mid-flight — the message is gone and
     there is no retry. You can't detect what you were never sent.

  3. THIS SCRIPT (poll)                  — asks Vapi directly for every call
     it placed today and checks each one against Airtable. A pull cannot be
     missed the way a push can. If layers 1 and 2 both failed, this still
     catches it, because nothing has to be delivered — we go and look.

The practical payoff: opt-outs and rejections can never silently vanish and
leave a lead to be dialed again in three days. That's a compliance problem,
not just a data-quality one, which is why the belt-and-braces is worth it.

REQUIRED ENV (set these on THIS cron job — Render crons do not inherit
anything from the web service):
  VAPI_API_KEY, VAPI_ASSISTANT_ID,
  AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME
"""

import os
from datetime import datetime, timedelta, timezone

import requests

from airtable_helpers import find_lead_by_phone, upsert_lead

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")
VAPI_ASSISTANT_ID = os.environ.get("VAPI_ASSISTANT_ID")

LOOKBACK_HOURS = int(os.environ.get("RECONCILE_LOOKBACK_HOURS", 24))


def get_recent_calls() -> list:
    """
    Pulls this assistant's recent calls from Vapi.

    Deliberately looks back 24h rather than 'since midnight': a call placed
    at 7:58 PM that ends at 8:01 PM would fall outside a naive same-day
    window, and timezone drift between Render (UTC) and Monterrey has
    already caused date-boundary bugs elsewhere in this project. Overlapping
    windows are harmless here because the script is idempotent.
    """
    if not VAPI_API_KEY or not VAPI_ASSISTANT_ID:
        raise RuntimeError("VAPI_API_KEY or VAPI_ASSISTANT_ID not set on this cron job")

    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).isoformat()
    headers = {"Authorization": f"Bearer {VAPI_API_KEY}"}
    params = {"assistantId": VAPI_ASSISTANT_ID, "createdAtGe": since, "limit": 100}

    response = requests.get("https://api.vapi.ai/call", headers=headers,
                            params=params, timeout=30)
    response.raise_for_status()
    calls = response.json()

    # Vapi has returned both a bare list and a {"results": [...]} envelope
    # across versions — handle both rather than assuming one.
    if isinstance(calls, dict):
        calls = calls.get("results", calls.get("data", []))
    return calls or []


def call_date(call: dict) -> str:
    """The local date this call happened, as an ISO string, for comparison
    against Airtable's last_call_date stamp."""
    started = call.get("startedAt") or call.get("createdAt")
    if not started:
        return ""
    try:
        return datetime.fromisoformat(started.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def call_duration(call: dict):
    """Duration in seconds, computed from timestamps when Vapi doesn't
    provide it directly."""
    started, ended = call.get("startedAt"), call.get("endedAt")
    if not (started and ended):
        return None
    try:
        s = datetime.fromisoformat(started.replace("Z", "+00:00"))
        e = datetime.fromisoformat(ended.replace("Z", "+00:00"))
        return int((e - s).total_seconds())
    except ValueError:
        return None


def reconcile(call: dict) -> str:
    """
    Returns one of: 'no_phone', 'no_lead', 'not_connected', 'already_logged',
    'backfilled', 'error' — so the summary at the end is honest about what
    actually happened rather than just claiming success.
    """
    call_id = call.get("id")
    phone = (call.get("customer") or {}).get("number")
    if not phone:
        return "no_phone"

    # Calls that never reached a human aren't outcomes worth recording.
    # cron_dispatch_calls.py already incremented #_calls when it dialed, so
    # the retry cadence is correct without this — writing a "call happened"
    # note for a voicemail or a no-answer just adds noise to the record and
    # can make a lead look contacted when nobody ever picked up.
    #
    # Matched as SUBSTRINGS, not exact values: Vapi prefixes some reasons
    # with a phase, e.g. the real string observed in testing was
    # "call.in-progress.error-assistant-did-not-receive-customer-audio",
    # not the bare "assistant-did-not-receive-customer-audio". An equality
    # check silently misses those and backfills a call nobody answered.
    ended_reason = call.get("endedReason", "unknown")
    duration = call_duration(call)
    NOT_CONNECTED_FRAGMENTS = (
        "did-not-answer",
        "customer-busy",
        "no-answer",
        "voicemail",
        "failed-to-connect",
        "did-not-receive-customer-audio",
        "did-not-give-microphone-permission",
        "pipeline-error",
        "twilio-failed",
    )
    reason_lower = ended_reason.lower()
    never_connected = any(frag in reason_lower for frag in NOT_CONNECTED_FRAGMENTS)

    # Duration threshold stays deliberately low. A genuine "take me off your
    # list" can be over in 12 seconds, and that is exactly the call that
    # MUST be recorded — so short calls are only skipped when the ended
    # reason also says nobody was there.
    if never_connected or (duration is not None and duration < 8):
        return "not_connected"

    record = find_lead_by_phone(phone)
    if not record:
        return "no_lead"

    fields = record["fields"]
    address = fields.get("address")
    when = call_date(call)
    existing_notes = fields.get("call_transcript_summary") or ""

    # Dedupe on the VAPI CALL ID, not on the date.
    #
    # An earlier version checked "is last_call_date == today", which breaks
    # the moment a lead is dialed twice in one day: the first call gets
    # backfilled, stamps today's date, and the second call then looks
    # already-handled and is silently dropped. Recording the call id in the
    # note text makes each call individually identifiable, needs no new
    # Airtable column, and survives reruns of this cron.
    if call_id and call_id in existing_notes:
        return "already_logged"

    # If the in-call tool logged this same call today, its note is richer
    # than anything reconstructable here — leave it alone. This is a weaker
    # check than the call-id one above (it can't tell two same-day calls
    # apart) so it runs second, only as a fallback for calls logged before
    # call ids were being recorded.
    if fields.get("last_call_date") == when and "[RECONCILED" not in existing_notes \
            and "[AUTO-LOGGED" not in existing_notes:
        return "already_logged"

    summary = (call.get("analysis") or {}).get("summary") or ""
    transcript = call.get("transcript") or ""

    note = (
        f"\n\n[RECONCILED by cron — neither the in-call tool nor the end-of-call "
        f"webhook recorded this call. vapi_call_id: {call_id}] Date: {when}. "
        f"Duration: {duration if duration is not None else 'unknown'}s. "
        f"Ended: {ended_reason}. "
    )
    if summary:
        note += f"Summary: {summary}"
    elif transcript:
        # No analysis.summary means no analysis plan is configured on the
        # assistant in Vapi. Say so explicitly rather than silently passing
        # off a raw transcript excerpt as though it were a summary.
        note += (f"(No Vapi analysis summary available — enable an analysis plan "
                 f"on the assistant to get real summaries.) Transcript excerpt: "
                 f"{transcript[:800]}")
    else:
        note += "No summary or transcript available."

    # APPEND, never overwrite. The record has one notes field and a lead can
    # be called many times; overwriting destroys the history of every prior
    # call, including any opt-out language a human would need to see.
    update = {
        "last_call_date": when,
        "call_transcript_summary": (existing_notes + note).strip()[:99000],
    }

    # Same conservative status rule as the webhook: only nudge "New" to
    # "Contacted". Never infer Rejected or Opt Out from a transcript here —
    # a wrong guess either kills a live lead or, far worse, leaves someone
    # who opted out looking dialable. Anything ambiguous stays for a human.
    if fields.get("status") in ("New", None):
        update["status"] = "Contacted"

    try:
        upsert_lead(address, update)
        print(f"  BACKFILLED {address} ({phone}) — {duration}s, {ended_reason}")
        return "backfilled"
    except Exception as e:
        print(f"  ERROR backfilling {address}: {e}")
        return "error"


if __name__ == "__main__":
    print(f"Reconciling Vapi calls from the last {LOOKBACK_HOURS}h against Airtable...")

    calls = get_recent_calls()
    print(f"  Vapi reports {len(calls)} call(s) in this window.")

    tally = {}
    for call in calls:
        result = reconcile(call)
        tally[result] = tally.get(result, 0) + 1

    print("\nSummary:")
    print(f"  already logged correctly : {tally.get('already_logged', 0)}")
    print(f"  backfilled by this cron  : {tally.get('backfilled', 0)}")
    print(f"  never connected (skipped): {tally.get('not_connected', 0)}")
    print(f"  no matching lead         : {tally.get('no_lead', 0)}")
    print(f"  no phone on call record  : {tally.get('no_phone', 0)}")
    print(f"  errors                   : {tally.get('error', 0)}")

    if tally.get("backfilled"):
        print(f"\n  NOTE: {tally['backfilled']} call(s) were missed by both the in-call "
              f"tool and the end-of-call webhook. Worth checking the Render logs for "
              f"those calls — a recurring pattern here means something upstream is broken.")
