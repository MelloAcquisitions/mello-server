"""
bump_max_tokens.py — sets maxTokens: 500 on all three Vapi tools.

Why: Vapi defaults maxTokens to 100 (~75 words) per tool call. A detailed
call_transcript_summary or notes field can get truncated mid-argument,
producing invalid JSON the model can't recover from mid-call. This raises
the ceiling so a real, detailed summary doesn't get cut off.

RUN:
    export VAPI_API_KEY=...
    python bump_max_tokens.py            # dry run — shows what it will do
    python bump_max_tokens.py --apply    # actually applies it
"""

import os
import sys

import requests

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")
APPLY = "--apply" in sys.argv

TOOL_IDS = {
    "calculate_mao": "a6589cb2-6d0d-456c-8d20-5ef047ba0d2a",
    "log_call_outcome": "788ec1d5-2349-4312-98f7-07971cf52775",
    "flag_for_human_review": "f0311622-ee1a-4230-92c4-b5644fe41e1a",
}

if not VAPI_API_KEY:
    sys.exit("Set VAPI_API_KEY first.")

headers = {"Authorization": f"Bearer {VAPI_API_KEY}", "Content-Type": "application/json"}

for name, tid in TOOL_IDS.items():
    print(f"\n=== {name} ({tid}) ===")

    current = requests.get(f"https://api.vapi.ai/tool/{tid}", headers=headers, timeout=20)
    if not current.ok:
        print(f"  Could not fetch current config: {current.status_code} {current.text[:200]}")
        continue
    before = (current.json().get("function") or {}).get("maxTokens")
    print(f"  current maxTokens: {before}")

    if not APPLY:
        print("  (dry run — pass --apply to send)")
        continue

    r = requests.patch(
        f"https://api.vapi.ai/tool/{tid}",
        headers=headers,
        json={"function": {"maxTokens": 500}},
        timeout=30,
    )
    if r.ok:
        after = (r.json().get("function") or {}).get("maxTokens")
        print(f"  PATCHED ok — maxTokens now: {after}")
    else:
        print(f"  FAILED {r.status_code}: {r.text[:400]}")

if not APPLY:
    print("\nNothing changed. Re-run with --apply to actually update the tools.")
