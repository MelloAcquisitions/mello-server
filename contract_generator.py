"""
Fills the Purchase Agreement template with deal-specific details and saves
a real .docx ready to send for signature.

This is the contract used to buy FROM the seller — not the later
assignment/resale step, which is handled manually outside this system.

SETUP: pip install docxtpl python-docx
Requires purchase_agreement_template.docx to sit beside this file.

CHANGES IN THIS CLEANUP
-----------------------
- The template path is now resolved relative to THIS FILE, not the working
  directory. `DocxTemplate("purchase_agreement_template.docx")` only worked
  when the process happened to be started from the repo root. Contract
  generation runs inside a FastAPI BackgroundTask on the live call path,
  where a wrong CWD means the contract silently fails to generate on the
  one request that matters most.
- Missing merge fields are now reported. docxtpl renders an unknown
  variable as an empty string, so a schema change would quietly ship a
  contract with a blank purchase price rather than raising.
- Removed a stale docstring line telling you to send the result via
  PandaDoc, which this project stopped using.
"""

import os

from docxtpl import DocxTemplate

TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "purchase_agreement_template.docx"
)

# Every merge field the template expects. Keep in sync with the .docx.
REQUIRED_FIELDS = {
    "contract_date", "seller_name", "buyer_name", "subject_property",
    "legal_description", "purchase_price", "acceptance_deadline",
    "closing_date", "title_company", "other_agreements", "governing_state",
    "seller_phone", "buyer_phone",
}


def generate_contract(deal: dict, output_path: str = "generated_contract.docx") -> str:
    """
    deal: dict with keys matching the template's merge fields.
    Returns the path to the generated file.
    """
    if not os.path.exists(TEMPLATE_PATH):
        raise FileNotFoundError(
            f"Contract template not found at {TEMPLATE_PATH}. It must be committed "
            f"to the repo alongside this file, not just present locally."
        )

    missing = REQUIRED_FIELDS - set(deal)
    if missing:
        # Not fatal — a contract with a placeholder is more useful than no
        # contract at all when a seller has already said yes — but it must
        # be visible, because docxtpl renders an unknown variable as an
        # empty string with no complaint.
        print(f"WARNING: contract is missing merge field(s): {sorted(missing)}. "
              f"They will render blank in the .docx.")
        deal = {**{k: f"[{k.upper()} MISSING]" for k in missing}, **deal}

    doc = DocxTemplate(TEMPLATE_PATH)
    doc.render(deal)
    doc.save(output_path)
    return output_path


if __name__ == "__main__":
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

    path = generate_contract(test_deal)
    print(f"Contract generated: {path}")
    print("Next: pass the deal + agreed price into dispatch_agreed_deal() in "
          "deal_dispatch.py to test the email-to-owner flow (Box Sign is on hold).")
