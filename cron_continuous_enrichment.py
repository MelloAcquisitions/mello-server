"""Render Cron Job: runs every 30 min, 8am-9pm. Schedule in Render: */30 8-20 * * *"""

from airtable_helpers import query_leads, upsert_lead
from orchestrator_lib import enrich_lead_with_valuation

# Same range as cron_morning_lead_prep.py — keep both filters in sync
MIN_ARV = 100000
MAX_ARV = 650000

# HARD CAP on retry attempts per lead. Without this, a lead RentCast can
# never value (bad address, no comps, no AVM data) gets picked up by this
# EVERY 30 MINUTES FOREVER — the query only excludes leads that already
# have an arv, and a lead that keeps failing never gets one. That alone
# can burn through RentCast's free-tier monthly cap in a single day from
# just one or two stuck addresses. Requires an "enrichment_attempts"
# (Number) column on the Leads table.
MAX_ENRICHMENT_ATTEMPTS = 3

if __name__ == "__main__":
    print("Running periodic enrichment check...")

    unenriched = query_leads("AND({status}='New', {arv}=BLANK())")
    print(f"  Found {len(unenriched)} leads needing enrichment")

    for record in unenriched:
        fields = record["fields"]
        address = fields.get("address")
        attempts_so_far = fields.get("enrichment_attempts", 0)
        if not address:
            continue

        try:
            valuation = enrich_lead_with_valuation(
                address, fields.get("city", ""), fields.get("state", ""), fields.get("zip", "")
            )
            arv = valuation.get("recommended_arv")

            if arv is None:
                # Explicit, catchable failure instead of a None-vs-int
                # TypeError from the range check below — this is what was
                # silently retrying forever with no attempt tracked.
                raise ValueError(
                    "RentCast/Zillow returned no usable ARV candidate "
                    "(no comps, no AVM estimate, no Zestimate)"
                )

            if not (MIN_ARV <= arv <= MAX_ARV):
                print(f"  {address} — ARV ${arv:,} outside target range, marking Rejected")
                upsert_lead(address, {"arv": arv, "status": "Rejected"})
                continue

            upsert_lead(address, {"arv": arv})
            print(f"  Enriched: {address} — ARV: {arv}")

        except Exception as e:
            new_attempts = attempts_so_far + 1
            print(f"  FAILED to enrich {address} (attempt {new_attempts}/{MAX_ENRICHMENT_ATTEMPTS}): {e}")

            if new_attempts >= MAX_ENRICHMENT_ATTEMPTS:
                upsert_lead(address, {
                    "status": "Rejected",
                    "enrichment_attempts": new_attempts,
                    "call_transcript_summary": f"Enrichment failed {new_attempts}x, giving up: {e}",
                })
                print(f"  Giving up on {address} after {new_attempts} failed attempts — marked Rejected, "
                      f"will not be retried again")
            else:
                upsert_lead(address, {"enrichment_attempts": new_attempts})
            continue
