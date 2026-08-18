"""
RentCast setup and property valuation (ARV) lookup.

SETUP (do this before running):
1. Sign up at rentcast.io, activate an API billing plan from your dashboard
   (Free tier: 50 requests/month, no credit card required)
2. Get your API key from your RentCast dashboard
3. Set it as an environment variable (never hardcode it):

   Mac/Linux:  export RENTCAST_API_KEY="your-key-here"
   Windows:    $env:RENTCAST_API_KEY="your-key-here"

4. pip install requests
5. Run: python rentcast_lookup.py

This uses RentCast's /avm/value endpoint — confirmed directly from their
developer docs at developers.rentcast.io/reference/value-estimate — which
returns a current market value / ARV estimate plus comparable sale listings
for a given address. This is what feeds your calculate_mao() function.
"""

import os
import requests

RENTCAST_API_KEY = os.environ.get("RENTCAST_API_KEY")
BASE_URL = "https://api.rentcast.io/v1/avm/value"


def get_sold_comps(
    full_address: str,
    subject_property: dict = None,
    radius_miles: float = 1.5,
    sold_within_days: int = 180,
    property_type: str = "Single Family",
    limit: int = 25,
) -> dict:
    """
    Pulls ACTUALLY SOLD properties near the subject address — not active
    listings — using RentCast's /properties endpoint with saleDateRange.

    subject_property: if provided (with squareFootage/bedrooms/bathrooms),
    tightens the query itself to similar-sized homes — e.g. sqft within
    +/-25% — rather than relying only on post-hoc similarity weighting.
    Without this, a 1.5-mile radius can pull in much larger/nicer homes
    that technically match "Single Family" but distort the average badly.
    """
    if not RENTCAST_API_KEY:
        raise RuntimeError("RENTCAST_API_KEY environment variable is not set.")

    headers = {
        "Accept": "application/json",
        "X-Api-Key": RENTCAST_API_KEY,
    }

    url = "https://api.rentcast.io/v1/properties"
    params = {
        "address": full_address,
        "radius": radius_miles,
        "propertyType": property_type,
        "saleDateRange": sold_within_days,
        "limit": limit,
    }

    if subject_property:
        sqft = subject_property.get("squareFootage")
        beds = subject_property.get("bedrooms")
        baths = subject_property.get("bathrooms")
        if sqft:
            low = round(sqft * 0.75)
            high = round(sqft * 1.25)
            params["squareFootage"] = f"{low}:{high}"
        if beds is not None:
            params["bedrooms"] = f"{max(beds - 1, 0)}:{beds + 1}"
        if baths is not None:
            params["bathrooms"] = f"{max(baths - 1, 0)}:{baths + 1}"

    response = requests.get(url, headers=headers, params=params, timeout=15)
    if not response.ok:
        print(f"RentCast returned an error ({response.status_code}):")
        print(response.text)
    response.raise_for_status()
    return response.json()


def _similarity_score(comp: dict, subject: dict) -> float:
    """
    Scores how closely a sold comp matches the subject property, 0-1.
    Weighted: sqft closeness matters most, then year built, then bed/bath match.
    A human skimming 5 comps on a call does this instinctively and roughly —
    this makes it explicit and consistent across every comp, every time.
    """
    score = 1.0

    subject_sqft = subject.get("squareFootage")
    comp_sqft = comp.get("squareFootage")
    if subject_sqft and comp_sqft:
        sqft_diff_pct = abs(comp_sqft - subject_sqft) / subject_sqft
        score -= min(sqft_diff_pct, 0.5) * 0.4  # up to -0.4 for sqft mismatch

    subject_year = subject.get("yearBuilt")
    comp_year = comp.get("yearBuilt")
    if subject_year and comp_year:
        year_diff = abs(comp_year - subject_year)
        score -= min(year_diff / 50, 1.0) * 0.25  # up to -0.25 for year mismatch

    if subject.get("bedrooms") is not None and comp.get("bedrooms") is not None:
        if subject["bedrooms"] != comp["bedrooms"]:
            score -= 0.15

    if subject.get("bathrooms") is not None and comp.get("bathrooms") is not None:
        if subject["bathrooms"] != comp["bathrooms"]:
            score -= 0.1

    return max(score, 0.0)


