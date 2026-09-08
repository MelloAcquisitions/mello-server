"""
set_tool_headers.py — adds the X-Mello-Token auth header to all three Vapi
apiRequest tools, without disturbing the headers already on them.

WHY A SCRIPT
------------
main.py's tool endpoints require an X-Mello-Token header once
MELLO_TOOL_SECRET is set on Render. Vapi will not send that header unless
each tool is configured to. Doing it by hand across three tools is easy to
get subtly wrong — a typo, or a header added to two tools and forgotten on
the third, produces a 401 only for the ONE tool you missed, and only during
a live call, which is the worst place to find out.

apiRequest tools store headers as a JSON-Schema-shaped object:

    "headers": {
      "type": "object",
      "properties": {
        "Content-Type":  {"type": "string", "value": "application/json"},
        "X-Mello-Token": {"type": "string", "value": "<the secret>"}
      }
    }

This reads each tool's CURRENT headers, adds or updates only X-Mello-Token,
and writes the whole object back — so Content-Type and anything else you
have set survives untouched.

RUN:
    export VAPI_API_KEY=...
    export MELLO_TOOL_SECRET=...        # must match Render exactly
    python3 tools/set_tool_headers.py            # dry run — shows the diff
    python3 tools/set_tool_headers.py --apply    # sends it

Then verify:
    python3 tools/dump_tools.py | grep -A12 headers
    python3 tools/preflight_check.py

ORDER MATTERS. Add the header here FIRST, then set MELLO_TOOL_SECRET on
Render. An unrecognised header is harmless to a server with no secret set,
so that order has zero downtime. The reverse breaks every call in between.

TOOL IDS ARE HARDCODED. If a tool is recreated in Vapi it gets a new id and
this silently updates nothing — run dump_tools.py and confirm they match.
"""

import copy
import json
import os
import sys

import requests

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")
MELLO_TOOL_SECRET = os.environ.get("MELLO_TOOL_SECRET")
APPLY = "--apply" in sys.argv

HEADER_NAME = "X-Mello-Token"

TOOL_IDS = {
    "calculate_mao": "a6589cb2-6d0d-456c-8d20-5ef047ba0d2a",
    "log_call_outcome": "788ec1d5-2349-4312-98f7-07971cf52775",
    "flag_for_human_review": "f0311622-ee1a-4230-92c4-b5644fe41e1a",
}

if not VAPI_API_KEY:
    sys.exit("Set VAPI_API_KEY first.")
if not MELLO_TOOL_SECRET:
    sys.exit("Set MELLO_TOOL_SECRET first — it must match the value on Render exactly.")
if MELLO_TOOL_SECRET != MELLO_TOOL_SECRET.strip():
    sys.exit("MELLO_TOOL_SECRET has leading/trailing whitespace. Re-export it cleanly.")
if MELLO_TOOL_SECRET[:1] in ("'", '"') or MELLO_TOOL_SECRET[-1:] in ("'", '"'):
    sys.exit("MELLO_TOOL_SECRET is wrapped in quotes — the quotes would become part "
             "of the header value. Re-export it without them.")

HEADERS = {"Authorization": f"Bearer {VAPI_API_KEY}", "Content-Type": "application/json"}

import hashlib  # noqa: E402
print(f"Secret to install: {len(MELLO_TOOL_SECRET)} chars, fingerprint "
      f"{hashlib.sha256(MELLO_TOOL_SECRET.encode()).hexdigest()[:12]}")
print("This must match the fingerprint preflight_check.py reports. If it does "
      "not, one of the two is wrong.\n")

changed, already_ok, failed = 0, 0, 0

for name, tool_id in TOOL_IDS.items():
    print(f"=== {name} ({tool_id}) ===")

    response = requests.get(f"https://api.vapi.ai/tool/{tool_id}", headers=HEADERS, timeout=20)
    if not response.ok:
        print(f"  FAILED to fetch: {response.status_code} {response.text[:200]}")
        if response.status_code == 404:
            print("  A 404 means this tool id is stale — the tool was recreated. "
                  "Run tools/dump_tools.py to get the real ids.")
        failed += 1
        continue

    tool = response.json()
    tool_type = tool.get("type")
    if tool_type != "apiRequest":
        print(f"  SKIPPED — type is {tool_type!r}, not 'apiRequest'. A function-type "
              f"tool keeps its config elsewhere; do not patch it with this script.")
        failed += 1
        continue

    existing = copy.deepcopy(tool.get("headers") or {"type": "object", "properties": {}})
    existing.setdefault("type", "object")
    existing.setdefault("properties", {})
    props = existing["properties"]

    current = (props.get(HEADER_NAME) or {}).get("value")
    if current == MELLO_TOOL_SECRET:
        print(f"  already correct — {HEADER_NAME} matches. Nothing to do.")
        print(f"  headers present: {sorted(props)}")
        already_ok += 1
        continue

    props[HEADER_NAME] = {"type": "string", "value": MELLO_TOOL_SECRET}

    print(f"  headers before: {sorted((tool.get('headers') or {}).get('properties', {}))}")
    print(f"  headers after : {sorted(props)}")
    if current:
        print(f"  ({HEADER_NAME} already existed with a DIFFERENT value — replacing it.)")

    if not APPLY:
        print("  (dry run — pass --apply to send)\n")
        changed += 1
        continue

    patch = requests.patch(
        f"https://api.vapi.ai/tool/{tool_id}", headers=HEADERS,
        json={"headers": existing}, timeout=30,
    )
    if not patch.ok:
        print(f"  PATCH FAILED {patch.status_code}: {patch.text[:400]}\n")
        failed += 1
        continue

    # Read back rather than trusting the write. A 200 that did not actually
    # persist the header would leave you believing the call path is fixed.
    verify = requests.get(f"https://api.vapi.ai/tool/{tool_id}", headers=HEADERS, timeout=20)
    saved = (((verify.json().get("headers") or {}).get("properties") or {})
             .get(HEADER_NAME, {}).get("value")) if verify.ok else None
    if saved == MELLO_TOOL_SECRET:
        print(f"  PATCHED and verified — {HEADER_NAME} is live on this tool.\n")
        changed += 1
    else:
        print(f"  PATCH returned 200 but the header did NOT persist. Set it by hand "
              f"in the Vapi dashboard.\n")
        failed += 1

print("=" * 62)
if not APPLY:
    print(f"DRY RUN — {changed} tool(s) would be updated, {already_ok} already correct, "
          f"{failed} could not be read.")
    print("Re-run with --apply to send.")
else:
    print(f"{changed} updated, {already_ok} already correct, {failed} failed.")
    if failed:
        print("Fix the failures before placing a call — a tool without the header "
              "will 401 mid-conversation.")
    else:
        print("All three tools carry the header. Now run:  python3 tools/preflight_check.py")
print("=" * 62)
