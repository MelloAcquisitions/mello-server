"""Render Cron Job: runs once at 8:00 AM. Schedule in Render: 0 8 * * *"""

import os

import requests

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")
VAPI_ASSISTANT_ID = os.environ.get("VAPI_ASSISTANT_ID")
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
PROPOSED_UPDATES_TABLE = os.environ.get("PROPOSED_UPDATES_TABLE", "Proposed Updates")
PROPOSED_UPDATES_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PROPOSED_UPDATES_TABLE}"


def get_approved_proposal():
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    params = {"filterByFormula": "{status}='Approved'"}
    response = requests.get(PROPOSED_UPDATES_URL, headers=headers, params=params, timeout=15)
    response.raise_for_status()
    records = response.json().get("records", [])
    return records[0] if records else None


def apply_prompt_update(proposed_change: str):
    """
    Fetches the FULL current assistant config first, then sends the FULL
    messages array back with the change appended — NEVER a partial PATCH.
    Vapi has a documented bug where partial updates can silently wipe the
    system prompt if the full messages object isn't included in the
    request, even when you only meant to change something else.
    """
    headers = {"Authorization": f"Bearer {VAPI_API_KEY}", "Content-Type": "application/json"}

    get_response = requests.get(f"https://api.vapi.ai/assistant/{VAPI_ASSISTANT_ID}", headers=headers, timeout=15)
    get_response.raise_for_status()
    assistant = get_response.json()

    current_messages = assistant.get("model", {}).get("messages", [])
    if not current_messages:
        raise RuntimeError("Could not find current system prompt on the assistant — aborting to avoid wiping it")

    # Append the approved change to the existing system prompt text
    current_messages[0]["content"] = current_messages[0]["content"] + "\n\n" + proposed_change

    full_model_object = assistant["model"]
    full_model_object["messages"] = current_messages

    patch_response = requests.patch(
        f"https://api.vapi.ai/assistant/{VAPI_ASSISTANT_ID}",
        headers=headers,
        json={"model": full_model_object},  # sending the FULL model object, not just the diff
        timeout=15,
    )
    patch_response.raise_for_status()


def mark_applied(record_id: str):
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}
    requests.patch(
        f"{PROPOSED_UPDATES_URL}/{record_id}",
        headers=headers,
        json={"fields": {"status": "Applied"}},
        timeout=15,
    )


if __name__ == "__main__":
    print("Checking for approved prompt updates...")

    proposal = get_approved_proposal()
    if not proposal:
        print("  Nothing approved — no changes applied today.")
    else:
        change_text = proposal["fields"].get("proposed_change", "")
        print(f"  Found approved change: {change_text[:80]}...")
        try:
            apply_prompt_update(change_text)
            mark_applied(proposal["id"])
            print("  Applied successfully and marked as Applied.")
        except Exception as e:
            print(f"  FAILED to apply update: {e} — proposal remains Approved for manual review.")
