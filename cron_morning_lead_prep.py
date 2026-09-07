"""
Render Cron Job: 7:30 AM Monterrey. Schedule (UTC): 30 13 * * *

REQUIRED ENV ON THIS JOB:
  AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME,
  BATCHDATA_API_KEY, RENTCAST_API_KEY, ZILLAPI_KEY

CHANGES IN THIS CLEANUP
-----------------------
1. THE DUPLICATE CHECK NOW ALSO MATCHES BY PHONE. It only did an exact
   match on the street address BatchData returned. BatchData's
   `formattedStreet` is not guaranteed to be byte-identical to what was
   stored on a previous day ("6506 Clubway Ln" vs "6506 Clubway Lane"), so
   the check could miss and re-create the lead with status "New" — which
   for someone previously marked Opt Out means putting them straight back
   into the dial queue. The phone number is the stronger key and catches
   exactly that case.
2. A HARD ENRICHMENT BUDGET. Each lead costs 2 RentCast requests (AVM +
   sold comps) and 1 Zillapi request. RentCast's free tier is 50 requests
   PER MONTH. At the previous target of 15 leads/day that is 30 requests a
   day — the entire month's allowance gone in under two days, after which
   every enrichment fails and leads pile up unvalued with no obvious cause.
   MAX_ENRICHMENTS_PER_DAY now caps it, shared with
   cron_continuous_enrichment.py through the Daily Log table.
3. Leads missing city/state/zip are skipped rather than sent to RentCast as
   "6506 Clubway Ln, None, None None", which burns a request to get nothing.
4. `except (AirtableError, Exception)` reduced to `except Exception` — the
   first clause was dead, since Exception already catches AirtableError.
"""

import os
import sys

from airtable_helpers import (
    find_lead_by_phone, find_lead_record, get_daily_log_value,
    increment_daily_log_field, require_config, upsert_lead,
)
from lead_sourcing import get_compliant_leads
from orchestrator_lib import enrich_lead_with_valuation

# Target zones — add/remove markets here to concentrate on places with real
# investor demand instead of landing in dead zones nationwide.
TARGET_MARKETS = [
    {"city": "Austin", "state": "TX"},
    # {"city": "San Antonio", "state": "TX"},
    # {"city": "Dallas", "state": "TX"},
]

# Keep leads inside the actual wholesale buyer pool's range. A $2M property
# has a completely different buyer market and seller psychology than typical
# wholesale deals and is not something the buyer network can absorb.
MIN_ARV = int(os.environ.get("MIN_ARV", 100000))
MAX_ARV = int(os.environ.get("MAX_ARV", 650000))

TARGET_NEW_LEADS = int(os.environ.get("TARGET_NEW_LEADS", 15))

# Shared with cron_continuous_enrichment.py via the Daily Log table. See
# the cost note in the module docstring — on RentCast's free tier (50/month)
# this needs to be about 1, not 15. Raise it once you are on a paid plan.
MAX_ENRICHMENTS_PER_DAY = int(os.environ.get("MAX_ENRICHMENTS_PER_DAY", 15))
ENRICHMENT_COUNTER = "enrichments_today"


def main():
    require_config("BATCHDATA_API_KEY", "RENTCAST_API_KEY")

    used_today = get_daily_log_value(ENRICHMENT_COUNTER, 0)
    budget = MAX_ENRICHMENTS_PER_DAY - used_today
    if budget <= 0:
        print(f"  Enrichment budget already spent today ({used_today}/"
              f"{MAX_ENRICHMENTS_PER_DAY}) — not sourcing. Raise "
              f"MAX_ENRICHMENTS_PER_DAY once off RentCast's free tier.")
        return

    print(f"Sourcing new leads and enriching data "
          f"(budget: {int(budget)}/{MAX_ENRICHMENTS_PER_DAY} enrichments left today)...")

    raw_leads = get_compliant_leads(markets=TARGET_MARKETS, target_count=TARGET_NEW_LEADS)
    print(f"  {len(raw_leads)} compliant lead(s) returned by BatchData")

    saved = 0
    for lead in raw_leads:
        if budget <= 0:
            print("  Enrichment budget exhausted for today — stopping here. "
                  "The rest will be picked up by continuous enrichment tomorrow.")
            break

        street = lead.get("address")
        if not street:
            continue

        # BatchData WILL surface the same property again on a later day if it
        # still matches the quicklist filters (still absentee, still tax
        # delinquent). That is expected, not a bug on their end. Without this
        # check, re-processing resets an existing lead's status back to
        # "New" — including ones marked Opt Out, Rejected or Exhausted, which
        # for Opt Out means re-contacting someone who explicitly asked to be
        # removed.
        existing = find_lead_record(street)
        if not existing and lead.get("phone"):
            # Address strings drift between BatchData responses; the phone
            # number does not. This is the check that actually protects an
            # opt-out from being resurrected under a slightly different
            # street spelling.
            existing = find_lead_by_phone(lead["phone"])
        if existing:
            print(f"  Skipping {street} — already in Airtable as "
                  f"'{existing['fields'].get('status', 'Unknown')}', not re-processing")
            continue

        if not (lead.get("city") and lead.get("state") and lead.get("zip")):
            print(f"  Skipping {street} — incomplete address from BatchData "
                  f"(city/state/zip missing); a valuation lookup would be wasted")
            continue

        try:
            budget -= 1
            increment_daily_log_field(ENRICHMENT_COUNTER, 1)

            valuation = enrich_lead_with_valuation(
                street, lead["city"], lead["state"], lead["zip"]
            )
            arv = valuation.get("recommended_arv")

            if arv is None:
                print(f"  Skipping {street} — no usable ARV "
                      f"(no comps, no AVM, no Zestimate)")
                continue

            if not (MIN_ARV <= arv <= MAX_ARV):
                print(f"  Skipping {street} — ARV ${arv:,} outside target range "
                      f"(${MIN_ARV:,}-${MAX_ARV:,})")
                continue

            upsert_lead(street, {
                "owner_name": lead.get("owner_name", ""),
                "phone": lead.get("phone", ""),
                "source": lead.get("source", ""),
                "status": "New",
                "arv": arv,
                "state": lead["state"],
                "city": lead.get("city", ""),
                "zip": lead.get("zip", ""),
                # date_created is deliberately NOT set — it is a computed
                # "Created time" field in Airtable that populates itself;
                # writing to it returns 422.
            })
            saved += 1
            print(f"  Saved: {street} — ARV ${arv:,}")

        except Exception as e:
            print(f"  FAILED to process {street}: {e}")
            continue

    print(f"\nDone. {saved} new lead(s) saved.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)
