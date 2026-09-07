# Mello Acquisitions — Cleanup & Bug Audit

**Date:** September 7, 2026
**Scope:** all 31 files in the project, read in full.
**Outcome:** 6 files deleted, 2 added, 17 rewritten. 41 issues found.

---

## The five things that matter most

If you read nothing else:

1. **The offer calculator could tell the agent to offer a seller $1.** A deal
   that barely cleared the $10K fee floor left the seller with whatever was
   left — sometimes almost nothing. Live, on a recorded call. Fixed with a
   minimum-offer guardrail; thin deals now return `no_deal`.
2. **The daily call cap reset at 6 PM every evening.** Render runs UTC, you
   run Monterrey. The Daily Log row is keyed on date, so calls placed between
   6 and 9 PM went to *tomorrow's* row and the counter went back to zero
   mid-calling-window. Up to 2× `MAX_CALLS_PER_DAY` could go out in one day.
3. **The reconcile cron threw away every call under 8 seconds.** The comment
   says short calls are only skipped when the ended reason *also* says nobody
   answered. The code was an `or`, not an `and`. A 6-second "take me off your
   list" — the single most important call in the system to record — was
   silently discarded by the layer built to guarantee it never is.
4. **Two crons were analyzing the wrong four hours.** `datetime.now()` on
   Render is UTC; midnight UTC is 6 PM Monterrey *the previous day*. Both the
   improvement-draft cron (fires 10 PM local) and the tool-health cron (9:30 PM
   local) were looking at a 6–10 PM sliver and ignoring the entire 8 AM–6 PM
   calling day.
5. **The tool endpoints had no authentication at all.** Anyone with the Render
   URL — which is pasted into Vapi's dashboard, appears in call logs, and was
   published in your own handoff doc — could write to your Leads table, mark a
   lead Agreed, and trigger contract generation and email.

And one that will bite you on day two rather than day one:

6. **RentCast's free tier is 50 requests per _month_,** and one enriched lead
   costs two. At 15 leads/day that is the whole month gone in under two days,
   after which enrichment fails silently and leads pile up unvalued. There is
   now a shared daily budget — **set `MAX_ENRICHMENTS_PER_DAY=1` until you are
   on a paid plan.**

---

## Files deleted (6)

| File | Why |
|---|---|
| `vapi_tools_router.py` | Written before the tool type was known. Speaks the `function`-tool protocol (`message.toolCallList` in, `{"results":[...]}` out). Your tools are `apiRequest`. Its two genuinely useful functions (`find_lead_flexible`, `resolve_address_for_write`) were already merged into `airtable_helpers.py`. |
| `fix_vapi_tools.py` | Marked "do not run" in your own handoff — written for function tools, wrong for apiRequest. A loaded gun in the repo. |
| `check_vapi_config.py` | Same wrong assumption. It would report `server.url: None` as a blocker (normal for apiRequest) and demand `/vapi/tools` exist. Would have sent you chasing a phantom bug. Replaced by `tools/dump_tools.py`, which shows raw config and interprets nothing. |
| `preflight_check.py` | Posted a function-tool envelope to `/vapi/tools`, and defaulted to the suffix-less hostname that resolves but has nothing behind it. **Rewritten** as `tools/preflight_check.py` for the real flat apiRequest shape. |
| `test_first_call.py`, `test_call_only.py` | The same script twice — one fetching a live valuation, one using a hardcoded ARV to save credits. That is a flag, not a second file. Both had a real personal phone number committed. **Merged** into `tools/test_call.py`, which takes `--phone` and `--enrich`. |
| `mello_system_prompt_v10.md` | Superseded by v13. |

**Also removed, inside files that stayed:**

- `main.py`: `/get_property_analysis` and `/calculate_final_fee` (no Vapi tool
  calls either; v13 explicitly tells the agent not to fetch data mid-call),
  and `/inbound_email` + `/inbound_sms` — both were TODO stubs that parsed a
  payload, printed one line and threw it away. They looked like working
  integrations. Removing them also drops `python-multipart`.
- `calculator.py`: `rental_calculator()`, `wholetail_calculator()` — faithful
  spreadsheet translations for two business lines this system doesn't run.
- `orchestrator_lib.py`: `send_sms()`, `send_email()` — both raised
  `NotImplementedError`, called by nothing.
