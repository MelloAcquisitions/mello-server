"""
PandaDoc integration — Create Document FROM TEMPLATE, not file upload.
This is the reliable, production-recommended pattern: fields are placed
once, manually, in PandaDoc's own editor (confirmed working — no more
fragile text-tag parsing), and this code just fills in real values and
sends it for each real deal.

SETUP:
1. export PANDADOC_API_KEY="your-key-here"
2. export PANDADOC_TEMPLATE_UUID="your-template-id-here"
   (found in the template's URL: app.pandadoc.com/a/#/templates/{ID}/content)
3. Set BUYER_EMAIL and BUYER_NAME below to your real business info —
   Mello Acquisitions is always the buyer, every deal, so this doesn't
   need to be passed in per-deal.

Field names below match EXACTLY what you named each field when building
the template in PandaDoc's editor — these must match precisely, since
PandaDoc matches by exact field name string.
"""

import os
import requests

PANDADOC_API_KEY = os.environ.get("PANDADOC_API_KEY")
PANDADOC_TEMPLATE_UUID = os.environ.get("PANDADOC_TEMPLATE_UUID")
BASE_URL = "https://api.pandadoc.com/public/v1/documents"

# Always true for every deal — you are always the buyer
BUYER_EMAIL = os.environ.get("BUYER_EMAIL", "your-business-email@example.com")
BUYER_NAME = os.environ.get("BUYER_NAME", "Mello Acquisitions LLC")


def _headers():
    if not PANDADOC_API_KEY:
        raise RuntimeError("PANDADOC_API_KEY not set")
    return {"Authorization": f"API-Key {PANDADOC_API_KEY}"}


def create_document_from_template(deal: dict, seller_email: str, seller_name: str,
                                    access_method: int = None) -> str:
    """
    deal: dict with contract_date, seller_name, buyer_name, subject_property,
    legal_description, purchase_price, acceptance_deadline, closing_date,
    title_company, other_agreements, governing_state, seller_phone, buyer_phone

    access_method: 1, 2, or 3 — which of the three PICTURES & ACCESS checkbox
    options applies to this deal (lockbox / key / scheduled walkthrough,
    matching whatever your three real options are). Leave None if not yet
    captured — all three checkboxes stay unchecked, safer than guessing wrong.
    This isn't captured anywhere else in the system yet — worth adding to
    the live call flow or human review step so this has a real answer.

    Only text/checkbox fields need values here — signature and date-of-
    signing fields are filled by each person as they actually sign, not by us.
    """
    if not PANDADOC_TEMPLATE_UUID:
        raise RuntimeError("PANDADOC_TEMPLATE_UUID not set")

    fields = {
        "DATE": {"value": deal["contract_date"]},
        "SELLER": {"value": deal["seller_name"]},
        "BUYER": {"value": deal["buyer_name"]},
        "SUBJECT PROPERTY": {"value": deal["subject_property"]},
        "LEGAL DESCRIPTION": {"value": deal["legal_description"]},
        "PURCHASE PRICE": {"value": deal["purchase_price"]},
        "DATE OF ACCEPTANCE": {"value": deal["acceptance_deadline"]},
        "DATE OF CLOSING": {"value": deal["closing_date"]},
        "TITLE COMPANY": {"value": deal["title_company"]},
        "OTHER AGREEMENTS": {"value": deal["other_agreements"]},
        "STATE OF": {"value": deal["governing_state"]},
        "SELLER PHONE NUMBER": {"value": deal["seller_phone"]},
        "BUYER PHONE NUMBER": {"value": deal["buyer_phone"]},
        "CHECK BOX 1": {"value": access_method == 1},
        "CHECK BOX 2": {"value": access_method == 2},
        "CHECK BOX 3": {"value": access_method == 3},
    }

    first_s, *last_s = seller_name.split(" ", 1)
    first_b, *last_b = deal["buyer_name"].split(" ", 1)

    payload = {
        "name": f"Purchase Agreement - {deal['subject_property']}",
        "template_uuid": PANDADOC_TEMPLATE_UUID,
        "recipients": [
            {"email": seller_email, "first_name": first_s,
             "last_name": last_s[0] if last_s else "", "role": "Seller"},
            {"email": BUYER_EMAIL, "first_name": first_b,
             "last_name": last_b[0] if last_b else "", "role": "Buyer"},
        ],
        "fields": fields,
    }

    response = requests.post(
        BASE_URL, headers={**_headers(), "Content-Type": "application/json"},
        json=payload, timeout=30,
    )
    if not response.ok:
        print(f"PandaDoc create failed ({response.status_code}): {response.text}")
    response.raise_for_status()
    print("Document created:")
    print(response.json())
    return response.json()["id"]


def send_document(document_id: str, message: str = "Please review and sign.") -> dict:
    """
    Documents created from a template usually reach 'document.draft' status
    fast (seconds, not the slower async processing file-upload had) — try
    sending directly; if it fails because the document isn't ready yet,
    wait a couple seconds and retry.
    """
    import time
    for attempt in range(5):
        response = requests.post(
            f"{BASE_URL}/{document_id}/send",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"message": message, "silent": False},
            timeout=15,
        )
        if response.ok:
            return response.json()
        print(f"Send attempt {attempt + 1} failed ({response.status_code}): {response.text}")
        time.sleep(3)
    response.raise_for_status()


def send_contract_for_signature(deal: dict, seller_email: str, seller_name: str,
                                  access_method: int = None) -> dict:
    """The full flow in one call — what you'd use in your real deal pipeline."""
    document_id = create_document_from_template(deal, seller_email, seller_name, access_method)
    result = send_document(document_id)
    print(f"Sent for signature: {document_id}")
    return result


if __name__ == "__main__":
    test_deal = {
        "contract_date": "August 26, 2026",
        "seller_name": "Jane Smith",
        "buyer_name": BUYER_NAME,
        "subject_property": "6506 Clubway Ln, Austin, TX 78745",
        "legal_description": "Lot 12, Block 3, Sunset Ridge Subdivision, Travis County, TX",
        "purchase_price": "$235,000",
        "acceptance_deadline": "September 2, 2026",
        "closing_date": "September 26, 2026",
        "title_company": "Austin Title Co.",
        "other_agreements": "None",
        "governing_state": "Texas",
        "seller_phone": "(555) 123-4567",
        "buyer_phone": "(555) 987-6543",
    }

    result = send_contract_for_signature(
        test_deal,
        seller_email="your-own-email@example.com",  # replace with YOUR real email to test safely
        seller_name="Jane Smith",
        access_method=1,
    )
    print("Final result:")
    print(result)
