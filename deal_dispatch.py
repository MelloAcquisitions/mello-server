"""
Fires the moment a seller verbally agrees to a price. Generates the filled
purchase agreement and emails it to YOU (the business owner) — not the
seller — since Box Sign is on hold and adding real signature fields is
still a manual step for now.

Interim workflow this supports:
  1. Seller agrees on a call -> flag_for_human_review is called
  2. This module generates the contract .docx and emails it to OWNER_EMAIL
     along with a deal summary (seller contact info, agreed price, ARV,
     repair estimate, call notes)
  3. You open the email, review, add a signature field, and send it to the
     seller yourself
  4. Once this is proven out and worth the cost, swap this module's
     "email the owner" step for box_sign.py's "send directly to seller for
     e-signature" flow — build_deal_dict() below stays the same either way.

SETUP: sends via plain SMTP — your existing email account (Gmail, Outlook,
etc.), no new third-party service or signup needed. Set:
  EMAIL_HOST        (e.g. smtp.gmail.com)
  EMAIL_PORT        (e.g. 587 — default below if unset)
  EMAIL_USERNAME    (the address you're sending FROM — e.g. you@gmail.com)
  EMAIL_PASSWORD    (an APP PASSWORD, not your normal login password —
                     see the deployment steps for how to generate one)
  OWNER_EMAIL       (where the contract + deal summary should land — can be
                     the same address as EMAIL_USERNAME)

Optional, all have reasonable defaults:
  BUYER_NAME, BUYER_PHONE, DEFAULT_TITLE_COMPANY,
  ACCEPTANCE_WINDOW_DAYS (default 5), CLOSING_WINDOW_DAYS (default 30)

HONEST LIMITATION: legal_description and title_company aren't tracked
anywhere in your Airtable schema, so they come through as explicit
placeholders in the generated contract — you (or your title company) still
need to fill those in before anything is signature-ready.
"""

import os
import re
import smtplib
from datetime import date, timedelta
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders

from contract_generator import generate_contract


def _clean_credential(value):
    """
    Strips ALL whitespace, including non-breaking spaces (\\xa0) — Google's
    app-password page displays the password with spaces for readability,
    and copy-pasting from a browser sometimes turns those into non-breaking
    spaces rather than normal ones, which smtplib's login step can't encode
    as ASCII. Stripping here means it doesn't matter how it was pasted.
    """
    if value is None:
        return None
    return re.sub(r"\s+", "", value)


EMAIL_HOST = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", 587))
EMAIL_USERNAME = _clean_credential(os.environ.get("EMAIL_USERNAME"))
EMAIL_PASSWORD = _clean_credential(os.environ.get("EMAIL_PASSWORD"))
OWNER_EMAIL = _clean_credential(os.environ.get("OWNER_EMAIL"))

BUYER_NAME = os.environ.get("BUYER_NAME", "Mello Acquisitions LLC")
BUYER_PHONE = os.environ.get("BUYER_PHONE", "")
DEFAULT_TITLE_COMPANY = os.environ.get("DEFAULT_TITLE_COMPANY", "TBD — confirm with title company")
ACCEPTANCE_WINDOW_DAYS = int(os.environ.get("ACCEPTANCE_WINDOW_DAYS", 5))
CLOSING_WINDOW_DAYS = int(os.environ.get("CLOSING_WINDOW_DAYS", 30))


def _build_full_address(lead_fields: dict) -> str:
    """
    Combines address + city + state + zip into one mailing address for the
    contract, if city/zip are available. Falls back gracefully to just
    address + state for records saved before those columns existed — this
    still produces a valid (if less complete) contract rather than erroring.
    """
    parts = [lead_fields.get("address") or "[ADDRESS MISSING]"]
    city = lead_fields.get("city")
    state = lead_fields.get("state")
    zip_code = lead_fields.get("zip")
    city_state_zip = ", ".join(p for p in [city, " ".join(p2 for p2 in [state, zip_code] if p2)] if p)
    if city_state_zip:
        parts.append(city_state_zip)
    return ", ".join(parts)


def build_deal_dict(lead_fields: dict, agreed_price: float) -> dict:
    """Maps an Airtable lead record + the agreed price into the contract
    template's merge fields."""
    today = date.today()
    return {
        "contract_date": today.strftime("%B %d, %Y"),
        "seller_name": lead_fields.get("owner_name") or "[SELLER NAME MISSING]",
        "buyer_name": BUYER_NAME,
        "subject_property": _build_full_address(lead_fields),
        "legal_description": "To be confirmed by title company prior to closing",
        "purchase_price": f"${agreed_price:,.0f}",
        "acceptance_deadline": (today + timedelta(days=ACCEPTANCE_WINDOW_DAYS)).strftime("%B %d, %Y"),
        "closing_date": (today + timedelta(days=CLOSING_WINDOW_DAYS)).strftime("%B %d, %Y"),
        "title_company": DEFAULT_TITLE_COMPANY,
        "other_agreements": "None",
        "governing_state": lead_fields.get("state") or "[STATE MISSING]",
        "seller_phone": lead_fields.get("phone") or "",
        "buyer_phone": BUYER_PHONE,
    }


