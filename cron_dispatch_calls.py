"""
Render Cron Job: every 15 min, 8am-9pm Monterrey. Schedule (UTC): */15 14-23,0-2 * * *

REQUIRED ENV ON THIS JOB (Render crons inherit nothing from the web service):
  AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME,
  VAPI_API_KEY, VAPI_ASSISTANT_ID,
  VAPI_PHONE_NUMBER_ID  (the TELNYX number id, not a stale Twilio one),
  MAX_CALLS_PER_DAY     (set to 10 for day one, not the code default)

CHANGES IN THIS CLEANUP
-----------------------
1. The daily cap is now re-checked INSIDE the loop. It was checked once at
   the start of a run, so a run beginning at 79/80 could still place a full
   batch and finish at 82.
2. The spend counter is incremented IMMEDIATELY after Vapi accepts the
   call, before any other write. It used to be incremented after an
   upsert_lead() that could fail — and if it did, a call that really was
   placed and really was billed never counted against the cap.
3. Opt-out suppression now compares NORMALIZED phone numbers. It compared
   raw stored strings, so the same person saved once as "(512) 555-1234"
   and once as "5125551234" would be suppressed under one record and dialled
   under the other. That is a compliance failure, not a data-quality one.
4. The daily cap is now keyed on the Monterrey date, not the UTC date. The
   UTC day rolled over at 6 PM local — in the middle of the calling window —
   so the counter reset and up to 2x MAX_CALLS_PER_DAY could go out in one
   calling day. See mello_time.py.
"""

import os
import sys
from datetime import timedelta

from airtable_helpers import (
    get_todays_call_count, increment_todays_call_count, normalize_phone,
    query_leads, require_config, upsert_lead,
)
from mello_time import today_local
from orchestrator_lib import (
    TARGET_CONCURRENT_CALLS, is_lead_exhausted, is_retry_due,
    is_within_calling_hours, trigger_vapi_call,
)

# Hard daily cap — a real circuit-breaker against runaway dialling. Note it
# is enforced against a counter that can only ever UNDER-count (see the
# atomicity note in airtable_helpers), so leave headroom.
MAX_CALLS_PER_DAY = int(os.environ.get("MAX_CALLS_PER_DAY", 80))


