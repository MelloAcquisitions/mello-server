"""
The smallest possible test of the new architecture: pre-fetch valuation
data, then trigger ONE real Vapi call with that data already baked in as
variables — no live tool-calling to Render needed during the conversation
at all. This is the new "true base" to prove before building anything else.

SETUP: same env vars as orchestrator.py — VAPI_API_KEY, VAPI_ASSISTANT_ID,
VAPI_PHONE_NUMBER_ID, RENTCAST_API_KEY, ZILLAPI_KEY

IMPORTANT: update mello-system-prompt.md so the disclosure/offer logic
references {{recommended_arv}}, {{mao_ceiling}}, {{opening_offer}} directly
via Liquid syntax, instead of instructing the agent to call
get_property_analysis mid-call. That's what actually removes the live
dependency — passing the variables alone doesn't help if the prompt still
tells the agent to fetch them itself.
"""

from orchestrator_lib import enrich_lead_with_valuation, trigger_vapi_call
from calculator import flip_mao

# Real test property — same one we've verified data for throughout this build
TEST_ADDRESS = "6506 Clubway Ln"
TEST_CITY = "Austin"
TEST_STATE = "TX"
TEST_ZIP = "78745"
TEST_PHONE = "+1XXXXXXXXXX"  # replace with YOUR OWN number to test safely

if __name__ == "__main__":
    print("Step 1: Pre-fetching valuation data...")
    valuation = enrich_lead_with_valuation(TEST_ADDRESS, TEST_CITY, TEST_STATE, TEST_ZIP)
    print(f"  Recommended ARV: {valuation['recommended_arv']}")

    print("\nStep 2: Sanity-checking the MAO formula locally (not sent to the call — ")
    print("the live agent runs this itself once it has a real repair estimate)...")
    mao = flip_mao(arv=valuation["recommended_arv"], repair_cost=15000)
    if mao["no_deal"]:
        print(f"  No deal: {mao['reason']}")
    else:
        print(f"  MAO floor: {mao['mao_floor']} | Opening offer: {mao['opening_offer']} | Fee: {mao['wholesale_fee']}")

    print("\nStep 3: Triggering the real Vapi call — only address/ARV/name pre-baked,")
    print("the agent calculates the actual offer live once it hears about the condition...")
    call_context = {
        "seller_name": "Test Seller",  # real leads have this from owner_name in lead_sourcing.py
        "property_address": f"{TEST_ADDRESS}, {TEST_CITY}, {TEST_STATE} {TEST_ZIP}",
        "recommended_arv": str(valuation["recommended_arv"]),
    }
    result = trigger_vapi_call(TEST_PHONE, call_context)
    print(f"  Call triggered: {result}")