def email_contract_to_owner(contract_path: str, lead_fields: dict, agreed_price: float) -> None:
    """
    Sends the generated contract to YOUR inbox (not the seller) via plain
    SMTP, with the deal's key facts in the body so you can review at a
    glance before adding a signature field and sending it on yourself.
    """
    if not all([EMAIL_USERNAME, EMAIL_PASSWORD, OWNER_EMAIL]):
        raise RuntimeError("EMAIL_USERNAME, EMAIL_PASSWORD, or OWNER_EMAIL not set")

    address = lead_fields.get("address", "Unknown address")
    seller_email = lead_fields.get("email") or "NOT CAPTURED — get this before forwarding the contract"
    arv = lead_fields.get("arv") or 0
    repair_estimate = lead_fields.get("repair_estimate") or 0

    body = (
        f"A seller verbally agreed to a price during today's call. The filled "
        f"contract is attached — this still needs your review, a signature "
        f"field, and to be sent on to the seller yourself.\n\n"
        f"Address: {address}\n"
        f"Seller: {lead_fields.get('owner_name', 'Unknown')}\n"
        f"Seller phone: {lead_fields.get('phone', '-')}\n"
        f"Seller email: {seller_email}\n"
        f"Agreed price: ${agreed_price:,.0f}\n"
        f"ARV: ${arv:,.0f}\n"
        f"Repair estimate: ${repair_estimate:,.0f}\n"
        f"Call notes: {lead_fields.get('call_transcript_summary', '')}\n\n"
        f"Reminder: legal description and title company in the attached "
        f"contract are placeholders, not real values yet — fill those in "
        f"before sending it to the seller."
    )

    msg = MIMEMultipart()
    msg["From"] = EMAIL_USERNAME
    msg["To"] = OWNER_EMAIL
    msg["Subject"] = f"Contract ready for review — {address}"
    msg.attach(MIMEText(body, "plain"))

    with open(contract_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header(
        "Content-Disposition", f'attachment; filename="{os.path.basename(contract_path)}"'
    )
    msg.attach(part)

    with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
        server.starttls()
        server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
        server.sendmail(EMAIL_USERNAME, [OWNER_EMAIL], msg.as_string())


def notify_human_call_request(lead_fields: dict) -> None:
    """
    Fires when a seller explicitly asks to speak with a person instead of
    continuing with the AI. Unlike dispatch_agreed_deal(), this sends no
    attachment — just a quick heads-up, since this is time-sensitive (a
    seller waiting on a callback) and previously had NO notification at all,
    only a silent Airtable status change you'd have to notice yourself.
    """
    if not all([EMAIL_USERNAME, EMAIL_PASSWORD, OWNER_EMAIL]):
        raise RuntimeError("EMAIL_USERNAME, EMAIL_PASSWORD, or OWNER_EMAIL not set")

    address = lead_fields.get("address", "Unknown address")
    body = (
        f"A seller asked to speak with a person directly during today's call — "
        f"this needs a callback.\n\n"
        f"Address: {address}\n"
        f"Seller: {lead_fields.get('owner_name', 'Unknown')}\n"
        f"Phone: {lead_fields.get('phone', '-')}\n"
        f"Notes: {lead_fields.get('call_transcript_summary', '')}\n"
    )
    msg = MIMEText(body, "plain")
    msg["From"] = EMAIL_USERNAME
    msg["To"] = OWNER_EMAIL
    msg["Subject"] = f"Human callback requested — {address}"

    with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
        server.starttls()
        server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
        server.sendmail(EMAIL_USERNAME, [OWNER_EMAIL], msg.as_string())


def dispatch_agreed_deal(lead_fields: dict, agreed_price: float) -> dict:
    """
    The one function to call the moment a deal is agreed: builds the
    contract, generates the .docx, and emails it to you for review.
    Raises on failure rather than swallowing errors — the caller (main.py)
    decides how to handle that without losing the "Agreed" status that was
    already saved to Airtable.
    """
    deal = build_deal_dict(lead_fields, agreed_price)
    address_slug = "".join(c if c.isalnum() else "_" for c in (lead_fields.get("address") or "contract"))[:50]
    output_path = f"/tmp/contract_{address_slug}.docx"
    contract_path = generate_contract(deal, output_path)
    email_contract_to_owner(contract_path, lead_fields, agreed_price)
    return {"contract_path": contract_path}
