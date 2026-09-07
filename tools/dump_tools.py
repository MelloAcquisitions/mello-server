"""
dump_tools.py — prints the RAW JSON of the assistant and every tool
attached to it. No interpretation, no guessing at field names.

START HERE for any Vapi config question. It exists because an earlier
diagnostic looked for `server.url`, which is where a FUNCTION tool keeps
its URL — but these tools are `apiRequest` tools, which keep it at the TOP
LEVEL as `url`. "server.url: None" was that script's blind spot, not
necessarily a bug. This shows everything.

RUN:
    export VAPI_API_KEY=... VAPI_ASSISTANT_ID=...
    python tools/dump_tools.py

Output contains no secrets beyond your own URLs.
"""

import json
import os
import sys

import requests

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")
VAPI_ASSISTANT_ID = os.environ.get("VAPI_ASSISTANT_ID")

if not VAPI_API_KEY or not VAPI_ASSISTANT_ID:
    sys.exit("Set VAPI_API_KEY and VAPI_ASSISTANT_ID first.")

H = {"Authorization": f"Bearer {VAPI_API_KEY}"}


def get(url):
    r = requests.get(url, headers=H, timeout=20)
    if not r.ok:
        return {"_error": f"HTTP {r.status_code}", "_body": r.text[:300]}
    return r.json()


assistant = get(f"https://api.vapi.ai/assistant/{VAPI_ASSISTANT_ID}")

print("=" * 70)
print("ASSISTANT (server + model config only)")
print("=" * 70)
print(json.dumps({
    "server": assistant.get("server"),
    "serverUrl": assistant.get("serverUrl"),
    "serverMessages": assistant.get("serverMessages"),
    "analysisPlan": assistant.get("analysisPlan"),
    "model": {k: v for k, v in (assistant.get("model") or {}).items() if k != "messages"},
}, indent=2, default=str))

# Quick reads on the things that have actually broken before.
server_url = (assistant.get("server") or {}).get("url") or assistant.get("serverUrl")
if not server_url:
    print("\n!! assistant.server.url is empty — end-of-call reports will never arrive.")
if not (assistant.get("serverMessages") or []):
    print("!! serverMessages is empty — 'end-of-call-report' must be in this list.")
elif "end-of-call-report" not in (assistant.get("serverMessages") or []):
    print("!! serverMessages does not include 'end-of-call-report'.")
if not assistant.get("analysisPlan"):
    print("!! No analysisPlan — analysis.summary stays empty, and BOTH fallback "
          "logging layers read that field.")

tool_ids = (assistant.get("model") or {}).get("toolIds") or []
print(f"\n{len(tool_ids)} tool(s) attached\n")

for tid in tool_ids:
    tool = get(f"https://api.vapi.ai/tool/{tid}")
    name = tool.get("name") or (tool.get("function") or {}).get("name") or "?"
    print("=" * 70)
    print(f"TOOL: {name}   ({tid})   type={tool.get('type')}")
    print("=" * 70)
    # For apiRequest tools the URL is top-level. function.name reading
    # "api_request_tool" is Vapi's internal placeholder and is normal —
    # the model-facing name is the top-level `name`.
    if tool.get("type") == "apiRequest" and not tool.get("url"):
        print("!! apiRequest tool with no top-level `url` — it cannot reach the server.")
    if tool.get("async"):
        print("!! async=true — Vapi does not wait for the response and marks the "
              "call successful immediately.")
    if not (tool.get("function") or {}).get("maxTokens"):
        print("!! no maxTokens — Vapi defaults to 100 (~75 words), which truncates "
              "long arguments like call notes. Run tools/bump_max_tokens.py --apply.")
    for noise in ("createdAt", "updatedAt", "orgId", "id"):
        tool.pop(noise, None)
    print(json.dumps(tool, indent=2, default=str))
    print()