- `rentcast_lookup.py`: `calculate_arv_from_sold_comps()` — a
  backwards-compatibility wrapper nothing imported.
- `airtable_helpers.py`: `increment_todays_call_count()` was a copy-paste of
  `increment_daily_log_field()`; now a one-line alias.

## Files added (2)

| File | Why |
|---|---|
| `mello_time.py` | One source of truth for "what day is it." Every `date.today()` in the project now routes through it. |
| `vapi_client.py` | One place that knows how to call Vapi's API — handles both response shapes and paginates. Four files were each getting this subtly wrong. |

---

## Critical — money, compliance, or a live call

### C1. `flip_mao` could produce a $1 offer
`calculator.py`

The only check was that the deal cleared the $10,000 minimum fee. Nothing
checked that anything was left for the seller.

```
ARV 200,000 / repairs 145,999
  after holding      30,001
  buyer profit       20,000
  available          10,001
  wholesale fee      10,000   <- takes essentially all of it
  mao_floor               1
  opening_offer           1   <- spoken out loud to a seller
```

Even the non-pathological case is bad: ARV 200,000 with 138,000 of repairs
gave `mao_floor 8,000`, `opening_offer 7,440`. A $7,440 offer on a $200K house
isn't a negotiating position, it's a call the seller tells people about.

**Fixed.** `MIN_OFFER_TO_SELLER` (default $15,000, env-overridable). A deal
must clear the fee floor *and* leave a real offer, or it returns `no_deal`
with a plain-language reason. There is also a final sanity check before
returning: if `mao_floor < MIN_OFFER_TO_SELLER`, or `opening_offer <= 0`, or
`mao_floor > arv`, it fails closed. Offers are rounded down to the nearest
$500 so the agent says "one eighty-seven five", not "$187,432".

Verified across 4,695 viable ARV/repair combinations — every one satisfies
`mao_floor >= 15000`, `0 < opening_offer <= mao_floor`, `fee >= 10000`.
The spreadsheet self-test still reproduces your original numbers exactly.

### C2. The daily call cap reset mid-evening
`airtable_helpers.py`, `cron_dispatch_calls.py`, `mello_time.py`

`date.today()` on Render is UTC. Monterrey is UTC-6. The Daily Log row is
keyed on `date`, and the dispatch cron runs until 9 PM local (03:00 UTC).
From 6 PM local onward, `get_todays_call_count()` was reading a fresh, empty
row for tomorrow — so the circuit breaker reset in the middle of the calling
window.

**Fixed** project-wide via `mello_time`. Also: the cap is now re-checked
*inside* the dispatch loop (it was checked once per run, so a run starting at
79/80 could still place a full batch and finish at 82).

### C3. A placed call could go uncounted against the cap
`cron_dispatch_calls.py`

`increment_todays_call_count()` ran *after* an `upsert_lead()` that could
fail. If it did, the call had been placed and billed but never counted. Now
the counter increments immediately after Vapi accepts the call, before any
other write, and a counter failure is logged as a warning rather than
aborting.

### C4. The reconcile cron discarded short opt-out calls
`cron_reconcile_calls.py`

```python
if never_connected or (duration is not None and duration < 8):
    return "not_connected"
```

The comment directly above it says short calls are only skipped when the
ended reason also says nobody was there. The code says `or`. Every call under
8 seconds was dropped regardless of reason — including a hangup right after
"stop calling me."

**Fixed** to `and`, matching the documented intent. A not-connected reason on
a call that ran long is now recorded *and* logged as a note, since that means
the reason-string list needs revisiting.

### C5. Tool endpoints were completely unauthenticated
`main.py`

No key, no signature, no rate limit on `/calculate_mao`,
`/log_call_outcome`, `/flag_for_human_review`. `flag_for_human_review` in
particular writes "Agreed" and triggers contract generation + an email.

**Fixed** with an `X-Mello-Token` header check. Deliberately fail-open with a
loud warning when `MELLO_TOOL_SECRET` is unset, so deploying this file cannot
take your live call path down. **Action: set `MELLO_TOOL_SECRET` on Render,
add the matching header to each of the three Vapi tools, then confirm
`GET /` reports `"tool_auth": "enabled"`.**

