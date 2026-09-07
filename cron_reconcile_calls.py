"""
Render Cron Job: run ~30 min after calling stops. Suggested UTC: 0 2 * * *
(8:00 PM Monterrey). Safe to run more often — it is idempotent.

WHY THIS EXISTS — the guarantee layer.

Three things record a call outcome, in descending order of data quality and
ASCENDING order of reliability:

  1. log_call_outcome (in-call tool) — richest data, LEAST reliable. The
     model has to choose to call it. If the seller hangs up, if the agent
     ends the call for abuse, if the tool 422s, if the model simply
     doesn't — nothing is written and you never find out.

  2. /vapi_call_ended (webhook) — fires from Vapi's side regardless of what
     the model did. Better. But it is a PUSH: if Vapi's webhook drops (a
     known intermittent bug), if the service is cold-starting, if a deploy
     is mid-flight — the message is gone and there is no retry. You cannot
     detect what you were never sent.

  3. THIS SCRIPT (poll) — asks Vapi directly for every call it placed and
     checks each against Airtable. A pull cannot be missed the way a push
     can. If layers 1 and 2 both failed, this still catches it, because
     nothing has to be delivered — we go and look.

The practical payoff: opt-outs and rejections cannot silently vanish and
leave a lead to be dialled again in three days. That is a compliance
problem, not just a data-quality one.

REQUIRED ENV ON THIS JOB:
  VAPI_API_KEY, VAPI_ASSISTANT_ID,
  AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME

CHANGES IN THIS CLEANUP
-----------------------
1. THE SHORT-CALL FILTER WAS AN `or` WHERE THE COMMENT SAID `and`. The code
   read `if never_connected or (duration < 8)`, so EVERY call under 8
   seconds was discarded regardless of why it ended — including a genuine
   "take me off your list" followed by a hangup, which is the single most
   important call in the system to record. Now a short call is only skipped
   when the ended reason ALSO says nobody was there.
2. PAGINATION. This fetched limit=100 and stopped. At MAX_CALLS_PER_DAY=80
   plus retries, a busy 24h window can exceed 100 calls, and the guarantee
   layer would silently miss the oldest ones. Now paginated (vapi_client).
3. Dates compared in Monterrey time, matching what the writers now stamp.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from airtable_helpers import (
    append_notes, find_lead_by_phone, require_config, upsert_lead,
)
from mello_time import BUSINESS_TIMEZONE, today_local
from vapi_client import customer_number, list_calls

LOOKBACK_HOURS = int(os.environ.get("RECONCILE_LOOKBACK_HOURS", 24))

# Ended reasons meaning nobody ever picked up. Matched as SUBSTRINGS, not
# exact values: Vapi prefixes some reasons with a phase — the real string
# observed in testing was
# "call.in-progress.error-assistant-did-not-receive-customer-audio", not
# the bare form. An equality check silently misses those and backfills a
# call nobody answered.
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

# Only applied ALONGSIDE a not-connected reason. Deliberately low, because a
# genuine opt-out can be over in 12 seconds.
MIN_CONNECTED_SECONDS = 8


def get_recent_calls() -> list:
    """
    Pulls this assistant's recent calls from Vapi.

    Deliberately looks back 24h rather than "since midnight": a call placed
    at 7:58 PM that ends at 8:01 PM would fall outside a naive same-day
    window, and timezone drift between Render (UTC) and Monterrey has caused
    date-boundary bugs elsewhere in this project. Overlapping windows are
    harmless because this script is idempotent.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).isoformat()
    return list_calls(created_at_ge=since)


def call_date(call: dict) -> str:
    """
    The BUSINESS-LOCAL date this call happened, as an ISO string, for
    comparison against Airtable's last_call_date stamp — which is now also
    written in business-local time.
    """
    started = call.get("startedAt") or call.get("createdAt")
    if not started:
        return ""
    try:
        utc_dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        return utc_dt.astimezone(ZoneInfo(BUSINESS_TIMEZONE)).date().isoformat()
    except (ValueError, TypeError):
        return ""


def call_duration(call: dict):
    """Duration in seconds, computed from timestamps when Vapi does not
    provide it directly."""
    started, ended = call.get("startedAt"), call.get("endedAt")
    if not (started and ended):
        return None
    try:
        s = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        e = datetime.fromisoformat(str(ended).replace("Z", "+00:00"))
        return int((e - s).total_seconds())
    except (ValueError, TypeError):
        return None


