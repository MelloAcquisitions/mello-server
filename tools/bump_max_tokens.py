"""
bump_max_tokens.py — sets maxTokens: 500 on the three custom Vapi tools.

Why: Vapi defaults maxTokens to 100 (~75 words) per tool call. A detailed
call_transcript_summary gets truncated mid-argument, producing invalid JSON
the model cannot recover from mid-call — so the call outcome is lost for a
reason nothing logs.

TOOL IDS ARE HARDCODED BELOW. If a tool is ever recreated in Vapi it gets a
new id and this silently updates nothing. Run tools/dump_tools.py first and
confirm the ids match.

RUN:
    export VAPI_API_KEY=...
    python tools/bump_max_tokens.py            # dry run
    python tools/bump_max_tokens.py --apply
"""

import os
import sys

import requests

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")
APPLY = "--apply" in sys.argv
TARGET_MAX_TOKENS = 500

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
        print("  If this is a 404, the tool was recreated and this id is stale — "
              "run tools/dump_tools.py to get the real ids.")
        continue

    body = current.json()
    before = (body.get("function") or {}).get("maxTokens")
    print(f"  name in Vapi: {body.get('name')}   type: {body.get('type')}")
    print(f"  current maxTokens: {before}")

    if before == TARGET_MAX_TOKENS:
        print("  already correct — nothing to do")
        continue

    if not APPLY:
        print(f"  would set maxTokens -> {TARGET_MAX_TOKENS}  (dry run; pass --apply)")
        continue

    r = requests.patch(
        f"https://api.vapi.ai/tool/{tid}", headers=headers,
        json={"function": {"maxTokens": TARGET_MAX_TOKENS}}, timeout=30,
    )
    if r.ok:
        print(f"  PATCHED — maxTokens now: {(r.json().get('function') or {}).get('maxTokens')}")
    else:
        print(f"  FAILED {r.status_code}: {r.text[:400]}")

if not APPLY:
    print("\nNothing changed. Re-run with --apply to actually update the tools.")
