"""
PandaDoc integration — replaces box_sign.py. Simpler auth than Box (a
static API key, no OAuth dance), same two-step spirit: create the document
first, then send it once it's ready.

SETUP:
1. Sign up at pandadoc.com (free plan includes real API access as of
   June 2026 — 60 documents/year, 2 recipients per document, 5 templates)
2. Settings → API and Integrations → API → generate your key
3. export PANDADOC_API_KEY="your-key-here"

Your contract_template.docx now has real PandaDoc field tags —
[signature:assignor___] and [signature:assignee___] — embedded directly
in the signature lines, confirmed against PandaDoc's own documented
bracket-notation syntax.
"""

import os
import time
import requests

PANDADOC_API_KEY = os.environ.get("PANDADOC_API_KEY")
BASE_URL = "https://api.pandadoc.com/public/v1/documents"


def _headers():
    if not PANDADOC_API_KEY:
        raise RuntimeError("PANDADOC_API_KEY not set")
    return {"Authorization": f"API-Key {PANDADOC_API_KEY}"}


def create_document_from_file(file_path: str, assignor_email: str, assignor_name: str,
                                assignee_email: str, assignee_name: str,
                                document_name: str = "Assignment of Contract") -> str:
    """
    Uploads the filled contract and creates a PandaDoc document from it.
    Two recipients, matching the [signature:assignor___] and
    [signature:assignee___] field tags in the template. Returns the new
    document's ID — NOT ready to send yet, see wait_for_draft_status().
    """
    first_a, *last_a = assignor_name.split(" ", 1)
    first_b, *last_b = assignee_name.split(" ", 1)

    data = {
        "name": document_name,
        "recipients": [
            {"email": assignor_email, "first_name": first_a,
             "last_name": last_a[0] if last_a else "", "role": "assignor"},
            {"email": assignee_email, "first_name": first_b,
             "last_name": last_b[0] if last_b else "", "role": "assignee"},
        ],
        "parse_form_fields": False,  # we're using bracket field TAGS in the text, not native PDF form fields
    }

    with open(file_path, "rb") as f:
        files = {
            "file": (os.path.basename(file_path), f),
        }
        import json
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
    """
    Document creation is asynchronous — starts at 'document.uploaded' and
    needs to reach 'document.draft' before it can be sent. Polls every 2
    seconds. Returns True once ready, False if it times out.
    """
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
    """Actually sends the document to both recipients for signature."""
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


def send_contract_for_signature(file_path: str, assignor_email: str, assignor_name: str,
                                  assignee_email: str, assignee_name: str) -> dict:
    """The full flow in one call — what you'd use in your real deal pipeline."""
    document_id = create_document_from_file(
        file_path, assignor_email, assignor_name, assignee_email, assignee_name
    )
    print(f"Document created: {document_id}, waiting for it to process...")

    if not wait_for_draft_status(document_id):
        raise TimeoutError(f"Document {document_id} never reached draft status — check it manually in PandaDoc")

    result = send_document(document_id)
    print(f"Sent for signature: {document_id}")
    return result


if __name__ == "__main__":
    from contract_generator import generate_contract

    test_deal = {
        "assignor_name": "Jane Smith",
        "assignee_name": "Mello Acquisitions LLC",
        "subject_property": "6506 Clubway Ln, Austin, TX 78745",
        "legal_description": "Lot 12, Block 3, Sunset Ridge Subdivision, Travis County, TX",
        "sale_price": "$297,000",
        "deposit_amount": "$1,000",
        "closing_attorney": "Austin Title Co.",
        "closing_terms": "Within 30 days of contract acceptance",
        "transaction_fee": "500",
    }

    contract_path = generate_contract(test_deal, "test_pandadoc_contract.docx")
    result = send_contract_for_signature(
        contract_path,
        assignor_email="rodrigo.lozhdz@gmail.com",  # replace with YOUR real email to test safely
        assignor_name="Jane Smith",
        assignee_email="mello.acquisitions@gmail.com",  # replace with your real business email
        assignee_name="Mello Acquisitions LLC",
    )
    print("Final result:")
    print(result)