### C6. Opt-out suppression compared raw phone strings
`cron_dispatch_calls.py`

`opted_out_phones` was a set of stored strings. The same person saved once as
`(512) 555-1234` and once as `5125551234` would be suppressed under one
record and dialled under the other. Now normalized to last-10-digits on both
sides (`normalize_phone()`), verified to collapse six written formats to one
key.

### C7. Re-sourcing could resurrect an opted-out lead
`cron_morning_lead_prep.py`

The duplicate check was an exact string match on the street address BatchData
returned. BatchData's `formattedStreet` is not guaranteed byte-identical
between days ("Ln" vs "Lane"), so the check could miss and re-create the lead
with status `New` — putting someone who explicitly asked to be removed back
in the dial queue. Now also checks by phone number, which doesn't drift.

---

## High — silent data loss or a job that can't do its job

### H1. `log_call_outcome` overwrote the entire call history
`main.py`

The webhook and the reconcile cron both carefully *append* to
`call_transcript_summary`. `log_call_outcome` — the richest and by far most
common writer — replaced it. So the successful path was the one destroying
prior call history, including opt-out language a human would need to see, and
including the `[AUTO-LOGGED]` / `[RECONCILED]` markers the fallback layers use
to avoid double-writing. `flag_for_human_review` did the same.

**Fixed.** Both now use `append_notes()`, which prefixes each entry with its
date and truncates from the *front* when the field fills, keeping the most
recent call.

### H2. The end-of-call webhook dropped a second same-day call
`main.py`

`already_logged = fields.get("last_call_date") == today`. Dial a lead twice in
one day: the first call stamps today, the second then looks already-handled
and is silently dropped — by layer 2, whose entire purpose is catching exactly
that. Your reconcile cron fixed this months ago by deduping on the Vapi call
id; the webhook never got the same fix. **Now dedupes on call id.**

### H3. `cron_draft_improvements` never had any ground truth
`cron_draft_improvements.py`

`get_real_outcome()` did an exact-string Airtable match on the phone number.
Vapi reports E.164, Airtable stores whatever BatchData returned. It therefore
almost never matched, so every transcript was labelled `Unknown` — and the
outcome-grounded analysis that is the entire premise of the script was never
actually happening. It was just reading conversation text.

**Fixed** to use `find_lead_by_phone()`. It now also reports how many calls
matched, and *skips the run entirely* if none did, rather than producing a
confident-sounding proposal built on nothing.

### H4. `model="claude-sonnet-5"` is not a valid API model id
`cron_draft_improvements.py` — would 404 every run. Now `ANTHROPIC_MODEL`, an
env var, defaulting to a real id. Verify the current list at
docs.claude.com/en/docs/about-claude/models before you enable this job.

### H5. The tool-health cron could never send its alert
`cron_tool_health_check.py`

It used `smtplib`, which Render's free tier blocks entirely — the exact
failure `deal_dispatch.py` was migrated off. An alerting job that cannot
alert. **Converted to the same Resend transport.** This was the reason it had
to stay disabled; it can now be enabled once the call pipeline has a day of
data.

### H6. Three files assumed Vapi returns a bare list
`cron_draft_improvements.py`, `cron_tool_health_check.py`, `dashboard.py`

`calls = response.json()` then `for c in calls: c.get(...)`. Vapi has returned
both a bare list and a `{"results": [...]}` envelope across versions — your
reconcile cron handles both, these three didn't. Iterating a dict yields its
*keys*, and `.get()` on a string is an `AttributeError`. All three now go
through `vapi_client`.

### H7. The guarantee layer had a silent 100-call ceiling
`cron_reconcile_calls.py`

`limit: 100`, no pagination. At `MAX_CALLS_PER_DAY=80` plus retries, a busy
24-hour window exceeds that, and the oldest calls fall out of the layer whose
entire value proposition is "a pull cannot be missed." Now paginated.

### H8. `dashboard.py` crashed on a null customer
`c.get("customer", {}).get("number")` — `.get` with a default returns `None`
when the key exists *with* a null value, and the chained `.get` raises. Now
`customer_number()` in `vapi_client`, which handles it.

### H9. Cost totals silently stopped counting after 100 days
`dashboard.py` — the Daily Log fetch was unpaginated. Now paginated, and the
response reports `days_tracked` so you can see it.

