"""
check_vapi_config.py — READ-ONLY. Changes nothing. Run this first.

Answers, in about 5 seconds, the question you've been chasing for weeks:
where is Vapi actually sending your tool calls?

RUN:
    export VAPI_API_KEY=...          # your private key, not the public one
    export VAPI_ASSISTANT_ID=...
    python check_vapi_config.py

Optional:
    export RENDER_BASE_URL=https://mello-server.onrender.com
"""

import json
import os
import sys

import requests

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")
VAPI_ASSISTANT_ID = os.environ.get("VAPI_ASSISTANT_ID")
RENDER_BASE_URL = os.environ.get("RENDER_BASE_URL", "https://mello-server.onrender.com").rstrip("/")

TOOL_IDS = {
    "calculate_mao": "a6589cb2-6d0d-456c-8d20-5ef047ba0d2a",
    "log_call_outcome": "788ec1d5-2349-4312-98f7-07971cf52775",
    "flag_for_human_review": "f0311622-ee1a-4230-92c4-b5644fe41e1a",
}

EXPECTED_TOOL_URL = f"{RENDER_BASE_URL}/vapi/tools"

problems = []
notes = []


def h():
    return {"Authorization": f"Bearer {VAPI_API_KEY}"}


def get(url):
    r = requests.get(url, headers=h(), timeout=20)
    if not r.ok:
        print(f"  ! {url} -> HTTP {r.status_code}: {r.text[:200]}")
        return None
    return r.json()


def dig(obj, *path, default=None):
    for key in path:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(key)
    return obj if obj is not None else default


def check_assistant():
    print("\n=== ASSISTANT ===")
    a = get(f"https://api.vapi.ai/assistant/{VAPI_ASSISTANT_ID}")
    if not a:
        problems.append("Could not fetch the assistant. Check VAPI_ASSISTANT_ID and the key.")
        return None

    server_url = dig(a, "server", "url") or a.get("serverUrl")
    server_messages = a.get("serverMessages")
    model = a.get("model", {}) or {}

    print(f"  name              : {a.get('name')}")
    print(f"  model             : {model.get('provider')}/{model.get('model')}")
    print(f"  server.url        : {server_url}")
    print(f"  serverMessages    : {server_messages}")
    print(f"  model.toolIds     : {model.get('toolIds')}")

    inline = model.get("tools") or []
    if inline:
        print(f"  model.tools (inline, {len(inline)}):")
        for t in inline:
            name = dig(t, "function", "name") or t.get("name")
            print(f"      - {name}  type={t.get('type')}  "
                  f"server.url={dig(t, 'server', 'url')}  async={t.get('async')}")
        notes.append("This assistant has INLINE tools as well as / instead of toolIds. "
                     "Inline tool definitions override the dashboard ones — make sure "
                     "you are editing the copy that is actually in use.")

    attached = set(model.get("toolIds") or [])
    for name, tid in TOOL_IDS.items():
        if tid not in attached and not any(
            (dig(t, "function", "name") or t.get("name")) == name for t in inline
        ):
            problems.append(f"Tool '{name}' is NOT attached to this assistant "
                            f"(not in model.toolIds and not inline). The model can "
                            f"never call it; it can only talk as if it did.")

    if server_url and server_url.rstrip("/").endswith("/vapi_call_ended"):
        notes.append("The assistant's server.url points at /vapi_call_ended. Any tool "
                     "with a blank Server URL falls through to this, gets a 200 back, "
                     "and is marked SUCCESSFUL while doing nothing. This is the "
                     "prime suspect for your symptom.")
    return a