def analyze_sold_comps(sold_comps: list, subject_property: dict = None) -> dict:
    """
    Richer version of a simple average — this is the part a human wouldn't
    have time to do live on a call, but an AI agent can compute instantly:

    1. Similarity-weighted ARV (better matches count more than loose ones)
    2. Median sold price (less sensitive to outliers than the mean)
    3. Investor/LLC buyer activity — what % of recent sales nearby went to
       an Organization (LLC, investor entity) rather than an Individual.

    subject_property: optional dict with squareFootage/yearBuilt/bedrooms/
    bathrooms for the property being evaluated, used for similarity scoring.
    If omitted, falls back to a simple unweighted average.

    IMPORTANT: only uses records with an actual lastSalePrice — does NOT
    fall back to a listing's current "price" field, since that can be a
    current asking price rather than what the property actually sold for,
    which would quietly contaminate a "sold comps" analysis.
    """
    if not sold_comps:
        return {
            "weighted_arv": None,
            "simple_average_price": None,
            "median_sold_price": None,
            "average_price_per_sqft": None,
            "comp_count": 0,
            "investor_buyer_pct": None,
            "investor_buyer_count": 0,
        }

    weighted_prices = []
    weights = []
    prices = []
    price_per_sqft_list = []
    investor_count = 0
    known_owner_type_count = 0

    for comp in sold_comps:
        price = comp.get("lastSalePrice")  # only real sold prices — no fallback to asking price
        sqft = comp.get("squareFootage")

        if price:
            prices.append(price)
            if sqft:
                price_per_sqft_list.append(price / sqft)

            if subject_property:
                weight = _similarity_score(comp, subject_property)
                weighted_prices.append(price * weight)
                weights.append(weight)

        owner_type = comp.get("owner", {}).get("type") if isinstance(comp.get("owner"), dict) else None
        if owner_type:
            known_owner_type_count += 1
            if owner_type == "Organization":
                investor_count += 1

    weighted_arv = (
        round(sum(weighted_prices) / sum(weights)) if weights and sum(weights) > 0 else None
    )
    simple_average = round(sum(prices) / len(prices)) if prices else None
    median_price = round(sorted(prices)[len(prices) // 2]) if prices else None
    avg_price_per_sqft = (
        round(sum(price_per_sqft_list) / len(price_per_sqft_list), 2) if price_per_sqft_list else None
    )
    investor_pct = (
        round(investor_count / known_owner_type_count * 100, 1) if known_owner_type_count else None
    )

    return {
        "weighted_arv": weighted_arv,
        "simple_average_price": simple_average,
        "median_sold_price": median_price,
        "average_price_per_sqft": avg_price_per_sqft,
        "comp_count": len(prices),
        "investor_buyer_pct": investor_pct,
        "investor_buyer_count": investor_count,
    }


def get_recommended_arv(sold_comps_analysis: dict, avm_result: dict) -> dict:
    """
    Reconciles the sold-comps analysis against the AVM estimate and picks
    the CONSERVATIVE (lower) figure as the recommended ARV to feed into
    calculate_mao(). Deliberately conservative for two reasons:
      1. Protects your margin if either estimate is running optimistic
      2. Protects your ability to actually resell — an end buyer will run
         their own comps before agreeing to your price, and a conservative
         ARV is more likely to survive that independent check

    Fully automated — no human review gate here. This is a data reconciliation
    step, not a binding commitment, so it always resolves to a number and lets
    the call continue. spread_pct is still reported for your own visibility
    when reviewing logs later, it just doesn't block anything live.
    """
    candidates = {
        "sold_comps_weighted": sold_comps_analysis.get("weighted_arv"),
        "sold_comps_median": sold_comps_analysis.get("median_sold_price"),
        "avm_estimate": avm_result.get("price"),
    }
    valid_candidates = {k: v for k, v in candidates.items() if v}

    if not valid_candidates:
        return {"recommended_arv": None, "source": None, "all_candidates": candidates, "spread_pct": None}

    chosen_source = min(valid_candidates, key=valid_candidates.get)
    recommended = valid_candidates[chosen_source]
    spread_pct = round(
        (max(valid_candidates.values()) - min(valid_candidates.values()))
        / min(valid_candidates.values()) * 100, 1
    ) if len(valid_candidates) > 1 else 0

    return {
        "recommended_arv": recommended,
        "source": chosen_source,
        "all_candidates": candidates,
        "spread_pct": spread_pct,
    }


# Kept for backwards compatibility with earlier version of this script
def calculate_arv_from_sold_comps(sold_comps: list) -> dict:
    """Simple unweighted average — use analyze_sold_comps() for the fuller version."""
    result = analyze_sold_comps(sold_comps)
    return {
        "average_sold_price": result["simple_average_price"],
        "average_price_per_sqft": result["average_price_per_sqft"],
        "comp_count": result["comp_count"],
    }


def get_property_valuation(full_address: str, comp_count: int = 5) -> dict:
    """
    full_address: a single string, e.g. "5312 Mulberry Grove Ln, Austin, TX 78723"
                   (RentCast takes one combined address string, not separate fields)
    comp_count: how many comparable sale listings to return (their default is 10)

    Returns the raw API response, including:
      - price: the estimated current market value / ARV
      - comparables: list of nearby comparable sales used to calculate it
      - subjectProperty: details on the property itself (sqft, beds, baths, etc.)
    """
    if not RENTCAST_API_KEY:
        raise RuntimeError(
            "RENTCAST_API_KEY environment variable is not set. "
            "See setup instructions at the top of this file."
        )

    headers = {
        "Accept": "application/json",
        "X-Api-Key": RENTCAST_API_KEY,
    }

    params = {
        "address": full_address,
        "compCount": comp_count,
    }

    response = requests.get(BASE_URL, headers=headers, params=params, timeout=15)
    if not response.ok:
        print(f"RentCast returned an error ({response.status_code}):")
        print(response.text)
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    test_address = "6506 Clubway Ln, Austin, TX 78745"

    print("=== Getting subject property attributes (via AVM call) ===")
    avm_result = get_property_valuation(test_address)
    subject = avm_result.get("subjectProperty", {})
    print(f"Subject: {subject.get('squareFootage')} sqft, "
          f"built {subject.get('yearBuilt')}, "
          f"{subject.get('bedrooms')}bd/{subject.get('bathrooms')}ba")

    print("\n=== SOLD comps (query tightened to similar size/beds/baths) ===")
    sold_result = get_sold_comps(test_address, subject_property=subject)
    sold_properties = sold_result if isinstance(sold_result, list) else sold_result.get("properties", [])
    print(f"Found {len(sold_properties)} sold comps after tightened query")

    print("\n=== Weighted analysis ===")
    analysis = analyze_sold_comps(sold_properties, subject_property=subject)
    for k, v in analysis.items():
        print(f"  {k}: {v}")

    print("\n=== AVM estimate (cross-check) ===")
    print(f"AVM estimated value: {avm_result.get('price')}")
    print(f"AVM range: {avm_result.get('priceRangeLow')} - {avm_result.get('priceRangeHigh')}")

    print("\n=== FINAL RECOMMENDED ARV (conservative — lowest of all estimates) ===")
    recommendation = get_recommended_arv(analysis, avm_result)
    print(f"Recommended ARV: {recommendation['recommended_arv']}")
    print(f"Source: {recommendation['source']}")
    print(f"All candidates: {recommendation['all_candidates']}")
    print(f"Spread between estimates: {recommendation['spread_pct']}% (informational only)")