def main():
    require_config("VAPI_API_KEY", "VAPI_ASSISTANT_ID", "VAPI_PHONE_NUMBER_ID")

    print(f"Checking for leads ready to call ({today_local()} Monterrey)...")

    todays_count = get_todays_call_count()
    if todays_count >= MAX_CALLS_PER_DAY:
        print(f"  DAILY CAP REACHED ({todays_count}/{MAX_CALLS_PER_DAY}) — "
              f"stopping for safety. No calls placed this run.")
        return
    print(f"  {todays_count}/{MAX_CALLS_PER_DAY} calls made today")

    # Build the suppression set FIRST — never call a number that has ever
    # opted out, even if a "new" lead record exists for that address again.
    opted_out = query_leads("{status}='Opt Out'")
    opted_out_phones = {
        normalize_phone(r["fields"].get("phone"))
        for r in opted_out if normalize_phone(r["fields"].get("phone"))
    }
    print(f"  {len(opted_out_phones)} numbers permanently suppressed (opted out)")

    # AI-owned, still-workable statuses. Agreed / Rejected / Opt Out /
    # Human Call / Priority Follow-up / Exhausted / Closed are terminal or
    # human-owned and deliberately excluded.
    #
    # NOTE the {arv}!=BLANK() clause: leads without an ARV are silently
    # skipped here forever. cron_continuous_enrichment.py only backfills
    # ARV for leads whose status is still 'New', so a lead that reached
    # 'Contacted' with a blank ARV is stranded — it will never be called
    # again and never marked anything. Worth a periodic manual check until
    # enrichment covers every workable status.
    ready_leads = query_leads(
        "AND(OR({status}='New', {status}='Contacted', {status}='Qualified', "
        "{status}='Offer Made'), {arv}!=BLANK())"
    )
    print(f"  Found {len(ready_leads)} workable leads (New, Contacted, Qualified, Offer Made)")

    calls_made = 0
    for record in ready_leads:
        # Re-check the cap every iteration, not once per run.
        if todays_count + calls_made >= MAX_CALLS_PER_DAY:
            print(f"  DAILY CAP REACHED mid-run ({todays_count + calls_made}/"
                  f"{MAX_CALLS_PER_DAY}) — stopping.")
            break

        fields = record["fields"]
        address = fields.get("address")
        state = fields.get("state")
        phone = fields.get("phone")
        call_count = fields.get("#_calls", 0) or 0
        date_created = fields.get("date_created")
        next_contact_date = fields.get("next_contact_date")

        if not (address and state and phone and date_created):
            missing = [n for n, v in (("address", address), ("state", state),
                                      ("phone", phone), ("date_created", date_created)) if not v]
            print(f"  Skipping {address or record.get('id')} — missing {', '.join(missing)}")
            continue

        if normalize_phone(phone) in opted_out_phones:
            print(f"  SUPPRESSED — {address} matches a previously opted-out number, marking Opt Out")
            upsert_lead(address, {"status": "Opt Out"})
            continue

        if is_lead_exhausted(date_created, call_count, next_contact_date):
            print(f"  {address} exhausted its retry schedule ({call_count} attempts) — marking Exhausted")
            upsert_lead(address, {"status": "Exhausted"})
            continue

        if not is_retry_due(date_created, call_count, next_contact_date,
                            status=fields.get("status"), last_call_date=fields.get("last_call_date")):
            print(f"  Skipping {address} — not due for the next attempt yet")
            continue

        if not is_within_calling_hours(state):
            print(f"  Skipping {address} — outside calling hours in {state} right now")
            continue

        new_count = call_count + 1  # a failed dial is still an attempt, so a
                                    # permanently broken number does not get
                                    # retried every single cycle

        city = fields.get("city")
        zip_code = fields.get("zip")
        city_state_zip = ", ".join(
            p for p in [city, " ".join(p2 for p2 in [state, zip_code] if p2)] if p
        )
        full_property_address = f"{address}, {city_state_zip}" if city_state_zip else f"{address}, {state}"

        call_context = {
            "seller_name": fields.get("owner_name") or "there",
            "property_address": full_property_address,
            "recommended_arv": str(fields.get("arv")),
        }

        try:
            result = trigger_vapi_call(phone, call_context)
        except Exception as e:
            # The call never went out. Still count the attempt so a
            # permanently unreachable number ages out of the schedule.
            try:
                upsert_lead(address, {"#_calls": new_count})
            except Exception as inner:
                print(f"  (also failed to record the attempt for {address}: {inner})")
            print(f"  FAILED to call {address} (counted as attempt {new_count}): {e}")
            continue

        # The call is placed and billing. Count it against the cap FIRST —
        # before any write that could fail — so a placed call can never go
        # uncounted.
        calls_made += 1
        try:
            increment_todays_call_count(1)
        except Exception as e:
            print(f"  WARNING: call placed but the daily counter did not increment "
                  f"({e}). The cap will under-count today.")

        update_fields = {"#_calls": new_count}
        if next_contact_date:
            # Safety net: if a scheduled callback goes unanswered, don't let
            # it retry every 15 minutes forever just because the date has
            # passed. Push it forward a few days. If the call connects,
            # log_call_outcome overwrites this with the real answer.
            update_fields["next_contact_date"] = (today_local() + timedelta(days=3)).isoformat()
        try:
            upsert_lead(address, update_fields)
        except Exception as e:
            print(f"  WARNING: called {address} but could not update the record: {e}")

        # Vapi returns the TRUE current active-call count across the whole
        # account in every trigger response — not just what this script has
        # fired. Use it to stop at the real target instead of blindly
        # dispatching a fixed batch while calls from a previous run are
        # still ongoing.
        limits = result.get("subscriptionLimits", {}) or {}
        concurrency_limit = limits.get("concurrencyLimit")
        remaining = limits.get("remainingConcurrentCalls")
        if concurrency_limit is not None and remaining is not None:
            currently_active = concurrency_limit - remaining
            print(f"  Called {address} (attempt {new_count}) — Vapi call ID: {result.get('id')} "
                  f"— {currently_active}/{TARGET_CONCURRENT_CALLS} target concurrency active")
            if currently_active >= TARGET_CONCURRENT_CALLS:
                print(f"  Reached target concurrency ({TARGET_CONCURRENT_CALLS}) — stopping this run")
                break
        else:
            print(f"  Called {address} (attempt {new_count}) — Vapi call ID: {result.get('id')}")
            if calls_made >= TARGET_CONCURRENT_CALLS:
                print(f"  Reached fallback batch limit ({TARGET_CONCURRENT_CALLS}) for this run")
                break

    print(f"\nDone. {calls_made} call(s) placed this run. "
          f"Day total now ~{todays_count + calls_made}/{MAX_CALLS_PER_DAY}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)
