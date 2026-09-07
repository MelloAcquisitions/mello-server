"""
Mello Acquisitions — Control Dashboard

One page, hosted on the existing Render web service, showing leads, deal
status, cost and daily activity instead of juggling Vapi's dashboard,
Airtable and Render's logs separately.

SETUP: DASHBOARD_USER (optional, defaults to "mello") and DASHBOARD_PASSWORD
(required). Anyone with the Render URL + /dashboard would otherwise see real
seller names, phone numbers and deal data.

Wired into main.py via:
    from dashboard import router as dashboard_router
    app.include_router(dashboard_router)

CHANGES IN THIS CLEANUP
-----------------------
1. THE USERNAME IS NOW CHECKED. check_password() compared only the
   password and ignored the username entirely.
2. THE COST PAGE NOW PAGINATES the Daily Log table. It did a single
   unpaginated GET, so "all-time" totals silently stopped counting after
   100 daily rows — roughly three months in, with no error.
3. LIVE CALLS NO LONGER CRASH ON A NULL CUSTOMER.
   `c.get("customer", {}).get("number")` returns None (not {}) when the key
   exists with a null value, and the chained .get then raises
   AttributeError. It also assumed a bare-list response; both are handled
   in vapi_client now.
4. LEAD DATA IS HTML-ESCAPED before rendering. call_transcript_summary
   contains text an AI transcribed from whatever a stranger said on a phone
   call, and it was being injected straight into innerHTML.
5. A 30-SECOND CACHE on the full-table scan. Opening the page did two full
   Airtable scans (stats + leads) and the productivity tab did a third,
   against a 5 request/second per-base limit shared with the live call path.
"""

import json as json_lib
import os
import secrets
import time

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from airtable_helpers import DAILY_LOG_URL
from mello_time import today_iso
from vapi_client import customer_number, list_calls

router = APIRouter()
security = HTTPBasic()

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "mello")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")
AIRTABLE_API_KEY = os.environ.get("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Leads")
AIRTABLE_URL = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"

_CACHE_TTL_SECONDS = 30
_leads_cache = {"at": 0.0, "records": None}