### H10. The prompt-update cron had no undo
`cron_apply_updates.py`

This job edits the thing that decides what an AI says to real sellers about
real money, and there was no snapshot of the prior state. It also appended to
`messages[0]` on the assumption that index 0 is the system prompt — Vapi
guarantees no ordering — and appended forever with no size ceiling, no dedupe
and no review of the merged result.

**Fixed:** finds the message by `role == "system"`; writes the full prior
prompt to a `previous_prompt` column on the proposal record *before*
patching, and refuses to patch if that snapshot write fails; refuses past
`MAX_PROMPT_CHARS` (default 25,000 — v13 is ~11,000) and tells you to
consolidate by hand.

**Action: add a `previous_prompt` (Long text) column to the Proposed Updates
table before enabling this job.**

### H11. A stuck proposal silently blocks all future drafts
`cron_apply_updates.py` + `cron_draft_improvements.py`

`mark_applied()` ignored its response. A failed status write leaves the
proposal on `Approved`, and `has_unresolved_proposal()` blocks every future
nightly draft on exactly that — forever, with no error anywhere. It also risks
re-applying the same change tomorrow. Now raises with an explicit instruction.

### H12. Leads could get stranded with no ARV
`cron_continuous_enrichment.py` only enriched `status='New'`;
`cron_dispatch_calls.py` filters on `{arv}!=BLANK()`. A lead that reached
`Contacted` without an ARV was excluded from both — never enriched, never
called, never marked anything. Enrichment now covers `New` *and* `Contacted`.

### H13. `enrichment_attempts` failure could loop forever
`cron_continuous_enrichment.py`

If the `enrichment_attempts` column doesn't exist, every write in the failure
path 422s — so the counter never increments, the lead never gives up, and the
cron retries it every 30 minutes forever. Which is precisely the failure the
counter exists to prevent. The counter write is now attempted **first and
alone**, and a failure prints an explicit "check that this column exists"
warning.

---

## Medium — correctness, cost, robustness

