"""
Fires the moment a seller verbally agrees to a price: generates the filled
purchase agreement and emails it to YOU (the business owner), not the
seller, since Box Sign is on hold and adding real signature fields is still
a manual step.

Interim workflow:
  1. Seller agrees on a call -> flag_for_human_review is called
  2. This module generates the contract .docx and emails it to OWNER_EMAIL
     with a deal summary (seller contact info, agreed price, ARV, repair
     estimate, call notes)
  3. You review, add a signature field, and send it to the seller yourself
  4. Once proven out and worth the cost, swap the "email the owner" step for
     box_sign.py's "send directly to seller" flow — build_deal_dict() stays
     the same either way.

EMAIL TRANSPORT — Resend's HTTPS API, not smtplib.
Render's free-tier services block ALL outbound SMTP ports (25, 465, 587)
as of Sept 2025 — confirmed via Render's own changelog. Every send failed
with [Errno 101] Network is unreachable, which is a network firewall, not
an auth or code problem, and no smtplib change fixes it. Port 443 is not
blocked. If you ever move off the free tier this file does not need to
change back: HTTPS works everywhere SMTP does, not the reverse.

SETUP:
  RESEND_API_KEY   from https://resend.com/api-keys
  OWNER_EMAIL      where the contract + deal summary should land
  RESEND_FROM      the "from" address. On Resend's free tier with no
                   verified domain this MUST be exactly
                   "onboarding@resend.dev" (the default here).

HONEST LIMITATION — Resend sandbox: until you verify a domain, Resend only
delivers to the address the Resend account was registered with. If
OWNER_EMAIL does not match it, sends succeed at the API level and never
arrive. Check Resend's "Emails" log after the first test send.

HONEST LIMITATION: legal_description and title_company are not tracked in
the Airtable schema, so they come through as explicit placeholders in the
generated contract. You or your title company still fill those in before
anything is signature-ready.

CHANGES IN THIS CLEANUP
-----------------------
- `send_owner_email()` is now public, so cron_evening_wrap.py and
  cron_tool_health_check.py can use the same working transport instead of
  their own smtplib block (which would fail on Render) or printing into
  logs nobody reads.
- Currency formatting is defensive: a value arriving as a string from
  Airtable used to raise inside an f-string and take down the whole send.
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

# Resend rejects attachments above ~40MB; a filled purchase agreement is
# tens of KB, so anything near this means something is wrong upstream.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024


class EmailError(Exception):
    """Raised when Resend rejects a send. A plain exception, same pattern as
    AirtableError, so callers can catch it specifically."""


def _money(value) -> str:
    """
    Formats a currency value for an email body, tolerating None, "" and
    strings. `f"${value:,.0f}"` raises TypeError on a string, and that used
    to abort the entire notification — losing the alert about a deal because
    a number was stored as text.
    """
    if value in (None, ""):
        return "-"
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def send_owner_email(subject: str, body_text: str, attachment_path: str = None) -> None:
    """
    Sends one email to OWNER_EMAIL through Resend's HTTPS API. Raises
    EmailError on any non-2xx so the caller's logging still works — only the
    transport underneath changed.
    """
    if not all([RESEND_API_KEY, OWNER_EMAIL]):
        raise EmailError("RESEND_API_KEY or OWNER_EMAIL not set on this service/job")

    payload = {
        "from": RESEND_FROM,
        "to": [OWNER_EMAIL],
        "subject": subject,
        "text": body_text,
    }

    if attachment_path:
        size = os.path.getsize(attachment_path)
        if size > MAX_ATTACHMENT_BYTES:
            raise EmailError(f"Attachment {attachment_path} is {size} bytes — too large to send")
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
        raise EmailError(f"Resend API error ({response.status_code}): {response.text[:400]}")


# Contracts read "the laws of the State of ___". Airtable stores the two-letter
# code, so this rendered as "the State of TX" — correct data, but it reads as a
# mail-merge slip on a document a seller is asked to sign. Falls back to
# whatever is stored if the code is unrecognised, so a new market never blocks
# a contract.
STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "DC": "District of Columbia",
}


def _state_name(code) -> str:
    if not code:
        return "[STATE MISSING]"
    return STATE_NAMES.get(str(code).strip().upper(), str(code))


def _build_full_address(lead_fields: dict) -> str:
    """
    Combines address + city + state + zip into one mailing address for the
    contract. Falls back to address + state for records saved before those
    columns existed — still a valid, if less complete, contract rather than
    an error.
    """
    parts = [lead_fields.get("address") or "[ADDRESS MISSING]"]
    city = lead_fields.get("city")
    state = lead_fields.get("state")
    zip_code = lead_fields.get("zip")
    city_state_zip = ", ".join(
        p for p in [city, " ".join(p2 for p2 in [state, zip_code] if p2)] if p
    )
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
        "purchase_price": _money(agreed_price),
        "acceptance_deadline": (today + timedelta(days=ACCEPTANCE_WINDOW_DAYS)).strftime("%B %d, %Y"),
        "closing_date": (today + timedelta(days=CLOSING_WINDOW_DAYS)).strftime("%B %d, %Y"),
        "title_company": DEFAULT_TITLE_COMPANY,
        "other_agreements": "None",
        "governing_state": _state_name(lead_fields.get("state")),
        "seller_phone": lead_fields.get("phone") or "",
        "buyer_phone": BUYER_PHONE,
    }


def email_contract_to_owner(contract_path: str, lead_fields: dict, agreed_price: float) -> None:
    """Sends the generated contract to YOUR inbox with the deal's key facts
    in the body, so you can review at a glance before adding a signature
    field and sending it on."""
    address = lead_fields.get("address", "Unknown address")
    seller_email = lead_fields.get("email") or "NOT CAPTURED — get this before forwarding the contract"

    body = (
        f"A seller verbally agreed to a price during today's call. The filled "
        f"contract is attached — it still needs your review, a signature field, "
        f"and to be sent on to the seller yourself.\n\n"
        f"Address: {address}\n"
        f"Seller: {lead_fields.get('owner_name', 'Unknown')}\n"
        f"Seller phone: {lead_fields.get('phone', '-')}\n"
        f"Seller email: {seller_email}\n"
        f"Agreed price: {_money(agreed_price)}\n"
        f"ARV: {_money(lead_fields.get('arv'))}\n"
        f"Repair estimate: {_money(lead_fields.get('repair_estimate'))}\n"
        f"Call notes: {lead_fields.get('call_transcript_summary', '')}\n\n"
        f"Reminder: the legal description and title company in the attached "
        f"contract are placeholders, not real values — fill those in before "
        f"sending it to the seller."
    )

    send_owner_email(
        subject=f"Contract ready for review — {address}",
        body_text=body,
        attachment_path=contract_path,
    )


def notify_attention_needed(lead_fields: dict, status: str) -> None:
    """
    Fires for the three call outcomes that genuinely warrant attention:
    "Human Call" (the seller asked for a person), "Offer Made" (a real
    number is on the table), and "Priority Follow-up" (a weak number but a
    strong, specific reason to sell). Deliberately does NOT fire for every
    outcome — a dead lead or a routine "check back later" should not
    interrupt you.
    """
    address = lead_fields.get("address", "Unknown address")

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
        f"ARV: {_money(lead_fields.get('arv'))}",
        f"Repair estimate: {_money(lead_fields.get('repair_estimate'))}",
    ]
    if lead_fields.get("offer_amount") is not None:
        body_lines.append(f"Seller's number / offer discussed: {_money(lead_fields.get('offer_amount'))}")
    if lead_fields.get("mao_floor") is not None:
        body_lines.append(f"Your ceiling (mao_floor): {_money(lead_fields.get('mao_floor'))}")
    if lead_fields.get("next_contact_date"):
        body_lines.append(f"Scheduled next contact: {lead_fields.get('next_contact_date')}")
    body_lines.append(f"Notes: {lead_fields.get('call_transcript_summary', '')}")

    send_owner_email(
        subject=subject_map.get(status, f"Lead needs attention — {address}"),
        body_text="\n".join(body_lines),
    )


def dispatch_agreed_deal(lead_fields: dict, agreed_price: float) -> dict:
    """
    The one function to call the moment a deal is agreed: builds the
    contract, generates the .docx, and emails it for review. Raises on
    failure rather than swallowing errors — the caller decides how to handle
    that without losing the "Agreed" status already saved to Airtable.
    """
    deal = build_deal_dict(lead_fields, agreed_price)
    address_slug = "".join(
        c if c.isalnum() else "_" for c in (lead_fields.get("address") or "contract")
    )[:50]
    output_path = f"/tmp/contract_{address_slug}.docx"
    contract_path = generate_contract(deal, output_path)
    email_contract_to_owner(contract_path, lead_fields, agreed_price)
    return {"contract_path": contract_path}
