"""
Mello Acquisitions — Control Dashboard

A single web page, hosted on your existing Render server, giving you one
place to see leads, deal status, and daily activity instead of juggling
Vapi's dashboard, Airtable, and Render's logs separately.

SETUP: add ONE new environment variable — DASHBOARD_PASSWORD — set to
whatever you want to log in with. Anyone with your Render URL + /dashboard
could otherwise see real lead names and deal data, so this is required,
not optional.

Import and include this in main.py:
    from dashboard import router as dashboard_router
    app.include_router(dashboard_router)
"""

import os
import secrets

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from airtable_helpers import DAILY_LOG_TABLE

router = APIRouter()
security = HTTPBasic()

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Leads")
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"


def check_password(credentials: HTTPBasicCredentials = Depends(security)):
    if not DASHBOARD_PASSWORD:
        raise HTTPException(500, "DASHBOARD_PASSWORD not set on the server")
    correct = secrets.compare_digest(credentials.password, DASHBOARD_PASSWORD)
    if not correct:
        raise HTTPException(401, "Incorrect password", headers={"WWW-Authenticate": "Basic"})
    return True


def _fetch_all_leads():
    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
        raise HTTPException(500, "AIRTABLE_API_KEY or AIRTABLE_BASE_ID not set on the dashboard service")

    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    response = requests.get(AIRTABLE_URL, headers=headers, timeout=15)
    if not response.ok:
        print(f"Airtable error in dashboard fetch ({response.status_code}): {response.text}")
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Airtable error: {response.text}",
        )
    return response.json().get("records", [])


@router.get("/api/dashboard/leads")
def get_leads_json(authorized: bool = Depends(check_password)):
    """Raw lead data as JSON — used by the dashboard page's JS, but also
    useful on its own if you ever want to pull this into another tool."""
    records = _fetch_all_leads()
    leads = [r["fields"] for r in records]
    return {"leads": leads, "count": len(leads)}


