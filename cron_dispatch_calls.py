"""Render Cron Job: every 15 min, 8am-9pm Monterrey time. Schedule in Render (UTC): */15 14-23,0-2 * * *"""

import os

from airtable_helpers import (
    query_leads, upsert_lead, get_todays_call_count, increment_todays_call_count,
)
from orchestrator_lib import (
    trigger_vapi_call, is_within_calling_hours, TARGET_CONCURRENT_CALLS,
    is_retry_due, is_lead_exhausted,
)

# Hard daily cap — a real circuit-breaker. Adjust via env var without a
# code change. Default is generous headroom above the ~45-60/day steady
# state estimate, while still stopping genuine runaway behavior from a bug.
MAX_CALLS_PER_DAY = int(os.environ.get("MAX_CALLS_PER_DAY", 80))

if __name__ == "__main__":
    print("Checking for leads ready to call...")

    todays_count = get_todays_call_count()
    if todays_count >= MAX_CALLS_PER_DAY:
        print(f"  DAILY CAP REACHED ({todays_count}/{MAX_CALLS_PER_DAY}) — "
              f"stopping for safety. No calls placed this run.")
        exit()
    print(f"  {todays_count}/{MAX_CALLS_PER_DAY} calls made today")

    # Build a suppression set FIRST — never call a number that's ever opted out,
    # even if a "new" lead record technically exists for that address again.
    opted_out = query_leads("{status}='Opt Out'")
    opted_out_phones = {r["fields"].get("phone") for r in opted_out if r["fields"].get("phone")}
    print(f"  {len(opted_out_phones)} numbers permanently suppressed (opted out)")

    # AI-owned, still-workable statuses — these get redialed per the retry
    # schedule below. Agreed/Rejected/Opt Out/Human Call/Priority Follow-up/
    # Exhausted/Closed are all terminal or human-owned and deliberately
    # excluded — the AI doesn't touch those again.
    ready_leads = query_leads(
        "AND(OR({status}='New', {status}='Contacted', {status}='Qualified', {status}='Offer Made'), {arv}!=BLANK())"
    )
    print(f"  Found {len(ready_leads)} workable leads (New, Contacted, Qualified, or Offer Made)")

    calls_made = 0
    for record in ready_leads:
        fields = record["fields"]
        address = fields.get("address")
        state = fields.get("state")
        phone = fields.get("phone")
        call_count = fields.get("#_calls", 0)
        date_created = fields.get("date_created")
        next_contact_date = fields.get("next_contact_date")  # set when a seller gave a real future timeframe

        if not (address and state and phone and date_created):
            print(f"  Skipping incomplete record (missing date_created?): {address}")
            continue

        if phone in opted_out_phones:
            print(f"  SUPPRESSED — {address} matches a previously opted-out number, marking Opt Out")
            upsert_lead(address, {"status": "Opt Out"})
            continue

        if is_lead_exhausted(date_created, call_count, next_contact_date):
            print(f"  {address} has exhausted its retry schedule ({call_count} attempts) — marking Exhausted")
            upsert_lead(address, {"status": "Exhausted"})
            continue

        if not is_retry_due(date_created, call_count, next_contact_date,
                            status=fields.get("status"), last_call_date=fields.get("last_call_date")):
            print(f"  Skipping {address} — not due for next attempt yet per retry schedule")
            continue

        if not is_within_calling_hours(state):
            print(f"  Skipping {address} — outside calling hours in {state} right now")
            continue

        new_count = call_count + 1  # increment regardless of success below — a
                                     # failed dial attempt still counts as an
                                     # attempt, so a permanently broken number
                                     # doesn't get retried every single cycle
        try:
            city = fields.get("city")
            zip_code = fields.get("zip")
            city_state_zip = ", ".join(p for p in [city, " ".join(p2 for p2 in [state, zip_code] if p2)] if p)
            full_property_address = f"{address}, {city_state_zip}" if city_state_zip else f"{address}, {state}"

            call_context = {
                "seller_name": fields.get("owner_name", "there"),
                "property_address": full_property_address,
                "recommended_arv": str(fields.get("arv")),
            }
            result = trigger_vapi_call(phone, call_context)
            # status stays whatever it was — we don't yet know if anyone
            # actually answered. If the call connects, the live agent's own
            # log_call_outcome call overwrites this with the true outcome.
            update_fields = {"#_calls": new_count}
            if next_contact_date:
                # Safety net: if this scheduled callback goes unanswered,
                # don't let it retry every 15 minutes forever just because
                # the date has passed. Push it forward a few days — if the
                # call actually connects, log_call_outcome overwrites this
                # with whatever the AI actually determines anyway.
                from datetime import date, timedelta
                update_fields["next_contact_date"] = (date.today() + timedelta(days=3)).isoformat()
            upsert_lead(address, update_fields)
            increment_todays_call_count(1)  # real spend counter — only on actual success
            calls_made += 1

            # This is the actual concurrency fix: Vapi returns the TRUE
            # current active-call count across your whole account in every
            # trigger response, not just what this script has fired. Instead
            # of blindly dispatching a fixed batch regardless of how many
            # calls from a PREVIOUS run are still ongoing, use this live
            # number to stop exactly at your real target — no more, no less.
            limits = result.get("subscriptionLimits", {})
            concurrency_limit = limits.get("concurrencyLimit")
            remaining = limits.get("remainingConcurrentCalls")
            if concurrency_limit is not None and remaining is not None:
                currently_active = concurrency_limit - remaining
                print(f"  Called {address} (attempt {new_count}) — Vapi call ID: {result.get('id')} "
                      f"— {currently_active}/{TARGET_CONCURRENT_CALLS} target concurrency now active")
                if currently_active >= TARGET_CONCURRENT_CALLS:
                    print(f"  Reached target concurrency ({TARGET_CONCURRENT_CALLS}) — stopping for this run")
                    break
            else:
                # Vapi didn't return the fields this run — fall back to the
                # simple count of calls WE'VE placed this run, safer than
                # dispatching with no limit at all.
                print(f"  Called {address} (attempt {new_count}) — Vapi call ID: {result.get('id')}")
                if calls_made >= TARGET_CONCURRENT_CALLS:
                    print(f"  Reached fallback batch limit ({TARGET_CONCURRENT_CALLS}) for this run")
                    break
        except Exception as e:
            upsert_lead(address, {"#_calls": new_count})
            print(f"  FAILED to call {address} (still counted as attempt {new_count}): {e}")
            continue
