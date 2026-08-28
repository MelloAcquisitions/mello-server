"""
Zillow valuation lookup via Zillapi (a third-party REST wrapper — Zillow
itself has no public API since 2021). This is your SECOND, independent
valuation source, meant to cross-check RentCast's numbers with a genuinely
different underlying methodology (Zillow's own Zestimate model).

SETUP:
1. Sign up at zillapi.com — free tier gives 100 credits, no card required
2. Get your API key from the dashboard (starts with zk_)
3. Set it as an environment variable: export ZILLAPI_KEY="zk_your-key-here"
4. Run: python zillapi_lookup.py

CONFIRMED (previously unverified, now fixed against a live 404): the
address-lookup endpoint is /v1/properties/by-address, not /v1/properties.
The response also comes wrapped in a {"data": {...}} envelope, not flat —
both the URL and the parsing below account for that now.
"""

import os
import requests

ZILLAPI_KEY = os.environ.get("ZILLAPI_KEY")
BASE_URL = "https://api.zillapi.com/v1/properties/by-address"


def get_zillow_valuation(full_address: str) -> dict:
    """
    Looks up a property by address and returns Zillow-sourced data,
    including the Zestimate — a genuinely independent valuation figure
    from RentCast's own AVM, useful as a real second opinion.

    full_address: a single string, e.g. "6506 Clubway Ln, Austin, TX 78745"

    Returns the "data" object directly (the envelope is unwrapped here),
    so callers don't need to know about Zillapi's response wrapper.
    """
    if not ZILLAPI_KEY:
        raise RuntimeError(
            "ZILLAPI_KEY environment variable is not set. "
            "See setup instructions at the top of this file."
        )

    headers = {"Authorization": f"Bearer {ZILLAPI_KEY}"}
    params = {"address": full_address}

    response = requests.get(BASE_URL, headers=headers, params=params, timeout=15)
    if not response.ok:
        print(f"Zillapi returned an error ({response.status_code}):")
        print(response.text)
    response.raise_for_status()
    body = response.json()
    # Real responses come wrapped as {"data": {...}, "request_id": "..."} —
    # unwrap it here, but fall back to the raw body if that shape ever
    # changes rather than silently returning nothing useful.
    return body.get("data", body)


def extract_zestimate(zillow_result: dict) -> float:
    """
    Pulls just the Zestimate number out of the (already-unwrapped) result,
    defensively — falls back to None rather than crashing if their schema
    differs from what's documented.
    """
    return zillow_result.get("zestimate") or zillow_result.get("price")


if __name__ == "__main__":
    test_address = "6506 Clubway Ln, Austin, TX 78745"
    result = get_zillow_valuation(test_address)
    print("Full response:")
    print(result)
    print(f"\nZestimate extracted: {extract_zestimate(result)}")
