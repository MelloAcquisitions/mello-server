"""Render Cron Job: runs every 15 min, 8am-9pm. Schedule in Render: */15 8-20 * * *"""

from airtable_helpers import query_leads, upsert_lead
from orchestrator_lib import trigger_vapi_call, is_within_calling_hours, MAX_CALLS_PER_DISPATCH_RUN

if __name__ == "__main__":
    print("Checking for leads ready to call...")

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

        if not (address and state and phone):
            print(f"  Skipping incomplete record: {address}")
            continue

        if not is_within_calling_hours(state):
            print(f"  Skipping {address} — outside calling hours in {state} right now")
            continue

        try:
            call_context = {
                "seller_name": fields.get("owner_name", "there"),
                "property_address": address,
                "recommended_arv": str(fields.get("arv")),
            }
            result = trigger_vapi_call(phone, call_context)
            upsert_lead(address, {"status": "Contacted"})
            print(f"  Called {address} — Vapi call ID: {result.get('id')}")
            calls_made += 1
        except Exception as e:
            print(f"  FAILED to call {address}: {e}")
            continue
