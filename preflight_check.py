"""
preflight_check.py — proves the whole tool path works WITHOUT placing a call.

This is the test the project has never actually had: it fires the exact
payloads Vapi fires, at the live Render URL, using a real lead from your
Airtable, then reads Airtable back to confirm the write landed on the
RIGHT record and did not create a duplicate.

Run it from your own machine (needs AIRTABLE_* vars, same as the crons).

    python preflight_check.py                    # picks a lead automatically
    python preflight_check.py --address "6506 Clubway Ln"

It writes a real status to a real record and then puts it back. Use a lead
you don't mind touching, or create a throwaway one first.
"""

import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from airtable_helpers import query_leads, find_lead_record, upsert_lead  # noqa: E402

RENDER_BASE_URL = os.environ.get("RENDER_BASE_URL", "https://mello-server.onrender.com").rstrip("/")
TOOL_URL = f"{RENDER_BASE_URL}/vapi/tools"

REQUIRED_WEB_VARS = [
    "AIRTABLE_API_KEY", "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_NAME",
    "EMAIL_USERNAME", "EMAIL_PASSWORD", "OWNER_EMAIL", "BUYER_NAME",
]

passed, failed = [], []


def ok(msg):
    passed.append(msg)
    print(f"  PASS  {msg}")


def bad(msg):
    failed.append(msg)
    print(f"  FAIL  {msg}")


def call_tool(name, args, call_id="call_preflight"):
    envelope = {"message": {"type": "tool-calls", "toolCallList": [
        {"id": call_id, "type": "function",
         "function": {"name": name, "arguments": args}}]}}
    r = requests.post(TOOL_URL, json=envelope, timeout=45)
    return r


def step_env():
    print("\n[1] Local environment")
    for var in REQUIRED_WEB_VARS:
        if os.environ.get(var):
            ok(f"{var} is set")
        else:
            bad(f"{var} is MISSING locally (check it's on the Render web service too)")


def step_health():
    print("\n[2] Render reachable")
    try:
        r = requests.get(f"{RENDER_BASE_URL}/", timeout=45)
        ok(f"GET / -> {r.status_code}") if r.ok else bad(f"GET / -> {r.status_code}")
    except Exception as e:
        bad(f"GET / failed: {e}")


def step_calculate_mao():
    print("\n[3] calculate_mao through the Vapi envelope")
    r = call_tool("calculate_mao", {"arv": 250000, "repair_cost": 15000})
    if r.status_code != 200:
        return bad(f"HTTP {r.status_code} — Vapi ignores anything that isn't 200. {r.text[:200]}")
    body = r.json()
    results = body.get("results") or []
    if not results:
        return bad(f"no results[] array: {body}")
    res = results[0]
    if res.get("toolCallId") != "call_preflight":
        return bad("toolCallId not echoed back")
    if not isinstance(res.get("result"), str):
        return bad("result is not a string — Vapi will drop it")
    if "\n" in res.get("result", ""):
        return bad("result contains line breaks — Vapi's parser chokes on those")
    ok(f"result: {res['result'][:120]}")
    try:
        data = json.loads(res["result"])
        if data.get("no_deal") or data.get("mao_floor"):
            ok("math came back sane")
    except Exception:
        bad("result was not valid JSON")


def pick_lead(explicit):
    if explicit:
        rec = find_lead_record(explicit)
        if not rec:
            sys.exit(f"No Airtable record with address exactly '{explicit}'")
        return rec
    leads = query_leads("{address}!=BLANK()", max_records=1)
    if not leads:
        sys.exit("No leads in Airtable to test against.")
    return leads[0]


def step_log_call_outcome(record):
    print("\n[4] log_call_outcome — the write path")
    fields = record["fields"]
    street = fields.get("address")
    city, state, zc = fields.get("city"), fields.get("state"), fields.get("zip")
    full = ", ".join(p for p in [street, city, " ".join(x for x in [state, zc] if x)] if p)

    original_status = fields.get("status")
    original_notes = fields.get("call_transcript_summary")
    before = len(query_leads(f"FIND('{street}', {{address}}) > 0"))

    print(f"  lead: '{street}'  (agent will send: '{full}')")

    r = call_tool("log_call_outcome", {
        "address": full,                     # exactly what {{property_address}} contains
        "status": "Contacted",
        "notes": "PREFLIGHT TEST — automated check, ignore.",
    }, call_id="call_preflight_log")

    if r.status_code != 200:
        return bad(f"HTTP {r.status_code}: {r.text[:200]}")
    res = (r.json().get("results") or [{}])[0]
    if res.get("error"):
        return bad(f"tool returned an error: {res['error']}")
    ok(f"tool responded: {str(res.get('result'))[:140]}")

    after = len(query_leads(f"FIND('{street}', {{address}}) > 0"))
    if after > before:
        bad(f"DUPLICATE CREATED — {before} record(s) before, {after} after. The full "
            f"address did not match the stored street-only address. Delete the orphan "
            f"row and make sure find_lead_flexible() is in use.")
    else:
        ok("no duplicate record created")

    check = find_lead_record(street)
    if check and check["fields"].get("status") == "Contacted":
        ok("status landed on the correct existing record")
    else:
        bad(f"status on '{street}' is {check['fields'].get('status') if check else 'MISSING'}")

    restore = {"status": original_status} if original_status else {}
    if original_notes is not None:
        restore["call_transcript_summary"] = original_notes
    if restore:
        upsert_lead(street, restore)
        print(f"  (restored status to {original_status})")


def step_unknown_tool():
    print("\n[5] Unknown tool name is handled, not crashed")
    r = call_tool("not_a_real_tool", {})
    if r.status_code == 200 and (r.json().get("results") or [{}])[0].get("error"):
        ok("unknown tool returns HTTP 200 with an error string")
    else:
        bad(f"unknown tool returned {r.status_code}: {r.text[:150]}")


if __name__ == "__main__":
    explicit = None
    if "--address" in sys.argv:
        explicit = sys.argv[sys.argv.index("--address") + 1]

    step_env()
    step_health()
    step_calculate_mao()
    step_log_call_outcome(pick_lead(explicit))
    step_unknown_tool()

    print("\n" + "=" * 66)
    print(f"{len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  - {f}")
    print("=" * 66)
    print("flag_for_human_review is deliberately NOT tested here — it emails a "
          "real contract. Test it once, by hand, when you're ready to receive one.")
    sys.exit(1 if failed else 0)
