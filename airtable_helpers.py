"""
Shared Airtable read/write logic — used by BOTH main.py (the web server)
and orchestrator.py (the background worker), so there's exactly one place
this logic lives instead of two copies drifting apart over time.
"""

import os
from typing import Optional

import requests

AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Leads")
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"


class AirtableError(Exception):
    """Raised on any Airtable API failure — plain Python exception, not tied
    to FastAPI, so this module works the same whether it's called from the
    web server or the standalone background worker."""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Airtable error ({status_code}): {detail}")


def _airtable_headers():
    if not AIRTABLE_API_KEY:
        raise AirtableError(500, "AIRTABLE_API_KEY not set")
    return {
        "Authorization": f"Bearer {AIRTABLE_API_KEY}",
        "Content-Type": "application/json",
    }


def find_lead_record(address: str) -> Optional[dict]:
    """Returns the full Airtable record (id + fields) matching this address, or None."""
    params = {"filterByFormula": f"{{address}} = '{address}'"}
    response = requests.get(AIRTABLE_URL, headers=_airtable_headers(), params=params, timeout=15)
    if not response.ok:
        raise AirtableError(response.status_code, f"lookup failed: {response.text}")
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
        raise AirtableError(response.status_code, f"write failed: {response.text}")
    return response.json()


def query_leads(filter_formula: str, max_records: int = 50) -> list:
    """
    Returns a list of full records matching an Airtable filter formula.
    Example: query_leads("AND({status}='New', {arv}='')") finds new leads
    that haven't been enriched with valuation data yet.
    """
    params = {"filterByFormula": filter_formula, "maxRecords": max_records}
    response = requests.get(AIRTABLE_URL, headers=_airtable_headers(), params=params, timeout=15)
    if not response.ok:
        raise AirtableError(response.status_code, f"query failed: {response.text}")
    return response.json().get("records", [])
