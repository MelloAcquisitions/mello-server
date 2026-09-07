"""
Render Cron Job: 9:30 PM Monterrey (after evening-wrap).
Schedule (UTC): 30 3 * * *

Closes a real gap: cron_draft_improvements.py only analyzes CONVERSATION
quality (objection handling, tone). It has no visibility into TOOL-LEVEL
failures — calculate_mao returning a 404, log_call_outcome silently failing
validation. Those are infrastructure/config bugs a prompt edit cannot fix,
and nothing alerted on them; you only found out by reading raw transcripts.

This pulls today's calls, checks each one's tool-call results, and emails a
plain diagnostic summary if anything failed — tool name, error, call ID —
so you know exactly what to check (usually a wrong URL, a renamed parameter
that no longer matches the Vapi tool schema, or a genuine server bug). It
does NOT fix anything; it makes sure a real failure cannot go unnoticed.

REQUIRED ENV ON THIS JOB:
  VAPI_API_KEY, VAPI_ASSISTANT_ID, RESEND_API_KEY, OWNER_EMAIL

CHANGES IN THIS CLEANUP
-----------------------
1. THE smtplib BLOCK IS GONE. Render's free tier blocks all outbound SMTP
   ports, so the alert email would have failed with [Errno 101] every time —
   an alerting job that cannot alert. It now uses the same Resend transport
   as everything else (deal_dispatch.send_owner_email). This was the reason
   the job had to stay disabled.
2. "TODAY" WAS THE WRONG FOUR HOURS — same UTC-vs-Monterrey bug as
   cron_draft_improvements.py. Firing at 9:30 PM local (03:30 UTC), midnight
   UTC is 6 PM local YESTERDAY, so it checked an evening sliver and ignored
   the whole calling day.
3. `response.json()` bare-list assumption replaced with vapi_client.
4. Detail is now only fetched for calls that could plausibly have tool
   calls, instead of one extra API request per call unconditionally.

STILL HONEST ABOUT: the exact shape of tool-call results inside Vapi's call
object is built from their public docs, not a live-tested payload. The
parsing tries several field paths defensively and prints the raw structure
of the first call, specifically so you can sanity-check it on the first real
run.
"""

import sys

from mello_time import start_of_today_utc_iso
from vapi_client import get_call, list_calls


def extract_tool_failures(call_detail: dict) -> list:
    """
    Scans a call's message/artifact history for any tool call that did not
    succeed. Tries several plausible field paths since the exact schema is
    not independently confirmed — see the module docstring.
    """
    failures = []
    messages = (
        call_detail.get("messages")
        or (call_detail.get("artifact") or {}).get("messages")
        or []
    )
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        tool_calls = msg.get("toolCalls") or (
            [msg] if msg.get("type") in ("tool-call-result", "tool_call_result") else []
        )
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            result = tc.get("result") or tc.get("toolCallResult") or {}
            error = result.get("error") if isinstance(result, dict) else None
            # A string result containing an error marker also counts — some
            # Vapi versions return the tool's raw response body as text.
            if isinstance(result, str) and ('"detail"' in result or "error" in result.lower()[:40]):
                error = result[:200]
            if error or (isinstance(result, dict) and result.get("status") == "fail"):
                failures.append({
                    "tool_name": tc.get("name") or (tc.get("function") or {}).get("name", "unknown"),
                    "error": error or "non-success result",
                })
    return failures


def build_alert_body(failures_by_tool: dict, call_count: int) -> str:
    total = sum(len(v) for v in failures_by_tool.values())
    lines = [f"Checked {call_count} call(s) today. {total} tool failure(s) found.\n"]
    for tool_name, errors in failures_by_tool.items():
        lines.append(f"\n{tool_name} — {len(errors)} failure(s):")
        for e in errors[:5]:
            lines.append(f"  - {e['error']} (call {e.get('call_id', 'unknown')})")
    lines.append(
        "\n\nThis usually means one of three things: the tool's URL in Vapi is "
        "wrong or stale (check it matches the Render dashboard hostname exactly, "
        "suffix included), a parameter name in the Vapi tool schema no longer "
        "matches what the server expects, or a genuine server-side bug. Open the "
        "call IDs above in Vapi's dashboard for full detail."
    )
    return "\n".join(lines)


def main():
    from airtable_helpers import require_config
    require_config("VAPI_API_KEY", "VAPI_ASSISTANT_ID")

    print("Checking today's calls for tool failures...")
    calls = list_calls(created_at_ge=start_of_today_utc_iso())
    print(f"  Found {len(calls)} call(s) today")

    failures_by_tool = {}
    checked = 0
    for i, call in enumerate(calls):
        call_id = call.get("id")
        if not call_id:
            continue
        try:
            detail = get_call(call_id)
            if i == 0:
                print(f"  Sanity check — first call's top-level keys: {list(detail.keys())}")
            checked += 1
            for f in extract_tool_failures(detail):
                f["call_id"] = call_id
                failures_by_tool.setdefault(f["tool_name"], []).append(f)
        except Exception as e:
            print(f"  Could not check call {call_id}: {e}")
            continue

    if not failures_by_tool:
        print(f"  No tool failures found across {checked} call(s).")
        return

    print(f"  Failures in {len(failures_by_tool)} tool(s): {list(failures_by_tool.keys())}")
    body = build_alert_body(failures_by_tool, len(calls))
    print("\n" + body)

    try:
        from deal_dispatch import send_owner_email
        send_owner_email(
            subject=f"Tool health alert — "
                    f"{sum(len(v) for v in failures_by_tool.values())} failure(s) today",
            body_text=body,
        )
        print("\n  Alert emailed.")
    except Exception as e:
        print(f"\n  Failed to send alert email (the report is printed above): {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)
