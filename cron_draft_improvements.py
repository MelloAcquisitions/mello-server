"""
Render Cron Job: 10:00 PM Monterrey. Schedule (UTC): 0 4 * * *

Reads today's real call transcripts, pairs each with the ACTUAL outcome
recorded in Airtable, and asks Claude for ONE small, specific, reviewable
change to the voice agent's system prompt. Small diffs are easier to approve
confidently than a wall of new text.

REQUIRED ENV ON THIS JOB:
  VAPI_API_KEY, VAPI_ASSISTANT_ID, ANTHROPIC_API_KEY,
  AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME
REQUIRED AIRTABLE TABLE: "Proposed Updates" with columns
  date, reasoning, proposed_change, status.

CHANGES IN THIS CLEANUP
-----------------------
1. "TODAY" WAS THE WRONG FOUR HOURS. `datetime.now().replace(hour=0,...)`
   on Render is midnight UTC, which is 6:00 PM Monterrey THE PREVIOUS DAY.
   This cron fires at 10 PM Monterrey (04:00 UTC), so it was analyzing
   roughly 6 PM to 10 PM and silently ignoring the entire 8 AM - 6 PM
   calling day — the great majority of every day's calls. Now uses
   mello_time.start_of_today_utc_iso().
2. OUTCOMES WERE ALWAYS "Unknown". get_real_outcome() did an exact-string
   Airtable match on the phone number, but Vapi reports E.164 and Airtable
   stores whatever BatchData returned. It therefore almost never matched,
   so every transcript was labelled "Unknown" and the ground truth this
   whole script is built around was never actually supplied. Now uses
   find_lead_by_phone().
3. `calls = response.json()` assumed a bare list; Vapi also returns a
   {"results": [...]} envelope, in which case iterating yields strings and
   the next .get() raises AttributeError. Now goes through vapi_client.
4. The model id is now an env var (ANTHROPIC_MODEL). The hardcoded
   "claude-sonnet-5" is not a valid API model id and would 404 every run.
   Set it to a real id from https://docs.claude.com/en/docs/about-claude/models.
"""

import os
import sys

import requests
from anthropic import Anthropic

from airtable_helpers import find_lead_by_phone, require_config
from mello_time import start_of_today_utc_iso, today_iso
from vapi_client import customer_number, list_calls

AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
PROPOSED_UPDATES_TABLE = os.environ.get("PROPOSED_UPDATES_TABLE", "Proposed Updates")
PROPOSED_UPDATES_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PROPOSED_UPDATES_TABLE}"

# Was hardcoded to "claude-sonnet-5", which is not a valid API model id.
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

MAX_CALLS_ANALYZED = 10


def has_unresolved_proposal() -> bool:
    """
    True if a Pending or Approved-but-not-yet-applied proposal already
    exists — prevents piling up a new draft every night before you have
    reviewed yesterday's.

    TO DENY A PROPOSAL without blocking future ones: set its status to
    anything other than "Pending" or "Approved" (e.g. "Rejected"). If the
    status column is a single-select, add "Rejected" as an option; if it is
    plain text this already works.

    KNOWN DEADLOCK: an Approved proposal that cron_apply_updates.py keeps
    failing to apply stays Approved forever, and blocks every future draft
    silently. That cron now logs loudly when it fails; if drafts stop
    appearing, check the Proposed Updates table for a stuck Approved row.
    """
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    params = {"filterByFormula": "OR({status}='Pending', {status}='Approved')"}
    response = requests.get(PROPOSED_UPDATES_URL, headers=headers, params=params, timeout=15)
    response.raise_for_status()
    return len(response.json().get("records", [])) > 0


def get_todays_transcripts() -> list:
    """Today's calls (business timezone) that produced a transcript, each
    with the dialed number so the real outcome can be looked up."""
    calls = list_calls(created_at_ge=start_of_today_utc_iso())
    return [
        {"phone": customer_number(c), "transcript": c.get("transcript", "")}
        for c in calls if c.get("transcript")
    ]


