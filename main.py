"""
Mello Acquisitions — Agent Tool Server

This is the "toolbox" Vapi calls into during a live call. Claude decides
WHEN to call these; this server does the actual work (property lookups,
math, writing to Airtable) and hands the result back.

SETUP:
1. pip install -r requirements.txt
2. Set these environment variables (locally for testing, and on your hosting
   platform once deployed):
     RENTCAST_API_KEY
     AIRTABLE_API_KEY
     AIRTABLE_BASE_ID       (from your Airtable base's URL or API docs page)
     AIRTABLE_TABLE_NAME    (e.g. "Leads")
3. Run locally to test: uvicorn main:app --reload
   Then visit http://127.0.0.1:8000/docs for an interactive test page —
   FastAPI builds this automatically, no extra work needed.
4. Deploy (see deployment steps provided separately) to get a public URL.
"""

import os
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from calculator import flip_mao, wholetail_calculator
from rentcast_lookup import (
    get_sold_comps,
    get_property_valuation,
    analyze_sold_comps,
    get_recommended_arv,
)

app = FastAPI(title="Mello Acquisitions Agent Tools")

AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Leads")
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"


# ---------------------------------------------------------------------------
# Request/response schemas — FastAPI uses these to validate incoming calls
# and auto-generate the /docs test page.
# ---------------------------------------------------------------------------

class PropertyAnalysisRequest(BaseModel):
    address: str
    city: str
    state: str
    zip_code: str


class MaoRequest(BaseModel):
    arv: float
    repair_cost: float
    wholesale_fee: float = 10000
    desired_profit: float = 0


class WholetailRequest(BaseModel):
    cmv: float
    repair_cost: float
    buyer_profit: float
    wholesale_fee: float


class LogCallRequest(BaseModel):
    address: str
    status: str  # new | contacted | qualified | offer_made | agreed | rejected | opt_out | callback_requested
    notes: Optional[str] = ""
    offer_amount: Optional[float] = None
    arv: Optional[float] = None


class FlagReviewRequest(BaseModel):
    address: str
    agreed_price: float
    call_transcript_summary: str


# ---------------------------------------------------------------------------
# Airtable helpers
# ---------------------------------------------------------------------------

def _airtable_headers():
    if not AIRTABLE_API_KEY:
        raise HTTPException(500, "AIRTABLE_API_KEY not set on the server")
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }


def find_lead_record(address: str) -> Optional[dict]:
    """Returns the full Airtable record (id + fields) matching this address, or None."""
    params = {"filterByFormula": f"{{address}} = '{address}'"}
    response = requests.get(AIRTABLE_URL, headers=_airtable_headers(), params=params, timeout=15)
    if not response.ok:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Airtable error on lookup: {response.text}",
        )
    records = response.json().get("records", [])
    return records[0] if records else None


def upsert_lead(address: str, fields: dict) -> dict:
    """Creates a new lead record, or updates the existing one for this address."""
    existing_record = find_lead_record(address)
    payload = {"fields": fields}

    if existing_record:
        response = requests.patch(
            f"{AIRTABLE_URL}/{existing_record['id']}", headers=_airtable_headers(), json=payload, timeout=15
        )
    else:
        payload["fields"]["address"] = address
        response = requests.post(AIRTABLE_URL, headers=_airtable_headers(), json=payload, timeout=15)

    if not response.ok:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Airtable error on write: {response.text}",
        )
    return response.json()


# ---------------------------------------------------------------------------
# Endpoints — these URLs are what you paste into Vapi's tool configuration
# ---------------------------------------------------------------------------

@app.get("/")
def health_check():
    """Quick check that the server is alive — visit this URL in a browser after deploying."""
    return {"status": "Mello agent server is running"}


@app.post("/get_property_analysis")
def get_property_analysis(req: PropertyAnalysisRequest):
    """
    The main research call. Pulls sold comps + AVM estimate, reconciles them,
    and returns a conservative recommended ARV plus supporting data
    (investor buyer activity, comp count, etc.) for the agent to use.
    """
    full_address = f"{req.address}, {req.city}, {req.state} {req.zip_code}"

    avm_result = get_property_valuation(full_address)
    subject = avm_result.get("subjectProperty", {})

    sold_result = get_sold_comps(full_address, subject_property=subject)
    sold_properties = sold_result if isinstance(sold_result, list) else sold_result.get("properties", [])

    analysis = analyze_sold_comps(sold_properties, subject_property=subject)
    recommendation = get_recommended_arv(analysis, avm_result)

    return {
        "recommended_arv": recommendation["recommended_arv"],
        "source": recommendation["source"],
        "spread_pct": recommendation["spread_pct"],
        "comp_count": analysis["comp_count"],
        "investor_buyer_pct": analysis["investor_buyer_pct"],
        "subject_property": {
            "squareFootage": subject.get("squareFootage"),
            "yearBuilt": subject.get("yearBuilt"),
            "bedrooms": subject.get("bedrooms"),
            "bathrooms": subject.get("bathrooms"),
        },
    }


@app.post("/calculate_mao")
def calculate_mao_endpoint(req: MaoRequest):
    """Standard flip-deal MAO calculation — returns ceiling and opening offer."""
    return flip_mao(
        arv=req.arv,
        repair_cost=req.repair_cost,
        wholesale_fee=req.wholesale_fee,
        desired_profit=req.desired_profit,
    )


@app.post("/calculate_wholetail")
def calculate_wholetail_endpoint(req: WholetailRequest):
    """Alternative calculation for higher-value / lighter-rehab deals."""
    return wholetail_calculator(
        cmv=req.cmv,
        repair_cost=req.repair_cost,
        buyer_profit=req.buyer_profit,
        wholesale_fee=req.wholesale_fee,
    )


@app.post("/log_call_outcome")
def log_call_outcome(req: LogCallRequest):
    """
    Writes (or updates) the lead's record in Airtable. Called at the end of
    EVERY call, regardless of outcome — including opt-outs and rejections.

    Automatically increments the #_calls column by 1 each time this fires,
    since one call outcome logged = one call made.

    NOTE: writes to an "offer_amount" field — this column must exist in
    your Airtable table (currency type) or this will fail. If you haven't
    added it yet, add it before testing this endpoint.
    """
    existing_record = find_lead_record(req.address)
    current_call_count = existing_record["fields"].get("#_calls", 0) if existing_record else 0

    fields = {
        "status": req.status,
        "call_transcript_summary": req.notes,
        "last_call_date": __import__("datetime").date.today().isoformat(),
        "#_calls": current_call_count + 1,
    }
    if req.offer_amount is not None:
        fields["offer_amount"] = req.offer_amount
    if req.arv is not None:
        fields["arv"] = req.arv

    result = upsert_lead(req.address, fields)
    return {"success": True, "airtable_record": result.get("id"), "call_count": current_call_count + 1}


@app.post("/flag_for_human_review")
def flag_for_human_review(req: FlagReviewRequest):
    """
    Called ONLY when a seller verbally agrees to a price. Marks the lead as
    needing human review before any contract is sent — this does NOT send
    a contract itself. Pair this with an Airtable automation (built in
    Airtable's own interface, no code needed) that emails/texts you whenever
    a record's status changes to "Agreed", so you get a fast notification
    to approve without holding up the call.
    """
    fields = {
        "status": "agreed",
        "offer_amount": req.agreed_price,
        "call_transcript_summary": req.call_transcript_summary,
        "last_call_date": __import__("datetime").date.today().isoformat(),
    }
    result = upsert_lead(req.address, fields)
    return {"success": True, "airtable_record": result.get("id"), "needs_human_review": True}
