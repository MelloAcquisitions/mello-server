"""
MAO calculator — translated directly from _Profit_Rental_Rehab_Wholetail_Calculator.xlsx

This replaces the simplified 70%-rule version from earlier. It faithfully
reproduces the three calculators from your spreadsheet:
  1. flip_calculator()      -> mirrors the "Profit Calculator" tab logic
  2. rental_calculator()    -> mirrors the "Rental Calculator" tab logic
  3. wholetail_calculator() -> mirrors the "Wholetail Calculator" tab logic
Plus:
  4. estimate_repair_cost() -> mirrors the "REHAB CALCULATOR" $/sqft tiers

No API keys needed — pure math. Run this file directly to see it reproduce
the exact numbers from your spreadsheet, confirming the translation is exact.
"""


# ---------------------------------------------------------------------------
# 1. FLIP / PROFIT CALCULATOR
# Mirrors: ARV -> less closing costs (%) -> less repairs ($) -> less holding
# costs (%) -> compare against Contract Price to Buyer (seller price + your fee)
# ---------------------------------------------------------------------------

def flip_calculator(
    arv: float,
    repair_cost: float,
    contract_price_to_seller: float,
    wholesale_fee: float,
    closing_cost_pct: float = 0.07,
    holding_cost_pct: float = 0.05,
) -> dict:
    closing_costs = arv * closing_cost_pct
    after_closing = arv - closing_costs
    after_repairs = after_closing - repair_cost
    holding_costs = arv * holding_cost_pct
    after_holding = after_repairs - holding_costs  # this is your spreadsheet's E5

    contract_price_to_buyer = contract_price_to_seller + wholesale_fee
    potential_profit = after_holding - contract_price_to_buyer
    profit_pct = potential_profit / arv if arv else 0
    all_in_number = contract_price_to_buyer + holding_costs + repair_cost + closing_costs

    return {
        "closing_costs": round(closing_costs),
        "holding_costs": round(holding_costs),
        "after_holding_ceiling": round(after_holding),  # max value before profit
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
    Solves the flip calculator backwards, with two hard guardrails:

    1. Wholesale fee floor: $10,000 minimum. If the deal can't support at
       least this much AFTER protecting the end buyer's margin, it's not a
       viable deal — return no_deal instead of a number that shortchanges
       either side.
    2. Buyer profit protection: reserves buyer_profit_pct of ARV (default
       10%, a common flip/wholetail minimum) for the END BUYER before any
       wholesale fee is taken. This was previously missing — the old
       formula could theoretically leave the buyer with zero margin.

    The wholesale fee itself is NOT fixed — it scales with how much room
    the deal actually has. Bigger, better deals support a bigger fee,
    capped only by what's left after the buyer's protected margin — no
    hard ceiling, but never taken at the seller's or buyer's expense.
    """
    closing_costs = arv * closing_cost_pct
    holding_costs = arv * holding_cost_pct
    after_holding = arv - closing_costs - repair_cost - holding_costs

    buyer_profit_target = round(arv * buyer_profit_pct)
    available_for_fee_and_seller = after_holding - buyer_profit_target

    if available_for_fee_and_seller < wholesale_fee_min:
        return {
            "no_deal": True,
            "reason": "Not enough spread to support the minimum wholesale fee while still protecting the end buyer's profit margin.",
            "mao_floor": None,
            "opening_offer": None,
            "wholesale_fee": None,
            "buyer_profit_target": buyer_profit_target,
        }

    # Fee scales at 5% of ARV, floored at $10K — calibrated against real
    # examples: $60K ARV -> $10K (floor kicks in), $300K -> $15K, $400K ->
    # $20K, scaling up naturally on bigger deals with no hard ceiling.
    fee_from_arv_pct = arv * 0.05
    wholesale_fee = round(max(wholesale_fee_min, min(fee_from_arv_pct, available_for_fee_and_seller)))

    mao_floor = round(available_for_fee_and_seller - wholesale_fee)
    opening_offer = round(mao_floor * 0.93)

    return {
        "no_deal": False,
        "mao_floor": mao_floor,
        "opening_offer": opening_offer,
        "wholesale_fee": wholesale_fee,  # MINIMUM target fee — the real fee can be higher, see calculate_final_fee()
        "buyer_profit_target": buyer_profit_target,
        "contract_price_to_buyer_max": round(available_for_fee_and_seller),
    }


def calculate_final_fee(arv: float, repair_cost: float, agreed_price: float, buyer_profit_pct: float = 0.10,
                          closing_cost_pct: float = 0.07, holding_cost_pct: float = 0.05) -> dict:
    """
    Call this AFTER a real price is negotiated with the seller — not during
    the initial ceiling calculation. The wholesale_fee from flip_mao() is
    only a MINIMUM target used to set the ceiling; your actual fee is
    whatever's left between the buyer's price cap and what you actually
    paid the seller. Negotiate a lower price than the ceiling, and you
    keep the difference — that's the point, not something to cap away.

    Example: a $200K ARV deal with a $146K ceiling (implying a $10K minimum
    fee) where the seller actually agrees to $136K nets you a real $20K fee
    — same deal, better negotiation, and the buyer's protected margin never
    changes either way.
    """
    closing_costs = arv * closing_cost_pct
    holding_costs = arv * holding_cost_pct
    after_holding = arv - closing_costs - repair_cost - holding_costs
    buyer_profit_target = round(arv * buyer_profit_pct)
    contract_price_to_buyer_max = round(after_holding - buyer_profit_target)

    actual_fee = contract_price_to_buyer_max - agreed_price

    return {
        "actual_fee": round(actual_fee),
        "contract_price_to_buyer_max": contract_price_to_buyer_max,
        "agreed_price": agreed_price,
    }


# ---------------------------------------------------------------------------
# 2. RENTAL CALCULATOR
# Same shape as the flip calculator, but with rental-typical closing/holding
# percentages, and reports Equity + Equity % instead of Profit.
# ---------------------------------------------------------------------------

def rental_calculator(
    arv: float,
    repair_cost: float,
    contract_price_to_seller: float,
    wholesale_fee: float,
    rent_rate: float = 0,
    closing_cost_pct: float = 0.01,
    holding_cost_pct: float = 0.01,
) -> dict:
    closing_costs = arv * closing_cost_pct
    after_closing = arv - closing_costs
    after_repairs = after_closing - repair_cost
    holding_costs = arv * holding_cost_pct
    after_holding = after_repairs - holding_costs

    contract_price_to_buyer = contract_price_to_seller + wholesale_fee
    equity = after_holding - contract_price_to_buyer
    equity_pct = equity / arv if arv else 0
    all_in_number = contract_price_to_buyer + holding_costs + repair_cost + closing_costs
    rent_pct = (rent_rate / all_in_number) if all_in_number else 0

    return {
        "closing_costs": round(closing_costs),
        "holding_costs": round(holding_costs),
        "after_holding_ceiling": round(after_holding),
        "contract_price_to_buyer": round(contract_price_to_buyer),
        "equity": round(equity),
        "equity_pct": round(equity_pct, 6),
        "all_in_number": round(all_in_number),
        "rent_pct": round(rent_pct, 6),
    }


# ---------------------------------------------------------------------------
# 3. WHOLETAIL CALCULATOR
# Simplest of the three: straight subtraction from CMV to a hard MAO.
# ---------------------------------------------------------------------------

def wholetail_calculator(
    cmv: float,
    repair_cost: float,
    buyer_profit: float,
    wholesale_fee: float,
    closing_cost_pct: float = 0.10,
) -> dict:
    closing_costs = cmv * closing_cost_pct
    mao = cmv - closing_costs - repair_cost - buyer_profit - wholesale_fee
    buyer_price = mao + wholesale_fee

    return {
        "closing_costs": round(closing_costs),
        "mao": round(mao),
        "buyer_price": round(buyer_price),
    }


# ---------------------------------------------------------------------------
# 4. REPAIR COST ESTIMATOR
# Mirrors the REHAB CALCULATOR tab's $/sqft tiers. Two reference price points
# are in your sheet (Mid Range ~1,528 sqft and Higher End ~3,500 sqft) with
# different $/sqft rates for each rehab tier. This function lets you pass any
# sqft and picks the appropriate rate set based on which reference point is
# closer, then multiplies by sqft — same math as your spreadsheet's C19=B18*B19.
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
    """
    tier must be one of the keys in REHAB_TIERS.
    Picks the mid-range or higher-end $/sqft rate depending on which
    reference size your property is closer to, then multiplies by sqft.
    """
    if tier not in REHAB_TIERS:
        raise ValueError(f"tier must be one of: {list(REHAB_TIERS.keys())}")

    mid_rate, high_rate = REHAB_TIERS[tier]

    dist_to_mid = abs(sqft - MID_RANGE_REFERENCE_SQFT)
    dist_to_high = abs(sqft - HIGHER_END_REFERENCE_SQFT)
    rate = mid_rate if dist_to_mid <= dist_to_high else high_rate

    return round(sqft * rate)


if __name__ == "__main__":
    print("=== FLIP CALCULATOR (should match your spreadsheet exactly) ===")
    result = flip_calculator(
        arv=300000, repair_cost=0, contract_price_to_seller=240000, wholesale_fee=10000
    )
    for k, v in result.items():
        print(f"  {k}: {v}")
    print("  Expected from spreadsheet: profit=14000, profit_pct=0.0467, all_in=286000")

    print("\n=== FLIP MAO (backwards-solve, breakeven) ===")
    mao = flip_mao(arv=300000, repair_cost=0)
    print(f"  Max offer to seller at breakeven: {mao}")

    print("\n=== RENTAL CALCULATOR (should match your spreadsheet exactly) ===")
    result = rental_calculator(
        arv=170000, repair_cost=30000, contract_price_to_seller=200000,
        wholesale_fee=10000, rent_rate=2000,
    )
    for k, v in result.items():
        print(f"  {k}: {v}")
    print("  Expected from spreadsheet: equity=-73400, equity_pct=-0.4318, all_in=243400, rent_pct=0.00822")

    print("\n=== WHOLETAIL CALCULATOR (should match your spreadsheet exactly) ===")
    result = wholetail_calculator(cmv=800000, repair_cost=10000, buyer_profit=70000, wholesale_fee=30000)
    for k, v in result.items():
        print(f"  {k}: {v}")
    print("  Expected from spreadsheet: mao=610000, buyer_price=640000")

    print("\n=== REPAIR ESTIMATOR ===")
    print(f"  1,528 sqft, Low tier: {estimate_repair_cost(1528, 'low (rental almost)')} (expected 22920)")
    print(f"  3,500 sqft, Gut Job: {estimate_repair_cost(3500, 'gut job')} (expected 234500)")
