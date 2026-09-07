"""
preflight_check.py — proves the whole tool path works WITHOUT placing a call.

REWRITTEN. The previous version posted a Vapi FUNCTION-tool envelope
({"message": {"toolCallList": [...]}}) to /vapi/tools. The three tools are
actually type `apiRequest`, which sends a FLAT body to a per-tool URL, and
/vapi/tools no longer exists — so every check in the old file would have
failed for the wrong reason and sent you chasing a phantom bug. Its default
hostname was also the suffix-less one that resolves but has nothing behind
it, which is the single most expensive mistake in this project's history.

WHAT IT CHECKS
  1. Required env vars are present locally
  2. The Render service is actually reachable at the URL you gave it
  3. calculate_mao returns sane math, including the no_deal guardrail
  4. log_call_outcome writes to the RIGHT existing record and does NOT
     create a duplicate when sent the full address the agent really sends
  5. An empty-string numeric (Vapi's "default": "" on number fields) does
     not 422 the call
  6. The tool secret is enforced if configured

RUN (from your own machine, with the same AIRTABLE_* vars the crons use):
    export RENDER_BASE_URL=https://mello-server-hqfi.onrender.com
    export MELLO_TOOL_SECRET=...        # if you have set one
    python tools/preflight_check.py
    python tools/preflight_check.py --address "6506 Clubway Ln"

It writes a real status to a real record and then restores it. Use a lead
you don't mind touching, or create a throwaway one first.

flag_for_human_review is deliberately NOT tested — it emails a real
contract. Test that once, by hand, when you're ready to receive one.
"""

import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from airtable_helpers import find_lead_record, query_leads, upsert_lead  # noqa: E402

# NO DEFAULT HOSTNAME ON PURPOSE. Render appends a random suffix when a
# service name collides or the service is recreated; the old suffix-less
# hostname still resolves through DNS but has nothing behind it, so requests
# hang forever instead of failing cleanly. Copy the URL from Render's
# dashboard — it is authoritative, and it does not match the service name.
RENDER_BASE_URL = (os.environ.get("RENDER_BASE_URL") or "").rstrip("/")
MELLO_TOOL_SECRET = os.environ.get("MELLO_TOOL_SECRET")

REQUIRED_VARS = [
    "AIRTABLE_API_KEY", "AIRTABLE_BASE_ID", "AIRTABLE_TABLE_NAME",
    "RESEND_API_KEY", "OWNER_EMAIL", "BUYER_NAME",
]

passed, failed = [], []


def ok(msg):
    passed.append(msg)
    print(f"  PASS  {msg}")


def bad(msg):
    failed.append(msg)
    print(f"  FAIL  {msg}")


def post_tool(path: str, body: dict, extra_headers: dict = None):
    """Exactly what an apiRequest tool sends: a flat JSON body to the
    tool's own URL, expecting any 2xx JSON back."""
    headers = {"Content-Type": "application/json"}
    if MELLO_TOOL_SECRET:
        headers["X-Mello-Token"] = MELLO_TOOL_SECRET
    headers.update(extra_headers or {})
    return requests.post(f"{RENDER_BASE_URL}{path}", json=body, headers=headers, timeout=45)


def step_env():
    print("\n[1] Local environment")
    if not RENDER_BASE_URL:
        bad("RENDER_BASE_URL is not set. Copy it from Render's dashboard "
            "(it includes a random suffix like -hqfi and does NOT match the service name).")
        sys.exit(1)
    ok(f"RENDER_BASE_URL = {RENDER_BASE_URL}")
    for var in REQUIRED_VARS:
        ok(f"{var} is set") if os.environ.get(var) else \
            bad(f"{var} is MISSING locally (check it is on the Render web service too)")
    if MELLO_TOOL_SECRET:
        ok("MELLO_TOOL_SECRET is set — auth will be exercised")
    else:
        print("  NOTE  MELLO_TOOL_SECRET not set locally. If the server has one, "
              "every check below will 401.")


def step_health():
    print("\n[2] Render reachable")
    try:
        r = requests.get(f"{RENDER_BASE_URL}/", timeout=45)
    except requests.Timeout:
        return bad("GET / timed out. A hang (rather than an error) is the classic "
                   "symptom of the WRONG HOSTNAME — the old one resolves but has "
                   "nothing behind it. Re-check the URL in Render's dashboard.")
    except Exception as e:
        return bad(f"GET / failed: {e}")

    if not r.ok:
        return bad(f"GET / -> {r.status_code}")
    ok(f"GET / -> {r.status_code}")
    try:
        auth = r.json().get("tool_auth")
        if auth == "enabled":
            ok("server reports tool auth ENABLED")
        else:
            print("  NOTE  server reports tool auth OPEN. Set MELLO_TOOL_SECRET on "
                  "Render and add an X-Mello-Token header to each Vapi tool.")
    except Exception:
        pass


