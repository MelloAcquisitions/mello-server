"""
Fills the contract template with deal-specific details (seller name,
address, price, dates) and saves a real .docx file ready to upload to Box.

SETUP: pip install docxtpl python-docx
Requires contract_template.docx to exist in the same folder — this is the
template with {{ merge_field }} placeholders.
"""

from docxtpl import DocxTemplate


def generate_contract(deal: dict, output_path: str = "generated_contract.docx") -> str:
    """
    deal: dict with keys matching the template's merge fields —
    seller_name, property_address, purchase_price, earnest_money,
    closing_date, contract_date

    Returns the path to the generated file, ready to upload to Box.
    """
    doc = DocxTemplate("contract_template.docx")
    doc.render(deal)
    doc.save(output_path)
    return output_path


if __name__ == "__main__":
    test_deal = {
        "seller_name": "Jane Smith",
        "property_address": "6506 Clubway Ln, Austin, TX 78745",
        "purchase_price": "297,000",
        "earnest_money": "1,000",
        "closing_date": "September 15, 2026",
        "contract_date": "August 20, 2026",
    }

    path = generate_contract(test_deal)
    print(f"Contract generated: {path}")
