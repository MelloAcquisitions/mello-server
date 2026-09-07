"""
RentCast property valuation (ARV) lookup.

SETUP:
1. Sign up at rentcast.io and activate an API plan.
2. Set RENTCAST_API_KEY as an environment variable (never hardcode it).
3. Run: python rentcast_lookup.py

*** COST WARNING — READ THIS ***
RentCast's FREE tier is 50 requests PER MONTH. One enriched lead costs TWO
requests here (the AVM call plus the sold-comps call). At 15 leads a day
that is 30 requests daily — the entire month's allowance gone in under two
days, after which every enrichment fails and leads pile up unvalued. The
enrichment crons now share a MAX_ENRICHMENTS_PER_DAY budget for exactly
this reason; set it to about 1 while on the free tier.

Uses /avm/value (confirmed at developers.rentcast.io/reference/value-estimate)
for the AVM plus /properties with saleDateRange for real sold comps.

CHANGES IN THIS CLEANUP
-----------------------
1. median_sold_price is now a REAL median. `sorted(prices)[len//2]` is the
   upper-middle value on an even-length list, not the median — it biased
   every even-count comp set upward.
2. get_recommended_arv() now discards implausibly low candidates before
   taking the minimum. Picking the lowest of three sources is the right
   conservative instinct, but with no floor a single bad data point (a
   distressed sale, a lot-only record, a bad address match) becomes the
   recommended ARV — and downstream that either produces no_deal on a
   perfectly good lead or, worse, an offer anchored to a wrong number.
3. REMOVED calculate_arv_from_sold_comps(), a backwards-compatibility
   wrapper nothing imported.
"""

import os

import requests

RENTCAST_API_KEY = os.environ.get("RENTCAST_API_KEY")
BASE_URL = "https://api.rentcast.io/v1/avm/value"

# A candidate valuation below this fraction of the highest candidate is
# treated as a data error rather than a conservative estimate. Two sources
# disagreeing by 2x on the same house means one of them is wrong, not that
# the house is cheap.
IMPLAUSIBLE_CANDIDATE_RATIO = 0.5