@router.get("/api/dashboard/stats")
def get_stats_json(authorized: bool = Depends(check_password)):
    """Quick counts by status — the numbers you actually check daily."""
    records = _fetch_all_leads()
    status_counts = {}
    for r in records:
        status = r["fields"].get("status", "Unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    total_calls = sum(r["fields"].get("#_calls", 0) for r in records)
    return {"status_counts": status_counts, "total_leads": len(records), "total_calls_made": total_calls}


@router.get("/api/dashboard/productivity")
def get_productivity_json(authorized: bool = Depends(check_password)):
    """
    Real productivity numbers from what's actually in Airtable. HONEST
    LIMITATION: we track cumulative counts per lead (#_calls), not
    per-attempt timestamps, so a true day/week/month breakdown of calls
    isn't available yet — that would need a separate call-log table with
    a timestamp per attempt. What's shown here is accurate for all-time
    totals and current status breakdown, which is most of what matters
    day to day.
    """
    records = _fetch_all_leads()
    status_counts = {}
    for r in records:
        status = r["fields"].get("status", "Unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    total_calls = sum(r["fields"].get("#_calls", 0) for r in records)
    total_leads = len(records)
    agreed = status_counts.get("Agreed", 0)
    rejected = status_counts.get("Rejected", 0)
    opt_out = status_counts.get("Opt Out", 0)
    exhausted = status_counts.get("Exhausted", 0)
    priority_follow_up = status_counts.get("Priority Follow-up", 0)
    closed = status_counts.get("Closed", 0)  # set manually by you once a deal actually funds — see dashboard notes

    conversion_rate = round((agreed / total_leads * 100), 1) if total_leads else 0
    closed_rate = round((closed / total_leads * 100), 1) if total_leads else 0

    return {
        "total_leads": total_leads,
        "total_calls_all_time": total_calls,
        "status_breakdown": status_counts,
        "agreed": agreed,
        "rejected": rejected,
        "opt_out": opt_out,
        "exhausted": exhausted,
        "priority_follow_up": priority_follow_up,
        "closed": closed,
        "closed_rate_pct": closed_rate,
        "conversion_rate_pct": conversion_rate,
        "note": "Day/week/month call breakdowns require a timestamped call log, not yet built — these are all-time totals. \"Conversion rate\" is Agreed as a share of all leads (a real price was reached); \"closed_rate_pct\" is deals that actually funded, which only you can mark — the system has no way to know a deal closed on its own.",
    }


@router.get("/api/dashboard/costs")
def get_costs_json(authorized: bool = Depends(check_password)):
    """
    Costs, split into what's genuinely real vs. still estimated:

    - VAPI_COST_PER_MINUTE and BATCHDATA_COST_PER_CALL are REAL rates
      derived from actual observed charges (not guesses) — set as env vars
      so you can refine them as you get more real data points, without a
      code change. Multiplied by REAL tracked usage (call_seconds_today,
      batchdata_calls_today — see main.py's vapi_call_ended webhook and
      lead_sourcing.py), this gives an actual usage-based estimate instead
      of a flat per-call/per-lead guess.
    - FIXED_MONTHLY is a hand-maintained list of what you actually pay —
      real, but only as accurate as you keep it updated here.
    - RentCast is deliberately NOT in FIXED_MONTHLY — still on the free
      tier (no current cost), so its future paid-tier cost is shown
      separately under "upcoming_costs" rather than inflating today's total.
    """
    VAPI_COST_PER_MINUTE = float(os.environ.get("VAPI_COST_PER_MINUTE", 0.0744))  # averaged from 2 real Telnyx-era calls: $0.44/341s and $0.22/185s
    # BatchData bills per property record returned, plus extra per skip-trace
    # match — NOT per API call (confirmed from their docs; a flat per-call
    # rate was disproven by real data ranging $0.006 to $1.20 per call on
    # different days). These two rates are UNCALIBRATED placeholders — pull
    # your real per-record and per-skip-trace-match rate from BatchData's own
    # billing/usage page and set these env vars once you have them.
    BATCHDATA_COST_PER_RECORD = float(os.environ.get("BATCHDATA_COST_PER_RECORD", 0.05))
    BATCHDATA_COST_PER_SKIPTRACE_MATCH = float(os.environ.get("BATCHDATA_COST_PER_SKIPTRACE_MATCH", 0.10))

    # Real, hand-maintained recurring costs. Update whenever a subscription changes.
    FIXED_MONTHLY = {
        "Box (paid annual plan, Sign not yet upgraded)": 15.00,  # $180/yr ÷ 12
        "Render (6 cron jobs, estimated)": 1.00,
        "Zillapi/Anthropic (light usage, estimated)": 5.00,
    }
    # Not currently being charged — shown separately so it's visible without
    # inflating today's real total.
    UPCOMING_COSTS = {
        "RentCast (after upgrading from free tier)": 74.00,
    }

    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    daily_log_url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{DAILY_LOG_TABLE}"
    response = requests.get(daily_log_url, headers=headers, timeout=15)
    daily_records = response.json().get("records", []) if response.ok else []

    total_call_seconds = sum(r["fields"].get("call_seconds_today", 0) for r in daily_records)
    total_batchdata_calls = sum(r["fields"].get("batchdata_calls_today", 0) for r in daily_records)
    total_batchdata_properties = sum(r["fields"].get("batchdata_properties_today", 0) for r in daily_records)
    total_batchdata_skiptrace_matches = sum(r["fields"].get("batchdata_skiptrace_matches_today", 0) for r in daily_records)

    from datetime import date
    today = date.today().isoformat()
    todays_record = next((r for r in daily_records if r["fields"].get("date") == today), None)
    todays_call_seconds = todays_record["fields"].get("call_seconds_today", 0) if todays_record else 0
    todays_batchdata_calls = todays_record["fields"].get("batchdata_calls_today", 0) if todays_record else 0
    todays_batchdata_properties = todays_record["fields"].get("batchdata_properties_today", 0) if todays_record else 0
    todays_batchdata_skiptrace_matches = todays_record["fields"].get("batchdata_skiptrace_matches_today", 0) if todays_record else 0

    def batchdata_cost(properties, skiptrace_matches):
        return round(properties * BATCHDATA_COST_PER_RECORD + skiptrace_matches * BATCHDATA_COST_PER_SKIPTRACE_MATCH, 2)

    todays_vapi_cost = round((todays_call_seconds / 60) * VAPI_COST_PER_MINUTE, 2)
    todays_batchdata_cost = batchdata_cost(todays_batchdata_properties, todays_batchdata_skiptrace_matches)

    fixed_monthly_total = sum(FIXED_MONTHLY.values())
    upcoming_monthly_total = sum(UPCOMING_COSTS.values())

    return {
        "today": {
            "call_minutes": round(todays_call_seconds / 60, 1),
            "vapi_cost": todays_vapi_cost,
            "batchdata_calls": todays_batchdata_calls,
            "batchdata_properties": todays_batchdata_properties,
            "batchdata_skiptrace_matches": todays_batchdata_skiptrace_matches,
            "batchdata_cost": todays_batchdata_cost,
            "estimated_cost": round(todays_vapi_cost + todays_batchdata_cost, 2),
        },
        "all_time": {
            "total_call_minutes": round(total_call_seconds / 60, 1),
            "estimated_vapi_cost": round((total_call_seconds / 60) * VAPI_COST_PER_MINUTE, 2),
            "total_batchdata_calls": total_batchdata_calls,
            "total_batchdata_properties": total_batchdata_properties,
            "total_batchdata_skiptrace_matches": total_batchdata_skiptrace_matches,
            "estimated_batchdata_cost": batchdata_cost(total_batchdata_properties, total_batchdata_skiptrace_matches),
        },
        "fixed_monthly_subscriptions": FIXED_MONTHLY,
        "fixed_monthly_total": round(fixed_monthly_total, 2),
        "upcoming_costs": UPCOMING_COSTS,
        "upcoming_monthly_total": round(upcoming_monthly_total, 2),
        "note": (
            f"Vapi cost uses a real derived rate (${VAPI_COST_PER_MINUTE:.4f}/min from an actual call) "
            f"× tracked call minutes. BatchData now tracks the actual billing units per their docs — "
            f"property records returned and skip-trace matches — but the two per-unit rates "
            f"(${BATCHDATA_COST_PER_RECORD:.2f}/record, ${BATCHDATA_COST_PER_SKIPTRACE_MATCH:.2f}/match) "
            f"are UNCALIBRATED placeholders, not real numbers yet. Pull your actual rates from BatchData's "
            f"billing page and set BATCHDATA_COST_PER_RECORD / BATCHDATA_COST_PER_SKIPTRACE_MATCH. "
            f"BatchData's $50 prepaid credit balance itself isn't tracked as a running total yet."
        ),
    }


@router.get("/api/dashboard/system_status")
def get_system_status_json(authorized: bool = Depends(check_password)):
    """
    Live status of your 6 Render cron jobs, pulled from Render's own API —
    not self-reported by the scripts, since a script that crashes hard
    before finishing can't reliably report its own failure.

    SETUP: RENDER_API_KEY (Render → Account Settings → API Keys) and
    RENDER_CRON_SERVICE_IDS as a JSON mapping, e.g.:
      {"apply-updates": "crn-xxx", "morning-lead-prep": "crn-yyy", ...}
    Service IDs are visible in each cron job's own URL in Render's dashboard.

    HONEST NOTE: built against Render's documented API structure but not
    yet confirmed against a live call — verify the exact response shape
    once RENDER_API_KEY is set.
    """
    render_api_key = os.environ.get("RENDER_API_KEY")
    service_ids_raw = os.environ.get("RENDER_CRON_SERVICE_IDS", "{}")

    if not render_api_key:
        return {"error": "RENDER_API_KEY not set — system status unavailable", "jobs": []}

    import json as json_lib
    try:
        service_ids = json_lib.loads(service_ids_raw)
    except json_lib.JSONDecodeError:
        return {"error": "RENDER_CRON_SERVICE_IDS is not valid JSON", "jobs": []}

    headers = {"Authorization": f"Bearer {render_api_key}"}
    jobs_status = []

    for name, service_id in service_ids.items():
        try:
            response = requests.get(
                f"https://api.render.com/v1/services/{service_id}/jobs",
                headers=headers, params={"limit": 1}, timeout=15,
            )
            if response.ok:
                jobs = response.json()
                latest = jobs[0] if jobs else None
                jobs_status.append({
                    "name": name,
                    "status": latest.get("status") if latest else "no runs yet",
                    "started_at": latest.get("startedAt") if latest else None,
                    "finished_at": latest.get("finishedAt") if latest else None,
                })
            else:
                jobs_status.append({"name": name, "status": "error checking status", "error": response.text})
        except Exception as e:
            jobs_status.append({"name": name, "status": "error checking status", "error": str(e)})

    return {"jobs": jobs_status}


@router.get("/api/dashboard/live_calls")
def get_live_calls_json(authorized: bool = Depends(check_password)):
    """
    Currently in-progress Vapi calls. HONEST NOTE: built against Vapi's
    general /call endpoint with a status filter — the exact filter param
    name isn't confirmed against a live account yet, same caution as every
    new endpoint in this build. Verify once tested live.
    """
    vapi_api_key = os.environ.get("VAPI_API_KEY")
    vapi_assistant_id = os.environ.get("VAPI_ASSISTANT_ID")

    if not vapi_api_key:
        return {"error": "VAPI_API_KEY not set", "live_calls": []}

    headers = {"Authorization": f"Bearer {vapi_api_key}"}
    try:
        response = requests.get(
            "https://api.vapi.ai/call",
            headers=headers,
            params={"assistantId": vapi_assistant_id},
            timeout=15,
        )
        if not response.ok:
            return {"error": f"Vapi returned {response.status_code}", "live_calls": []}

        all_calls = response.json()
        in_progress = [
            {
                "id": c.get("id"),
                "phone": c.get("customer", {}).get("number"),
                "started_at": c.get("createdAt"),
            }
            for c in all_calls if c.get("status") == "in-progress"
        ]
        return {"live_calls": in_progress, "count": len(in_progress)}
    except Exception as e:
        return {"error": str(e), "live_calls": []}


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(authorized: bool = Depends(check_password)):
    """The actual dashboard page — plain HTML/JS, no framework needed for v1."""
    return """
<!DOCTYPE html>
<html>
<head>
<title>Mello Acquisitions — Dashboard</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, sans-serif; margin: 0; background: #0d0d0d; color: #eee; display: flex; }

  .rail { width: 64px; background: #141414; border-right: 1px solid #262626; min-height: 100vh; padding-top: 24px; display: flex; flex-direction: column; align-items: center; }
  .status-dot { width: 12px; height: 12px; border-radius: 50%; margin-bottom: 6px; }
  .status-dot.live { background: #4ade80; box-shadow: 0 0 8px #4ade8080; }
  .status-dot.error { background: #f87171; box-shadow: 0 0 8px #f8717180; }
  .status-label { font-size: 9px; color: #999; writing-mode: vertical-rl; margin-top: 8px; }

  .main { flex: 1; max-width: 1000px; margin: 0 auto; padding: 32px 24px; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .subtitle { font-size: 13px; color: #999; margin-bottom: 24px; }

  .top-tabs { display: flex; gap: 4px; margin-bottom: 24px; border-bottom: 1px solid #262626; padding-bottom: 12px; flex-wrap: wrap; }
  .top-tab { padding: 8px 16px; border-radius: 6px; font-size: 13px; cursor: pointer; color: #999; background: transparent; border: 1px solid #333; }
  .top-tab.active { background: #262626; color: #fff; border-color: #4ade80; }

  .page { display: none; }
  .page.active { display: block; }

  .stats { display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap; }
  .stat-card { background: #1a1a1a; border: 1px solid #333; border-radius: 8px; padding: 14px 18px; min-width: 100px; }
  .stat-card .num { font-size: 24px; font-weight: bold; }
  .stat-card .label { font-size: 11px; color: #999; }

  .tabs { display: flex; gap: 4px; margin-bottom: 12px; }
  .tab { padding: 6px 14px; border-radius: 6px; font-size: 13px; cursor: pointer; color: #999; background: transparent; border: 1px solid #333; }
  .tab.active { background: #262626; color: #fff; border-color: #444; }

  .lead-card { background: #1a1a1a; border: 1px solid #262626; border-radius: 8px; margin-bottom: 8px; padding: 12px 16px; cursor: pointer; }
  .lead-card:hover { border-color: #444; }
  .lead-top { display: flex; justify-content: space-between; align-items: center; }
  .lead-address { font-size: 14px; }
  .lead-meta { font-size: 12px; color: #999; margin-top: 2px; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; }
  .badge-agreed { background: #4ade8020; color: #4ade80; }
  .badge-qualified { background: #60a5fa20; color: #60a5fa; }
  .badge-offer-made { background: #facc1520; color: #facc15; }
  .badge-contacted { background: #a78bfa20; color: #a78bfa; }
  .badge-priority { background: #f8717120; color: #f87171; }
  .badge-human-call { background: #fb923c20; color: #fb923c; }
  .badge-closed { background: #34d39920; color: #34d399; }
  .badge-default { background: #99999920; color: #999; }

  .detail { display: none; margin-top: 12px; padding-top: 12px; border-top: 1px solid #333; font-size: 13px; }
  .detail.open { display: block; }
  .detail-row { display: flex; justify-content: space-between; padding: 4px 0; color: #ccc; }
  .detail-row span:first-child { color: #999; }

  .job-row, .call-row { background: #1a1a1a; border: 1px solid #262626; border-radius: 8px; margin-bottom: 8px; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center; }
  .job-status { font-size: 12px; padding: 2px 10px; border-radius: 10px; }
  .job-status.succeeded { background: #4ade8020; color: #4ade80; }
  .job-status.failed { background: #f8717120; color: #f87171; }
  .job-status.running { background: #60a5fa20; color: #60a5fa; }
  .job-status.default { background: #99999920; color: #999; }

  .note-box { background: #1a1a1a; border: 1px solid #333; border-radius: 8px; padding: 12px 16px; font-size: 12px; color: #999; margin-top: 16px; }

  #loading { color: #999; padding: 20px 0; }
</style>
</head>
<body>
  <div class="rail">
    <div class="status-dot live" id="status-dot"></div>
    <div class="status-label" id="status-label">LIVE</div>
  </div>

  <div class="main">
    <h1>Mello Acquisitions</h1>
    <div class="subtitle">Control dashboard</div>

    <div class="top-tabs">
      <div class="top-tab active" data-page="leads-page">Leads</div>
      <div class="top-tab" data-page="status-page">System Status</div>
      <div class="top-tab" data-page="costs-page">Costs</div>
      <div class="top-tab" data-page="productivity-page">Productivity</div>
      <div class="top-tab" data-page="live-page">Live Calls</div>
    </div>

    <div id="loading">Loading...</div>

    <div class="page active" id="leads-page">
      <div class="stats" id="stats" style="display:none"></div>
      <div class="tabs" style="display:none" id="tabs">
        <div class="tab active" data-filter="active">Active</div>
        <div class="tab" data-filter="all">All</div>
      </div>
      <div id="leads-list"></div>
    </div>

    <div class="page" id="status-page">
      <div id="status-content">Loading system status...</div>
    </div>

    <div class="page" id="costs-page">
      <div id="costs-content">Loading costs...</div>
    </div>

    <div class="page" id="productivity-page">
      <div id="productivity-content">Loading productivity...</div>
    </div>

    <div class="page" id="live-page">
      <div id="live-content">Loading live calls...</div>
    </div>
  </div>

<script>
const ACTIVE_STATUSES = ['Contacted', 'Qualified', 'Offer Made', 'Agreed', 'Priority Follow-up', 'Human Call'];
let allLeads = [];
let currentFilter = 'active';

document.querySelectorAll('.top-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.top-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(tab.dataset.page).classList.add('active');

    if (tab.dataset.page === 'status-page') loadSystemStatus();
    if (tab.dataset.page === 'costs-page') loadCosts();
    if (tab.dataset.page === 'productivity-page') loadProductivity();
    if (tab.dataset.page === 'live-page') loadLiveCalls();
  });
});

function badgeClass(status) {
  const map = { 'Agreed': 'agreed', 'Qualified': 'qualified', 'Offer Made': 'offer-made', 'Contacted': 'contacted', 'Priority Follow-up': 'priority', 'Human Call': 'human-call', 'Closed': 'closed' };
  return 'badge-' + (map[status] || 'default');
}

function renderLeads() {
  const filtered = currentFilter === 'active'
    ? allLeads.filter(l => ACTIVE_STATUSES.includes(l.status))
    : allLeads;

  document.getElementById('leads-list').innerHTML = filtered.map((lead, i) => `
    <div class="lead-card" onclick="toggleDetail(${i})">
      <div class="lead-top">
        <div>
          <div class="lead-address">${lead.address || 'No address'}</div>
          <div class="lead-meta">${lead.owner_name || 'Unknown owner'} · ${lead['#_calls'] || 0} calls</div>
        </div>
        <span class="badge ${badgeClass(lead.status)}">${lead.status || 'New'}</span>
      </div>
      <div class="detail" id="detail-${i}">
        <div class="detail-row"><span>Phone</span><span>${lead.phone || '-'}</span></div>
        <div class="detail-row"><span>Source</span><span>${lead.source || '-'}</span></div>
        <div class="detail-row"><span>ARV</span><span>${lead.arv ? '$' + lead.arv.toLocaleString() : '-'}</span></div>
        <div class="detail-row"><span>Repair estimate</span><span>${lead.repair_estimate ? '$' + lead.repair_estimate.toLocaleString() : '-'}</span></div>
        <div class="detail-row"><span>MAO floor</span><span>${lead.mao_floor ? '$' + lead.mao_floor.toLocaleString() : '-'}</span></div>
        <div class="detail-row"><span>Offer amount</span><span>${lead.offer_amount ? '$' + lead.offer_amount.toLocaleString() : '-'}</span></div>
        <div class="detail-row"><span>Last call</span><span>${lead.last_call_date || '-'}</span></div>
        <div class="detail-row"><span>Notes</span><span>${lead.call_transcript_summary || '-'}</span></div>
      </div>
    </div>
  `).join('') || '<div style="color:#999;padding:20px 0">No leads in this view.</div>';
}

function toggleDetail(i) {
  document.getElementById('detail-' + i).classList.toggle('open');
}

document.getElementById('tabs').addEventListener('click', (e) => {
  if (!e.target.classList.contains('tab')) return;
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  e.target.classList.add('active');
  currentFilter = e.target.dataset.filter;
  renderLeads();
});

async function loadDashboard() {
  try {
    const statsRes = await fetch('/api/dashboard/stats');
    if (!statsRes.ok) {
      const errBody = await statsRes.json().catch(() => ({}));
      throw new Error(errBody.detail || `stats fetch failed (${statsRes.status})`);
    }
    const stats = await statsRes.json();

    const leadsRes = await fetch('/api/dashboard/leads');
    if (!leadsRes.ok) {
      const errBody = await leadsRes.json().catch(() => ({}));
      throw new Error(errBody.detail || `leads fetch failed (${leadsRes.status})`);
    }
    const leadsData = await leadsRes.json();
    allLeads = leadsData.leads;

    document.getElementById('loading').style.display = 'none';
    document.getElementById('tabs').style.display = 'flex';

    const statsDiv = document.getElementById('stats');
    statsDiv.style.display = 'flex';
    statsDiv.innerHTML = `
      <div class="stat-card"><div class="num">${stats.total_leads}</div><div class="label">Total leads</div></div>
      <div class="stat-card"><div class="num">${stats.total_calls_made}</div><div class="label">Calls made</div></div>
      ${Object.entries(stats.status_counts).map(([status, count]) =>
        `<div class="stat-card"><div class="num">${count}</div><div class="label">${status}</div></div>`
      ).join('')}
    `;

    renderLeads();

    document.getElementById('status-dot').className = 'status-dot live';
    document.getElementById('status-label').textContent = 'LIVE';
  } catch (err) {
    document.getElementById('loading').textContent = 'Error: ' + err.message;
    document.getElementById('status-dot').className = 'status-dot error';
    document.getElementById('status-label').textContent = 'ERROR';
    console.error(err);
  }
}

async function loadSystemStatus() {
  const el = document.getElementById('status-content');
  el.innerHTML = 'Loading...';
  try {
    const res = await fetch('/api/dashboard/system_status');
    const data = await res.json();
    if (data.error) {
      el.innerHTML = `<div class="note-box">${data.error}</div>`;
      return;
    }
    el.innerHTML = data.jobs.map(job => `
      <div class="job-row">
        <div>
          <strong>${job.name}</strong>
          <div class="lead-meta">${job.started_at || 'no runs yet'}</div>
        </div>
        <span class="job-status ${job.status === 'succeeded' ? 'succeeded' : job.status === 'failed' ? 'failed' : job.status === 'running' ? 'running' : 'default'}">${job.status}</span>
      </div>
    `).join('') || '<div class="note-box">No cron jobs configured yet.</div>';
  } catch (err) {
    el.innerHTML = `<div class="note-box">Error loading status: ${err.message}</div>`;
  }
}

async function loadCosts() {
  const el = document.getElementById('costs-content');
  el.innerHTML = 'Loading...';
  try {
    const res = await fetch('/api/dashboard/costs');
    const data = await res.json();
    el.innerHTML = `
      <div class="stats">
        <div class="stat-card"><div class="num">$${data.today.estimated_cost}</div><div class="label">Today (Vapi + BatchData)</div></div>
        <div class="stat-card"><div class="num">${data.today.call_minutes}</div><div class="label">Call minutes today</div></div>
        <div class="stat-card"><div class="num">${data.today.batchdata_properties}</div><div class="label">BatchData records today</div></div>
        <div class="stat-card"><div class="num">${data.today.batchdata_skiptrace_matches}</div><div class="label">Skip-trace matches today</div></div>
        <div class="stat-card"><div class="num">$${data.fixed_monthly_total}</div><div class="label">Fixed monthly subs</div></div>
      </div>
      <h3 style="font-size:14px;margin-top:24px">All-time usage</h3>
      <div class="job-row"><span>Total call minutes</span><span>${data.all_time.total_call_minutes} min (est. $${data.all_time.estimated_vapi_cost})</span></div>
      <div class="job-row"><span>Total BatchData records / skip-trace matches</span><span>${data.all_time.total_batchdata_properties} / ${data.all_time.total_batchdata_skiptrace_matches} (est. $${data.all_time.estimated_batchdata_cost})</span></div>
      <h3 style="font-size:14px;margin-top:24px">Fixed monthly subscriptions</h3>
      ${Object.entries(data.fixed_monthly_subscriptions).map(([name, cost]) =>
        `<div class="job-row"><span>${name}</span><span>$${cost}/mo</span></div>`
      ).join('')}
      <h3 style="font-size:14px;margin-top:24px">Upcoming (not yet being charged)</h3>
      ${Object.entries(data.upcoming_costs).map(([name, cost]) =>
        `<div class="job-row"><span>${name}</span><span>$${cost}/mo</span></div>`
      ).join('')}
      <div class="note-box">${data.note}</div>
    `;
  } catch (err) {
    el.innerHTML = `<div class="note-box">Error loading costs: ${err.message}</div>`;
  }
}

async function loadProductivity() {
  const el = document.getElementById('productivity-content');
  el.innerHTML = 'Loading...';
  try {
    const res = await fetch('/api/dashboard/productivity');
    const data = await res.json();
    el.innerHTML = `
      <div class="stats">
        <div class="stat-card"><div class="num">${data.total_leads}</div><div class="label">Total leads</div></div>
        <div class="stat-card"><div class="num">${data.total_calls_all_time}</div><div class="label">Total calls</div></div>
        <div class="stat-card"><div class="num">${data.agreed}</div><div class="label">Agreed</div></div>
        <div class="stat-card"><div class="num">${data.closed}</div><div class="label">Actually Closed</div></div>
        <div class="stat-card"><div class="num">${data.priority_follow_up}</div><div class="label">Priority Follow-up</div></div>
        <div class="stat-card"><div class="num">${data.rejected}</div><div class="label">Rejected</div></div>
        <div class="stat-card"><div class="num">${data.opt_out}</div><div class="label">Opt Out</div></div>
        <div class="stat-card"><div class="num">${data.exhausted}</div><div class="label">Exhausted</div></div>
        <div class="stat-card"><div class="num">${data.conversion_rate_pct}%</div><div class="label">Agreed rate</div></div>
        <div class="stat-card"><div class="num">${data.closed_rate_pct}%</div><div class="label">Actually closed rate</div></div>
      </div>
      <div class="note-box">${data.note}</div>
    `;
  } catch (err) {
    el.innerHTML = `<div class="note-box">Error loading productivity: ${err.message}</div>`;
  }
}

async function loadLiveCalls() {
  const el = document.getElementById('live-content');
  el.innerHTML = 'Loading...';
  try {
    const res = await fetch('/api/dashboard/live_calls');
    const data = await res.json();
    if (data.error) {
      el.innerHTML = `<div class="note-box">${data.error}</div>`;
      return;
    }
    el.innerHTML = `
      <div class="stats"><div class="stat-card"><div class="num">${data.count}</div><div class="label">Currently live</div></div></div>
      ${data.live_calls.map(c => `
        <div class="call-row">
          <span>${c.phone || 'Unknown number'}</span>
          <span class="lead-meta">Started: ${c.started_at || '-'}</span>
        </div>
      `).join('') || '<div class="note-box">No calls in progress right now.</div>'}
    `;
  } catch (err) {
    el.innerHTML = `<div class="note-box">Error loading live calls: ${err.message}</div>`;
  }
}

loadDashboard();
</script>
</body>
</html>
"""
