"""
Automated daily lead sourcing — pulls distressed-owner leads using
BatchData's quicklist filters (the stacking strategy PropStream/BatchLeads
are known for: absentee owner + tax delinquent + high equity = the
highest-motivation segment). Skip-traces in the SAME call, since BatchData's
property search supports a skipTrace option that returns phone numbers
directly — no separate skip-trace request needed.

Uses the same BatchData account and permissions already set up
(property-search + phone-tcpa + phone-dnc + property-skip-trace).

SETUP: BATCHDATA_API_KEY

VERIFY ON FIRST REAL RUN: sessionId is included on every request so
BatchData excludes properties already delivered in a past request, avoiding
being billed again for a property that still matches the same quicklist
filters day after day. If a property you KNOW was returned yesterday shows
up again, the exclusion may require their v2 endpoint plus
useCursorPagination rather than this v1 one — get the v2 details from
BatchData support before migrating.

CHANGES IN THIS CLEANUP
-----------------------
1. get_compliant_leads() now STOPS when a page returns no raw properties.
   It ran the full max_pages regardless, paying for empty pages after a
   market was exhausted.
2. Usage counters are incremented once per market page rather than being
   silently skipped when `meta` is absent, and a failed counter write no
   longer looks like a successful one.
3. Leads without a usable city/state/zip are dropped here rather than
   downstream, where they would have burned a RentCast request to return
   nothing.
"""

import os

import requests

from airtable_helpers import increment_daily_log_field

BATCHDATA_API_KEY = os.environ.get("BATCHDATA_API_KEY")
BASE_URL = "https://api.batchdata.com/api/v1/property/search"

# Persistent across EVERY run, forever. This is what makes BatchData's
# session-based delivery exclude properties already returned to you, so a
# still-matching property is not billed again just because it still
# qualifies. The exact value does not matter — it must stay IDENTICAL
# forever. Change it and you lose the delivery history tied to it and start
# paying for repeats again.
BATCHDATA_SESSION_ID = os.environ.get(
    "BATCHDATA_SESSION_ID", "mello-acquisitions-daily-sourcing-v1"
)


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

    markets: list of {"city", "state"} dicts — target zones, so leads stay
    concentrated where there is real buyer demand. Takes priority over the
    single city/state args.

    quick_lists: which BatchData quicklist filters to stack. Values
    confirmed against BatchData's docs — kebab-case, not camelCase:
      absentee-owner, tax-default, high-equity

    limit: leads PER MARKET per page.  skip: pagination offset.
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
                # Nested {"equals": ...} structure, confirmed against
                # BatchData's documented searchCriteria schema. The earlier
                # flat "city"/"state" keys were silently ignored, which is
                # why leads came back scattered nationwide instead of
                # confined to the target market.
                "address": {
                    "city": {"equals": market["city"]},
                    "state": {"equals": market["state"]},
                },
            },
            "options": {
                "take": limit,
                "skip": skip,
                "skipTrace": True,          # phone numbers in the SAME call
                "sessionId": BATCHDATA_SESSION_ID,  # the actual cost fix
            },
        }

        response = requests.post(BASE_URL, headers=headers, json=payload, timeout=30)
        if not response.ok:
            print(f"BatchData error for {market} ({response.status_code}): {response.text[:400]}")
        response.raise_for_status()

        result = response.json()
        properties = result.get("results", {}).get("properties", []) or []
        print(f"  {market['city']}, {market['state']}: {len(properties)} properties found")
        all_properties.extend(properties)

        # BatchData bills per property record returned, plus extra per
        # skip-trace phone match — NOT per API call. Track the real billing
        # units. The exact location of resultCount/skipTraceMatchCount in
        # the response is not confirmed against a live payload; the meta
        # object is printed so it can be corrected on the next real run.
        meta = result.get("meta") or result.get("results", {}).get("meta") or {}
        if meta:
            print(f"  BatchData meta for {market}: {meta}")
        result_count = meta.get("resultCount", len(properties))
        skiptrace_match_count = meta.get(
            "skipTraceMatchCount",
            sum(1 for p in properties if (p.get("owner") or {}).get("phoneNumbers")),
        )

        for field, amount in (
            ("batchdata_calls_today", 1),
            ("batchdata_properties_today", result_count),
            ("batchdata_skiptrace_matches_today", skiptrace_match_count),
        ):
            try:
                increment_daily_log_field(field, amount)
            except Exception as e:
                print(f"  Failed to record {field} for cost tracking: {e}")

    return {"results": {"properties": all_properties}}


