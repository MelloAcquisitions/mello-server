"""
Render Cron Job: 9:00 PM Monterrey. Schedule (UTC): 0 3 * * *

REQUIRED ENV ON THIS JOB:
  AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME
OPTIONAL (recommended):
  RESEND_API_KEY, OWNER_EMAIL, RESEND_FROM — see below.

CHANGES IN THIS CLEANUP
-----------------------
This job produced a genuinely useful daily summary and then printed it into
Render's log viewer, where nobody was ever going to read it — which meant
"3 leads agreed, needing contract follow-up" could sit unseen for days. It
now emails the summary if RESEND_API_KEY and OWNER_EMAIL are set on this
job, and still prints either way so nothing is lost if email is not
configured yet.

Also: the summary is now skipped when there is genuinely nothing to report
(no agreed, no callbacks, no priority leads), so a daily email stays worth
opening rather than becoming noise you filter away.
"""

import os
import sys

from airtable_helpers import query_leads, require_config
from mello_time import today_local


def bucket(records: list, status: str) -> list:
    return [r for r in records if r["fields"].get("status") == status]


def describe(record: dict) -> str:
    f = record["fields"]
    return f"  {f.get('address', '?')} — {f.get('owner_name', 'Unknown')} — {f.get('phone', '-')}"


def main():
    require_config()

    print(f"Wrapping up {today_local()} (Monterrey)...")

    all_leads = query_leads("TRUE()")  # every record — used for a status count
    status_counts = {}
    for record in all_leads:
        status = record["fields"].get("status", "Unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    total_calls = sum(r["fields"].get("#_calls", 0) or 0 for r in all_leads)

    lines = [
        f"=== Daily Summary — {today_local()} ===",
        f"Total leads in system: {len(all_leads)}",
        f"Total call attempts (all-time): {total_calls}",
        "",
    ]
    for status, count in sorted(status_counts.items()):
        lines.append(f"  {status}: {count}")

    agreed = bucket(all_leads, "Agreed")
    human_call = bucket(all_leads, "Human Call")
    priority = bucket(all_leads, "Priority Follow-up")
    offer_made = bucket(all_leads, "Offer Made")

    needs_you = bool(agreed or human_call or priority)

    if agreed:
        lines += ["", f"[!] {len(agreed)} lead(s) status 'Agreed' — need contract follow-up:"]
        lines += [describe(r) for r in agreed]
    if human_call:
        lines += ["", f"[phone] {len(human_call)} lead(s) requested a human callback:"]
        lines += [describe(r) for r in human_call]
    if priority:
        lines += ["", f"[hot] {len(priority)} lead(s) flagged Priority Follow-up "
                      f"(weak number, strong reason to sell):"]
        lines += [describe(r) for r in priority]
    if offer_made:
        lines += ["", f"[info] {len(offer_made)} lead(s) with a number on the table "
                      f"(Offer Made) — still in the normal retry cycle, informational only."]
    if not needs_you:
        lines += ["", "Nothing is waiting on you tonight."]

    summary = "\n".join(lines)
    print("\n" + summary)

    # Email it only if there is something actionable AND email is configured.
    if not needs_you:
        return
    if not (os.environ.get("RESEND_API_KEY") and os.environ.get("OWNER_EMAIL")):
        print("\n(Not emailed — set RESEND_API_KEY and OWNER_EMAIL on this cron job "
              "to get this summary in your inbox instead of the Render log viewer.)")
        return

    try:
        from deal_dispatch import send_owner_email
        send_owner_email(
            subject=f"Evening wrap — {len(agreed)} agreed, {len(human_call)} callbacks, "
                    f"{len(priority)} priority",
            body_text=summary,
        )
        print("\nSummary emailed.")
    except Exception as e:
        print(f"\nCould not email the summary (it is printed above): {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)