def check_password(credentials: HTTPBasicCredentials = Depends(security)):
    if not DASHBOARD_PASSWORD:
        raise HTTPException(500, "DASHBOARD_PASSWORD not set on the server")
    # Both checks run unconditionally (no short-circuit) so the response
    # time does not reveal which half was wrong.
    user_ok = secrets.compare_digest(credentials.username or "", DASHBOARD_USER)
    pass_ok = secrets.compare_digest(credentials.password or "", DASHBOARD_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(401, "Incorrect username or password",
                            headers={"WWW-Authenticate": "Basic"})
    return True


def _fetch_paginated(url: str) -> list:
    """
    Every record from an Airtable table, following the offset cursor.
    Airtable returns at most 100 per page; without this, every count on this
    dashboard is silently capped at the first page with no error shown.
    """
    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
        raise HTTPException(500, "AIRTABLE_API_KEY or AIRTABLE_BASE_ID not set")

    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    records = []
    params = {"pageSize": 100}

    while True:
        response = requests.get(url, headers=headers, params=params, timeout=15)
        if not response.ok:
            print(f"Airtable error in dashboard fetch ({response.status_code}): {response.text[:300]}")
            raise HTTPException(status_code=response.status_code,
                                detail=f"Airtable error: {response.text[:300]}")
        payload = response.json()
        records.extend(payload.get("records", []))
        offset = payload.get("offset")
        if not offset:
            break
        params["offset"] = offset

    return records


def _fetch_all_leads():
    """Cached for 30 seconds. Loading the dashboard used to trigger two full
    table scans, and switching to the productivity tab a third — all against
    the same 5 req/sec per-base budget the live call path depends on."""
    now = time.time()
    if _leads_cache["records"] is not None and (now - _leads_cache["at"]) < _CACHE_TTL_SECONDS:
        return _leads_cache["records"]
    records = _fetch_paginated(AIRTABLE_URL)
    _leads_cache.update({"at": now, "records": records})
    return records


def _status_counts(records: list) -> dict:
    counts = {}
    for r in records:
        status = r["fields"].get("status", "Unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


@router.get("/api/dashboard/leads")
def get_leads_json(authorized: bool = Depends(check_password)):
    """Raw lead data as JSON — used by the page's JS, and useful on its own."""
    records = _fetch_all_leads()
    return {"leads": [r["fields"] for r in records], "count": len(records)}


@router.get("/api/dashboard/stats")
def get_stats_json(authorized: bool = Depends(check_password)):
    """Quick counts by status — the numbers you check daily."""
    records = _fetch_all_leads()
    total_calls = sum(r["fields"].get("#_calls", 0) or 0 for r in records)
    return {
        "status_counts": _status_counts(records),
        "total_leads": len(records),
        "total_calls_made": total_calls,
    }


@router.get("/api/dashboard/productivity")
def get_productivity_json(authorized: bool = Depends(check_password)):
    """
    Real productivity numbers from what is actually in Airtable.

    HONEST LIMITATION: cumulative counts per lead (#_calls) are tracked, not
    per-attempt timestamps, so a true day/week/month breakdown is not
    available — that needs a separate call-log table with a timestamp per
    attempt. What is shown is accurate for all-time totals and the current
    status breakdown.
    """
    records = _fetch_all_leads()
    status_counts = _status_counts(records)
    total_calls = sum(r["fields"].get("#_calls", 0) or 0 for r in records)
    total_leads = len(records)

    agreed = status_counts.get("Agreed", 0)
    closed = status_counts.get("Closed", 0)  # set manually once a deal funds

    return {
        "total_leads": total_leads,
        "total_calls_all_time": total_calls,
        "status_breakdown": status_counts,
        "agreed": agreed,
        "rejected": status_counts.get("Rejected", 0),
        "opt_out": status_counts.get("Opt Out", 0),
        "exhausted": status_counts.get("Exhausted", 0),
        "priority_follow_up": status_counts.get("Priority Follow-up", 0),
        "closed": closed,
        "conversion_rate_pct": round(agreed / total_leads * 100, 1) if total_leads else 0,
        "closed_rate_pct": round(closed / total_leads * 100, 1) if total_leads else 0,
        "note": ("Day/week/month call breakdowns need a timestamped call log, not yet "
                 "built — these are all-time totals. \"Agreed rate\" is Agreed as a share "
                 "of all leads (a real price was reached); \"actually closed rate\" is "
                 "deals that funded, which only you can mark — the system has no way to "
                 "know a deal closed."),
    }


@router.get("/api/dashboard/costs")
def get_costs_json(authorized: bool = Depends(check_password)):
    """
    Costs, split into what is genuinely real vs. still estimated.

    VAPI_COST_PER_MINUTE is a REAL rate derived from observed charges,
    multiplied by REAL tracked usage (call_seconds_today, recorded by the
    end-of-call webhook). The two BatchData rates are UNCALIBRATED
    placeholders — pull your real per-record and per-skip-trace-match rates
    from BatchData's billing page and set the env vars.

    FIXED_MONTHLY is hand-maintained: real, but only as accurate as you keep
    it. RentCast is deliberately in "upcoming" rather than fixed — still on
    the free tier, so its future paid cost is shown without inflating today.
    """
    VAPI_COST_PER_MINUTE = float(os.environ.get("VAPI_COST_PER_MINUTE", 0.0744))
    BATCHDATA_COST_PER_RECORD = float(os.environ.get("BATCHDATA_COST_PER_RECORD", 0.05))
    BATCHDATA_COST_PER_SKIPTRACE_MATCH = float(os.environ.get("BATCHDATA_COST_PER_SKIPTRACE_MATCH", 0.10))

    FIXED_MONTHLY = {
        "Box (paid annual plan, Sign not yet upgraded)": 15.00,  # $180/yr / 12
        "Render (6 cron jobs, estimated)": 1.00,
        "Zillapi/Anthropic (light usage, estimated)": 5.00,
    }
    UPCOMING_COSTS = {"RentCast (after upgrading from free tier)": 74.00}

    try:
        daily_records = _fetch_paginated(DAILY_LOG_URL)
    except HTTPException:
        daily_records = []

    def total(field):
        return sum(r["fields"].get(field, 0) or 0 for r in daily_records)

    today = today_iso()
    todays = next((r["fields"] for r in daily_records if r["fields"].get("date") == today), {})

    def batchdata_cost(properties, matches):
        return round(properties * BATCHDATA_COST_PER_RECORD
                     + matches * BATCHDATA_COST_PER_SKIPTRACE_MATCH, 2)

    todays_seconds = todays.get("call_seconds_today", 0) or 0
    todays_vapi_cost = round((todays_seconds / 60) * VAPI_COST_PER_MINUTE, 2)
    todays_batchdata_cost = batchdata_cost(
        todays.get("batchdata_properties_today", 0) or 0,
        todays.get("batchdata_skiptrace_matches_today", 0) or 0,
    )

    return {
        "today": {
            "call_minutes": round(todays_seconds / 60, 1),
            "vapi_cost": todays_vapi_cost,
            "batchdata_calls": todays.get("batchdata_calls_today", 0) or 0,
            "batchdata_properties": todays.get("batchdata_properties_today", 0) or 0,
            "batchdata_skiptrace_matches": todays.get("batchdata_skiptrace_matches_today", 0) or 0,
            "batchdata_cost": todays_batchdata_cost,
            "enrichments": todays.get("enrichments_today", 0) or 0,
            "estimated_cost": round(todays_vapi_cost + todays_batchdata_cost, 2),
        },
        "all_time": {
            "total_call_minutes": round(total("call_seconds_today") / 60, 1),
            "estimated_vapi_cost": round((total("call_seconds_today") / 60) * VAPI_COST_PER_MINUTE, 2),
            "total_batchdata_calls": total("batchdata_calls_today"),
            "total_batchdata_properties": total("batchdata_properties_today"),
            "total_batchdata_skiptrace_matches": total("batchdata_skiptrace_matches_today"),
            "estimated_batchdata_cost": batchdata_cost(
                total("batchdata_properties_today"), total("batchdata_skiptrace_matches_today")
            ),
            "days_tracked": len(daily_records),
        },
        "fixed_monthly_subscriptions": FIXED_MONTHLY,
        "fixed_monthly_total": round(sum(FIXED_MONTHLY.values()), 2),
        "upcoming_costs": UPCOMING_COSTS,
        "upcoming_monthly_total": round(sum(UPCOMING_COSTS.values()), 2),
        "note": (
            f"Vapi cost uses a real derived rate (${VAPI_COST_PER_MINUTE:.4f}/min) x tracked "
            f"call minutes. BatchData tracks the real billing units (records returned and "
            f"skip-trace matches), but the two per-unit rates "
            f"(${BATCHDATA_COST_PER_RECORD:.2f}/record, "
            f"${BATCHDATA_COST_PER_SKIPTRACE_MATCH:.2f}/match) are UNCALIBRATED placeholders. "
            f"Pull your real rates from BatchData's billing page and set "
            f"BATCHDATA_COST_PER_RECORD / BATCHDATA_COST_PER_SKIPTRACE_MATCH. RentCast's "
            f"free tier is 50 requests/MONTH and each enriched lead costs 2 — watch the "
            f"'enrichments' number above."
        ),
    }


@router.get("/api/dashboard/system_status")
def get_system_status_json(authorized: bool = Depends(check_password)):
    """
    Live status of the Render cron jobs, from Render's own API — not
    self-reported, since a script that crashes hard cannot report its own
    failure.

    SETUP: RENDER_API_KEY (Render > Account Settings > API Keys) and
    RENDER_CRON_SERVICE_IDS as a JSON mapping, e.g.
      {"apply-updates": "crn-xxx", "morning-lead-prep": "crn-yyy"}

    HONEST NOTE: built against Render's documented API but not confirmed
    against a live call — verify the response shape once the key is set.
    """
    render_api_key = os.environ.get("RENDER_API_KEY")
    if not render_api_key:
        return {"error": "RENDER_API_KEY not set — system status unavailable", "jobs": []}

    try:
        service_ids = json_lib.loads(os.environ.get("RENDER_CRON_SERVICE_IDS", "{}"))
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
            if not response.ok:
                jobs_status.append({"name": name, "status": "error checking status",
                                    "error": response.text[:200]})
                continue
            jobs = response.json()
            # Render has returned both a bare list and a list of
            # {"job": {...}} wrappers depending on version.
            latest = jobs[0] if jobs else None
            if isinstance(latest, dict) and "job" in latest:
                latest = latest["job"]
            jobs_status.append({
                "name": name,
                "status": (latest or {}).get("status") or "no runs yet",
                "started_at": (latest or {}).get("startedAt"),
                "finished_at": (latest or {}).get("finishedAt"),
            })
        except Exception as e:
            jobs_status.append({"name": name, "status": "error checking status", "error": str(e)[:200]})

    return {"jobs": jobs_status}


@router.get("/api/dashboard/live_calls")
def get_live_calls_json(authorized: bool = Depends(check_password)):
    """Currently in-progress Vapi calls."""
    try:
        all_calls = list_calls()
        in_progress = [
            {"id": c.get("id"), "phone": customer_number(c), "started_at": c.get("createdAt")}
            for c in all_calls if c.get("status") == "in-progress"
        ]
        return {"live_calls": in_progress, "count": len(in_progress)}
    except Exception as e:
        return {"error": str(e)[:200], "live_calls": []}


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(authorized: bool = Depends(check_password)):
    """The dashboard page — plain HTML/JS, no framework needed."""
    return DASHBOARD_HTML


DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
  .lead-top { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  .lead-address { font-size: 14px; }
  .lead-meta { font-size: 12px; color: #999; margin-top: 2px; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; white-space: nowrap; }
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
  .detail-row { display: flex; justify-content: space-between; gap: 16px; padding: 4px 0; color: #ccc; }
  .detail-row span:first-child { color: #999; flex: 0 0 auto; }
  .detail-row span:last-child { text-align: right; white-space: pre-wrap; }
  .job-row, .call-row { background: #1a1a1a; border: 1px solid #262626; border-radius: 8px; margin-bottom: 8px; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  .job-status { font-size: 12px; padding: 2px 10px; border-radius: 10px; }
  .job-status.succeeded { background: #4ade8020; color: #4ade80; }
  .job-status.failed { background: #f8717120; color: #f87171; }
  .job-status.running { background: #60a5fa20; color: #60a5fa; }
  .job-status.default { background: #99999920; color: #999; }
  .note-box { background: #1a1a1a; border: 1px solid #333; border-radius: 8px; padding: 12px 16px; font-size: 12px; color: #999; margin-top: 16px; line-height: 1.5; }
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

    <div class="page" id="status-page"><div id="status-content">Loading system status...</div></div>
    <div class="page" id="costs-page"><div id="costs-content">Loading costs...</div></div>
    <div class="page" id="productivity-page"><div id="productivity-content">Loading productivity...</div></div>
    <div class="page" id="live-page"><div id="live-content">Loading live calls...</div></div>
  </div>

<script>
// Lead fields contain text an AI transcribed from whatever a stranger said
// on a phone call. Escape everything before it touches innerHTML.
function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function money(v) {
  const n = Number(v);
  return (v === null || v === undefined || v === '' || isNaN(n)) ? '-' : '$' + n.toLocaleString();
}

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
          <div class="lead-address">${esc(lead.address) || 'No address'}</div>
          <div class="lead-meta">${esc(lead.owner_name) || 'Unknown owner'} &middot; ${esc(lead['#_calls'] || 0)} calls</div>
        </div>
        <span class="badge ${badgeClass(lead.status)}">${esc(lead.status) || 'New'}</span>
      </div>
      <div class="detail" id="detail-${i}">
        <div class="detail-row"><span>Phone</span><span>${esc(lead.phone) || '-'}</span></div>
        <div class="detail-row"><span>Source</span><span>${esc(lead.source) || '-'}</span></div>
        <div class="detail-row"><span>ARV</span><span>${money(lead.arv)}</span></div>
        <div class="detail-row"><span>Repair estimate</span><span>${money(lead.repair_estimate)}</span></div>
        <div class="detail-row"><span>MAO floor</span><span>${money(lead.mao_floor)}</span></div>
        <div class="detail-row"><span>Offer amount</span><span>${money(lead.offer_amount)}</span></div>
        <div class="detail-row"><span>Last call</span><span>${esc(lead.last_call_date) || '-'}</span></div>
        <div class="detail-row"><span>Next contact</span><span>${esc(lead.next_contact_date) || '-'}</span></div>
        <div class="detail-row"><span>Notes</span><span>${esc(lead.call_transcript_summary) || '-'}</span></div>
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

async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${url} failed (${res.status})`);
  }
  return res.json();
}

async function loadDashboard() {
  try {
    const stats = await fetchJson('/api/dashboard/stats');
    const leadsData = await fetchJson('/api/dashboard/leads');
    allLeads = leadsData.leads;

    document.getElementById('loading').style.display = 'none';
    document.getElementById('tabs').style.display = 'flex';

    const statsDiv = document.getElementById('stats');
    statsDiv.style.display = 'flex';
    statsDiv.innerHTML = `
      <div class="stat-card"><div class="num">${esc(stats.total_leads)}</div><div class="label">Total leads</div></div>
      <div class="stat-card"><div class="num">${esc(stats.total_calls_made)}</div><div class="label">Calls made</div></div>
      ${Object.entries(stats.status_counts).map(([status, count]) =>
        `<div class="stat-card"><div class="num">${esc(count)}</div><div class="label">${esc(status)}</div></div>`
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
    const data = await fetchJson('/api/dashboard/system_status');
    if (data.error) { el.innerHTML = `<div class="note-box">${esc(data.error)}</div>`; return; }
    el.innerHTML = data.jobs.map(job => `
      <div class="job-row">
        <div><strong>${esc(job.name)}</strong><div class="lead-meta">${esc(job.started_at) || 'no runs yet'}</div></div>
        <span class="job-status ${['succeeded','failed','running'].includes(job.status) ? job.status : 'default'}">${esc(job.status)}</span>
      </div>
    `).join('') || '<div class="note-box">No cron jobs configured yet.</div>';
  } catch (err) {
    el.innerHTML = `<div class="note-box">Error loading status: ${esc(err.message)}</div>`;
  }
}

async function loadCosts() {
  const el = document.getElementById('costs-content');
  el.innerHTML = 'Loading...';
  try {
    const data = await fetchJson('/api/dashboard/costs');
    el.innerHTML = `
      <div class="stats">
        <div class="stat-card"><div class="num">$${esc(data.today.estimated_cost)}</div><div class="label">Today (Vapi + BatchData)</div></div>
        <div class="stat-card"><div class="num">${esc(data.today.call_minutes)}</div><div class="label">Call minutes today</div></div>
        <div class="stat-card"><div class="num">${esc(data.today.batchdata_properties)}</div><div class="label">BatchData records today</div></div>
        <div class="stat-card"><div class="num">${esc(data.today.enrichments)}</div><div class="label">Enrichments today</div></div>
        <div class="stat-card"><div class="num">$${esc(data.fixed_monthly_total)}</div><div class="label">Fixed monthly subs</div></div>
      </div>
      <h3 style="font-size:14px;margin-top:24px">All-time usage (${esc(data.all_time.days_tracked)} days tracked)</h3>
      <div class="job-row"><span>Total call minutes</span><span>${esc(data.all_time.total_call_minutes)} min (est. $${esc(data.all_time.estimated_vapi_cost)})</span></div>
      <div class="job-row"><span>BatchData records / skip-trace matches</span><span>${esc(data.all_time.total_batchdata_properties)} / ${esc(data.all_time.total_batchdata_skiptrace_matches)} (est. $${esc(data.all_time.estimated_batchdata_cost)})</span></div>
      <h3 style="font-size:14px;margin-top:24px">Fixed monthly subscriptions</h3>
      ${Object.entries(data.fixed_monthly_subscriptions).map(([name, cost]) =>
        `<div class="job-row"><span>${esc(name)}</span><span>$${esc(cost)}/mo</span></div>`).join('')}
      <h3 style="font-size:14px;margin-top:24px">Upcoming (not yet charged)</h3>
      ${Object.entries(data.upcoming_costs).map(([name, cost]) =>
        `<div class="job-row"><span>${esc(name)}</span><span>$${esc(cost)}/mo</span></div>`).join('')}
      <div class="note-box">${esc(data.note)}</div>
    `;
  } catch (err) {
    el.innerHTML = `<div class="note-box">Error loading costs: ${esc(err.message)}</div>`;
  }
}

async function loadProductivity() {
  const el = document.getElementById('productivity-content');
  el.innerHTML = 'Loading...';
  try {
    const data = await fetchJson('/api/dashboard/productivity');
    el.innerHTML = `
      <div class="stats">
        <div class="stat-card"><div class="num">${esc(data.total_leads)}</div><div class="label">Total leads</div></div>
        <div class="stat-card"><div class="num">${esc(data.total_calls_all_time)}</div><div class="label">Total calls</div></div>
        <div class="stat-card"><div class="num">${esc(data.agreed)}</div><div class="label">Agreed</div></div>
        <div class="stat-card"><div class="num">${esc(data.closed)}</div><div class="label">Actually Closed</div></div>
        <div class="stat-card"><div class="num">${esc(data.priority_follow_up)}</div><div class="label">Priority Follow-up</div></div>
        <div class="stat-card"><div class="num">${esc(data.rejected)}</div><div class="label">Rejected</div></div>
        <div class="stat-card"><div class="num">${esc(data.opt_out)}</div><div class="label">Opt Out</div></div>
        <div class="stat-card"><div class="num">${esc(data.exhausted)}</div><div class="label">Exhausted</div></div>
        <div class="stat-card"><div class="num">${esc(data.conversion_rate_pct)}%</div><div class="label">Agreed rate</div></div>
        <div class="stat-card"><div class="num">${esc(data.closed_rate_pct)}%</div><div class="label">Actually closed rate</div></div>
      </div>
      <div class="note-box">${esc(data.note)}</div>
    `;
  } catch (err) {
    el.innerHTML = `<div class="note-box">Error loading productivity: ${esc(err.message)}</div>`;
  }
}

async function loadLiveCalls() {
  const el = document.getElementById('live-content');
  el.innerHTML = 'Loading...';
  try {
    const data = await fetchJson('/api/dashboard/live_calls');
    if (data.error) { el.innerHTML = `<div class="note-box">${esc(data.error)}</div>`; return; }
    el.innerHTML = `
      <div class="stats"><div class="stat-card"><div class="num">${esc(data.count)}</div><div class="label">Currently live</div></div></div>
      ${data.live_calls.map(c => `
        <div class="call-row">
          <span>${esc(c.phone) || 'Unknown number'}</span>
          <span class="lead-meta">Started: ${esc(c.started_at) || '-'}</span>
        </div>`).join('') || '<div class="note-box">No calls in progress right now.</div>'}
    `;
  } catch (err) {
    el.innerHTML = `<div class="note-box">Error loading live calls: ${esc(err.message)}</div>`;
  }
}

loadDashboard();
</script>
</body>
</html>
"""
