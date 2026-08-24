"""Render Cron Job: runs every 15 min, 8am-9pm. Schedule in Render: */15 8-20 * * *"""

from airtable_helpers import query_leads, upsert_lead
from orchestrator_lib import (
    trigger_vapi_call, is_within_calling_hours, MAX_CALLS_PER_DISPATCH_RUN,
    is_retry_due, is_lead_exhausted,
)

if __name__ == "__main__":
    print("Checking for leads ready to call...")

    # Build a suppression set FIRST — never call a number that's ever opted out,
    # even if a "new" lead record technically exists for that address again.
    opted_out = query_leads("{status}='Opt Out'")
    opted_out_phones = {r["fields"].get("phone") for r in opted_out if r["fields"].get("phone")}
    print(f"  {len(opted_out_phones)} numbers permanently suppressed (opted out)")

    ready_leads = query_leads("AND({status}='New', {arv}!=BLANK())")
    print(f"  Found {len(ready_leads)} enriched leads with status New")

    calls_made = 0
    for record in ready_leads:
        if calls_made >= MAX_CALLS_PER_DISPATCH_RUN:
            print(f"  Reached batch limit ({MAX_CALLS_PER_DISPATCH_RUN}) for this run")
            break

        fields = record["fields"]
        address = fields.get("address")
        state = fields.get("state")
        phone = fields.get("phone")
        call_count = fields.get("#_calls", 0)
        date_created = fields.get("date_created")

        if not (address and state and phone and date_created):
            print(f"  Skipping incomplete record (missing date_created?): {address}")
            continue

        if phone in opted_out_phones:
            print(f"  SUPPRESSED — {address} matches a previously opted-out number, marking Opt Out")
            upsert_lead(address, {"status": "Opt Out"})
            continue

        if is_lead_exhausted(date_created, call_count):
            print(f"  {address} has exhausted its retry schedule ({call_count} attempts) — marking Exhausted")
            upsert_lead(address, {"status": "Exhausted"})
            continue

        if not is_retry_due(date_created, call_count):
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
            call_context = {
                "seller_name": fields.get("owner_name", "there"),
                "property_address": address,
                "recommended_arv": str(fields.get("arv")),
            }
            result = trigger_vapi_call(phone, call_context)
            # status stays "New" — we don't yet know if anyone actually
            # answered. If the call connects, the live agent's own
            # log_call_outcome call overwrites this with the true outcome.
            upsert_lead(address, {"#_calls": new_count})
            print(f"  Called {address} (attempt {new_count}) — Vapi call ID: {result.get('id')}")
            calls_made += 1
        except Exception as e:
            upsert_lead(address, {"#_calls": new_count})
            print(f"  FAILED to call {address} (still counted as attempt {new_count}): {e}")
            continue
