"""
Box Sign integration — uploads the generated Purchase Agreement and
creates a real signature request for Seller and Buyer.

SETUP:
1. Upgrade to Box Business Starter (or above) — required for Box Sign API
   access with a real Admin Console (confirmed: Solo/Personal plans don't
   have this at all).
2. Create a Custom App in the Box Developer Console:
   - App Type: Server (Client Credentials Grant)
   - App Access Level: App Access Only
   - Content Actions: enable "Manage signature requests" (this
     auto-includes the read/write file scopes it depends on)
3. Authorize the app in Admin Console > Apps > Custom Apps Manager
4. Get your Enterprise ID from Account Settings (NOT the developer token —
   that expires in 1 hour and isn't for production use)
5. Set environment variables:
     BOX_CLIENT_ID
     BOX_CLIENT_SECRET
     BOX_ENTERPRISE_ID

HONEST NOTE: the upload + create-sign-request flow below is confirmed
against Box's own API documentation, but we have NOT yet run a live test
against a real Box account to confirm exact signature field placement
behavior. Box Sign can either (a) auto-place a generic signature block per
recipient, or (b) require you to visit a "prepare_url" once to manually
position fields — similar to the "build once, reuse via template" pattern
we learned the hard way with PandaDoc. Run this once, see what Box
actually does, and we'll adjust based on the real result rather than
guessing further.
"""

import os
import requests

BOX_CLIENT_ID = os.environ.get("BOX_CLIENT_ID")
BOX_CLIENT_SECRET = os.environ.get("BOX_CLIENT_SECRET")
BOX_ENTERPRISE_ID = os.environ.get("BOX_ENTERPRISE_ID")


def get_box_access_token() -> str:
    """
    Requests a fresh access token using Client Credentials Grant. Tokens
    expire after about an hour — call this fresh each time, don't cache
    long-term.
    """
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
        print(f"Box token request failed ({response.status_code}): {response.text}")
    response.raise_for_status()
    return response.json()["access_token"]


def upload_file_to_box(access_token: str, file_path: str, folder_id: str = "0") -> str:
    """
    Uploads a file to Box, returns the new file's ID. folder_id="0" is
    your root folder — create a dedicated "Contracts" folder in Box and
    use its ID here instead, to keep things organized as volume grows.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    with open(file_path, "rb") as f:
        files = {
            "attributes": (None, f'{{"name": "{os.path.basename(file_path)}", "parent": {{"id": "{folder_id}"}}}}'),
            "file": (os.path.basename(file_path), f),
        }
        response = requests.post(
            "https://upload.box.com/api/2.0/files/content",
            headers=headers,
            files=files,
            timeout=30,
        )
    if not response.ok:
        print(f"Box upload failed ({response.status_code}): {response.text}")
    response.raise_for_status()
    return response.json()["entries"][0]["id"]


def create_sign_request(access_token: str, file_id: str, seller_email: str, buyer_email: str,
                          folder_id: str = "0") -> dict:
    """
    Creates the signature request, sent to both parties by Box directly.
    Two signers: seller and buyer (Mello Acquisitions). Returns Box's full
    response — check this for a "prepare_url" field, which would mean a
    one-time manual field-placement step is needed (see module docstring).
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
    response = requests.post(
        "https://api.box.com/2.0/sign_requests",
        headers=headers,
        json=payload,
        timeout=30,
    )
    if not response.ok:
        print(f"Box sign request failed ({response.status_code}): {response.text}")
    response.raise_for_status()
    return response.json()


def send_contract_for_signature(file_path: str, seller_email: str, buyer_email: str) -> dict:
    """The full flow in one call — what you'd use in your real deal pipeline."""
    token = get_box_access_token()
    file_id = upload_file_to_box(token, file_path)
    result = create_sign_request(token, file_id, seller_email, buyer_email)
    return result


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

    contract_path = generate_contract(test_deal, "test_box_contract.docx")
    result = send_contract_for_signature(
        contract_path,
        seller_email="rodrigo.lozhdz@gmail.com",       # replace with YOUR real email to test safely
        buyer_email="mello.acquisitions@gmail.com",   # replace with your real business email
    )
    print("Full result:")
    print(result)
