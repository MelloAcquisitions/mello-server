"""
MAO calculator — translated from _Profit_Rental_Rehab_Wholetail_Calculator.xlsx.

WHAT'S USED LIVE
----------------
  flip_mao()            -> the `calculate_mao` Vapi tool. This is the one
                           that decides what the agent offers a real seller.
  calculate_final_fee() -> post-negotiation fee math (not currently wired
                           to a Vapi tool; kept because it is the correct
                           way to compute your real fee after a close).
  estimate_repair_cost()-> the REHAB CALCULATOR $/sqft tiers.
  flip_calculator()     -> forward-direction check, used by the self-test
                           below to prove the translation still matches the
                           spreadsheet exactly.

REMOVED IN CLEANUP: rental_calculator() and wholetail_calculator() were
faithful spreadsheet translations for two business lines this system does
not run. Nothing imported them. They were dead weight in a file that
decides what money gets offered to real people — the fewer unexercised
paths in here the better.

No API keys needed — pure math. Run this file directly to confirm the
translation still reproduces the spreadsheet's numbers.
"""

import os

# ---------------------------------------------------------------------------
# Guardrail: the smallest offer that is a real offer.
#
# THIS FIXES A GENUINE, LIVE-CALL BUG. The old logic only checked that the
# deal could support the $10K minimum wholesale fee. It never checked that
# anything meaningful was left for the SELLER. So a deal where
#   available_for_fee_and_seller = $10,001
# produced wholesale_fee = $10,000 and mao_floor = $1 — and the agent would
# have opened at 93% of that and offered a seller ONE DOLLAR for their
# house, live, on a recorded call.
#
# Worked example of the old behaviour:
#   ARV 200,000 / repairs 138,000
#   -> closing 14,000, holding 10,000, after_holding 38,000
#   -> buyer profit 20,000, available 18,000
#   -> fee 10,000, mao_floor 8,000, opening offer 7,440
# A $7,440 offer on a $200K house is not a negotiating position, it is a
# call the seller hangs up on and tells people about.
#
# Now the deal must clear the fee floor AND leave at least this much for
# the seller, or it returns no_deal and the agent wraps up honestly.
# ---------------------------------------------------------------------------
MIN_OFFER_TO_SELLER = float(os.environ.get("MIN_OFFER_TO_SELLER", 15000))

# Spoken offers get rounded to this increment. "One eighty-seven five"
# sounds like a real offer; "$187,432" sounds like a machine reading out a
# calculation, and gives the seller a number to pick apart.
OFFER_ROUNDING = 500


