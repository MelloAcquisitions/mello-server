"""Render Cron Job: runs once at 7:30 AM. Schedule in Render: 30 7 * * *"""

from lead_sourcing import get_compliant_leads
from airtable_helpers import upsert_lead, find_lead_record, AirtableError
from orchestrator_lib import enrich_lead_with_valuation

# Your specific target zones — add/remove markets here to focus on places
# with real investor demand instead of landing in dead zones nationwide.
TARGET_MARKETS = [
    {"city": "Austin", "state": "TX"},
    # {"city": "San Antonio", "state": "TX"},
    # {"city": "Dallas", "state": "TX"},
]

# Keep leads within your actual wholesale buyer pool's range — a $2M+
# property has a completely different buyer market and seller psychology
# than typical wholesale deals, and isn't something your buyer network
# can realistically absorb.
MIN_ARV = 100000
MAX_ARV = 650000

if __name__ == "__main__":
    print("Sourcing new leads and enriching data...")
    raw_leads = get_compliant_leads(markets=TARGET_MARKETS, target_count=15)
    new_leads = raw_leads

    for lead in new_leads:
        full_address = lead.get("address")
        if not full_address:
            continue

        # BatchData WILL surface the same property again on a later day if it
        # still matches the quicklist filters (still absentee, still
        # tax-delinquent, etc.) — this is expected, not a bug on their end.
        # Without this check, re-processing it would blindly reset an
        # existing lead's status back to "New", including ones already
        # marked Opt Out, Rejected, or Exhausted — which for Opt Out
        # specifically means re-contacting someone who explicitly asked to
        # be removed. Skip entirely if the address already exists, before
        # spending a RentCast/Zillapi lookup on it too.
        existing = find_lead_record(full_address)
        if existing:
            print(f"  Skipping {full_address} — already in Airtable as status "
                  f"'{existing['fields'].get('status', 'Unknown')}', not re-processing")
            continue

        try:
            valuation = enrich_lead_with_valuation(
                full_address, lead["city"], lead["state"], lead["zip"]
            )
            arv = valuation.get("recommended_arv")

            if arv is None:
                print(f"  Skipping {full_address} — RentCast/Zillow returned no usable ARV "
                      f"(no comps, no AVM, no Zestimate)")
                continue

            if not (MIN_ARV <= arv <= MAX_ARV):
                print(f"  Skipping {full_address} — ARV ${arv:,} outside target range "
                      f"(${MIN_ARV:,}-${MAX_ARV:,})")
                continue

            fields = {
                "owner_name": lead.get("owner_name", ""),
                "phone": lead.get("phone", ""),
                "source": lead.get("source", ""),
                "status": "New",
                "arv": arv,
                "state": lead["state"],
                "city": lead.get("city", ""),
                "zip": lead.get("zip", ""),
                # date_created intentionally NOT set here — it's a computed
                # "Created time" field in Airtable that auto-populates
                # itself; writing to it manually causes a 422 error.
            }
            upsert_lead(full_address, fields)
            print(f"  Saved to Airtable: {full_address} — ARV: {arv}")
        except (AirtableError, Exception) as e:
            print(f"  FAILED to process {full_address}: {e}")
            continue
