# Mello Acquisitions

Automated lead sourcing and outbound acquisition calling for real estate
wholesaling. A Vapi voice agent calls distressed-owner leads, and this repo is
everything around it: sourcing, valuation, the tool server the agent calls
mid-conversation, the retry cadence, contract generation, and a dashboard.

See `CLEANUP_AUDIT_2026-09-07.md` for what changed in the Sept 7 cleanup and
what is still open.

## Layout

```
main.py                  FastAPI tool server — what Vapi calls mid-call
dashboard.py             /dashboard + /api/dashboard/* (mounted on main.py)
calculator.py            MAO / offer math. The file that decides what a seller is offered.
airtable_helpers.py      All Airtable reads and writes. Retries, pagination, notes appending.
mello_time.py            Business timezone. Nothing else calls date.today().
vapi_client.py           All Vapi API reads. Handles both response shapes, paginates.
orchestrator_lib.py      Retry cadence, calling hours, call triggering, enrichment
lead_sourcing.py         BatchData sourcing + DNC/litigator filtering
rentcast_lookup.py       Sold comps + AVM  (FREE TIER = 50 REQUESTS/MONTH)
zillapi_lookup.py        Zestimate, the independent second opinion
contract_generator.py    Fills purchase_agreement_template.docx
deal_dispatch.py         Resend email transport + the agreed-deal flow
box_sign.py              ON HOLD. Not wired in. Kept for when Box Sign is worth the cost.

cron_morning_lead_prep.py       07:30 local   source + enrich new leads
cron_apply_updates.py           08:00 local   apply an approved prompt change
cron_dispatch_calls.py          */15 08-21    place calls
cron_continuous_enrichment.py   */30 08-21    backfill missing ARVs
cron_evening_wrap.py            21:00 local   daily summary (emailed)
cron_tool_health_check.py       21:30 local   alert on tool-level failures
cron_reconcile_calls.py         ~20:00 local  THE GUARANTEE LAYER
cron_draft_improvements.py      22:00 local   draft one prompt improvement

tools/preflight_check.py   prove the tool path works without placing a call
tools/test_call.py         place one real call by hand
tools/dump_tools.py        raw Vapi assistant + tool config. Start here for config questions.
tools/bump_max_tokens.py   set maxTokens=500 on the three tools
```

## Do not delete `.python-version`

It pins **3.12.8**. Render otherwise defaults to the newest Python it
supports, and on the first deploy of the pinned requirements that was 3.14.3
— for which `pydantic-core` had no prebuilt wheel. pip fell back to
compiling it from Rust source, and Render's build image mounts the cargo
registry read-only, so that build can never succeed:

```
error: failed to create directory `/usr/local/cargo/registry/cache/...`
Caused by: Read-only file system (os error 30)
💥 maturin failed
```

Every cron job shares this repo and therefore this file, so all of them
would have hit the identical failure. If you ever move to a newer Python,
bump `pydantic` first and confirm a wheel exists for that interpreter — a
missing wheel here doesn't degrade, it fails the whole build.

## Two things that cost this project weeks

**The Render hostname includes a random suffix.** It is
`https://mello-server-hqfi.onrender.com`, not `mello-server.onrender.com`.
The suffix-less name still resolves through DNS but has nothing behind it, so
requests **hang indefinitely rather than failing cleanly** — which looks like
a timeout bug, not a config bug. The URL in Render's dashboard is
authoritative and does not match the service name. No default hostname is
hardcoded anywhere in this repo on purpose.

**Render crons inherit nothing from the web service.** Every cron job needs
its own full set of environment variables, and crons do not reliably
auto-deploy on push — use Manual Deploy → Deploy latest commit, or the job
silently runs the previous version. Every cron now calls `require_config()`
and dies immediately naming a missing variable.

## Three-layer call logging

A tool the model has to *choose* to call can never be a guarantee. If the
seller hangs up, or the agent ends the call for abuse, or the model just
doesn't, there is no turn left in which a tool could fire. The guarantee has
to live outside the model.