def check_tools():
    print("\n=== TOOLS ===")
    for name, tid in TOOL_IDS.items():
        print(f"\n  [{name}]  {tid}")
        t = get(f"https://api.vapi.ai/tool/{tid}")
        if not t:
            problems.append(f"Tool '{name}' could not be fetched — the ID may be stale.")
            continue

        ttype = t.get("type")
        is_async = t.get("async")
        surl = dig(t, "server", "url")
        timeout = dig(t, "server", "timeoutSeconds")
        fn = t.get("function", {}) or {}
        props = list(dig(fn, "parameters", "properties", default={}).keys())
        required = dig(fn, "parameters", "required", default=[])
        msgs = [m.get("type") for m in (t.get("messages") or [])]

        print(f"    type            : {ttype}")
        print(f"    async           : {is_async}")
        print(f"    server.url      : {surl}")
        print(f"    timeoutSeconds  : {timeout}")
        print(f"    backoffPlan     : {dig(t, 'server', 'backoffPlan')}")
        print(f"    maxTokens       : {fn.get('maxTokens')}")
        print(f"    parameters      : {props}")
        print(f"    required        : {required}")
        print(f"    messages        : {msgs}")

        if ttype == "function":
            if not surl:
                problems.append(
                    f"'{name}' is a FUNCTION tool with NO server.url. Vapi falls back to "
                    f"the assistant's server URL, or emits a client-side event that no "
                    f"phone call can answer. Nothing reaches your tool endpoint. "
                    f"FIX: set server.url to {EXPECTED_TOOL_URL}")
            elif surl.rstrip("/") != EXPECTED_TOOL_URL:
                problems.append(
                    f"'{name}' posts to {surl}, which is not the Vapi-protocol endpoint. "
                    f"A function tool sends a message.toolCallList envelope; your old "
                    f"per-tool endpoints expect a flat body and will 422. "
                    f"FIX: point it at {EXPECTED_TOOL_URL}")
        elif ttype == "apiRequest":
            notes.append(f"'{name}' is an apiRequest tool. That sends a FLAT body and "
                         f"accepts any 2xx JSON, which matches your existing main.py "
                         f"endpoints. Valid alternative — but then the URL must be the "
                         f"per-tool path, not /vapi/tools.")
        else:
            problems.append(f"'{name}' has unexpected type {ttype!r}.")

        if is_async:
            problems.append(f"'{name}' has async=true. Vapi does not wait for your "
                            f"response and marks the call successful immediately. "
                            f"FIX: set async=false.")
        if timeout is not None and timeout < 15:
            notes.append(f"'{name}' timeout is {timeout}s. On Render free tier, allow 20s.")
        if not fn.get("maxTokens"):
            notes.append(f"'{name}' has no maxTokens — Vapi defaults to 100, which "
                         f"truncates longer arguments like call notes. Set 500.")

        if name == "log_call_outcome":
            if "next_contact_date" not in props:
                problems.append("log_call_outcome's schema is missing next_contact_date — "
                                "the agent physically cannot schedule a callback, so the "
                                "whole next_contact_date branch in orchestrator_lib is dead.")
            desc = json.dumps(dig(fn, "parameters", "properties", "status", default={}))
            for s in ("Priority Follow-up", "Exhausted"):
                if s not in desc:
                    notes.append(f"log_call_outcome's status description does not mention "
                                 f"'{s}'. The model will not use a status it cannot see.")


def probe_render():
    print("\n=== RENDER ===")
    try:
        r = requests.get(f"{RENDER_BASE_URL}/", timeout=30)
        print(f"  GET /            -> {r.status_code} {r.text[:80]}")
    except Exception as e:
        problems.append(f"Render health check failed: {e}")
        return

    envelope = {
        "message": {
            "type": "tool-calls",
            "toolCallList": [{
                "id": "call_probe_1",
                "type": "function",
                "function": {"name": "calculate_mao",
                             "arguments": {"arv": 250000, "repair_cost": 15000}},
            }],
        }
    }
    try:
        r = requests.post(f"{RENDER_BASE_URL}/vapi/tools", json=envelope, timeout=30)
        print(f"  POST /vapi/tools -> {r.status_code}")
        print(f"    {r.text[:300]}")
        if r.status_code == 404:
            problems.append("/vapi/tools does not exist yet — deploy vapi_tools_router.py "
                            "and wire it into main.py before repointing the tools.")
        elif r.ok:
            body = r.json()
            results = body.get("results")
            if not results:
                problems.append("/vapi/tools returned 200 but no results[] array.")
            elif results[0].get("toolCallId") != "call_probe_1":
                problems.append("toolCallId did not echo back — Vapi will discard this.")
            elif not isinstance(results[0].get("result", ""), str):
                problems.append("result must be a STRING, not an object.")
            else:
                print("  OK: envelope parsed, toolCallId echoed, result is a string.")
    except Exception as e:
        problems.append(f"POST /vapi/tools failed: {e}")


if __name__ == "__main__":
    if not VAPI_API_KEY or not VAPI_ASSISTANT_ID:
        sys.exit("Set VAPI_API_KEY and VAPI_ASSISTANT_ID first.")

    check_assistant()
    check_tools()
    probe_render()

    print("\n" + "=" * 70)
    if problems:
        print(f"BLOCKERS ({len(problems)}):")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
    else:
        print("No blockers found in Vapi config.")
    if notes:
        print(f"\nWorth fixing ({len(notes)}):")
        for i, n in enumerate(notes, 1):
            print(f"  {i}. {n}")
    print("=" * 70)