def _headers():
    if not RENTCAST_API_KEY:
        raise RuntimeError("RENTCAST_API_KEY environment variable is not set.")
    return {"Accept": "application/json", "X-Api-Key": RENTCAST_API_KEY}


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
    listings — via /properties with saleDateRange.

    subject_property: if provided (squareFootage/bedrooms/bathrooms),
    tightens the query itself to similar-sized homes rather than relying
    only on post-hoc similarity weighting. Without it a 1.5-mile radius
    pulls in much larger homes that technically match "Single Family" and
    distort the average badly.
    """
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
            params["squareFootage"] = f"{round(sqft * 0.75)}:{round(sqft * 1.25)}"
        if beds is not None:
            params["bedrooms"] = f"{max(beds - 1, 0)}:{beds + 1}"
        if baths is not None:
            params["bathrooms"] = f"{max(baths - 1, 0)}:{baths + 1}"

    response = requests.get("https://api.rentcast.io/v1/properties",
                            headers=_headers(), params=params, timeout=15)
    if not response.ok:
        print(f"RentCast returned an error ({response.status_code}): {response.text[:400]}")
    response.raise_for_status()
    return response.json()


def get_property_valuation(full_address: str, comp_count: int = 5) -> dict:
    """
    full_address: one combined string, e.g. "5312 Mulberry Grove Ln, Austin, TX 78723"
    (RentCast takes a single address string, not separate fields).

    Returns the raw response including `price` (estimated current market
    value / ARV), `comparables`, and `subjectProperty` (sqft, beds, baths).
    """
    params = {"address": full_address, "compCount": comp_count}
    response = requests.get(BASE_URL, headers=_headers(), params=params, timeout=15)
    if not response.ok:
        print(f"RentCast returned an error ({response.status_code}): {response.text[:400]}")
    response.raise_for_status()
    return response.json()


def _similarity_score(comp: dict, subject: dict) -> float:
    """
    Scores how closely a sold comp matches the subject property, 0-1.
    Weighted: sqft closeness matters most, then year built, then bed/bath.
    A human skimming five comps mid-call does this instinctively and
    roughly; this makes it explicit and consistent every time.
    """
    score = 1.0

    subject_sqft = subject.get("squareFootage")
    comp_sqft = comp.get("squareFootage")
    if subject_sqft and comp_sqft:
        score -= min(abs(comp_sqft - subject_sqft) / subject_sqft, 0.5) * 0.4

    subject_year = subject.get("yearBuilt")
    comp_year = comp.get("yearBuilt")
    if subject_year and comp_year:
        score -= min(abs(comp_year - subject_year) / 50, 1.0) * 0.25

    if subject.get("bedrooms") is not None and comp.get("bedrooms") is not None:
        if subject["bedrooms"] != comp["bedrooms"]:
            score -= 0.15

    if subject.get("bathrooms") is not None and comp.get("bathrooms") is not None:
        if subject["bathrooms"] != comp["bathrooms"]:
            score -= 0.1

    return max(score, 0.0)


def _median(values: list) -> float:
    """A real median. `sorted(v)[len//2]` returns the upper-middle value on
    an even-length list, which biased every even-count comp set upward."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _remove_price_outliers(comps_with_prices: list, k: float = 1.5) -> list:
    """
    Removes statistical outliers by the IQR method before they skew the ARV
    — one distressed off-market sale at half the normal price should not
    drag the whole average down. Needs at least 4 comps for meaningful
    quartiles; returns everything unchanged if fewer.
    """
    if len(comps_with_prices) < 4:
        return comps_with_prices

    sorted_prices = sorted(c["price"] for c in comps_with_prices)
    n = len(sorted_prices)
    q1 = sorted_prices[n // 4]
    q3 = sorted_prices[(3 * n) // 4]
    iqr = q3 - q1
    lower, upper = q1 - k * iqr, q3 + k * iqr

    return [c for c in comps_with_prices if lower <= c["price"] <= upper]


def analyze_sold_comps(sold_comps: list, subject_property: dict = None) -> dict:
    """
    A richer version of a simple average — the part a human would not have
    time to do live on a call:

      1. Outlier removal (IQR) before anything skews the number
      2. Similarity-weighted ARV (better matches count more)
      3. Median sold price (less sensitive to outliers than the mean)
      4. Investor/LLC buyer activity — what share of recent nearby sales
         went to an Organization rather than an Individual

    Only uses records with a real lastSalePrice — no fallback to a listing's
    current `price`, which can be an asking price rather than a sale.
    """
    empty = {
        "weighted_arv": None, "simple_average_price": None, "median_sold_price": None,
        "average_price_per_sqft": None, "comp_count": 0, "outliers_removed": 0,
        "investor_buyer_pct": None, "investor_buyer_count": 0,
    }
    if not sold_comps:
        return empty

    priced_comps = []
    investor_count = 0
    known_owner_type_count = 0

    for comp in sold_comps:
        price = comp.get("lastSalePrice")
        if price:
            priced_comps.append({"comp": comp, "price": price})

        owner = comp.get("owner")
        owner_type = owner.get("type") if isinstance(owner, dict) else None
        if owner_type:
            known_owner_type_count += 1
            if owner_type == "Organization":
                investor_count += 1

    filtered_comps = _remove_price_outliers(priced_comps)
    outliers_removed = len(priced_comps) - len(filtered_comps)

    weighted_prices, weights, prices, price_per_sqft_list = [], [], [], []

    for item in filtered_comps:
        comp, price = item["comp"], item["price"]
        prices.append(price)
        sqft = comp.get("squareFootage")
        if sqft:
            price_per_sqft_list.append(price / sqft)
        if subject_property:
            weight = _similarity_score(comp, subject_property)
            weighted_prices.append(price * weight)
            weights.append(weight)

    median_price = _median(prices)

    return {
        "weighted_arv": round(sum(weighted_prices) / sum(weights)) if weights and sum(weights) > 0 else None,
        "simple_average_price": round(sum(prices) / len(prices)) if prices else None,
        "median_sold_price": round(median_price) if median_price is not None else None,
        "average_price_per_sqft": round(sum(price_per_sqft_list) / len(price_per_sqft_list), 2)
                                  if price_per_sqft_list else None,
        "comp_count": len(prices),
        "outliers_removed": outliers_removed,
        "investor_buyer_pct": round(investor_count / known_owner_type_count * 100, 1)
                              if known_owner_type_count else None,
        "investor_buyer_count": investor_count,
    }


def get_recommended_arv(sold_comps_analysis: dict, avm_result: dict,
                        zillow_estimate: float = None) -> dict:
    """
    Reconciles up to four valuation candidates and picks the CONSERVATIVE
    (lowest plausible) one:
      1. RentCast sold-comps weighted average (real closed transactions)
      2. RentCast sold-comps median
      3. RentCast's own AVM estimate
      4. Zillow's Zestimate, if provided — a genuinely independent model,
         unlike 1-3 which all derive from the same provider

    Conservative on purpose: it protects margin if an estimate runs
    optimistic, and protects resale, because an end buyer runs their own
    numbers before agreeing to a price.

    NEW — implausible candidates are discarded first. Taking the raw minimum
    with no floor means one bad data point (a distressed sale, a lot-only
    record, a wrong address match) silently becomes the recommended ARV.
    Downstream that either kills a good lead as no_deal or anchors a real
    offer to a wrong number. Any candidate below
    IMPLAUSIBLE_CANDIDATE_RATIO of the highest candidate is dropped and
    reported in `discarded_candidates`, so the discard is visible rather
    than magic.
    """
    candidates = {
        "sold_comps_weighted": sold_comps_analysis.get("weighted_arv"),
        "sold_comps_median": sold_comps_analysis.get("median_sold_price"),
        "avm_estimate": avm_result.get("price"),
    }
    if zillow_estimate:
        candidates["zillow_zestimate"] = zillow_estimate

    valid = {k: v for k, v in candidates.items() if v}

    if not valid:
        return {"recommended_arv": None, "source": None, "all_candidates": candidates,
                "spread_pct": None, "discarded_candidates": {}}

    discarded = {}
    if len(valid) > 1:
        highest = max(valid.values())
        threshold = highest * IMPLAUSIBLE_CANDIDATE_RATIO
        plausible = {k: v for k, v in valid.items() if v >= threshold}
        discarded = {k: v for k, v in valid.items() if v < threshold}
        if plausible:  # never discard everything
            valid = plausible

    chosen_source = min(valid, key=valid.get)
    spread_pct = round(
        (max(valid.values()) - min(valid.values())) / min(valid.values()) * 100, 1
    ) if len(valid) > 1 else 0

    return {
        "recommended_arv": valid[chosen_source],
        "source": chosen_source,
        "all_candidates": candidates,
        "discarded_candidates": discarded,
        "spread_pct": spread_pct,
    }


if __name__ == "__main__":
    test_address = "6506 Clubway Ln, Austin, TX 78745"

    print("=== Subject property attributes (via AVM call) ===")
    avm_result = get_property_valuation(test_address)
    subject = avm_result.get("subjectProperty", {})
    print(f"Subject: {subject.get('squareFootage')} sqft, built {subject.get('yearBuilt')}, "
          f"{subject.get('bedrooms')}bd/{subject.get('bathrooms')}ba")

    print("\n=== SOLD comps (query tightened to similar size/beds/baths) ===")
    sold_result = get_sold_comps(test_address, subject_property=subject)
    sold_properties = sold_result if isinstance(sold_result, list) else sold_result.get("properties", [])
    print(f"Found {len(sold_properties)} sold comps")

    print("\n=== Weighted analysis ===")
    analysis = analyze_sold_comps(sold_properties, subject_property=subject)
    for k, v in analysis.items():
        print(f"  {k}: {v}")

    print("\n=== AVM estimate (cross-check) ===")
    print(f"AVM: {avm_result.get('price')} "
          f"(range {avm_result.get('priceRangeLow')} - {avm_result.get('priceRangeHigh')})")

    print("\n=== FINAL RECOMMENDED ARV (lowest plausible estimate) ===")
    recommendation = get_recommended_arv(analysis, avm_result)
    for k, v in recommendation.items():
        print(f"  {k}: {v}")
    print("\nThat was 2 RentCast requests. The free tier allows 50 per MONTH.")
