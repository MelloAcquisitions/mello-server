"""Render Cron Job: runs once at 9:00 PM. Schedule in Render: 0 21 * * *"""

from airtable_helpers import query_leads

if __name__ == "__main__":
    print("Wrapping up the day...")

    all_leads = query_leads("TRUE()")  # every record — used purely for a status count
    status_counts = {}
    for record in all_leads:
        status = record["fields"].get("status", "Unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    total_calls = sum(r["fields"].get("#_calls", 0) for r in all_leads)

    print("\n=== Daily Summary ===")
    print(f"Total leads in system: {len(all_leads)}")
    print(f"Total call attempts (all-time): {total_calls}")
    for status, count in sorted(status_counts.items()):
        print(f"  {status}: {count}")

    # Surface anything that genuinely needs your attention tonight
    agreed = [r for r in all_leads if r["fields"].get("status") == "Agreed"]
    if agreed:
        print(f"\n⚠️  {len(agreed)} lead(s) with status 'Agreed' — need contract follow-up:")
        for r in agreed:
            print(f"  {r['fields'].get('address')} — {r['fields'].get('owner_name')} — "
                  f"{r['fields'].get('phone')}")
    else:
        print("\nNo leads currently awaiting contract follow-up.")

    human_call = [r for r in all_leads if r["fields"].get("status") == "Human Call"]
    if human_call:
        print(f"\n📞 {len(human_call)} lead(s) requested a human callback:")
        for r in human_call:
            print(f"  {r['fields'].get('address')} — {r['fields'].get('phone')}")

    priority = [r for r in all_leads if r["fields"].get("status") == "Priority Follow-up"]
    if priority:
        print(f"\n🔥 {len(priority)} lead(s) flagged Priority Follow-up — weak number, strong reason to sell, worth your personal touch:")
        for r in priority:
            print(f"  {r['fields'].get('address')} — {r['fields'].get('owner_name')} — {r['fields'].get('phone')}")

    offer_made = [r for r in all_leads if r["fields"].get("status") == "Offer Made"]
    if offer_made:
        print(f"\n💬 {len(offer_made)} lead(s) with a number on the table (Offer Made) — still in the normal retry cycle, informational only.")
