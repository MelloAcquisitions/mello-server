"""
Box Sign integration — uploads a generated contract and creates a real
signature request. Two-step process, confirmed from Box's own docs:
upload the file first, THEN create the sign request referencing it.

SETUP:
1. Complete the Box Custom App setup (Client Credentials Grant, App Access
   Only, "Manage signature requests" scope under Content Actions)
2. Set these environment variables:
     BOX_CLIENT_ID
     BOX_CLIENT_SECRET
     BOX_ENTERPRISE_ID   (from Account Settings — NOT the developer token)

NOTE: I haven't tested this against a live Box account — verify the exact
field names in create_sign_request()'s response against what Box actually
returns before trusting this fully in production, same precaution as
every other new integration in this build.
"""

import os
import requests

BOX_CLIENT_ID = os.environ.get("BOX_CLIENT_ID")
BOX_CLIENT_SECRET = os.environ.get("BOX_CLIENT_SECRET")
BOX_ENTERPRISE_ID = os.environ.get("BOX_ENTERPRISE_ID")


def get_box_access_token() -> str:
    """
    Requests a fresh access token using Client Credentials Grant — this is
    what your Server-type app with App Access Only actually uses, not the
    developer token. Tokens expire after about an hour, so call this fresh
    each time you need one rather than caching it long-term.
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
    Uploads a file to Box, returns the new file's ID. folder_id="0" means
    your root folder — create a dedicated "Contracts" folder in Box and use
    its ID here instead, to keep things organized.
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


def create_sign_request(access_token: str, file_id: str, signer_email: str, folder_id: str = "0") -> dict:
    """
    Creates the actual signature request, sent to the signer's email by
    Box directly. Returns Box's response, which includes a status and
    (once available) a link to the signing session.
    """
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    payload = {
        "source_files": [{"type": "file", "id": file_id}],
        "signers": [{"email": signer_email, "role": "signer"}],
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


def send_contract_for_signature(file_path: str, signer_email: str) -> dict:
    """
    The full flow in one call: get a token, upload the file, create the
    sign request. This is what you'd call right after generating a
    contract with contract_generator.py.
    """
    token = get_box_access_token()
    file_id = upload_file_to_box(token, file_path)
    result = create_sign_request(token, file_id, signer_email)
    return result


if __name__ == "__main__":
    # Test with a real generated contract and a real email you control
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

    contract_path = generate_contract(test_deal, "test_box_contract.docx")
    result = send_contract_for_signature(contract_path, "your-own-email@example.com")
    print("Sign request created:")
    print(result)
