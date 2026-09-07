"""
Zillow valuation lookup via Zillapi (a third-party REST wrapper — Zillow
itself has had no public API since 2021). This is the SECOND, independent
valuation source, cross-checking RentCast with a genuinely different
underlying model (Zillow's Zestimate).

SETUP:
1. Sign up at zillapi.com — free tier gives 100 credits, no card required
2. Set ZILLAPI_KEY="zk_your-key-here"
3. Run: python zillapi_lookup.py

CONFIRMED against a live 404: the address-lookup endpoint is
/v1/properties/by-address, not /v1/properties, and the response comes
wrapped in a {"data": {...}} envelope, not flat.

CHANGES IN THIS CLEANUP
-----------------------
extract_zestimate() now handles a list response and a non-dict result.
Zillapi's by-address endpoint has been observed returning a single object,
but a `.get()` on a list raises AttributeError — and because the caller
(orchestrator_lib.enrich_lead_with_valuation) wraps the whole Zillow lookup
in a broad `except Exception`, that crash would have been swallowed as
"Zillow lookup failed" and the third valuation source silently dropped for
every lead, forever, with no obvious symptom.
"""

import os

import requests

ZILLAPI_KEY = os.environ.get("ZILLAPI_KEY")
BASE_URL = "https://api.zillapi.com/v1/properties/by-address"


def get_zillow_valuation(full_address: str) -> dict:
    """
    Looks up a property by address and returns Zillow-sourced data,
    including the Zestimate — a genuinely independent figure from RentCast's
    own AVM, useful as a real second opinion.

    Returns the "data" object directly (the envelope is unwrapped here), so
    callers do not need to know about Zillapi's response wrapper.
    """
    if not ZILLAPI_KEY:
        raise RuntimeError("ZILLAPI_KEY environment variable is not set.")

    response = requests.get(
        BASE_URL,
        headers={"Authorization": f"Bearer {ZILLAPI_KEY}"},
        params={"address": full_address},
        timeout=15,
    )
    if not response.ok:
        print(f"Zillapi returned an error ({response.status_code}): {response.text[:400]}")
    response.raise_for_status()

    body = response.json()
    # Real responses come wrapped as {"data": {...}, "request_id": "..."}.
    # Fall back to the raw body if that shape ever changes, rather than
    # silently returning nothing useful.
    if isinstance(body, dict):
        return body.get("data", body)
    return body


def extract_zestimate(zillow_result) -> float:
    """
    Pulls just the Zestimate out of the (already-unwrapped) result,
    defensively — returns None rather than raising if the schema differs.
    """
    if isinstance(zillow_result, list):
        zillow_result = zillow_result[0] if zillow_result else None
    if not isinstance(zillow_result, dict):
        return None
    value = zillow_result.get("zestimate")
    if value in (None, ""):
        value = zillow_result.get("price")
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    test_address = "6506 Clubway Ln, Austin, TX 78745"
    result = get_zillow_valuation(test_address)
    print("Full response:")
    print(result)
    print(f"\nZestimate extracted: {extract_zestimate(result)}")
