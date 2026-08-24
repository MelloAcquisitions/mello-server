"""Render Cron Job: runs once at 10:00 PM. Schedule in Render: 0 22 * * *"""

import os
from datetime import datetime, timedelta

import requests
from anthropic import Anthropic

from airtable_helpers import query_leads  # reused pattern, but targets a new table below

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")
VAPI_ASSISTANT_ID = os.environ.get("VAPI_ASSISTANT_ID")
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
PROPOSED_UPDATES_TABLE = os.environ.get("PROPOSED_UPDATES_TABLE", "Proposed Updates")
PROPOSED_UPDATES_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PROPOSED_UPDATES_TABLE}"


def has_unresolved_proposal() -> bool:
    """True if there's already a Pending or Approved-but-not-yet-applied
    proposal sitting there — prevents piling up a new draft every night if
    you haven't gotten to reviewing yesterday's yet."""
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    params = {"filterByFormula": "OR({status}='Pending', {status}='Approved')"}
    response = requests.get(PROPOSED_UPDATES_URL, headers=headers, params=params, timeout=15)
    response.raise_for_status()
    return len(response.json().get("records", [])) > 0


def get_todays_transcripts() -> list:
    """
    Pulls today's call transcripts from Vapi. NOTE: I haven't confirmed the
    exact filter parameter names against a live account — verify this
    against Vapi's current API reference (or test via /docs-equivalent)
    before trusting it fully, same precaution as every other new endpoint
    in this build.
    """
    headers = {"Authorization": f"Bearer {VAPI_API_KEY}"}
    today_start = datetime.now().replace(hour=0, minute=0, second=0).isoformat()
    params = {"assistantId": VAPI_ASSISTANT_ID, "createdAtGe": today_start}
    response = requests.get("https://api.vapi.ai/call", headers=headers, params=params, timeout=30)
    response.raise_for_status()
    calls = response.json()
    return [c.get("transcript", "") for c in calls if c.get("transcript")]


def draft_proposal(transcripts: list) -> dict:
    """Sends today's transcripts to Claude, asking for a specific, small
    proposed change — not a full prompt rewrite. Small, reviewable diffs
    are easier for you to approve confidently than a wall of new text."""
    client = Anthropic()  # reads ANTHROPIC_API_KEY from environment automatically

    combined = "\n\n---CALL---\n\n".join(transcripts[:10])  # cap to avoid huge prompts
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": (
                "Below are today's real sales call transcripts for an AI real estate "
                "acquisitions agent. Review them for ONE specific, recurring pattern "
                "worth fixing — an objection handled poorly, a phrase that caused "
                "friction, a moment the agent should have said something different. "
                "Propose ONE small, specific addition or edit to the agent's system "
                "prompt that would fix it. Do not rewrite the whole prompt — just the "
                "one change. Format your response as:\n\nREASONING: [why this change]\n"
                "PROPOSED CHANGE: [the exact text to add/change]\n\n"
                f"TRANSCRIPTS:\n{combined}"
            ),
        }],
    )
    text = message.content[0].text
    reasoning = text.split("PROPOSED CHANGE:")[0].replace("REASONING:", "").strip()
    proposed_change = text.split("PROPOSED CHANGE:")[-1].strip() if "PROPOSED CHANGE:" in text else text
    return {"reasoning": reasoning, "proposed_change": proposed_change}


def save_proposal(proposal: dict):
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "fields": {
            "date": datetime.now().date().isoformat(),
            "reasoning": proposal["reasoning"],
            "proposed_change": proposal["proposed_change"],
            "status": "Pending",
        }
    }
    response = requests.post(PROPOSED_UPDATES_URL, headers=headers, json=payload, timeout=15)
    response.raise_for_status()


if __name__ == "__main__":
    print("Analyzing today's calls for improvement ideas...")

    if has_unresolved_proposal():
        print("  An unresolved proposal already exists — skipping tonight's draft "
              "until you approve or reject the pending one. No backlog piles up.")
    else:
        transcripts = get_todays_transcripts()
        if not transcripts:
            print("  No transcripts found for today — nothing to analyze.")
        else:
            print(f"  Analyzing {len(transcripts)} call(s)...")
            proposal = draft_proposal(transcripts)
            save_proposal(proposal)
            print(f"  Proposal saved: {proposal['reasoning'][:80]}...")
