"""
Render Cron Job: 8:00 AM Monterrey. Schedule (UTC): 0 14 * * *

Applies an APPROVED prompt change from the "Proposed Updates" table to the
live Vapi assistant.

REQUIRED ENV ON THIS JOB:
  VAPI_API_KEY, VAPI_ASSISTANT_ID,
  AIRTABLE_API_KEY, AIRTABLE_BASE_ID
REQUIRED AIRTABLE COLUMNS on "Proposed Updates":
  status, proposed_change, and (new) previous_prompt.

CHANGES IN THIS CLEANUP
-----------------------
1. THE PREVIOUS PROMPT IS NOW SNAPSHOT BEFORE PATCHING. This job edits the
   thing that decides what an AI says to real sellers about real money, and
   there was no undo — no copy of what it looked like before, no way to
   revert a change that turned out badly except retyping it from memory.
   The full prior prompt is now written to the proposal record first; if the
   snapshot write fails, the patch does not happen.
2. IT NO LONGER ASSUMES messages[0] IS THE SYSTEM PROMPT. It appended to
   whatever was at index 0. If Vapi ever returns the messages in a different
   order, or a non-system message is added, the change would have been
   appended to the wrong message — silently, with no error.
3. PROMPT GROWTH IS NOW BOUNDED. Every approved change was appended forever
   with no ceiling, no dedupe and no review of the merged result. Over
   months that means a prompt that grows without limit, costs more per turn,
   responds more slowly, and eventually contains rules that contradict each
   other. It now refuses to apply past MAX_PROMPT_CHARS and tells you to
   consolidate by hand.
4. mark_applied() now checks its response. It ignored the result, so a
   failed status write left the proposal Approved — which permanently blocks
   every future nightly draft (see cron_draft_improvements).
"""

import os
import sys

import requests

from airtable_helpers import require_config

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")
VAPI_ASSISTANT_ID = os.environ.get("VAPI_ASSISTANT_ID")
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
PROPOSED_UPDATES_TABLE = os.environ.get("PROPOSED_UPDATES_TABLE", "Proposed Updates")
PROPOSED_UPDATES_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PROPOSED_UPDATES_TABLE}"

# A voice-agent system prompt past roughly this size starts costing real
# per-turn latency and inviting internal contradictions. v13 is ~11k chars.
MAX_PROMPT_CHARS = int(os.environ.get("MAX_PROMPT_CHARS", 25000))


def _airtable_headers():
    return {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}


def _vapi_headers():
    return {"Authorization": f"Bearer {VAPI_API_KEY}", "Content-Type": "application/json"}


def get_approved_proposal():
    params = {"filterByFormula": "{status}='Approved'"}
    response = requests.get(PROPOSED_UPDATES_URL, headers=_airtable_headers(),
                            params=params, timeout=15)
    response.raise_for_status()
    records = response.json().get("records", [])
    return records[0] if records else None


def _find_system_message(messages: list) -> int:
    """
    Index of the system message, by ROLE — not by position.

    The old code used messages[0] unconditionally. Vapi does not guarantee
    ordering, and appending a behavioural rule to, say, an assistant
    priming message instead of the system prompt would apply it subtly
    wrongly with no error anywhere.
    """
    for i, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "system":
            return i
    return -1


def snapshot_previous_prompt(record_id: str, prompt_text: str) -> None:
    """
    Writes the pre-change prompt onto the proposal record, so there is
    always a copy of exactly what the assistant said before this change.
    Raises on failure — no snapshot, no patch.
    """
    response = requests.patch(
        f"{PROPOSED_UPDATES_URL}/{record_id}",
        headers=_airtable_headers(),
        json={"fields": {"previous_prompt": prompt_text[:99000]}},
        timeout=15,
    )
    if not response.ok:
        raise RuntimeError(
            f"Could not snapshot the current prompt ({response.status_code}: "
            f"{response.text[:200]}). Not applying the change — without a snapshot "
            f"there is no way back. Add a `previous_prompt` (Long text) column to "
            f"the {PROPOSED_UPDATES_TABLE} table if it does not exist."
        )


