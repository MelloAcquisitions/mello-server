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

EMAIL TRANSPORT — CHANGED from smtplib to Resend's HTTPS API:
Render's free-tier web services block ALL outbound SMTP ports (25, 465,
587) as of Sept 2025 — confirmed via Render's own changelog. Every send
was failing with [Errno 101] Network is unreachable, a low-level network
block, not an auth or code problem. Port 443 (plain HTTPS) is NOT blocked,
so Resend's API (or any transactional-email HTTP API) sidesteps it
entirely. If you ever move off Render's free tier, this file doesn't need
to change back — HTTPS works everywhere SMTP does, not the reverse.

SETUP:
  RESEND_API_KEY    from https://resend.com/api-keys
  OWNER_EMAIL       where the contract + deal summary should land
  RESEND_FROM       the "from" address Resend sends as. On Resend's free
                     tier with no verified domain, this MUST be exactly
                     "onboarding@resend.dev" — trying to send from your own
                     address without domain verification will fail. Once
                     you verify a domain in Resend, you can use a real
                     address here (e.g. deals@youracquisitions.com).
                     Defaults to "onboarding@resend.dev" if unset.

HONEST LIMITATION — Resend sandbox mode: until you verify a domain in
Resend, their API will only deliver to the email address you signed up to
Resend with. If OWNER_EMAIL doesn't match your Resend account's own email,
sends will silently succeed at the API level but never arrive. Verify this
by checking Resend's dashboard "Emails" log after your first test send —
it'll show "delivered" or the actual rejection reason, which is more
visibility than smtplib ever gave you.

Optional, all have reasonable defaults:
  BUYER_NAME, BUYER_PHONE, DEFAULT_TITLE_COMPANY,
  ACCEPTANCE_WINDOW_DAYS (default 5), CLOSING_WINDOW_DAYS (default 30)

HONEST LIMITATION: legal_description and title_company aren't tracked
anywhere in your Airtable schema, so they come through as explicit
placeholders in the generated contract — you (or your title company) still
need to fill those in before anything is signature-ready.
"""

import base64
import os
from datetime import date, timedelta

import requests

from contract_generator import generate_contract

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM = os.environ.get("RESEND_FROM", "onboarding@resend.dev")
OWNER_EMAIL = os.environ.get("OWNER_EMAIL")
RESEND_URL = "https://api.resend.com/emails"

BUYER_NAME = os.environ.get("BUYER_NAME", "Mello Acquisitions LLC")
BUYER_PHONE = os.environ.get("BUYER_PHONE", "")
DEFAULT_TITLE_COMPANY = os.environ.get("DEFAULT_TITLE_COMPANY", "TBD")
ACCEPTANCE_WINDOW_DAYS = int(os.environ.get("ACCEPTANCE_WINDOW_DAYS", 5))
CLOSING_WINDOW_DAYS = int(os.environ.get("CLOSING_WINDOW_DAYS", 30))


class EmailError(Exception):
    """Raised when Resend's API rejects a send — kept as a plain exception,
    same pattern as AirtableError, so callers can catch it specifically if
    they ever want to (main.py currently just logs and moves on)."""
    pass


def _send_via_resend(subject: str, body_text: str, attachment_path: str = None) -> None:
    """
    Sends one email through Resend's HTTPS API. Raises EmailError on any
    non-2xx response so the caller's existing try/except logging still
    works exactly as before — only the transport underneath changed.
    """
    if not all([RESEND_API_KEY, OWNER_EMAIL]):
        raise EmailError("RESEND_API_KEY or OWNER_EMAIL not set")

    payload = {
        "from": RESEND_FROM,
        "to": [OWNER_EMAIL],
        "subject": subject,
        "text": body_text,
    }

    if attachment_path:
        with open(attachment_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
        payload["attachments"] = [{
            "filename": os.path.basename(attachment_path),
            "content": encoded,
        }]

    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    response = requests.post(RESEND_URL, headers=headers, json=payload, timeout=20)
    if not response.ok:
        raise EmailError(f"Resend API error ({response.status_code}): {response.text}")


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
    Sends the generated contract to YOUR inbox (not the seller) via Resend,
    with the deal's key facts in the body so you can review at a glance
    before adding a signature field and sending it on yourself.
    """
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

    _send_via_resend(
        subject=f"Contract ready for review — {address}",
        body_text=body,
        attachment_path=contract_path,
    )


def notify_attention_needed(lead_fields: dict, status: str) -> None:
    """
    Fires for the three call outcomes that genuinely warrant your attention —
    "Human Call" (seller asked for a person), "Offer Made" (a real number is
    on the table, close to a deal), and "Priority Follow-up" (weak number
    but a strong, specific reason to sell — worth handling personally rather
    than letting the standard retry schedule handle it). Deliberately does
    NOT fire for every outcome — a dead lead or a routine "check back later"
    doesn't need to interrupt you, only ones that actually do.
    """
    address = lead_fields.get("address", "Unknown address")
    arv = lead_fields.get("arv") or 0
    offer_amount = lead_fields.get("offer_amount")
    repair_estimate = lead_fields.get("repair_estimate") or 0
    mao_floor = lead_fields.get("mao_floor")
    next_contact_date = lead_fields.get("next_contact_date")

    subject_map = {
        "Human Call": f"Human callback requested — {address}",
        "Offer Made": f"Close to a deal — {address}",
        "Priority Follow-up": f"Priority lead, needs your touch — {address}",
    }
    intro_map = {
        "Human Call": "A seller asked to speak with a person directly during today's call — this needs a callback.",
        "Offer Made": "A real number came up on today's call and it's close to a deal — worth following up while it's warm.",
        "Priority Follow-up": "The number wasn't close, but this seller gave a strong, specific reason to sell — worth your personal handling rather than the standard retry schedule.",
    }

    body_lines = [
        intro_map.get(status, "This call needs your attention."),
        "",
        f"Address: {address}",
        f"Seller: {lead_fields.get('owner_name', 'Unknown')}",
        f"Phone: {lead_fields.get('phone', '-')}",
        f"ARV: ${arv:,.0f}",
        f"Repair estimate: ${repair_estimate:,.0f}",
    ]
    if offer_amount is not None:
        body_lines.append(f"Seller's number / offer discussed: ${offer_amount:,.0f}")
    if mao_floor is not None:
        body_lines.append(f"Your ceiling (mao_floor): ${mao_floor:,.0f}")
    if next_contact_date:
        body_lines.append(f"Scheduled next contact: {next_contact_date}")
    body_lines.append(f"Notes: {lead_fields.get('call_transcript_summary', '')}")

    _send_via_resend(
        subject=subject_map.get(status, f"Lead needs attention — {address}"),
        body_text="\n".join(body_lines),
    )


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
