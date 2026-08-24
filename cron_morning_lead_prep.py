"""Render Cron Job: runs once at 7:30 AM. Schedule in Render: 30 7 * * *"""

from lead_sourcing import get_daily_leads, extract_lead_summary
from airtable_helpers import upsert_lead, AirtableError
from orchestrator_lib import enrich_lead_with_valuation

if __name__ == "__main__":
    print("Sourcing new leads and enriching data...")
    raw_leads = get_daily_leads(city="Austin", state="TX", limit=15)  # adjust market
    new_leads = extract_lead_summary(raw_leads)

    for lead in new_leads:
        full_address = lead.get("address")
        if not full_address:
            continue

        try:
            valuation = enrich_lead_with_valuation(
                full_address, lead["city"], lead["state"], lead["zip"]
            )
            fields = {
                "owner_name": lead.get("owner_name", ""),
                "phone": lead.get("phone", ""),
                "source": lead.get("source", ""),
                "status": "New",
                "arv": valuation["recommended_arv"],
                "state": lead["state"],
            }
            upsert_lead(full_address, fields)
            print(f"  Saved to Airtable: {full_address} — ARV: {valuation['recommended_arv']}")
        except (AirtableError, Exception) as e:
            print(f"  FAILED to process {full_address}: {e}")
            continue
