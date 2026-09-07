"""
test_call.py — place ONE real Vapi call, by hand.

Replaces test_first_call.py and test_call_only.py, which were the same
script twice: one that fetched a live valuation first and one that used a
hardcoded cached ARV to avoid burning RentCast credits. That is a flag, not
a second file. Both also had a real personal phone number committed in
them; this takes the number as an argument instead.

USAGE
    export VAPI_API_KEY=... VAPI_ASSISTANT_ID=... VAPI_PHONE_NUMBER_ID=...

    # Cheapest: no valuation lookup at all, ARV supplied by you.
    python tools/test_call.py --phone +15125551234 --arv 355000

    # Full path: fetch a real valuation first (costs 2 RentCast requests
    # out of a 50/MONTH free tier, plus 1 Zillapi credit).
    export RENTCAST_API_KEY=... ZILLAPI_KEY=...
    python tools/test_call.py --phone +15125551234 --enrich

    # Optional overrides
    --address "6506 Clubway Ln" --city Austin --state TX --zip 78745
    --name "Test Seller"

WHAT TO WATCH FOR, per the open items in the handoff:
  - the agent proactively discloses it is an AI in the greeting
  - calculate_mao fires and there is no dead air after it returns
  - the agent does not revise its repair estimate down at your insistence
  - log_call_outcome fires on its own at a natural ending
  - EXACTLY ONE Airtable record is updated afterward (proves the
    full-address vs street-only fix), and prior notes are still there
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator_lib import trigger_vapi_call  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Place one test Vapi call.")
    parser.add_argument("--phone", required=True,
                        help="Number to call, E.164 (+15125551234). USE YOUR OWN.")
    parser.add_argument("--arv", type=int, help="ARV to hand the agent. Required unless --enrich.")
    parser.add_argument("--enrich", action="store_true",
                        help="Fetch a real valuation first (2 RentCast + 1 Zillapi request).")
    parser.add_argument("--address", default="6506 Clubway Ln")
    parser.add_argument("--city", default="Austin")
    parser.add_argument("--state", default="TX")
    parser.add_argument("--zip", dest="zip_code", default="78745")
    parser.add_argument("--name", default="Test Seller")
    args = parser.parse_args()

    if not args.phone.startswith("+"):
        sys.exit("Phone must be E.164 and start with '+', e.g. +15125551234")

    arv = args.arv
    if args.enrich:
        from orchestrator_lib import enrich_lead_with_valuation
        print("Fetching a real valuation (2 RentCast requests out of 50/month)...")
        valuation = enrich_lead_with_valuation(
            args.address, args.city, args.state, args.zip_code
        )
        arv = valuation.get("recommended_arv")
        print(f"  Recommended ARV: {arv}  (source: {valuation.get('source')})")
        if valuation.get("discarded_candidates"):
            print(f"  Discarded as implausible: {valuation['discarded_candidates']}")
        if arv is None:
            sys.exit("No usable ARV came back — nothing to hand the agent.")
    elif arv is None:
        sys.exit("Pass --arv, or --enrich to look one up.")

    # Sanity-check the offer math locally before the call, so you know what
    # the agent SHOULD say and can tell immediately if it says something else.
    from calculator import flip_mao
    for repairs in (15000, 40000):
        mao = flip_mao(arv=arv, repair_cost=repairs)
        if mao["no_deal"]:
            print(f"  At ${repairs:,} repairs: NO DEAL — {mao['reason']}")
        else:
            print(f"  At ${repairs:,} repairs: open at ${mao['opening_offer']:,}, "
                  f"ceiling ${mao['mao_floor']:,}, fee ${mao['wholesale_fee']:,}")

    full_address = f"{args.address}, {args.city}, {args.state} {args.zip_code}"
    print(f"\nCalling {args.phone} about {full_address}...")

    result = trigger_vapi_call(args.phone, {
        "seller_name": args.name,
        "property_address": full_address,
        "recommended_arv": str(arv),
    })
    print(f"  Call ID: {result.get('id')}")
    print(f"  Status:  {result.get('status')}")
    print("\nWatch the Render logs live. Afterwards, run the reconcile cron — "
          "expect 'already logged correctly: 1'.")


if __name__ == "__main__":
    main()
