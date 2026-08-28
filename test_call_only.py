"""
Retry ONLY the Vapi call trigger — skips RentCast/Zillow entirely by using
a cached ARV from a previous successful run. Use this while troubleshooting
the phone-not-answering issue so you don't burn RentCast credits on every
retry.
"""

from orchestrator_lib import trigger_vapi_call

# Cached from our last successful pre-fetch — no need to hit RentCast again
# while we're just testing whether the call itself connects and rings.
CACHED_ARV = 355000

TEST_ADDRESS = "6506 Clubway Ln, Austin, TX 78745"
TEST_PHONE = "+19565177010"  # your number, already confirmed correct format

if __name__ == "__main__":
    print("Triggering call with cached ARV (no RentCast call made)...")
    call_context = {
        "seller_name": "Test Seller",
        "property_address": TEST_ADDRESS,
        "recommended_arv": str(CACHED_ARV),
    }
    result = trigger_vapi_call(TEST_PHONE, call_context)
    print(f"Call triggered: {result.get('id')}")
    print(f"Status: {result.get('status')}")
