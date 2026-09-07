"""
Single source of truth for "what day is it" across the whole system.

WHY THIS EXISTS
---------------
Render runs in UTC. The business runs in Monterrey (UTC-6, no DST).
`date.today()` on Render therefore rolls over at 6:00 PM local time, not
midnight, and that broke three separate things:

  1. THE DAILY CALL CAP. The Daily Log record is keyed on `date`. Calls
     placed between 6:00 PM and 9:00 PM Monterrey were written to
     TOMORROW's record, so `get_todays_call_count()` reset to 0 mid-
     evening and cron_dispatch_calls.py could place up to 2x
     MAX_CALLS_PER_DAY inside a single calling day. That is real money and
     a real compliance surface.

  2. "TODAY'S CALLS" IN THE ANALYSIS CRONS. cron_draft_improvements.py
     (10 PM local = 04:00 UTC next day) and cron_tool_health_check.py
     (9:30 PM local = 03:30 UTC next day) both computed
     `datetime.now().replace(hour=0,...)`, which on Render is midnight
     UTC = 6:00 PM Monterrey THE PREVIOUS DAY. They were analyzing a
     ~4-hour evening slice and silently ignoring the entire 8 AM - 6 PM
     calling day.

  3. `last_call_date` STAMPS. A call at 7 PM Monterrey was stamped with
     tomorrow's date, which skews the retry cadence by a day and makes the
     end-of-call webhook's "already logged today?" check unreliable.

USAGE — never call `date.today()` or `datetime.now()` anywhere else in
this project. Use `today_local()` / `now_local()` / `today_iso()`.

ONE-TIME SEAM: rows written before this module existed carry UTC-derived
dates. For evening calls those are one day ahead of the true local date.
Nothing needs migrating — the retry schedule is day-granular and self-
corrects on the next call — but a lead that looks like it was called
"tomorrow" in your Airtable history is this, not a bug.

BUSINESS_TIMEZONE is overridable by env var so this doesn't need a code
change if the operating market moves.
"""

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BUSINESS_TIMEZONE = os.environ.get("BUSINESS_TIMEZONE", "America/Monterrey")

try:
    _TZ = ZoneInfo(BUSINESS_TIMEZONE)
except ZoneInfoNotFoundError:
    # Slim/alpine Python images ship without the tzdata database. Rather
    # than crash a cron at import time, fall back to UTC and say so loudly
    # in the logs — `tzdata` is in requirements.txt specifically to stop
    # this from happening, so seeing this line means the install is wrong.
    print(
        f"WARNING: timezone '{BUSINESS_TIMEZONE}' not found on this system — "
        f"falling back to UTC. Date boundaries will be wrong after 6 PM local. "
        f"Fix: make sure `tzdata` is installed (it is in requirements.txt)."
    )
    _TZ = ZoneInfo("UTC")


def now_local() -> datetime:
    """Current time in the business timezone (timezone-aware)."""
    return datetime.now(_TZ)


def today_local() -> date:
    """Today's date in the business timezone. Use this, never date.today()."""
    return now_local().date()


def today_iso() -> str:
    """Today's date as an ISO string — the format Airtable date fields use."""
    return today_local().isoformat()


def days_ago_local(days: int) -> date:
    return today_local() - timedelta(days=days)


def start_of_today_utc_iso() -> str:
    """
    Midnight *local* today, expressed as a UTC ISO timestamp.

    This is what you pass to Vapi's `createdAtGe` filter to mean "calls
    from today's calling day", instead of midnight UTC (which is 6 PM the
    previous local day).
    """
    local_midnight = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(ZoneInfo("UTC")).isoformat()


def parse_date_safely(date_string: str) -> date:
    """
    Handles both a plain date string ("2026-08-27") and a full ISO
    timestamp with time/timezone ("2026-08-27T15:30:00.000Z") — Airtable's
    "Created time" field type returns the latter, which date.fromisoformat()
    alone cannot parse.

    Returns None for empty/malformed input rather than raising, so one bad
    row can't take down a whole cron run.
    """
    if not date_string:
        return None
    try:
        return date.fromisoformat(str(date_string)[:10])
    except (ValueError, TypeError):
        return None
