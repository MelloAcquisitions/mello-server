"""
Fills the Purchase Agreement template with deal-specific details and saves
a real .docx file ready to send via PandaDoc.

SETUP: pip install docxtpl python-docx
Requires purchase_agreement_template.docx to exist in the same folder.

This is the contract used to buy FROM the seller — not the later
assignment/resale step, which is handled manually outside this system.
"""

from docxtpl import DocxTemplate


def generate_contract(deal: dict, output_path: str = "generated_contract.docx") -> str:
    """
    deal: dict with keys matching the template's merge fields —
    contract_date, seller_name, buyer_name, subject_property,
    legal_description, purchase_price, acceptance_deadline, closing_date,
    title_company, other_agreements, governing_state, seller_phone, buyer_phone

    Returns the path to the generated file, ready to send via PandaDoc.
    """
    doc = DocxTemplate("purchase_agreement_template.docx")
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
