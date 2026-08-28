"""Render Cron Job: runs every 30 min, 8am-9pm. Schedule in Render: */30 8-20 * * *"""

from airtable_helpers import query_leads, upsert_lead
from orchestrator_lib import enrich_lead_with_valuation

# Same range as cron_morning_lead_prep.py — keep both filters in sync
MIN_ARV = 100000
MAX_ARV = 650000

if __name__ == "__main__":
    print("Running periodic enrichment check...")

    unenriched = query_leads("AND({status}='New', {arv}=BLANK())")
    print(f"  Found {len(unenriched)} leads needing enrichment")

    for record in unenriched:
        fields = record["fields"]
        address = fields.get("address")
        if not address:
            continue
        try:
            valuation = enrich_lead_with_valuation(
                address, fields.get("city", ""), fields.get("state", ""), fields.get("zip", "")
            )
            arv = valuation["recommended_arv"]

            if not (MIN_ARV <= arv <= MAX_ARV):
                print(f"  {address} — ARV ${arv:,} outside target range, marking Rejected")
                upsert_lead(address, {"arv": arv, "status": "Rejected"})
                continue

            upsert_lead(address, {"arv": arv})
            print(f"  Enriched: {address} — ARV: {arv}")
        except Exception as e:
            print(f"  FAILED to enrich {address}: {e}")
            continue