def _round_offer(amount: float) -> int:
    """Rounds DOWN to the nearest OFFER_ROUNDING so rounding never pushes an
    offer above the ceiling it was derived from."""
    return int(amount // OFFER_ROUNDING) * OFFER_ROUNDING


def flip_calculator(
    arv: float,
    repair_cost: float,
    contract_price_to_seller: float,
    wholesale_fee: float,
    closing_cost_pct: float = 0.07,
    holding_cost_pct: float = 0.05,
) -> dict:
    """
    Forward direction, mirroring the spreadsheet's "Profit Calculator" tab:
    ARV -> less closing costs (%) -> less repairs ($) -> less holding costs
    (%) -> compare against contract price to buyer (seller price + fee).
    """
    closing_costs = arv * closing_cost_pct
    after_closing = arv - closing_costs
    after_repairs = after_closing - repair_cost
    holding_costs = arv * holding_cost_pct
    after_holding = after_repairs - holding_costs  # the spreadsheet's E5

    contract_price_to_buyer = contract_price_to_seller + wholesale_fee
    potential_profit = after_holding - contract_price_to_buyer
    profit_pct = potential_profit / arv if arv else 0
    all_in_number = contract_price_to_buyer + holding_costs + repair_cost + closing_costs

    return {
        "closing_costs": round(closing_costs),
        "holding_costs": round(holding_costs),
        "after_holding_ceiling": round(after_holding),
        "contract_price_to_buyer": round(contract_price_to_buyer),
        "potential_profit": round(potential_profit),
        "profit_pct": round(profit_pct, 6),
        "all_in_number": round(all_in_number),
    }


def flip_mao(
    arv: float,
    repair_cost: float,
    wholesale_fee_min: float = 10000,
    buyer_profit_pct: float = 0.10,
    closing_cost_pct: float = 0.07,
    holding_cost_pct: float = 0.05,
) -> dict:
    """
    Solves the flip calculator backwards, with THREE hard guardrails:

    1. Buyer profit protection — reserves buyer_profit_pct of ARV (default
       10%, a common flip minimum) for the END BUYER before any wholesale
       fee is taken. Without it the formula could leave the buyer zero
       margin, and an end buyer runs their own numbers before agreeing to
       your price.
    2. Wholesale fee floor — $10,000 minimum. If the deal cannot support
       that after protecting the buyer, it is not a viable deal.
    3. Minimum offer to the seller — see MIN_OFFER_TO_SELLER above. This is
       the guardrail that was missing, and its absence could have produced
       a live one-dollar offer.

    Returns no_deal=True with a plain-language `reason` if any guardrail
    fails. The agent is instructed to wrap up warmly on no_deal rather than
    improvise a number.

    The wholesale fee is NOT fixed — it scales with the room the deal
    actually has (5% of ARV, floored at $10K), capped only by what remains
    after the buyer's protected margin and the seller's minimum.
    """
    # Defensive input handling. These arrive from a voice model transcribing
    # what a seller said, so they can be strings, negative, or nonsense.
    try:
        arv = float(arv)
        repair_cost = float(repair_cost)
        wholesale_fee_min = float(wholesale_fee_min)
        buyer_profit_pct = float(buyer_profit_pct)
    except (TypeError, ValueError):
        return _no_deal("Invalid numbers supplied to the MAO calculation.", None)

    if arv <= 0:
        return _no_deal("No usable ARV for this property, so no offer can be calculated.", None)
    if repair_cost < 0:
        repair_cost = 0.0

    closing_costs = arv * closing_cost_pct
    holding_costs = arv * holding_cost_pct
    after_holding = arv - closing_costs - repair_cost - holding_costs

    buyer_profit_target = round(arv * buyer_profit_pct)
    available_for_fee_and_seller = after_holding - buyer_profit_target

    # Guardrail 2 + 3 checked together: the deal must clear the fee floor
    # AND still leave a real offer for the seller.
    required = wholesale_fee_min + MIN_OFFER_TO_SELLER
    if available_for_fee_and_seller < required:
        return _no_deal(
            "The repair cost is too high relative to the value for this to work — "
            "after protecting the end buyer's margin there isn't enough left to "
            "make the seller a real offer and still earn a minimum fee.",
            buyer_profit_target,
        )

    # Fee scales at 5% of ARV, floored at $10K, and can never eat into the
    # seller's minimum: $60K ARV -> $10K (floor), $300K -> $15K, $400K ->
    # $20K, scaling up naturally on bigger deals with no hard ceiling.
    fee_from_arv_pct = arv * 0.05
    fee_ceiling = available_for_fee_and_seller - MIN_OFFER_TO_SELLER
    wholesale_fee = round(max(wholesale_fee_min, min(fee_from_arv_pct, fee_ceiling)))

    mao_floor = _round_offer(available_for_fee_and_seller - wholesale_fee)
    opening_offer = _round_offer(mao_floor * 0.93)

    # Final sanity check before this number reaches a live call. Belt and
    # braces: if any of the arithmetic above ever drifts, fail closed with
    # no_deal rather than speak an absurd number to a seller.
    if mao_floor < MIN_OFFER_TO_SELLER or opening_offer <= 0 or mao_floor > arv:
        return _no_deal(
            "The numbers on this one don't support a sensible offer.",
            buyer_profit_target,
        )

    return {
        "no_deal": False,
        "mao_floor": mao_floor,
        "opening_offer": opening_offer,
        # MINIMUM target fee — the real fee can be higher, see calculate_final_fee()
        "wholesale_fee": wholesale_fee,
        "buyer_profit_target": buyer_profit_target,
        "contract_price_to_buyer_max": round(available_for_fee_and_seller),
    }


def _no_deal(reason: str, buyer_profit_target) -> dict:
    """Uniform no_deal shape, so the agent never sees a partially-populated
    result it might read a number out of."""
    return {
        "no_deal": True,
        "reason": reason,
        "mao_floor": None,
        "opening_offer": None,
        "wholesale_fee": None,
        "buyer_profit_target": buyer_profit_target,
    }


def calculate_final_fee(
    arv: float,
    repair_cost: float,
    agreed_price: float,
    buyer_profit_pct: float = 0.10,
    closing_cost_pct: float = 0.07,
    holding_cost_pct: float = 0.05,
) -> dict:
    """
    Call this AFTER a real price is negotiated — not during the initial
    ceiling calculation. The wholesale_fee from flip_mao() is only a
    MINIMUM target used to set the ceiling; the actual fee is whatever is
    left between the buyer's price cap and what you actually paid the
    seller. Negotiate below the ceiling and you keep the difference.

    Example: a $200K ARV deal with a $146K ceiling (implying a $10K minimum
    fee) where the seller agrees at $136K nets a real $20K fee — same deal,
    better negotiation, and the buyer's protected margin never changes.

    `fee_is_negative` is returned explicitly: if you agreed a price ABOVE
    the buyer cap, the "fee" is a loss, and that needs to be obvious rather
    than shown as a small number.
    """
    closing_costs = arv * closing_cost_pct
    holding_costs = arv * holding_cost_pct
    after_holding = arv - closing_costs - repair_cost - holding_costs
    buyer_profit_target = round(arv * buyer_profit_pct)
    contract_price_to_buyer_max = round(after_holding - buyer_profit_target)

    actual_fee = round(contract_price_to_buyer_max - agreed_price)

    return {
        "actual_fee": actual_fee,
        "fee_is_negative": actual_fee < 0,
        "contract_price_to_buyer_max": contract_price_to_buyer_max,
        "agreed_price": agreed_price,
    }


# ---------------------------------------------------------------------------
# REPAIR COST ESTIMATOR
# Mirrors the REHAB CALCULATOR tab's $/sqft tiers. Two reference price
# points exist in the sheet (Mid Range ~1,528 sqft and Higher End ~3,500
# sqft) with different $/sqft rates per tier. This picks the closer
# reference and multiplies by sqft — the spreadsheet's C19 = B18*B19.
# ---------------------------------------------------------------------------

# tier -> (mid_range $/sqft, higher_end $/sqft)
REHAB_TIERS = {
    "low (rental almost)": (15, 25),
    "mid (cheaper materials, some salvageable)": (25, 35),
    "full (interior cosmetics)": (35, 45),
    "add exterior cosmetics": (40, 50),
    "full rehab plus some of the big 6": (45, 55),
    "gut job": (62, 67),
}

MID_RANGE_REFERENCE_SQFT = 1528
HIGHER_END_REFERENCE_SQFT = 3500


def estimate_repair_cost(sqft: float, tier: str) -> float:
    """tier must be one of the keys in REHAB_TIERS."""
    if tier not in REHAB_TIERS:
        raise ValueError(f"tier must be one of: {list(REHAB_TIERS.keys())}")

    mid_rate, high_rate = REHAB_TIERS[tier]
    dist_to_mid = abs(sqft - MID_RANGE_REFERENCE_SQFT)
    dist_to_high = abs(sqft - HIGHER_END_REFERENCE_SQFT)
    rate = mid_rate if dist_to_mid <= dist_to_high else high_rate

    return round(sqft * rate)


if __name__ == "__main__":
    print("=== FLIP CALCULATOR (must still match the spreadsheet exactly) ===")
    result = flip_calculator(
        arv=300000, repair_cost=0, contract_price_to_seller=240000, wholesale_fee=10000
    )
    for k, v in result.items():
        print(f"  {k}: {v}")
    print("  Expected from spreadsheet: profit=14000, profit_pct=0.0467, all_in=286000")

    print("\n=== FLIP MAO (backwards-solve) ===")
    print(f"  Healthy deal (300K ARV, 20K repairs): {flip_mao(arv=300000, repair_cost=20000)}")

    print("\n=== THE BUG THIS RELEASE FIXES ===")
    print("  200K ARV with 138K of repairs — old code returned mao_floor=8000,")
    print("  opening_offer=7440 and the agent would have said it out loud.")
    thin = flip_mao(arv=200000, repair_cost=138000)
    print(f"  Now: no_deal={thin['no_deal']} — {thin['reason']}")

    print("\n  Pathological case (available barely clears the fee floor):")
    # after_holding = arv*0.88 - repairs ; available = that - arv*0.10
    # solve so available ~= 10,001 on a 200K ARV -> repairs = 145,999
    edge = flip_mao(arv=200000, repair_cost=145999)
    print(f"  Old code: mao_floor=$1, opening_offer=$0.93 spoken to a seller.")
    print(f"  Now: no_deal={edge['no_deal']}")

    print("\n=== REPAIR ESTIMATOR ===")
    print(f"  1,528 sqft, Low tier: {estimate_repair_cost(1528, 'low (rental almost)')} (expected 22920)")
    print(f"  3,500 sqft, Gut Job: {estimate_repair_cost(3500, 'gut job')} (expected 234500)")