| # | File | Issue | Fix |
|---|---|---|---|
| M1 | `airtable_helpers.py` | No retry on any HTTP call. Airtable rate-limits at **5 req/sec per base**, shared between six crons, a full-table dashboard scan, and live in-call writes. A 429 during a call meant the outcome was simply lost. | All calls go through `_request()` with backoff on 429/5xx. |
| M2 | `contract_generator.py` | `DocxTemplate("purchase_agreement_template.docx")` — a relative path. Only works if the process happens to start from the repo root. This runs in a FastAPI BackgroundTask on the live call path. | Resolved relative to `__file__`; raises a clear error if missing. |
| M3 | `contract_generator.py` | docxtpl renders an unknown variable as an empty string, so a schema change would ship a contract with a blank purchase price and no error. | Missing merge fields are filled with a visible `[FIELD MISSING]` placeholder and warned about. |
| M4 | `rentcast_lookup.py` | `sorted(prices)[len//2]` is not a median — it's the upper-middle value on even-length lists, biasing every even comp set upward. | Real median. |
| M5 | `rentcast_lookup.py` | `get_recommended_arv` takes the raw minimum of up to four candidates with no floor. One bad data point (a lot-only record, a distressed sale, a wrong address match) becomes the ARV — which either kills a good lead as `no_deal` or anchors a real offer to a wrong number. | Candidates below 50% of the highest are discarded and reported in `discarded_candidates`. Verified: a $40K outlier against $300K comps no longer wins. |
| M6 | `orchestrator_lib.py` | `is_within_calling_hours()` caught only `ValueError`. `ZoneInfoNotFoundError` subclasses `KeyError` — on an image without tzdata the dispatch cron would crash on the first lead instead of failing safe. The alternative to "don't call" here is "call someone at 3 AM." | Catches it; `tzdata` pinned in requirements. |
| M7 | `orchestrator_lib.py` | `CALL_SCHEDULE_DAYS[call_count]` — a `#_calls` value hand-edited in Airtable past the end of the schedule is an `IndexError` that kills the whole dispatch run mid-loop, leaving the rest of the day's leads uncalled with no obvious cause. | Bounds-checked. Verified for `call_count` 0–39 including malformed dates. |
| M8 | `main.py` | Vapi tool schemas set `"default": ""` on `number` fields. If Vapi injects those, the body arrives as `{"arv": ""}` → 422 → the model sees the tool fail mid-call and the outcome is never written. (Flagged as unconfirmed in your handoff §7.8.) | `_num()` coerces `""` → `None`. No longer depends on whether Vapi does it. |
| M9 | `main.py` | A status string the Airtable single-select doesn't recognise 422s the **entire write** — losing the whole call outcome because the model said "Follow Up" instead of "Priority Follow-up". | `normalize_status()` maps near-misses and falls back to `Contacted` rather than failing. |
| M10 | `main.py` | `flag_for_human_review` did resolve → upsert → re-fetch: up to five Airtable round trips on a live call against a 5 req/sec limit. | Reuses the record it already fetched. |
| M11 | `dashboard.py` | `check_password()` compared only the password and ignored the username entirely. | Both checked, both with `compare_digest`, no short-circuit. `DASHBOARD_USER` env var (defaults `mello`). |
| M12 | `dashboard.py` | Lead fields injected straight into `innerHTML`. `call_transcript_summary` is text an AI transcribed from whatever a stranger said on a phone call. | Everything escaped; currency formatted through a helper. |
| M13 | `dashboard.py` | Opening the page did two full Airtable table scans; the productivity tab a third — against the same rate limit the live call path needs. | 30-second cache on the full-table scan. |
| M14 | `lead_sourcing.py` | `get_compliant_leads()` ran all `max_pages` even after a market returned zero properties — paying for empty pages. | Stops on an empty page. |
| M15 | `lead_sourcing.py` | Leads with a missing city/state/zip flowed downstream and were sent to RentCast as `"6506 Clubway Ln, None, None None"` — a wasted request from a 50/month allowance. | Dropped at extraction. |
| M16 | `deal_dispatch.py` | `f"${value:,.0f}"` raises `TypeError` on a string. An ARV stored as text would abort the whole notification — losing the alert about a live deal because of a field type. | `_money()` helper, tolerant of `None`, `""` and strings. |
| M17 | `zillapi_lookup.py` | `extract_zestimate` assumed a dict. A list response raises `AttributeError` — swallowed by the broad `except Exception` around the Zillow lookup, silently dropping your third valuation source for every lead, forever, with no symptom. | Handles list, non-dict, and non-numeric. |
| M18 | `requirements.txt` | No version pins. A free-tier service that reinstalls on every push can break from an upstream release with no change on your side. | All pinned. `tzdata` added (required for `zoneinfo`). `python-multipart` removed with the stub endpoints. |
| M19 | all crons | Missing env vars produced a 404 against `.../None/Leads`, which reads like an Airtable problem rather than a config problem — the repeated Render-crons-don't-inherit-env-vars failure. | `require_config()` at the top of every cron; dies immediately naming the missing variable. |
| M20 | `cron_evening_wrap.py` | Produced a genuinely useful daily summary and printed it into Render's log viewer, where nobody would read it. "3 leads Agreed, needing contract follow-up" could sit unseen for days. | Emails it via Resend when there's something actionable; still prints either way. |
| M21 | `box_sign.py` | Running the file directly generated a contract and immediately fired a **real** signature request to two placeholder addresses. | Dry run by default; `--send` to actually send. |

---

## Known gaps left open (deliberately)

These are real and I did not paper over them:

1. **Opt-outs spoken on a call the agent failed to log are still not
   suppressed.** Both fallback layers only ever move `New` → `Contacted`; they
   never infer `Opt Out` from a transcript. That's the right call — a wrong
   guess either kills a live lead or, far worse, leaves someone who opted out
   looking dialable — but it means the note gets written and nothing acts on
   it. Closing it properly means classifying transcripts with a model, which
   is worth doing deliberately once you have a week of real calls.
2. **The Daily Log counters are read-modify-write.** Airtable has no atomic
   increment, so two writers in the same instant can lose a count. Drift is
   rare and always toward *under*-counting, so the cap is a safety net, not an
   exact meter. Leave headroom.
3. **`get_lead_local_hour` uses one timezone per state.** A lead in El Paso
   (Mountain) is treated as Central, making the window an hour *earlier* than
   it should be there. Fine for Texas metros; needs a per-zip lookup before
   you expand.
