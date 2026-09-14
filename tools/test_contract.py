"""
test_contract.py — exercise the whole "seller agreed" path without a call.

WHY THIS EXISTS
---------------
flag_for_human_review is the endpoint that matters most and the one least
tested. When a seller says yes, it: writes Agreed to Airtable, builds a deal
dict from the lead, renders purchase_agreement_template.docx, and emails the
result. Every step of that has failed at least once in this project's
history, and all of it runs in a FastAPI BackgroundTask where an exception
reaches nobody — the call has already hung up.

The contract template was also deleted from the repo at one point and nobody
noticed for days, because nothing ever rendered one.

This runs the same code path against a real Airtable lead and tells you
exactly what a seller would receive. It writes NOTHING to Airtable and,
unless you pass --send, emails nothing.

    python3 tools/test_contract.py                      # render only
    python3 tools/test_contract.py --address "6506 Clubway Ln"
    python3 tools/test_contract.py --send               # actually email it

RESEND SANDBOX WARNING: until a domain is verified, Resend only delivers to
the address the Resend account was registered with. If OWNER_EMAIL differs,
--send reports success and the mail never arrives. Check Resend's "Emails"
log after sending, not your inbox.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from airtable_helpers import find_lead_flexible, query_leads, require_config  # noqa: E402

AGREED_PRICE = float(os.environ.get("TEST_AGREED_PRICE", 185000))


def main():
    require_config()
    send = "--send" in sys.argv

    address = None
    if "--address" in sys.argv:
        address = sys.argv[sys.argv.index("--address") + 1]

    if address:
        record = find_lead_flexible(address)
        if not record:
            sys.exit(f"No Airtable lead matching {address!r}")
    else:
        leads = query_leads("{address}!=BLANK()", max_records=1)
        if not leads:
            sys.exit("No leads in Airtable to build a contract from.")
        record = leads[0]

    fields = record["fields"]
    print(f"Using lead: {fields.get('address')} "
          f"({fields.get('city')}, {fields.get('state')})\n")

    # ---- what the contract will actually say -----------------------------
    from deal_dispatch import build_deal_dict
    deal = build_deal_dict(fields, AGREED_PRICE)

    print("=" * 62)
    print("MERGE FIELDS — this is literally what the seller would read")
    print("=" * 62)
    placeholders = []
    for k, v in deal.items():
        flag = ""
        if isinstance(v, str) and ("MISSING" in v or v.strip() in ("", "TBD")):
            flag = "   <-- PLACEHOLDER"
            placeholders.append(k)
        print(f"  {k:22s} {str(v)[:60]}{flag}")

    # ---- render ----------------------------------------------------------
    print(f"\n{'=' * 62}\nRENDER\n{'=' * 62}")
    from contract_generator import generate_contract, TEMPLATE_PATH
    print(f"  template: {TEMPLATE_PATH}")
    if not os.path.exists(TEMPLATE_PATH):
        sys.exit("  TEMPLATE IS MISSING. Contract generation would fail on every "
                 "agreed deal. Restore it: git checkout <commit> -- "
                 "purchase_agreement_template.docx")

    out = "/tmp/test_contract_preview.docx"
    try:
        path = generate_contract(deal, out)
    except Exception as e:
        sys.exit(f"  RENDER FAILED: {e}")
    print(f"  rendered OK -> {path} ({os.path.getsize(path):,} bytes)")

    # Confirm the values actually landed. docxtpl silently renders an unknown
    # variable as an empty string, so "it rendered" is not "it worked".
    try:
        from docx import Document

        def all_text(p):
            d = Document(p)
            parts = [x.text for x in d.paragraphs]
            for t in d.tables:
                for row in t.rows:
                    for c in row.cells:
                        parts += [x.text for x in c.paragraphs]
            for s in d.sections:
                for hf in (s.header, s.footer):
                    parts += [x.text for x in hf.paragraphs]
            return "\n".join(parts)

        text = all_text(path)
        import re
        leftover = sorted(set(re.findall(r"\{\{\s*(\w+)\s*\}\}", text)))
        print(f"  unrendered tags left in the document: {leftover or 'none'}")

        print("\n  spot-check — are the real values in the document?")
        for label, value in (("purchase price", deal["purchase_price"]),
                             ("seller name", deal["seller_name"]),
                             ("property", deal["subject_property"].split(",")[0]),
                             ("governing state", deal["governing_state"])):
            present = str(value) in text
            print(f"    {label:18s} {str(value)[:34]:36s} {'OK' if present else '*** NOT FOUND ***'}")
    except ImportError:
        print("  (python-docx not installed locally — skipping content verification)")

    # ---- verdict ---------------------------------------------------------
    print(f"\n{'=' * 62}")
    if placeholders:
        print(f"CONTRACT RENDERS, but {len(placeholders)} field(s) are placeholders:")
        for k in placeholders:
            print(f"  - {k}")
        print("\nlegal_description and title_company are EXPECTED placeholders — they")
        print("are not tracked in Airtable and you or the title company fill them in.")
        print("Anything else on that list is missing lead data and would go out to a")
        print("seller looking exactly like that.")
    else:
        print("CONTRACT RENDERS CLEANLY — no placeholder fields.")
    print("=" * 62)

    # ---- optional real send ---------------------------------------------
    if not send:
        print(f"\nOpen {path} to read it. Pass --send to email it for real.")
        return

    print("\nSending via Resend...")
    missing = [v for v in ("RESEND_API_KEY", "OWNER_EMAIL") if not os.environ.get(v)]
    if missing:
        sys.exit(f"  Cannot send — {', '.join(missing)} not set in this shell. "
                 f"They live on the Render web service; add them to .env to test "
                 f"sending from here.")
    try:
        from deal_dispatch import email_contract_to_owner
        email_contract_to_owner(path, fields, AGREED_PRICE)
        print(f"  Resend accepted the message for {os.environ['OWNER_EMAIL']}.")
        print("  NOW CHECK RESEND'S 'Emails' LOG, not just your inbox. On the free")
        print("  tier with no verified domain, Resend accepts mail for any address")
        print("  but only DELIVERS to the one the account was registered with.")
    except Exception as e:
        sys.exit(f"  SEND FAILED: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)