def get_real_outcome(phone: str) -> str:
    """
    The ACTUAL outcome Airtable recorded for this number — the ground truth
    the model needs. Without it, it is just reading conversation text with
    no idea whether the call worked.
    """
    if not phone:
        return "Unknown (no phone number on call record)"
    try:
        record = find_lead_by_phone(phone)
        if record:
            return record["fields"].get("status", "Unknown")
    except Exception as e:
        print(f"  Could not look up outcome for {phone}: {e}")
    return "Unknown"


def draft_proposal(calls_with_outcomes: list) -> dict:
    client = Anthropic()  # reads ANTHROPIC_API_KEY from the environment

    combined = "\n\n---CALL---\n\n".join(
        f"OUTCOME: {c['outcome']}\nTRANSCRIPT:\n{c['transcript']}"
        for c in calls_with_outcomes[:MAX_CALLS_ANALYZED]
    )
    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": (
                "Below are today's real sales call transcripts for an AI real estate "
                "acquisitions agent, each labeled with its ACTUAL outcome (Agreed, "
                "Rejected, Opt Out, etc.) from the CRM. Use the outcome to judge which "
                "patterns actually worked versus which led to a bad result — don't just "
                "react to how a call sounded. Review them for ONE specific, recurring "
                "pattern worth fixing — an objection handled poorly, a phrase that "
                "caused friction, a moment the agent should have said something "
                "different. Propose ONE small, specific addition or edit to the agent's "
                "system prompt that would fix it. Do not rewrite the whole prompt — just "
                "the one change. Format your response as:\n\n"
                "REASONING: [why this change, referencing the real outcomes]\n"
                "PROPOSED CHANGE: [the exact text to add/change]\n\n"
                f"CALLS:\n{combined}"
            ),
        }],
    )
    text = message.content[0].text
    if "PROPOSED CHANGE:" in text:
        reasoning = text.split("PROPOSED CHANGE:")[0].replace("REASONING:", "").strip()
        proposed_change = text.split("PROPOSED CHANGE:")[-1].strip()
    else:
        reasoning = "(Model did not use the requested format — full response saved below.)"
        proposed_change = text.strip()
    return {"reasoning": reasoning, "proposed_change": proposed_change}


def save_proposal(proposal: dict):
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}
    payload = {"fields": {
        "date": today_iso(),
        "reasoning": proposal["reasoning"],
        "proposed_change": proposal["proposed_change"],
        "status": "Pending",
    }}
    response = requests.post(PROPOSED_UPDATES_URL, headers=headers, json=payload, timeout=15)
    response.raise_for_status()


def main():
    require_config("VAPI_API_KEY", "VAPI_ASSISTANT_ID", "ANTHROPIC_API_KEY")

    print("Analyzing today's calls for improvement ideas...")

    if has_unresolved_proposal():
        print("  An unresolved proposal already exists — skipping tonight's draft "
              "until you approve or reject the pending one.")
        return

    calls = get_todays_transcripts()
    if not calls:
        print("  No transcripts found for today — nothing to analyze.")
        return

    print(f"  Looking up real outcomes for {len(calls)} call(s)...")
    for call in calls:
        call["outcome"] = get_real_outcome(call["phone"])

    known = sum(1 for c in calls if not str(c["outcome"]).startswith("Unknown"))
    print(f"  {known}/{len(calls)} call(s) matched to a real Airtable outcome")
    if known == 0:
        print("  WARNING: no call matched a lead. The proposal would be based on "
              "transcripts with no ground truth, which is what this script exists "
              "to avoid. Skipping — check that leads have phone numbers saved.")
        return

    proposal = draft_proposal(calls)
    save_proposal(proposal)
    print(f"  Proposal saved: {proposal['reasoning'][:120]}...")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)