def apply_prompt_update(proposed_change: str, record_id: str):
    """
    Fetches the FULL current assistant config, appends the change to the
    SYSTEM message, and sends the FULL model object back — never a partial
    PATCH. Vapi has a documented bug where partial updates can silently wipe
    the system prompt if the full messages object is not included.
    """
    get_response = requests.get(
        f"https://api.vapi.ai/assistant/{VAPI_ASSISTANT_ID}",
        headers=_vapi_headers(), timeout=15,
    )
    get_response.raise_for_status()
    assistant = get_response.json()

    model_object = assistant.get("model") or {}
    messages = model_object.get("messages") or []
    index = _find_system_message(messages)
    if index == -1:
        raise RuntimeError(
            "No message with role='system' on this assistant — aborting rather "
            "than guessing which message is the prompt and risking wiping it."
        )

    current_prompt = messages[index].get("content") or ""
    if not current_prompt.strip():
        raise RuntimeError("The system prompt came back empty — aborting to avoid wiping it.")

    snapshot_previous_prompt(record_id, current_prompt)

    new_prompt = current_prompt + "\n\n" + proposed_change
    if len(new_prompt) > MAX_PROMPT_CHARS:
        raise RuntimeError(
            f"Applying this would take the system prompt to {len(new_prompt)} "
            f"characters, past the {MAX_PROMPT_CHARS} limit. Nightly additions have "
            f"accumulated — consolidate the prompt by hand (rewrite it as a clean "
            f"version incorporating what has stuck), paste it into Vapi, then "
            f"re-approve this proposal."
        )

    messages[index]["content"] = new_prompt
    model_object["messages"] = messages

    patch_response = requests.patch(
        f"https://api.vapi.ai/assistant/{VAPI_ASSISTANT_ID}",
        headers=_vapi_headers(),
        json={"model": model_object},  # the FULL model object, not a diff
        timeout=15,
    )
    patch_response.raise_for_status()
    print(f"  Prompt is now {len(new_prompt)} characters "
          f"(was {len(current_prompt)}); previous version saved on the proposal record.")


def mark_applied(record_id: str):
    response = requests.patch(
        f"{PROPOSED_UPDATES_URL}/{record_id}",
        headers=_airtable_headers(),
        json={"fields": {"status": "Applied"}},
        timeout=15,
    )
    if not response.ok:
        # This matters more than it looks: a proposal stuck on "Approved"
        # permanently blocks every future nightly draft, silently.
        raise RuntimeError(
            f"Prompt WAS updated, but marking the proposal Applied failed "
            f"({response.status_code}: {response.text[:200]}). Set its status to "
            f"'Applied' by hand — while it sits on 'Approved' no new improvement "
            f"drafts will be generated, AND it may be re-applied tomorrow."
        )


def main():
    require_config("VAPI_API_KEY", "VAPI_ASSISTANT_ID")

    print("Checking for approved prompt updates...")

    proposal = get_approved_proposal()
    if not proposal:
        print("  Nothing approved — no changes applied today.")
        return

    change_text = (proposal["fields"].get("proposed_change") or "").strip()
    if not change_text:
        print("  Approved proposal has an empty proposed_change — skipping. "
              "Set its status to 'Rejected' so it stops blocking new drafts.")
        return

    print(f"  Found approved change: {change_text[:100]}...")
    try:
        apply_prompt_update(change_text, proposal["id"])
        mark_applied(proposal["id"])
        print("  Applied successfully and marked as Applied.")
    except Exception as e:
        print(f"  FAILED to apply update: {e}")
        print("  The proposal remains Approved. NOTE: while it does, "
              "cron_draft_improvements.py will not generate new drafts.")
        sys.exit(1)


if __name__ == "__main__":
    main()