def step_calculate_mao():
    print("\n[3] calculate_mao")
    r = post_tool("/calculate_mao", {"arv": 250000, "repair_cost": 15000})
    if r.status_code == 401:
        return bad("401 — the server has MELLO_TOOL_SECRET set and this run's token "
                   "doesn't match. Vapi's tools need the same X-Mello-Token header.")
    if r.status_code != 200:
        return bad(f"HTTP {r.status_code} — Vapi ignores anything that isn't 2xx. {r.text[:200]}")
    data = r.json()
    if data.get("no_deal"):
        return bad(f"a healthy 250k/15k deal returned no_deal: {data.get('reason')}")
    if not data.get("mao_floor") or data["mao_floor"] <= 0:
        return bad(f"mao_floor is not a usable number: {data}")
    if data["opening_offer"] > data["mao_floor"]:
        return bad("opening_offer is ABOVE mao_floor — the agent would open above its ceiling")
    ok(f"mao_floor {data['mao_floor']:,}, opening {data['opening_offer']:,}, "
       f"fee {data['wholesale_fee']:,}")

    print("\n[3b] calculate_mao guardrail — a deal too thin to offer on")
    r2 = post_tool("/calculate_mao", {"arv": 200000, "repair_cost": 145999})
    thin = r2.json()
    if thin.get("no_deal") is True and thin.get("opening_offer") is None:
        ok("thin deal correctly returns no_deal with no number attached")
    else:
        bad(f"THIN DEAL RETURNED A NUMBER — the agent could offer it out loud: {thin}")


def step_empty_string_numeric():
    print("\n[4] Vapi's empty-string numeric defaults")
    r = post_tool("/calculate_mao", {"arv": 250000, "repair_cost": 15000,
                                     "wholesale_fee_min": "", "buyer_profit_pct": ""})
    if r.status_code == 422:
        bad("422 on an empty-string numeric. The Vapi tool schemas set "
            '"default": "" on number fields; if Vapi injects those, every '
            "call fails here. This is what the _num() coercion in main.py fixes.")
    elif r.ok:
        ok("empty-string numerics are coerced, not rejected")
    else:
        bad(f"HTTP {r.status_code}: {r.text[:200]}")


def pick_lead(explicit):
    if explicit:
        record = find_lead_record(explicit)
        if not record:
            sys.exit(f"No Airtable record with address exactly '{explicit}'")
        return record
    leads = query_leads("{address}!=BLANK()", max_records=1)
    if not leads:
        sys.exit("No leads in Airtable to test against.")
    return leads[0]


def step_log_call_outcome(record):
    print("\n[5] log_call_outcome — the write path")
    fields = record["fields"]
    street = fields.get("address")
    full = ", ".join(p for p in [
        street, fields.get("city"),
        " ".join(x for x in [fields.get("state"), fields.get("zip")] if x)
    ] if p)

    original_status = fields.get("status")
    original_notes = fields.get("call_transcript_summary")
    escaped = street.replace("\\", "\\\\").replace("'", "\\'")
    before = len(query_leads(f"FIND('{escaped}', {{address}}) > 0"))

    print(f"  lead: '{street}'  (the agent will send: '{full}')")

    r = post_tool("/log_call_outcome", {
        "address": full,  # exactly what {{property_address}} contains
        "status": "Contacted",
        "notes": "PREFLIGHT TEST — automated check, ignore.",
        "arv": "",  # the empty-string default again, on the real write path
    })

    if r.status_code != 200:
        return bad(f"HTTP {r.status_code}: {r.text[:300]}")
    ok(f"tool responded: {json.dumps(r.json())[:160]}")

    after = len(query_leads(f"FIND('{escaped}', {{address}}) > 0"))
    if after > before:
        bad(f"DUPLICATE CREATED — {before} record(s) before, {after} after. The full "
            f"address did not match the stored street-only address. Delete the orphan "
            f"row and confirm find_lead_flexible() is in use.")
    else:
        ok("no duplicate record created")

    check = find_lead_record(street)
    if check and check["fields"].get("status") == "Contacted":
        ok("status landed on the correct existing record")
    else:
        bad(f"status on '{street}' is "
            f"{check['fields'].get('status') if check else 'MISSING'}")

    new_notes = (check or {}).get("fields", {}).get("call_transcript_summary") or ""
    if original_notes and original_notes[:40] not in new_notes:
        bad("PRIOR NOTES WERE WIPED — log_call_outcome should append, not overwrite.")
    else:
        ok("prior call notes were preserved")

    restore = {}
    if original_status:
        restore["status"] = original_status
    if original_notes is not None:
        restore["call_transcript_summary"] = original_notes
    if restore:
        upsert_lead(street, restore)
        print(f"  (restored status to {original_status} and notes to their prior value)")


def step_bad_status():
    print("\n[6] An unrecognised status does not lose the outcome")
    leads = query_leads("{address}!=BLANK()", max_records=1)
    if not leads:
        return
    street = leads[0]["fields"].get("address")
    original = leads[0]["fields"].get("status")
    r = post_tool("/log_call_outcome", {
        "address": street, "status": "Follow Up Later", "notes": ""
    })
    if r.ok and r.json().get("status_saved") in ("Contacted", "Priority Follow-up"):
        ok(f"unknown status normalised to '{r.json()['status_saved']}' instead of 422-ing")
    else:
        bad(f"unknown status returned {r.status_code}: {r.text[:200]}")
    if original:
        upsert_lead(street, {"status": original})


if __name__ == "__main__":
    explicit = None
    if "--address" in sys.argv:
        explicit = sys.argv[sys.argv.index("--address") + 1]

    step_env()
    step_health()
    step_calculate_mao()
    step_empty_string_numeric()
    step_log_call_outcome(pick_lead(explicit))
    step_bad_status()

    print("\n" + "=" * 66)
    print(f"{len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  - {f}")
    print("=" * 66)
    sys.exit(1 if failed else 0)
