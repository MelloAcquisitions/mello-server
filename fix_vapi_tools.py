"""
fix_vapi_tools.py — applies the Vapi-side configuration in one shot.

Does four things to all three tools:
  1. server.url  -> https://<your-render>/vapi/tools   (the actual fix)
  2. async       -> false                              (stop fake successes)
  3. timeoutSeconds -> 20, maxTokens -> 500
  4. log_call_outcome gets next_contact_date + the full status list
     (open issue #3 in your handoff doc, written but never executed)

RUN:
    export VAPI_API_KEY=...
    python fix_vapi_tools.py            # dry run, prints the payloads
    python fix_vapi_tools.py --apply    # actually PATCHes
    python fix_vapi_tools.py --apply --with-backoff   # also try backoffPlan

Deploy vapi_tools_router.py FIRST. If you repoint the tools before the
endpoint exists, every tool call 404s.
"""

import json
import os
import sys

import requests

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")
RENDER_BASE_URL = os.environ.get("RENDER_BASE_URL", "https://mello-server.onrender.com").rstrip("/")
TOOL_URL = f"{RENDER_BASE_URL}/vapi/tools"

APPLY = "--apply" in sys.argv
WITH_BACKOFF = "--with-backoff" in sys.argv

TOOL_IDS = {
    "calculate_mao": "a6589cb2-6d0d-456c-8d20-5ef047ba0d2a",
    "log_call_outcome": "788ec1d5-2349-4312-98f7-07971cf52775",
    "flag_for_human_review": "f0311622-ee1a-4230-92c4-b5644fe41e1a",
}

STATUS_DESCRIPTION = (
    "The outcome of this call. Must be exactly one of: "
    "Contacted (they answered but the call went nowhere useful); "
    "Qualified (real conversation and real information, but no number was discussed); "
    "Offer Made (a real number was discussed and not rejected); "
    "Agreed (a price was agreed - use flag_for_human_review instead); "
    "Rejected (firmly not interested, weak reason to sell); "
    "Opt Out (asked to be removed, permanent); "
    "Human Call (they asked to speak to a person); "
    "Priority Follow-up (number is far off but they have a strong, specific reason to sell). "
    "Never use New, Exhausted or Closed - the system sets those."
)

FUNCTION_SCHEMAS = {
    "log_call_outcome": {
        "name": "log_call_outcome",
        "description": ("Records the outcome of this call. Call this on EVERY call before "
                        "hanging up, including opt-outs and rejections."),
        "parameters": {
            "type": "object",
            "properties": {
                "address": {"type": "string",
                            "description": "The property address, exactly as given to you."},
                "status": {"type": "string", "description": STATUS_DESCRIPTION},
                "notes": {"type": "string",
                          "description": ("What actually happened, in the seller's own words "
                                          "where possible. Include their reason to sell and "
                                          "what a human should do next.")},
                "offer_amount": {"type": "number",
                                 "description": "The dollar figure discussed, if any."},
                "arv": {"type": "number", "description": "The ARV you worked from."},
                "repair_estimate": {"type": "number",
                                    "description": "Your repair estimate in dollars."},
                "mao_floor": {"type": "number",
                              "description": "The MAO ceiling returned by calculate_mao."},
                "email": {"type": "string",
                          "description": "The seller's email address if they gave one."},
                "next_contact_date": {
                    "type": "string",
                    "description": ("ISO date (YYYY-MM-DD) for a scheduled callback, only "
                                    "when the seller gave a real future timeframe. Schedule "
                                    "EARLIER than they said - roughly two thirds of their "
                                    "stated timeframe. Omit entirely if no timeframe was given."),
                },
            },
            "required": ["address", "status", "notes"],
        },
        "strict": False,
        "maxTokens": 500,
    },
}


def build_patch(name: str) -> dict:
    server = {"url": TOOL_URL, "timeoutSeconds": 20}
    if WITH_BACKOFF and name != "flag_for_human_review":
        # Deliberately NOT on flag_for_human_review: a retry after a request
        # that actually succeeded server-side would email you a second contract.
        server["backoffPlan"] = {"type": "fixed", "maxRetries": 2, "baseDelaySeconds": 1}

    patch = {"async": False, "server": server}
    if name in FUNCTION_SCHEMAS:
        patch["function"] = FUNCTION_SCHEMAS[name]
    return patch


def main():
    if not VAPI_API_KEY:
        sys.exit("Set VAPI_API_KEY first.")

    headers = {"Authorization": f"Bearer {VAPI_API_KEY}",
               "Content-Type": "application/json"}

    for name, tid in TOOL_IDS.items():
        patch = build_patch(name)
        print(f"\n=== {name} ({tid}) ===")
        print(json.dumps(patch, indent=2))

        if not APPLY:
            print("  (dry run — pass --apply to send)")
            continue

        r = requests.patch(f"https://api.vapi.ai/tool/{tid}",
                           headers=headers, json=patch, timeout=30)
        if r.ok:
            print(f"  PATCHED ok ({r.status_code})")
        else:
            print(f"  FAILED {r.status_code}: {r.text[:400]}")
            if WITH_BACKOFF and "backoffPlan" in r.text:
                print("  -> backoffPlan was rejected. Re-run without --with-backoff; "
                      "the core fix does not depend on it.")

    if APPLY:
        print("\nDone. Re-run check_vapi_config.py to confirm, then preflight_check.py.")


if __name__ == "__main__":
    main()
