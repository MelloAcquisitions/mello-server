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

VERIFY ON FIRST REAL RUN: sessionId is now included on every request (see
BATCHDATA_SESSION_ID below) specifically so BatchData excludes properties
already delivered to you in a past request, avoiding being billed again for
a property that still matches the same quicklist filters day after day.
This is confirmed as a real BatchData feature, but if a property you KNOW
was already returned yesterday shows up again after this change, the
exclusion behavior may specifically require their v2 endpoint plus
useCursorPagination rather than working on this v1 endpoint — flag that to
me if you see it, and get the v2 endpoint details from BatchData support so
I can migrate this properly.

This is designed to plug into the 8am phase of the future orchestrator
("central brain") — for now it's a standalone script you can run manually
or wire into a scheduled job later.
"""

import os
import requests

from airtable_helpers import increment_daily_log_field

BATCHDATA_API_KEY = os.environ.get("BATCHDATA_API_KEY")
BASE_URL = "https://api.batchdata.com/api/v1/property/search"

# Persistent across EVERY run, forever — this is what makes BatchData's
# session-based delivery exclude properties already returned to you in a
# past request, so a still-matching property doesn't get billed again just
# because it still qualifies for the same quicklist filters. Confirmed
# field name: "sessionId" (BatchData's own support, per their docs on
# search sessions / incremental delivery). The exact value doesn't matter —
# it just needs to stay IDENTICAL across every request, forever. Do not
# change this string once it's live, or you lose the delivery history tied
# to it and start getting billed for repeats again.
BATCHDATA_SESSION_ID = os.environ.get("BATCHDATA_SESSION_ID", "mello-acquisitions-daily-sourcing-v1")


def get_daily_leads(
    markets: list = None,
    city: str = None,
    state: str = None,
    quick_lists: list = None,
    limit: int = 15,
    skip: int = 0,
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

    limit: how many leads to pull PER MARKET per page
    skip: pagination offset — used by get_compliant_leads() below to fetch
    additional pages when earlier ones don't yield enough compliant leads
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
                "skip": skip,
                "skipTrace": True,  # pulls phone numbers in the SAME call
                "sessionId": BATCHDATA_SESSION_ID,  # excludes previously-delivered properties across ALL past requests using this session — this is the actual cost fix
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

        # BatchData bills per property record returned, plus extra per
        # skip-trace phone match — NOT per API call (confirmed from their
        # docs). Track the real billing units instead of call count. The
        # exact location of resultCount/skipTraceMatchCount in the response
        # isn't confirmed against a live payload yet — printed below so you
        # can verify on the next real run and I can correct the path if needed.
        meta = result.get("meta") or result.get("results", {}).get("meta") or {}
        if meta:
            print(f"  BatchData meta for {market}: {meta}")
        result_count = meta.get("resultCount", len(properties))
        skiptrace_match_count = meta.get("skipTraceMatchCount", 0)

        try:
            increment_daily_log_field("batchdata_calls_today", 1)
            increment_daily_log_field("batchdata_properties_today", result_count)
            increment_daily_log_field("batchdata_skiptrace_matches_today", skiptrace_match_count)
        except Exception as e:
            print(f"Failed to record BatchData usage for cost tracking: {e}")

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


def get_compliant_leads(markets: list, target_count: int = 15, quick_lists: list = None,
                          page_size: int = 20, max_pages: int = 4) -> list:
    """
    Keeps fetching additional pages from BatchData until it has actually
    gathered target_count COMPLIANT leads (post DNC/litigator/reachability
    filtering) — not just target_count raw properties. Roughly 30-40% of
    raw results typically get filtered out, so a single fixed-size request
    reliably falls short of what you actually wanted.

    max_pages caps the worst case cost/runtime if a market genuinely has
    few compliant leads available — after this many pages, returns
    whatever was found rather than fetching indefinitely.
    """
    compliant_leads = []
    skip = 0

    for page in range(max_pages):
        if len(compliant_leads) >= target_count:
            break

        raw = get_daily_leads(markets=markets, quick_lists=quick_lists, limit=page_size, skip=skip)
        new_leads = extract_lead_summary(raw)
        compliant_leads.extend(new_leads)
        print(f"  Page {page + 1}: {len(new_leads)} compliant leads this page, "
              f"{len(compliant_leads)}/{target_count} total so far")

        skip += page_size

    return compliant_leads[:target_count]


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
