# Handoff — state of the system, and what to check next

Last updated: September 14, 2026.

Read `CLEANUP_AUDIT_2026-09-07.md` for the full bug history. This is the
current state and the open list.

---

## How to ask for a fresh review in a new chat

Start a new conversation **inside this project** and paste something like:

> Do a full bug and logic review of this codebase. The project docs contain
> every source file. Read `HANDOFF.md` first for current state, then
> `CLEANUP_AUDIT_2026-09-07.md` for what has already been found and fixed —
> don't re-report those unless they regressed.
>
> Focus on paths that have never run in production: `cron_dispatch_calls.py`,
> `cron_morning_lead_prep.py`, `cron_draft_improvements.py`,
> `cron_apply_updates.py`. Check them against the real Airtable schema using
> the Airtable tools — base `appIcVAo8Y0BWFIHT`, tables Leads / Daily Log /
> Proposed Updates. Schema mismatches have been the highest-yield bug class
> in this project and are invisible until a job runs.
>
> My repo is at `~/Desktop/Mello Acquisitions/Mello Agent/mello-server-git`.
> Ask for folder access so you can read and edit it directly rather than
> sending me files.
>
> Tell me what's actually wrong. Don't restate what's already documented.

**Two things worth telling a fresh reviewer**, because both cost real time
here:

1. **The sandbox cannot reach api.vapi.ai, api.airtable.com or
   onrender.com.** The MCP Airtable tools work; raw HTTP from either shell
   does not. Anything hitting those services has to run in the user's own
   Terminal. `./run_checks.sh` batches the common ones.
2. **`git push` needs the macOS keychain**, which the sandbox cannot reach.
   Claude can commit; the user pushes.

---

## Current state

| | |
|---|---|
| Web service | live, healthy, tool auth enabled |
| Preflight | 15/15 passing |
| Vapi tool headers | `X-Mello-Token` on all three, verified |
| Vapi speech settings | applied via `tools/tune_voice.py` |
| System prompt | v18 in Vapi |
| Contract path | **verified end to end** — rendered, attached, delivered |
| Reconcile cron (layer 3) | **verified in production** — backfilled real calls |
| Leads table | 1 test record. No pipeline yet. |
| Dashboard | username `mello` (or `DASHBOARD_USER`) |

**Verified by a real call:** nothing. The one live test died in 54 seconds
before any tool fired. Every tool-path claim above rests on preflight and on
`tools/test_contract.py`, not on a conversation.

---

## The two things left before launch

### 1. One clean test call
See `NEXT_test_call.md`. The bar: reaches an offer, ends naturally,
`log_call_outcome` fires on its own, exactly one Airtable record changes.

### 2. Confirm sourcing and the crons
Never successfully run — `cron_morning_lead_prep` would have failed on every
lead until the `enrichments_today` column was created on Sept 14.

```bash
set -a; . ./.env; set +a
python3 cron_morning_lead_prep.py      # spends BatchData + RentCast
python3 tools/lead_readiness.py        # how many are actually dialable
```

Then deploy each cron on Render individually — they inherit no environment
variables and do not reliably auto-deploy on push.

---

## Known gaps, deliberately open

1. **Opt-outs spoken on a call the agent failed to log are not
   auto-suppressed.** All three logging layers only ever move `New` ->
   `Contacted`; none infers `Opt Out` from a transcript. That is the right
   call — a wrong guess either kills a live lead or, worse, leaves someone
   who opted out looking dialable — but it means a human has to read the
   note. Closing it properly means classifying transcripts with a model.
2. **Daily Log counters are read-modify-write.** Airtable has no atomic
   increment, so concurrent writers can lose a count. Drift is always toward
   UNDER-counting, so `MAX_CALLS_PER_DAY` is a safety net, not a meter.
3. **One timezone per state.** A lead in El Paso (Mountain) is treated as
   Central, so the calling window opens an hour early there. Fine for Texas
   metros; needs a per-zip lookup before expanding.
4. **No per-attempt call log.** `#_calls` is a cumulative counter with no
   timestamps, so the dashboard can only show all-time totals. A `Call Log`
   table with one row per attempt would fix that and make reconcile's dedupe
   cheaper.
5. **Recording disclosure** is reactive only. Whether it must be proactive
   depends on the seller's state; flagged in the prompt, unresolved.
6. **`#_messages` exists in Airtable and nothing writes it.** Left from the
   SMS integration that was never built.

---

## Things that cost days here. Don't relearn them.

- **The Render hostname has a random suffix**:
  `https://mello-server-hqfi.onrender.com`. The suffix-less name still
  resolves but has nothing behind it, so requests **hang instead of
  failing** — which looks like a timeout bug, not a config bug.
- **Render crons inherit nothing** and don't reliably auto-deploy on push.
  Use Manual Deploy → Deploy latest commit.
- **Render free tier blocks all outbound SMTP.** Everything uses Resend's
  HTTPS API. Do not reintroduce `smtplib`.
- **Resend only delivers to the account's own address** until a domain is
  verified. Sends report success and vanish. Check Resend's Emails log.
- **Airtable allows 5 requests/second per base**, shared by every cron, the
  dashboard, and the live call path. `airtable_helpers` retries on 429.
- **RentCast is pay-as-you-go**, ~2 requests per lead. Earlier notes in this
  repo claimed a 50/month free tier and a ~25 lead/month ceiling. That was
  wrong and has been corrected; `MAX_ENRICHMENTS_PER_DAY` is a spend guard,
  not a quota workaround.
- **Schema mismatches are the highest-yield bug class in this project.**
  Three separate ones have shipped: a missing Daily Log column that would
  have made every sourced lead fail, a missing `Applied` select option that
  would have re-appended the same paragraph to the system prompt nightly,
  and a missing `enrichment_attempts` column that would have caused infinite
  retries. None is visible until the job runs. Check the live schema first.

---

## Security

- **Rotate the Airtable PAT and the Vapi API key.** Both were pasted into a
  chat transcript on Sept 8. `MELLO_TOOL_SECRET` was generated in-session and
  never exposed.
- `.env` is gitignored. Verify it stays that way.
- Tool endpoints require `X-Mello-Token`. `/vapi_call_ended` deliberately
  does not — Vapi posts it without the tool headers — and it only ever moves
  `New` -> `Contacted`. If that changes, sign it.
