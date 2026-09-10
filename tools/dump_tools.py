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

# ---------------------------------------------------------------------------
# SPEECH TIMING — where "the agent talked over me / went silent" actually lives
#
# The first live test call died here, not in the tool path. The agent's
# opening ran ~13 seconds; the seller spoke at second 16 while it was still
# talking; that audio went nowhere; then 18 seconds of dead air. Measured
# LLM latency was 659ms the whole time, so the latency dashboard looked
# perfectly healthy while the call was unusable.
#
# stopSpeakingPlan  = how easily a human can INTERRUPT the agent.
#   numWords: 0 means barge-in on any sound. Higher = the seller has to keep
#   talking over the agent before it yields. If this is high and your opening
#   is long, an interruption is simply discarded.
# startSpeakingPlan = how long the agent waits before it starts talking.
#   waitSeconds too high reads as lag; too low and it steps on the seller.
# silenceTimeoutSeconds = how long dead air runs before Vapi kills the call.
# ---------------------------------------------------------------------------
print("=" * 70)
print("SPEECH TIMING / INTERRUPTION")
print("=" * 70)
_timing = {
    "firstMessage": assistant.get("firstMessage"),
    "firstMessageMode": assistant.get("firstMessageMode"),
    "silenceTimeoutSeconds": assistant.get("silenceTimeoutSeconds"),
    "maxDurationSeconds": assistant.get("maxDurationSeconds"),
    "responseDelaySeconds": assistant.get("responseDelaySeconds"),
    "backchannelingEnabled": assistant.get("backchannelingEnabled"),
    "backgroundSound": assistant.get("backgroundSound"),
    "startSpeakingPlan": assistant.get("startSpeakingPlan"),
    "stopSpeakingPlan": assistant.get("stopSpeakingPlan"),
    "voice": {k: v for k, v in (assistant.get("voice") or {}).items()
              if k in ("provider", "voiceId", "model", "speed", "stability")},
}
print(json.dumps(_timing, indent=2, default=str))

_stop = assistant.get("stopSpeakingPlan") or {}
if not _stop:
    print("\n!! No stopSpeakingPlan set — Vapi uses its defaults. If sellers report "
          "being talked over, set numWords: 0 so any sound interrupts the agent.")
elif (_stop.get("numWords") or 0) > 0:
    print(f"\n!! stopSpeakingPlan.numWords = {_stop.get('numWords')} — the seller must "
          f"say that many words BEFORE the agent stops talking. Combined with a long "
          f"opening line, a short interruption ('Hello?') is discarded entirely. "
          f"Consider numWords: 0.")

_fm = assistant.get("firstMessage") or ""
if len(_fm.split()) > 12:
    print(f"\n!! firstMessage is {len(_fm.split())} words. The opening should be one "
          f"short question — long openings are what sellers talk over.")

print()

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
    # maxTokens only governs the ARGUMENTS a model generates for a tool call.
    # A built-in endCall/transferCall tool takes no arguments, so warning about
    # it is noise — and worse, it points at bump_max_tokens.py, which only
    # targets the three custom tools and would do nothing here.
    if tool.get("type") not in ("endCall", "transferCall", "dtmf") \
            and not (tool.get("function") or {}).get("maxTokens"):
        print("!! no maxTokens — Vapi defaults to 100 (~75 words), which "
              "truncates longer arguments like call notes. Run "
              "tools/bump_max_tokens.py --apply.")
    for noise in ("createdAt", "updatedAt", "orgId", "id"):
        tool.pop(noise, None)
    print(json.dumps(tool, indent=2, default=str))
    print()
