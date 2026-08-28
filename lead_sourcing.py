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
    markets: list = None,
    city: str = None,
    state: str = None,
    quick_lists: list = None,
    limit: int = 15,
) -> dict:
    """
    Pulls a stacked distressed-owner list with contact info included.

    markets: list of {"city": ..., "state": ...} dicts — your specific
    target zones, so leads stay concentrated where you actually have buyer
    demand instead of landing in random dead zones nationwide. Example:
        [{"city": "Austin", "state": "TX"}, {"city": "San Antonio", "state": "TX"}]
    If provided, this takes priority over the single city/state args below.

    city, state: single-market shorthand, kept for backwards compatibility —
    equivalent to markets=[{"city": city, "state": state}].

    quick_lists: which BatchData quicklist filters to stack. Values
    confirmed against BatchData's own docs — kebab-case, not camelCase:
      - absentee-owner: owner doesn't live at the property
      - tax-default: behind on property taxes
      - high-equity: owns significantly more than they owe

    limit: how many leads to pull PER MARKET (matches your ~10-15/day target
    per zone — if you list 3 markets, expect roughly 3x this many total)
    """
    if markets is None:
        if city and state:
            markets = [{"city": city, "state": state}]
        else:
            raise ValueError("Provide either markets=[...] or both city and state")

    if quick_lists is None:
        quick_lists = ["absentee-owner", "tax-default", "high-equity"]

    if not BATCHDATA_API_KEY:
        raise RuntimeError("BATCHDATA_API_KEY environment variable is not set.")

    headers = {
        "Authorization": f"Bearer {BATCHDATA_API_KEY}",
        "Content-Type": "application/json",
    }

    all_properties = []
    for market in markets:
        payload = {
            "searchCriteria": {
                "quickLists": quick_lists,
                # Nested {"equals": ...} structure — confirmed against
                # BatchData's own documented searchCriteria schema. The
                # earlier flat "city"/"state" keys were silently ignored,
                # which is why leads came back scattered nationwide instead
                # of confined to the target market.
                "address": {
                    "city": {"equals": market["city"]},
                    "state": {"equals": market["state"]},
                },
            },
            "options": {
                "take": limit,
                "skip": 0,
                "skipTrace": True,  # pulls phone numbers in the SAME call
            },
        }

        response = requests.post(BASE_URL, headers=headers, json=payload, timeout=30)
        if not response.ok:
            print(f"BatchData returned an error for {market} ({response.status_code}):")
            print(response.text)
        response.raise_for_status()

        result = response.json()
        properties = result.get("results", {}).get("properties", [])
        print(f"  {market['city']}, {market['state']}: {len(properties)} properties found")
        all_properties.extend(properties)

    return {"results": {"properties": all_properties}}


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
        # Only ever select a reachable, non-DNC, non-litigator number.
        # NO fallback to a risky number if no clean one exists — skip the
        # lead entirely rather than ever calling a DNC-listed or flagged
        # TCPA litigator number. This was a real gap: the previous fallback
        # would silently pick ANY number, including a flagged one, if no
        # clean option existed.
        best_phone = next(
            (p["number"] for p in phone_numbers
             if p.get("reachable") and not p.get("dnc") and not p.get("litigator")),
            None,
        )

        if not best_phone:
            print(f"  Skipping {address.get('formattedStreet') or address.get('street')} — "
                  f"no compliant phone number (DNC, litigator, or unreachable)")
            continue

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
    # Test pull — adjust markets to your actual target zones
    raw = get_daily_leads(markets=[{"city": "Austin", "state": "TX"}], limit=15)
    leads = extract_lead_summary(raw)

    print(f"Pulled {len(leads)} compliant leads:")
    for lead in leads:
        print(f"  {lead['address']}, {lead['city']}, {lead['state']} — "
              f"{lead['owner_name']} — {lead['phone']} — {lead['source']}")

    # IMPORTANT: run this once and actually look at the output below —
    # confirms the exact field names BatchData uses for DNC/litigator
    # status, so extract_lead_summary()'s filtering is checking the right
    # keys rather than a guessed name.
    print("\n=== RAW phoneNumbers structure (verify DNC/litigator field names) ===")
    properties = raw.get("results", {}).get("properties", [])
    if properties:
        raw_phones = properties[0].get("owner", {}).get("phoneNumbers", [])
        for phone in raw_phones[:3]:
            print(f"  {phone}")
