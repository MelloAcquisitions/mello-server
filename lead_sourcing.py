"""
Automated daily lead sourcing — pulls distressed-owner leads using BatchData's
quicklist filters (the same stacking strategy PropStream/BatchLeads are known
for: absentee owner + tax delinquent + high equity = the highest-motivation
segment). Also skip-traces in the SAME call, since BatchData's property
search supports a skipTrace option that returns phone numbers directly —
no separate skip-trace call needed.

This uses the SAME BatchData account and permissions already set up earlier
(property-search + phone-tcpa + phone-dnc + property-skip-trace), so no new
account or subscription needed.

SETUP: same BATCHDATA_API_KEY environment variable as batchdata_lookup.py

This is designed to plug into the 8am phase of the future orchestrator
("central brain") — for now it's a standalone script you can run manually
or wire into a scheduled job later.
"""

import os
import requests

BATCHDATA_API_KEY = os.environ.get("BATCHDATA_API_KEY")
BASE_URL = "https://api.batchdata.com/api/v1/property/search"


def get_daily_leads(
    city: str,
    state: str,
    quick_lists: list = None,
    limit: int = 15,
) -> dict:
    """
    Pulls a stacked distressed-owner list with contact info included.

    quick_lists: which BatchData quicklist filters to stack. Defaults to the
    three highest-motivation indicators — combining them targets owners who
    check multiple distress boxes at once, not just one.
      - absenteeOwner: owner doesn't live at the property
      - taxDefault: behind on property taxes
      - highEquity: owns significantly more than they owe

    limit: how many leads to pull (matches your ~10-15/day target)
    """
    if quick_lists is None:
        quick_lists = ["absenteeOwner", "taxDefault", "highEquity"]

    if not BATCHDATA_API_KEY:
        raise RuntimeError("BATCHDATA_API_KEY environment variable is not set.")

    headers = {
        "Authorization": f"Bearer {BATCHDATA_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "searchCriteria": {
            "quickLists": quick_lists,
            "general": {
                # city/state filtering — adjust field names if BatchData's
                # actual schema differs; verify against a live test call
            },
        },
        "options": {
            "take": limit,
            "skip": 0,
            "skipTrace": True,  # pulls phone numbers in the SAME call
        },
    }
    # City/state go in searchCriteria per BatchData's address-based filtering
    payload["searchCriteria"]["city"] = city
    payload["searchCriteria"]["state"] = state

    response = requests.post(BASE_URL, headers=headers, json=payload, timeout=30)
    if not response.ok:
        print(f"BatchData returned an error ({response.status_code}):")
        print(response.text)
    response.raise_for_status()
    return response.json()


def extract_lead_summary(raw_result: dict) -> list:
    """
    Trims BatchData's large raw response down to just what a new Airtable
    lead record needs: address, owner name, phone, and which distress
    signals triggered the match — useful context for the agent's opening call.
    """
    properties = raw_result.get("results", {}).get("properties", [])
    leads = []

    for prop in properties:
        address = prop.get("address", {})
        owner = prop.get("owner", {})
        phone_numbers = owner.get("phoneNumbers", [])
        # Prefer a reachable, non-DNC number if one exists
        best_phone = next(
            (p["number"] for p in phone_numbers if p.get("reachable") and not p.get("dnc")),
            phone_numbers[0]["number"] if phone_numbers else None,
        )

        quick_lists = prop.get("quickLists", {})
        matched_signals = [k for k, v in quick_lists.items() if v is True]

        leads.append({
            "address": address.get("formattedStreet") or address.get("street"),
            "city": address.get("city"),
            "state": address.get("state"),
            "zip": address.get("zip"),
            "owner_name": owner.get("fullName"),
            "phone": best_phone,
            "source": "BatchData quicklist: " + ", ".join(matched_signals) if matched_signals else "BatchData",
        })

    return leads


if __name__ == "__main__":
    # Test pull — adjust city/state to your actual target market
    raw = get_daily_leads(city="Austin", state="TX", limit=15)
    leads = extract_lead_summary(raw)

    print(f"Pulled {len(leads)} leads:")
    for lead in leads:
        print(f"  {lead['address']}, {lead['city']}, {lead['state']} — "
              f"{lead['owner_name']} — {lead['phone']} — {lead['source']}")