4. **No per-attempt call log.** The dashboard can only show all-time totals,
   not a day/week/month breakdown, because `#_calls` is a cumulative counter
   with no timestamps. A `Call Log` table with one row per attempt would fix
   this and also make the reconcile cron's dedupe cheaper.
5. **AI disclosure** is in the v13 prompt greeting, which is right — but the
   proactive-recording-disclosure question is still marked pending legal
   confirmation in the prompt itself. Worth closing before volume.
6. **Repair-estimate guardrail** (handoff §7.5 — the agent let a seller
   negotiate the repair estimate down, mechanically raising the offer) is a
   *prompt* change, not a code change. Not in this pass. Suggested wording is
   at the end of this document.

---

## What to do, in order

1. **Add the two Airtable columns** this pass depends on:
   `enrichment_attempts` (Number) on **Leads**, `previous_prompt` (Long text)
   on **Proposed Updates**. Confirm `enrichment_attempts` exists — without it,
   enrichment retries forever.
2. **Set `MAX_ENRICHMENTS_PER_DAY=1`** on both enrichment crons while you're on
   RentCast's free tier.
3. **Deploy the web service.** Check `GET /` returns healthy.
4. **Set `MELLO_TOOL_SECRET`** on the web service, add the matching
   `X-Mello-Token` header to all three Vapi tools, redeploy, confirm `GET /`
   reports `"tool_auth": "enabled"`.
5. **Run `tools/preflight_check.py`** with `RENDER_BASE_URL` set to the
   dashboard hostname (suffix included). It checks the duplicate-record fix,
   the empty-string-numeric fix, note preservation, the status fallback, and
   the `no_deal` guardrail — all without placing a call.
6. **Verify leads actually have phone numbers and ARV.** Still the number one
   launch blocker from your handoff, and nothing in this pass changes it.
7. **Deploy every cron individually** (Render crons don't reliably auto-deploy
   on push — use Manual Deploy → Deploy latest commit) and set each one's full
   env var set. They now fail loudly on a missing variable instead of
   producing a confusing 404.
8. **One test call:** `python tools/test_call.py --phone +1... --arv 355000`.
   Confirm afterwards that exactly one Airtable record was updated, prior notes
   are still present, and `log_call_outcome` fired on its own.
9. **Run reconcile** — expect `already logged correctly: 1`.
10. **Then** enable `dispatch-calls` with `MAX_CALLS_PER_DAY=10`.
11. Keep `draft-improvements` disabled until `ANTHROPIC_MODEL` is set to a real
    model id. `tool-health-check` can now be enabled whenever you want — its
    email actually works.

---

## Suggested prompt addition (v14) — the repair-estimate guardrail

Not applied; this is a Vapi dashboard change. Handoff §7.5.

> **Your repair estimate is not negotiable.** Once you've settled on a repair
> figure from what the seller described, that number is an input to your math,
> not a term of the deal. If the seller pushes back on it — "it's not that bad,"
> "fifteen thousand max" — you can acknowledge it warmly and ask what they'd
> budget, but you do not revise your own estimate downward to raise your offer.
> If they insist, say plainly that you'd rather have someone take a proper look
> than guess low, and log it as a `Priority Follow-up` if their reason to sell
> is strong.

---

## New environment variables

| Variable | Where | Default | Why |
|---|---|---|---|
| `MELLO_TOOL_SECRET` | web service | *(none — open)* | Authenticates the Vapi tool endpoints. Set this. |
| `BUSINESS_TIMEZONE` | everywhere | `America/Monterrey` | The date boundary everything keys on. |
| `DASHBOARD_USER` | web service | `mello` | Now actually checked. |
| `MAX_ENRICHMENTS_PER_DAY` | both enrichment crons | `15` | **Set to 1 on RentCast's free tier.** |
| `MIN_OFFER_TO_SELLER` | web service | `15000` | The guardrail from C1. |
| `ANTHROPIC_MODEL` | draft-improvements | `claude-sonnet-4-5` | Was a hardcoded invalid id. |
| `MAX_PROMPT_CHARS` | apply-updates | `25000` | Stops unbounded prompt growth. |
| `RENDER_BASE_URL` | your machine | *(none)* | Required by `tools/preflight_check.py`. No default on purpose. |