def extract_lead_summary(raw_result: dict) -> list:
    """
    Trims BatchData's large raw response to what a new Airtable lead record
    needs: address, owner name, phone, and which distress signals matched.
    """
    properties = raw_result.get("results", {}).get("properties", []) or []
    leads = []

    for prop in properties:
        address = prop.get("address", {}) or {}
        owner = prop.get("owner", {}) or {}
        phone_numbers = owner.get("phoneNumbers", []) or []

        # Only ever select a reachable, non-DNC, non-litigator number. NO
        # fallback to a risky number if no clean one exists — skip the lead
        # entirely rather than call a DNC-listed or flagged TCPA litigator
        # number. The previous fallback would silently pick ANY number,
        # including a flagged one, when no clean option existed.
        best_phone = next(
            (p["number"] for p in phone_numbers
             if p.get("reachable") and not p.get("dnc") and not p.get("litigator")),
            None,
        )

        street = address.get("formattedStreet") or address.get("street")

        if not best_phone:
            print(f"  Skipping {street} — no compliant phone number "
                  f"(DNC, litigator, or unreachable)")
            continue

        # An incomplete address cannot be valued and cannot be put on a
        # contract. Dropping it here saves a RentCast request that would
        # return nothing.
        if not (street and address.get("city") and address.get("state") and address.get("zip")):
            print(f"  Skipping {street or '(no street)'} — incomplete address from BatchData")
            continue

        quick_lists = prop.get("quickLists", {}) or {}
        matched_signals = [k for k, v in quick_lists.items() if v is True]

        leads.append({
            "address": street,
            "city": address.get("city"),
            "state": address.get("state"),
            "zip": address.get("zip"),
            "owner_name": owner.get("fullName"),
            "phone": best_phone,
            "source": ("BatchData quicklist: " + ", ".join(matched_signals))
                      if matched_signals else "BatchData",
        })

    return leads


def get_compliant_leads(markets: list, target_count: int = 15, quick_lists: list = None,
                        page_size: int = 20, max_pages: int = 4) -> list:
    """
    Keeps fetching pages until it has target_count COMPLIANT leads (post
    DNC/litigator/reachability/completeness filtering) — not target_count
    raw properties. Roughly 30-40% of raw results get filtered out, so a
    single fixed-size request reliably falls short.

    max_pages caps the worst-case cost if a market genuinely has few
    compliant leads. It also now stops the moment a page returns no raw
    properties at all, instead of paying for three more empty pages.
    """
    compliant_leads = []
    skip = 0

    for page in range(max_pages):
        if len(compliant_leads) >= target_count:
            break

        raw = get_daily_leads(markets=markets, quick_lists=quick_lists,
                              limit=page_size, skip=skip)
        raw_count = len(raw.get("results", {}).get("properties", []) or [])
        if raw_count == 0:
            print(f"  Page {page + 1}: no properties returned — market exhausted, stopping.")
            break

        new_leads = extract_lead_summary(raw)
        compliant_leads.extend(new_leads)
        print(f"  Page {page + 1}: {len(new_leads)} compliant of {raw_count} raw, "
              f"{len(compliant_leads)}/{target_count} total so far")

        skip += page_size

    return compliant_leads[:target_count]


if __name__ == "__main__":
    raw = get_daily_leads(markets=[{"city": "Austin", "state": "TX"}], limit=15)
    leads = extract_lead_summary(raw)

    print(f"\nPulled {len(leads)} compliant leads:")
    for lead in leads:
        print(f"  {lead['address']}, {lead['city']}, {lead['state']} — "
              f"{lead['owner_name']} — {lead['phone']} — {lead['source']}")

    # Run this once and actually read the output: it confirms the exact
    # field names BatchData uses for DNC/litigator status, so the filtering
    # above is checking real keys rather than guessed ones.
    print("\n=== RAW phoneNumbers structure (verify DNC/litigator field names) ===")
    properties = raw.get("results", {}).get("properties", [])
    if properties:
        for phone in (properties[0].get("owner", {}).get("phoneNumbers", []))[:3]:
            print(f"  {phone}")
