"""
lead_readiness.py — how many leads could actually be called today, and why
the rest could not.

WHY THIS EXISTS
---------------
"Leads have no phone numbers" has been launch blocker #1 in every handoff,
and the answer has never been a number. cron_dispatch_calls.py applies six
independent gates before it dials, and a lead failing ANY of them is skipped
silently — no error, no status change, nothing in Airtable that looks wrong.
So a table that looks full can dial zero leads and give you no clue why.

This runs the EXACT same gates, in the same order, against your real data,
and reports where every lead falls out. It reads Airtable and writes
nothing. It places no calls.

    export AIRTABLE_API_KEY=... AIRTABLE_BASE_ID=... AIRTABLE_TABLE_NAME=Leads
    python3 tools/lead_readiness.py

Run it before enabling the dispatch cron, and again any morning the call
volume looks wrong.
"""

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from airtable_helpers import normalize_phone, query_leads, require_config  # noqa: E402
from orchestrator_lib import (  # noqa: E402
    _normalize_state, is_lead_exhausted, is_retry_due, is_within_calling_hours,
)

# The exact statuses cron_dispatch_calls.py considers workable.
WORKABLE = {"New", "Contacted", "Qualified", "Offer Made"}


def pct(n, total):
    return f"{(n / total * 100):.0f}%" if total else "—"


def bar(n, total, width=28):
    filled = round((n / total) * width) if total else 0
    return "#" * filled + "." * (width - filled)


def main():
    require_config()

    print("Reading every lead from Airtable...\n")
    leads = query_leads("TRUE()")
    total = len(leads)
    if not total:
        sys.exit("No leads in the table at all.")

    status_counts = Counter(r["fields"].get("status", "(no status)") for r in leads)

    print(f"{'=' * 62}\n{total} leads total\n{'=' * 62}")
    for status, count in status_counts.most_common():
        print(f"  {status:22s} {count:5d}  {pct(count, total):>4s}  {bar(count, total)}")

    # ---- field completeness across the WHOLE table -----------------------
    print(f"\n{'=' * 62}\nFIELD COMPLETENESS (all {total} leads)\n{'=' * 62}")
    fields_to_check = [
        ("phone", "cannot be called at all"),
        ("state", "cannot be called — calling-hours check needs it"),
        ("arv", "excluded by the dispatch query ({arv}!=BLANK())"),
        ("city", "contract + full address will be incomplete"),
        ("zip", "contract + full address will be incomplete"),
        ("owner_name", "agent greets them as 'there'"),
        ("date_created", "cannot be placed on the retry schedule"),
    ]
    for field, consequence in fields_to_check:
        have = sum(1 for r in leads if r["fields"].get(field) not in (None, ""))
        missing = total - have
        flag = "  <-- " + consequence if missing else ""
        print(f"  {field:14s} {have:5d}/{total} have it  ({pct(have, total):>4s}){flag}")

    # Unknown state codes fail the calling-hours lookup and are never dialled.
    # Uses the same normalizer the dispatch cron uses, so this report can
    # never disagree with what will actually happen at dial time.
    bad_states = Counter(
        r["fields"]["state"] for r in leads
        if r["fields"].get("state") and not _normalize_state(r["fields"]["state"])
    )
    if bad_states:
        print(f"\n  UNRECOGNISED state codes (these leads can NEVER be called — "
              f"is_within_calling_hours fails safe): {dict(bad_states)}")

    # ---- the actual dispatch funnel -------------------------------------
    print(f"\n{'=' * 62}\nDISPATCH FUNNEL — the six gates, in order\n{'=' * 62}")

    opted_out_phones = {
        normalize_phone(r["fields"].get("phone"))
        for r in leads if r["fields"].get("status") == "Opt Out"
        and normalize_phone(r["fields"].get("phone"))
    }

    workable = [r for r in leads if r["fields"].get("status") in WORKABLE]
    print(f"  workable status ({'/'.join(sorted(WORKABLE))})".ljust(46)
          + f"{len(workable):5d}")

    with_arv = [r for r in workable if r["fields"].get("arv") not in (None, "")]
    print("  ...and has an ARV".ljust(46) + f"{len(with_arv):5d}")

    reasons = Counter()
    dialable_now, dialable_later = [], []

    for r in with_arv:
        f = r["fields"]
        address, state, phone = f.get("address"), f.get("state"), f.get("phone")
        created, count = f.get("date_created"), f.get("#_calls", 0) or 0
        next_contact = f.get("next_contact_date")

        if not (address and state and phone and created):
            missing = [n for n, v in (("address", address), ("state", state),
                                      ("phone", phone), ("date_created", created)) if not v]
            reasons[f"missing {', '.join(missing)}"] += 1
            continue
        if normalize_phone(phone) in opted_out_phones:
            reasons["phone matches an opted-out number"] += 1
            continue
        if is_lead_exhausted(created, count, next_contact):
            reasons["retry schedule exhausted"] += 1
            continue
        if not is_retry_due(created, count, next_contact,
                            status=f.get("status"), last_call_date=f.get("last_call_date")):
            reasons["not due yet (retry cadence)"] += 1
            dialable_later.append(r)
            continue
        if not is_within_calling_hours(state):
            reasons["outside calling hours right now"] += 1
            dialable_later.append(r)
            continue
        dialable_now.append(r)

    for reason, n in reasons.most_common():
        print(f"  ...minus {reason}".ljust(46) + f"{-n:5d}")

    print("  " + "-" * 44)
    print("  WOULD BE CALLED ON THE NEXT RUN".ljust(46) + f"{len(dialable_now):5d}")

    # ---- verdict ---------------------------------------------------------
    print(f"\n{'=' * 62}")
    if dialable_now:
        print(f"READY: {len(dialable_now)} lead(s) would be dialled on the next "
              f"dispatch run.")
        print("Sample:")
        for r in dialable_now[:5]:
            f = r["fields"]
            print(f"  {f.get('address')}, {f.get('city')}, {f.get('state')} — "
                  f"{f.get('phone')} — ARV {f.get('arv')} — {f.get('status')}")
    elif dialable_later:
        print(f"NOT NOW, BUT NOT BROKEN: 0 leads are due this minute, but "
              f"{len(dialable_later)} are otherwise complete and will come due "
              f"on schedule or once calling hours open.")
    else:
        print("BLOCKED: zero leads can be dialled, and none will become dialable "
              "on their own.")
        print("The dispatch cron would run, log nothing unusual, and place no "
              "calls. Fix the top reason above before enabling it.")
    print("=" * 62)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)
