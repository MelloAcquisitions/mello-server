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
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}
    response = requests.get(AIRTABLE_URL, headers=headers, timeout=15)
    response.raise_for_status()
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
  body { font-family: -apple-system, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; background: #0d0d0d; color: #eee; }
  h1 { font-size: 20px; }
  .stats { display: flex; gap: 16px; margin: 20px 0; flex-wrap: wrap; }
  .stat-card { background: #1a1a1a; border: 1px solid #333; border-radius: 8px; padding: 16px; min-width: 120px; }
  .stat-card .num { font-size: 28px; font-weight: bold; }
  .stat-card .label { font-size: 12px; color: #999; }
  table { width: 100%; border-collapse: collapse; margin-top: 20px; }
  th, td { text-align: left; padding: 8px; border-bottom: 1px solid #333; font-size: 14px; }
  th { color: #999; font-weight: normal; }
  .status-agreed { color: #4ade80; }
  .status-rejected { color: #f87171; }
  .status-new { color: #60a5fa; }
  #loading { color: #999; }
</style>
</head>
<body>
  <h1>Mello Acquisitions — Control Dashboard</h1>
  <div id="loading">Loading...</div>
  <div class="stats" id="stats" style="display:none"></div>
  <table id="leads-table" style="display:none">
    <thead>
      <tr><th>Address</th><th>Owner</th><th>Status</th><th>ARV</th><th>Offer</th><th>Calls</th></tr>
    </thead>
    <tbody id="leads-body"></tbody>
  </table>

<script>
async function loadDashboard() {
  const statsRes = await fetch('/api/dashboard/stats');
  const stats = await statsRes.json();
  const leadsRes = await fetch('/api/dashboard/leads');
  const leadsData = await leadsRes.json();

  document.getElementById('loading').style.display = 'none';

  const statsDiv = document.getElementById('stats');
  statsDiv.style.display = 'flex';
  statsDiv.innerHTML = `
    <div class="stat-card"><div class="num">${stats.total_leads}</div><div class="label">Total leads</div></div>
    <div class="stat-card"><div class="num">${stats.total_calls_made}</div><div class="label">Calls made</div></div>
    ${Object.entries(stats.status_counts).map(([status, count]) =>
      `<div class="stat-card"><div class="num">${count}</div><div class="label">${status}</div></div>`
    ).join('')}
  `;

  const tbody = document.getElementById('leads-body');
  document.getElementById('leads-table').style.display = 'table';
  tbody.innerHTML = leadsData.leads.map(lead => `
    <tr>
      <td>${lead.address || '-'}</td>
      <td>${lead.owner_name || '-'}</td>
      <td class="status-${(lead.status || '').toLowerCase().replace(' ', '-')}">${lead.status || '-'}</td>
      <td>${lead.arv ? '$' + lead.arv.toLocaleString() : '-'}</td>
      <td>${lead.offer_amount ? '$' + lead.offer_amount.toLocaleString() : '-'}</td>
      <td>${lead['#_calls'] || 0}</td>
    </tr>
  `).join('');
}
loadDashboard();
</script>
</body>
</html>
"""