| Layer | Mechanism | Data quality | Reliability |
|---|---|---|---|
| 1 `log_call_outcome` | in-call tool | richest | lowest |
| 2 `/vapi_call_ended` | Vapi webhook (push) | medium | medium — pushes drop silently |
| 3 `cron_reconcile_calls` | polls `GET /call` (pull) | lowest | highest — a pull cannot be missed |

Layer 3 is the actual guarantee: nothing has to be delivered, we go and look.
All three dedupe on the **Vapi call id** and **append** to notes rather than
replacing them. All three only ever move `New` → `Contacted`; none of them
infers `Rejected` or `Opt Out` from a transcript.

## Airtable schema this code expects

**Leads:** `address` (street only), `city`, `state`, `zip`, `owner_name`,
`phone`, `email`, `source`, `status` (single select), `arv`, `repair_estimate`,
`mao_floor`, `offer_amount`, `#_calls`, `#_emails`, `last_call_date`,
`next_contact_date`, `call_transcript_summary` (long text),
`enrichment_attempts` (number), `date_created` (Created time — never written to).

`status` options: `New`, `Contacted`, `Qualified`, `Offer Made`, `Agreed`,
`Rejected`, `Opt Out`, `Human Call`, `Priority Follow-up`, `Exhausted`,
`Closed`. `Closed` is set by hand once a deal funds — nothing else can know.

**Daily Log:** `date`, `calls_today`, `call_seconds_today`, `enrichments_today`,
`batchdata_calls_today`, `batchdata_properties_today`,
`batchdata_skiptrace_matches_today`.

**Proposed Updates:** `date`, `reasoning`, `proposed_change`, `status`,
`previous_prompt` (long text).

## Environment variables

**Web service:** `AIRTABLE_API_KEY`, `AIRTABLE_BASE_ID`, `AIRTABLE_TABLE_NAME`,
`RESEND_API_KEY`, `OWNER_EMAIL`, `RESEND_FROM`, `BUYER_NAME`, `BUYER_PHONE`,
`DEFAULT_TITLE_COMPANY`, `DASHBOARD_PASSWORD`, `MELLO_TOOL_SECRET`

**dispatch-calls:** the Airtable three, plus `VAPI_API_KEY`,
`VAPI_ASSISTANT_ID`, `VAPI_PHONE_NUMBER_ID` (the **Telnyx** id, not a stale
Twilio one), `MAX_CALLS_PER_DAY`

**reconcile-calls / tool-health-check:** the Airtable three, plus
`VAPI_API_KEY`, `VAPI_ASSISTANT_ID` (and `RESEND_API_KEY` + `OWNER_EMAIL` for
tool-health's alert)

**morning-lead-prep / continuous-enrichment:** the Airtable three, plus
`BATCHDATA_API_KEY`, `RENTCAST_API_KEY`, `ZILLAPI_KEY`,
`MAX_ENRICHMENTS_PER_DAY`

**draft-improvements:** the Airtable three, plus `VAPI_API_KEY`,
`VAPI_ASSISTANT_ID`, `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`

**apply-updates:** `AIRTABLE_API_KEY`, `AIRTABLE_BASE_ID`, `VAPI_API_KEY`,
`VAPI_ASSISTANT_ID`

## Cost ceilings worth knowing before you scale

- **RentCast free tier: 50 requests per MONTH.** One enriched lead = 2
  requests. `MAX_ENRICHMENTS_PER_DAY` should be **1** until you upgrade.
- **Zillapi free tier: 100 credits.** One per enriched lead.
- **Resend sandbox:** with no verified domain, it only delivers to the address
  the Resend account was registered with. Sends "succeed" and never arrive.
- **Render free tier** blocks all outbound SMTP (25/465/587). Everything here
  uses Resend's HTTPS API. Do not reintroduce `smtplib`.
- **Airtable: 5 requests/second per base**, shared by every cron, the
  dashboard, and the live call path. `airtable_helpers` retries on 429.

## Local development

```bash
pip install -r requirements.txt
uvicorn main:app --reload      # http://127.0.0.1:8000/docs
python calculator.py           # self-test against the original spreadsheet
```
