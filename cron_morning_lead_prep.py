"""Render Cron Job: runs once at 7:30 AM. Schedule in Render: 30 7 * * *"""

from lead_sourcing import get_daily_leads, extract_lead_summary
from airtable_helpers import upsert_lead, AirtableError
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
    raw_leads = get_daily_leads(markets=TARGET_MARKETS, limit=15)
    new_leads = extract_lead_summary(raw_leads)

    for lead in new_leads:
        full_address = lead.get("address")
        if not full_address:
            continue

        try:
            valuation = enrich_lead_with_valuation(
                full_address, lead["city"], lead["state"], lead["zip"]
            )
            arv = valuation["recommended_arv"]

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
                # date_created intentionally NOT set here — it's a computed
                # "Created time" field in Airtable that auto-populates
                # itself; writing to it manually causes a 422 error.
            }
            upsert_lead(full_address, fields)
            print(f"  Saved to Airtable: {full_address} — ARV: {arv}")
        except (AirtableError, Exception) as e:
            print(f"  FAILED to process {full_address}: {e}")
            continue
