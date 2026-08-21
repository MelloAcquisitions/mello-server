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

  /* Left status rail */
  .rail { width: 64px; background: #141414; border-right: 1px solid #262626; min-height: 100vh; padding-top: 24px; display: flex; flex-direction: column; align-items: center; }
  .status-dot { width: 12px; height: 12px; border-radius: 50%; margin-bottom: 6px; }
  .status-dot.live { background: #4ade80; box-shadow: 0 0 8px #4ade8080; }
  .status-dot.error { background: #f87171; box-shadow: 0 0 8px #f8717180; }
  .status-label { font-size: 9px; color: #999; writing-mode: vertical-rl; margin-top: 8px; }

  /* Main area */
  .main { flex: 1; max-width: 900px; margin: 0 auto; padding: 32px 24px; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .subtitle { font-size: 13px; color: #999; margin-bottom: 24px; }

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
  .badge-default { background: #99999920; color: #999; }

  .detail { display: none; margin-top: 12px; padding-top: 12px; border-top: 1px solid #333; font-size: 13px; }
  .detail.open { display: block; }
  .detail-row { display: flex; justify-content: space-between; padding: 4px 0; color: #ccc; }
  .detail-row span:first-child { color: #999; }

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

    <div id="loading">Loading...</div>
    <div class="stats" id="stats" style="display:none"></div>

    <div class="tabs" style="display:none" id="tabs">
      <div class="tab active" data-filter="active">Active</div>
      <div class="tab" data-filter="all">All</div>
    </div>

    <div id="leads-list"></div>
  </div>

<script>
const ACTIVE_STATUSES = ['Contacted', 'Qualified', 'Offer Made', 'Agreed'];
let allLeads = [];
let currentFilter = 'active';

function badgeClass(status) {
  const map = { 'Agreed': 'agreed', 'Qualified': 'qualified', 'Offer Made': 'offer-made', 'Contacted': 'contacted' };
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
loadDashboard();
</script>
</body>
</html>
"""

