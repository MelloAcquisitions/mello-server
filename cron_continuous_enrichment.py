"""
Render Cron Job: every 30 min, 8am-9pm Monterrey. Schedule (UTC): */30 14-23,0-2 * * *

REQUIRED ENV ON THIS JOB:
  AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_TABLE_NAME,
  RENTCAST_API_KEY, ZILLAPI_KEY

REQUIRED AIRTABLE COLUMN: `enrichment_attempts` (Number) on the Leads
table. If it does not exist, every write in the failure path below returns
422 — meaning the attempt counter never increments, the lead never gives
up, and this cron retries it every 30 minutes forever, which is the exact
failure the counter exists to prevent. Verify the column exists before
enabling this job.

CHANGES IN THIS CLEANUP
-----------------------
1. Shares the daily enrichment budget with cron_morning_lead_prep.py. This
   job runs 26 times a day; combined with morning prep it could exhaust
   RentCast's 50-request free MONTHLY tier in a single day.
2. The give-up path no longer overwrites call_transcript_summary — it
   appends. On a lead that had been called, that write was destroying the
   call history.
3. The enrichment_attempts write is now attempted FIRST in the failure
   path, so a lead still gets its counter bumped even if the longer
   status/notes write fails.
4. Now also enriches 'Contacted' leads with a blank ARV, not only 'New'.
   cron_dispatch_calls.py filters on {arv}!=BLANK(), so a lead that reached
   'Contacted' without an ARV was stranded: never enriched (this query
   excluded it) and never called (that filter excluded it).
"""

import os
import sys

from airtable_helpers import (
    append_notes, get_daily_log_value, increment_daily_log_field,
    query_leads, require_config, upsert_lead,
)
from mello_time import today_iso
from orchestrator_lib import enrich_lead_with_valuation

# Keep in sync with cron_morning_lead_prep.py.
MIN_ARV = int(os.environ.get("MIN_ARV", 100000))
MAX_ARV = int(os.environ.get("MAX_ARV", 650000))

MAX_ENRICHMENTS_PER_DAY = int(os.environ.get("MAX_ENRICHMENTS_PER_DAY", 15))
ENRICHMENT_COUNTER = "enrichments_today"

# Hard cap on retry attempts per lead. Without it, a lead RentCast can never
# value (bad address, no comps, no AVM data) is picked up EVERY 30 MINUTES
# FOREVER — the query only excludes leads that already have an ARV, and a
# lead that keeps failing never gets one. One or two stuck addresses can
# burn a month's free-tier allowance in a day.
MAX_ENRICHMENT_ATTEMPTS = int(os.environ.get("MAX_ENRICHMENT_ATTEMPTS", 3))


def main():
    require_config("RENTCAST_API_KEY")

    used_today = get_daily_log_value(ENRICHMENT_COUNTER, 0)
    budget = MAX_ENRICHMENTS_PER_DAY - used_today
    if budget <= 0:
        print(f"Enrichment budget spent for today ({int(used_today)}/"
              f"{MAX_ENRICHMENTS_PER_DAY}) — skipping this run.")
        return

    print(f"Running periodic enrichment check "
          f"(budget: {int(budget)}/{MAX_ENRICHMENTS_PER_DAY} left today)...")

    unenriched = query_leads(
        "AND(OR({status}='New', {status}='Contacted'), {arv}=BLANK())"
    )
    print(f"  Found {len(unenriched)} lead(s) needing enrichment")

    for record in unenriched:
        if budget <= 0:
            print("  Budget exhausted — stopping. Remaining leads will be picked "
                  "up on a later run.")
            break

        fields = record["fields"]
        address = fields.get("address")
        if not address:
            continue

        attempts_so_far = fields.get("enrichment_attempts", 0) or 0
        if attempts_so_far >= MAX_ENRICHMENT_ATTEMPTS:
            continue  # already given up on; the query should not return these
                      # once status flipped, but guard anyway

        try:
            budget -= 1
            increment_daily_log_field(ENRICHMENT_COUNTER, 1)

            valuation = enrich_lead_with_valuation(
                address, fields.get("city", ""), fields.get("state", ""), fields.get("zip", "")
            )
            arv = valuation.get("recommended_arv")

            if arv is None:
                # Explicit, catchable failure instead of a None-vs-int
                # TypeError from the range check below. This is what used to
                # retry forever with no attempt recorded.
                raise ValueError(
                    "RentCast/Zillow returned no usable ARV candidate "
                    "(no comps, no AVM estimate, no Zestimate)"
                )

            if not (MIN_ARV <= arv <= MAX_ARV):
                print(f"  {address} — ARV ${arv:,} outside target range, marking Rejected")
                upsert_lead(address, {"arv": arv, "status": "Rejected"})
                continue

            upsert_lead(address, {"arv": arv})
            print(f"  Enriched: {address} — ARV ${arv:,}")

        except Exception as e:
            new_attempts = attempts_so_far + 1
            print(f"  FAILED to enrich {address} "
                  f"(attempt {new_attempts}/{MAX_ENRICHMENT_ATTEMPTS}): {e}")

            # Record the attempt FIRST and on its own. If the richer write
            # below fails (bad field name, rate limit), the counter has still
            # moved and this lead still ages out instead of retrying forever.
            try:
                upsert_lead(address, {"enrichment_attempts": new_attempts})
            except Exception as counter_error:
                print(f"    WARNING: could not record the attempt "
                      f"({counter_error}). If this persists, check that the "
                      f"`enrichment_attempts` column exists on the Leads table "
                      f"— without it this lead WILL be retried forever.")
                continue

            if new_attempts >= MAX_ENRICHMENT_ATTEMPTS:
                try:
                    upsert_lead(address, {
                        "status": "Rejected",
                        "call_transcript_summary": append_notes(
                            fields.get("call_transcript_summary"),
                            f"[{today_iso()}] Enrichment failed {new_attempts}x, "
                            f"giving up: {e}",
                        ),
                    })
                    print(f"  Giving up on {address} after {new_attempts} failed "
                          f"attempts — marked Rejected, will not be retried")
                except Exception as final_error:
                    print(f"    Could not mark {address} Rejected: {final_error}")
            continue


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)
