"""
dump_tools.py — prints the RAW JSON of the assistant and every tool attached
to it. No interpretation, no guessing at field names.

I need this because my earlier script looked for `server.url`, which is where
a FUNCTION tool keeps its URL. Your tools are `apiRequest` tools, which keep
the URL at the TOP LEVEL as `url`. So "server.url: None" was my script's blind
spot, not necessarily your bug. This shows everything.

RUN:
    export VAPI_API_KEY=...
    export VAPI_ASSISTANT_ID=...
    python dump_tools.py

Paste the output back. It contains no secrets beyond your own URLs.
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
    "model": {k: v for k, v in (assistant.get("model") or {}).items()
              if k != "messages"},          # skip the system prompt, it's long
}, indent=2, default=str))

tool_ids = (assistant.get("model") or {}).get("toolIds") or []
print(f"\n{len(tool_ids)} tool(s) attached\n")

for tid in tool_ids:
    tool = get(f"https://api.vapi.ai/tool/{tid}")
    name = tool.get("name") or (tool.get("function") or {}).get("name") or "?"
    print("=" * 70)
    print(f"TOOL: {name}   ({tid})   type={tool.get('type')}")
    print("=" * 70)
    # Drop the noisy audit fields, keep everything that affects behaviour.
    for noise in ("createdAt", "updatedAt", "orgId", "id"):
        tool.pop(noise, None)
    print(json.dumps(tool, indent=2, default=str))
    print()