def reconcile(call: dict) -> str:
    """
    Returns one of: 'no_phone', 'no_lead', 'not_connected', 'already_logged',
    'backfilled', 'error' — so the summary is honest about what happened
    rather than just claiming success.
    """
    call_id = call.get("id")
    phone = customer_number(call)
    if not phone:
        return "no_phone"

    ended_reason = call.get("endedReason") or "unknown"
    duration = call_duration(call)
    reason_lower = str(ended_reason).lower()
    never_connected = any(frag in reason_lower for frag in NOT_CONNECTED_FRAGMENTS)

    # AND, not OR. A short call is only dismissed when the ended reason also
    # says nobody was there. A 6-second "stop calling me" *click* is now
    # recorded; previously it was thrown away by the layer whose entire
    # purpose is to make sure opt-outs cannot vanish.
    if never_connected and (duration is None or duration < MIN_CONNECTED_SECONDS):
        return "not_connected"
    # A not-connected reason on a call that somehow ran long is still not a
    # conversation — but log it loudly rather than silently, because it means
    # the reason strings need revisiting.
    if never_connected:
        print(f"  NOTE: call {call_id} has a not-connected reason ({ended_reason}) "
              f"but ran {duration}s — treating as connected and recording it.")

    record = find_lead_by_phone(phone)
    if not record:
        return "no_lead"

    fields = record["fields"]
    address = fields.get("address")
    if not address:
        print(f"  Lead {record.get('id')} matched {phone} but has no address — cannot write.")
        return "error"

    when = call_date(call)
    existing_notes = fields.get("call_transcript_summary") or ""

    # Dedupe on the VAPI CALL ID, not on the date. A date check breaks the
    # moment a lead is dialled twice in one day: the first call gets
    # backfilled, stamps the date, and the second then looks already-handled
    # and is silently dropped. The call id in the note text makes each call
    # individually identifiable, needs no new Airtable column, and survives
    # reruns of this cron.
    if call_id and call_id in existing_notes:
        return "already_logged"

    # Weaker fallback for calls logged before call ids were recorded. Runs
    # second because it cannot tell two same-day calls apart.
    if when and fields.get("last_call_date") == when \
            and "[RECONCILED" not in existing_notes and "[AUTO-LOGGED" not in existing_notes:
        return "already_logged"

    summary = (call.get("analysis") or {}).get("summary") or ""
    transcript = call.get("transcript") or ""

    note = (
        f"[RECONCILED by cron — neither the in-call tool nor the end-of-call "
        f"webhook recorded this call. vapi_call_id: {call_id}] Date: {when}. "
        f"Duration: {duration if duration is not None else 'unknown'}s. "
        f"Ended: {ended_reason}. "
    )
    if summary:
        note += f"Summary: {summary}"
    elif transcript:
        # No analysis.summary means no analysis plan is configured on the
        # assistant. Say so explicitly rather than passing a raw transcript
        # excerpt off as though it were a summary.
        note += (f"(No Vapi analysis summary available — enable an analysis plan "
                 f"on the assistant to get real summaries.) Transcript excerpt: "
                 f"{transcript[:800]}")
    else:
        note += "No summary or transcript available."

    update = {
        "last_call_date": when or today_local().isoformat(),
        "call_transcript_summary": append_notes(existing_notes, note),
    }

    # Same conservative status rule as the webhook: only nudge "New" to
    # "Contacted". Never infer Rejected or Opt Out from a transcript — a
    # wrong guess either kills a live lead or, far worse, leaves someone who
    # opted out looking dialable. Anything ambiguous stays for a human.
    #
    # KNOWN, ACCEPTED GAP: this means an opt-out spoken on a call the agent
    # failed to log is NOT automatically suppressed. The note is written and
    # a human can see it, but nothing acts on it. Closing that properly
    # means classifying transcripts (see the audit's "opt-out
    # classification" item) — those carry legal weight and are worth doing
    # deliberately rather than with a keyword match.
    if fields.get("status") in ("New", None):
        update["status"] = "Contacted"

    try:
        upsert_lead(address, update)
        print(f"  BACKFILLED {address} ({phone}) — {duration}s, {ended_reason}")
        return "backfilled"
    except Exception as e:
        print(f"  ERROR backfilling {address}: {e}")
        return "error"


def main():
    require_config("VAPI_API_KEY", "VAPI_ASSISTANT_ID")

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

    if tally.get("no_lead"):
        print(f"\n  NOTE: {tally['no_lead']} call(s) had no matching Airtable lead. "
              f"Usually this means the lead has no phone number saved, or it is "
              f"saved in a format phone_variants() does not generate.")

    if tally.get("backfilled"):
        print(f"\n  NOTE: {tally['backfilled']} call(s) were missed by BOTH the in-call "
              f"tool and the end-of-call webhook. Check the Render logs for those "
              f"calls — a recurring pattern means something upstream is broken.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)
