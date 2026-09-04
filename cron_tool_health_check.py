"""
Render Cron Job: 9:30 PM Monterrey time (after evening-wrap).
Schedule in Render (UTC): 30 3 * * *

Closes a real gap: cron_draft_improvements.py only analyzes CONVERSATION
quality (objection handling, tone) — it has no visibility into TOOL-LEVEL
failures like calculate_mao returning a 404, or log_call_outcome silently
failing validation. Those are infrastructure/config bugs, not something a
prompt edit can fix, and previously nothing alerted on them at all — you
only found out by reading raw call transcripts and asking me to diagnose
them by hand.

This pulls every call from today, checks each one's actual tool-call
results (success/fail, per Vapi's Get Call API), and emails you a plain
diagnostic summary if anything failed — tool name, error message, call ID,
so you know exactly what to check (usually: a wrong Server URL, a renamed
parameter that no longer matches your Vapi tool schema, or a genuine
server-side bug). This does NOT fix anything automatically — it can't, the
fix usually lives in Vapi's tool config or your own code — it just makes
sure a real failure can't go unnoticed for days again.

HONEST LIMITATION: the exact shape of tool-call results inside Vapi's call
object isn't 100% pinned down here — built from their public docs and
debugging guides, not a live-tested payload. The parsing below tries a
couple of reasonable field paths defensively and prints the raw structure
of the first call it processes, specifically so you can sanity-check it
against reality on the first real run and tell me if anything needs
adjusting — same precaution used for every other unverified integration in
this project.
"""

import os
from datetime import datetime

import requests

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")
VAPI_ASSISTANT_ID = os.environ.get("VAPI_ASSISTANT_ID")

EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", 587))
EMAIL_USERNAME = os.environ.get("EMAIL_USERNAME")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL")


def get_todays_calls() -> list:
    """Same list pattern already used in cron_draft_improvements.py."""
    headers = {"Authorization": f"Bearer {VAPI_API_KEY}"}
    today_start = datetime.now().replace(hour=0, minute=0, second=0).isoformat()
    params = {"assistantId": VAPI_ASSISTANT_ID, "createdAtGe": today_start}
    response = requests.get("https://api.vapi.ai/call", headers=headers, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def get_call_detail(call_id: str) -> dict:
    """Full call object — the list endpoint may not include per-message
    tool-call results, so fetch each call individually for real detail."""
    headers = {"Authorization": f"Bearer {VAPI_API_KEY}"}
    response = requests.get(f"https://api.vapi.ai/call/{call_id}", headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def extract_tool_failures(call_detail: dict) -> list:
    """
    Scans a call's message/artifact history for any tool call that did not
    succeed. Tries a couple of plausible field paths defensively since the
    exact schema isn't independently confirmed — see module docstring.
    """
    failures = []
    messages = (
        call_detail.get("messages")
        or call_detail.get("artifact", {}).get("messages")
        or []
    )
    for msg in messages:
        tool_calls = msg.get("toolCalls") or ([msg] if msg.get("type") == "tool-call-result" else [])
        for tc in tool_calls:
            result = tc.get("result") or tc.get("toolCallResult") or {}
            error = result.get("error") if isinstance(result, dict) else None
            if error or (isinstance(result, dict) and result.get("status") == "fail"):
                failures.append({
                    "tool_name": tc.get("name") or tc.get("function", {}).get("name", "unknown"),
                    "error": error or "non-success result",
                })
    return failures


def email_alert(failures_by_tool: dict, call_count: int):
    import smtplib
    from email.mime.text import MIMEText

    if not all([EMAIL_USERNAME, EMAIL_PASSWORD, OWNER_EMAIL]):
        raise RuntimeError("EMAIL_USERNAME, EMAIL_PASSWORD, or OWNER_EMAIL not set")

    lines = [f"Checked {call_count} call(s) today. Tool failures found:\n"]
    for tool_name, errors in failures_by_tool.items():
        lines.append(f"\n{tool_name} — {len(errors)} failure(s):")
        for e in errors[:5]:
            lines.append(f"  - {e['error']} (call {e.get('call_id', 'unknown')})")
    lines.append(
        "\n\nThis usually means either: the tool's Server URL in Vapi is wrong/stale, "
        "a parameter name in Vapi's tool schema no longer matches what your server expects, "
        "or a real server-side bug. Check the raw call log in Vapi's dashboard for the exact "
        "call IDs above for full detail."
    )
    body = "\n".join(lines)

    msg = MIMEText(body, "plain")
    msg["From"] = EMAIL_USERNAME
    msg["To"] = OWNER_EMAIL
    msg["Subject"] = f"Tool health alert — {sum(len(v) for v in failures_by_tool.values())} failure(s) today"

    with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
        server.starttls()
        server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
        server.sendmail(EMAIL_USERNAME, [OWNER_EMAIL], msg.as_string())


if __name__ == "__main__":
    print("Checking today's calls for tool failures...")
    calls = get_todays_calls()
    print(f"  Found {len(calls)} call(s) today")

    failures_by_tool = {}
    for i, call in enumerate(calls):
        call_id = call.get("id")
        if not call_id:
            continue
        try:
            detail = get_call_detail(call_id)
            if i == 0:
                print(f"  Sanity check — first call's top-level keys: {list(detail.keys())}")
            failures = extract_tool_failures(detail)
            for f in failures:
                f["call_id"] = call_id
                failures_by_tool.setdefault(f["tool_name"], []).append(f)
        except Exception as e:
            print(f"  Could not check call {call_id}: {e}")
            continue

    if not failures_by_tool:
        print("  No tool failures found today.")
    else:
        print(f"  Found failures in {len(failures_by_tool)} tool(s): {list(failures_by_tool.keys())}")
        try:
            email_alert(failures_by_tool, len(calls))
            print("  Alert emailed.")
        except Exception as e:
            print(f"  Failed to send alert email: {e}")
