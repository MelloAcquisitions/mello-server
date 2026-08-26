"""
PandaDoc integration — uploads the generated Purchase Agreement and
creates a real signature request for Seller and Buyer.

SETUP:
1. Sign up at pandadoc.com (free plan includes real API access — 60
   documents/year, 2 recipients per document, 5 templates)
2. Settings -> API and Integrations -> API -> generate your key
3. export PANDADOC_API_KEY="your-key-here"

purchase_agreement_template.docx has real PandaDoc field tags embedded in
BOTH signature blocks (the document has two identical copies of the
signature line): [signature:seller:sig1___], [date:seller:dt1___],
[signature:buyer:sig2___], [date:buyer:dt2___] for the first block, and
sig3/dt3/sig4/dt4 for the second — confirmed against PandaDoc's documented
[fieldtype:role:uniqueID___] bracket syntax.
"""

import os
import json
import time
import requests

PANDADOC_API_KEY = os.environ.get("PANDADOC_API_KEY")
BASE_URL = "https://api.pandadoc.com/public/v1/documents"


def _headers():
    if not PANDADOC_API_KEY:
        raise RuntimeError("PANDADOC_API_KEY not set")
    return {"Authorization": f"API-Key {PANDADOC_API_KEY}"}


def create_document_from_file(file_path: str, seller_email: str, seller_name: str,
                                buyer_email: str, buyer_name: str,
                                document_name: str = "Purchase Agreement") -> str:
    """
    Uploads the filled Purchase Agreement and creates a PandaDoc document
    from it. Two recipients — seller and buyer — matching the field tags
    in the template (both signature blocks). Returns the new document's
    ID, NOT ready to send yet — see wait_for_draft_status().
    """
    first_s, *last_s = seller_name.split(" ", 1)
    first_b, *last_b = buyer_name.split(" ", 1)

    data = {
        "name": document_name,
        "recipients": [
            {"email": seller_email, "first_name": first_s,
             "last_name": last_s[0] if last_s else "", "role": "seller"},
            {"email": buyer_email, "first_name": first_b,
             "last_name": last_b[0] if last_b else "", "role": "buyer"},
        ],
        "parse_form_fields": False,  # using bracket field TAGS in the text, not native PDF form fields
        # Every field tag's unique ID must also be declared explicitly here —
        # the tag in the document text alone isn't sufficient for PandaDoc
        # to recognize it. Four fields per role since the document has the
        # signature block duplicated (sig1/dt1/sig3/dt3 for seller,
        # sig2/dt2/sig4/dt4 for buyer).
        "fields": {
            "sig1": {"value": "", "role": "seller"},
            "dt1": {"value": "", "role": "seller"},
            "sig2": {"value": "", "role": "buyer"},
            "dt2": {"value": "", "role": "buyer"},
            "sig3": {"value": "", "role": "seller"},
            "dt3": {"value": "", "role": "seller"},
            "sig4": {"value": "", "role": "buyer"},
            "dt4": {"value": "", "role": "buyer"},
        },
    }

    with open(file_path, "rb") as f:
        files = {"file": (os.path.basename(file_path), f)}
        response = requests.post(
            BASE_URL,
            headers=_headers(),
            files=files,
            data={"data": json.dumps(data)},
            timeout=30,
        )
    if not response.ok:
        print(f"PandaDoc create failed ({response.status_code}): {response.text}")
    response.raise_for_status()
    return response.json()["id"]


def wait_for_draft_status(document_id: str, timeout_seconds: int = 60) -> bool:
    """Polls every 2 seconds until the document reaches 'document.draft'."""
    elapsed = 0
    while elapsed < timeout_seconds:
        response = requests.get(f"{BASE_URL}/{document_id}", headers=_headers(), timeout=15)
        response.raise_for_status()
        status = response.json().get("status")
        print(f"  Document status: {status}")
        if status == "document.draft":
            return True
        time.sleep(2)
        elapsed += 2
    return False


def send_document(document_id: str, message: str = "Please review and sign.") -> dict:
    """Sends the document to both recipients for signature."""
    payload = {"message": message, "silent": False}
    response = requests.post(
        f"{BASE_URL}/{document_id}/send",
        headers={**_headers(), "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    if not response.ok:
        print(f"PandaDoc send failed ({response.status_code}): {response.text}")
    response.raise_for_status()
    return response.json()


def send_contract_for_signature(file_path: str, seller_email: str, seller_name: str,
                                  buyer_email: str, buyer_name: str) -> dict:
    """The full flow in one call — what you'd use in your real deal pipeline."""
    document_id = create_document_from_file(file_path, seller_email, seller_name, buyer_email, buyer_name)
    print(f"Document created: {document_id}, waiting for it to process...")

    if not wait_for_draft_status(document_id):
        raise TimeoutError(f"Document {document_id} never reached draft status — check it manually in PandaDoc")

    result = send_document(document_id)
    print(f"Sent for signature: {document_id}")
    return result


if __name__ == "__main__":
    from contract_generator import generate_contract

    test_deal = {
        "contract_date": "August 26, 2026",
        "seller_name": "Jane Smith",
        "buyer_name": "Mello Acquisitions LLC",
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

    contract_path = generate_contract(test_deal, "test_pandadoc_contract.docx")
    result = send_contract_for_signature(
        contract_path,
        seller_email="rodrigo.lozhdz@gmail.com",       # replace with YOUR real email to test safely
        seller_name="Jane Smith",
        buyer_email="mello.acquisitions@gmail.com",   # replace with your real business email
        buyer_name="Mello Acquisitions LLC",
    )
    print("Final result:")
    print(result)
