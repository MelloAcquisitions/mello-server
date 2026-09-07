"""
Box Sign integration — ON HOLD, NOT WIRED INTO THE LIVE PIPELINE.

Kept because the flow is written and correct against Box's documented API,
and because deal_dispatch.build_deal_dict() is deliberately shaped so this
can replace the "email the owner" step without touching anything upstream.
Nothing imports this today: contracts currently email to OWNER_EMAIL for
manual signature routing.

SETUP (when you pick this back up):
1. Upgrade to Box Business Starter or above — Box Sign API access needs a
   real Admin Console (Solo/Personal plans do not have one).
2. Create a Custom App in the Box Developer Console:
   - App Type: Server (Client Credentials Grant)
   - App Access Level: App Access Only
   - Content Actions: enable "Manage signature requests"
3. Authorize the app in Admin Console > Apps > Custom Apps Manager
4. Get your Enterprise ID from Account Settings (NOT the developer token —
   that expires in an hour and is not for production).
5. Set BOX_CLIENT_ID, BOX_CLIENT_SECRET, BOX_ENTERPRISE_ID.

HONEST NOTE: the upload + create-sign-request flow is confirmed against
Box's documentation but has NOT been run against a real Box account. Box
Sign may either auto-place a generic signature block per recipient, or
require visiting a "prepare_url" once to position fields manually — the
same "build once, reuse via template" pattern learned the hard way with
PandaDoc. Run it once, look at what Box actually returns, then adjust.

CHANGE IN THIS CLEANUP: running this file directly is now a DRY RUN. It
previously generated a contract and immediately fired a real signature
request to two hardcoded placeholder addresses. Pass --send to actually
send, once you have put real addresses in.
"""

import os
import sys

import requests

BOX_CLIENT_ID = os.environ.get("BOX_CLIENT_ID")
BOX_CLIENT_SECRET = os.environ.get("BOX_CLIENT_SECRET")
BOX_ENTERPRISE_ID = os.environ.get("BOX_ENTERPRISE_ID")


def get_box_access_token() -> str:
    """Fresh access token via Client Credentials Grant. Tokens expire after
    about an hour — call this each time, do not cache long-term."""
    if not all([BOX_CLIENT_ID, BOX_CLIENT_SECRET, BOX_ENTERPRISE_ID]):
        raise RuntimeError("BOX_CLIENT_ID, BOX_CLIENT_SECRET, or BOX_ENTERPRISE_ID not set")

    response = requests.post(
        "https://api.box.com/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": BOX_CLIENT_ID,
            "client_secret": BOX_CLIENT_SECRET,
            "box_subject_type": "enterprise",
            "box_subject_id": BOX_ENTERPRISE_ID,
        },
        timeout=15,
    )
    if not response.ok:
        print(f"Box token request failed ({response.status_code}): {response.text[:400]}")
    response.raise_for_status()
    return response.json()["access_token"]


def upload_file_to_box(access_token: str, file_path: str, folder_id: str = "0") -> str:
    """
    Uploads a file to Box, returns the new file's ID. folder_id="0" is the
    root folder — create a dedicated "Contracts" folder and use its ID here
    instead, to stay organized as volume grows.
    """
    import json
    headers = {"Authorization": f"Bearer {access_token}"}
    attributes = json.dumps({"name": os.path.basename(file_path), "parent": {"id": folder_id}})
    with open(file_path, "rb") as f:
        files = {
            "attributes": (None, attributes),
            "file": (os.path.basename(file_path), f),
        }
        response = requests.post(
            "https://upload.box.com/api/2.0/files/content",
            headers=headers, files=files, timeout=30,
        )
    if not response.ok:
        print(f"Box upload failed ({response.status_code}): {response.text[:400]}")
    response.raise_for_status()
    return response.json()["entries"][0]["id"]


def create_sign_request(access_token: str, file_id: str, seller_email: str,
                        buyer_email: str, folder_id: str = "0") -> dict:
    """
    Creates the signature request, sent to both parties by Box directly.
    Check the response for a "prepare_url" field — that means a one-time
    manual field-placement step is needed (see module docstring).
    """
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    payload = {
        "source_files": [{"type": "file", "id": file_id}],
        "signers": [
            {"email": seller_email, "role": "signer"},
            {"email": buyer_email, "role": "signer"},
        ],
        "parent_folder": {"id": folder_id, "type": "folder"},
    }
    response = requests.post("https://api.box.com/2.0/sign_requests",
                             headers=headers, json=payload, timeout=30)
    if not response.ok:
        print(f"Box sign request failed ({response.status_code}): {response.text[:400]}")
    response.raise_for_status()
    return response.json()


def send_contract_for_signature(file_path: str, seller_email: str, buyer_email: str) -> dict:
    """The full flow in one call — what the real deal pipeline would use."""
    token = get_box_access_token()
    file_id = upload_file_to_box(token, file_path)
    return create_sign_request(token, file_id, seller_email, buyer_email)


if __name__ == "__main__":
    from contract_generator import generate_contract

    test_deal = {
        "contract_date": "August 27, 2026",
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

    contract_path = generate_contract(test_deal, "/tmp/test_box_contract.docx")
    print(f"Generated: {contract_path}")

    if "--send" not in sys.argv:
        print("\nDRY RUN. This would upload the file to Box and create a real "
              "signature request.\nPut your own real addresses in below, then "
              "re-run with --send.")
        sys.exit(0)

    result = send_contract_for_signature(
        contract_path,
        seller_email="your-own-email@example.com",      # replace to test safely
        buyer_email="your-business-email@example.com",  # replace with your business email
    )
    print("Full result:")
    print(result)
