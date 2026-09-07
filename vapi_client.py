"""
One place that knows how to talk to Vapi's REST API.

WHY THIS EXISTS
---------------
Four separate files were each calling GET /call with their own slightly
different assumptions, and three of them were wrong in the same two ways:

  1. RESPONSE SHAPE. Vapi has returned both a bare list and a
     {"results": [...]} envelope across versions. cron_reconcile_calls.py
     handled both; cron_draft_improvements.py, cron_tool_health_check.py and
     dashboard.py all did `calls = response.json()` and then iterated. If
     the account ever returns the envelope, iterating a dict yields its KEYS
     (strings), and the next line calls `.get()` on a string — an
     AttributeError that kills the cron with a confusing traceback.

  2. PAGINATION. cron_reconcile_calls.py — the layer that exists
     specifically to guarantee no call goes unrecorded — passed limit=100
     and stopped there. At MAX_CALLS_PER_DAY=80 plus retries, a busy day
     can exceed 100 calls in the 24h lookback window, and the guarantee
     layer would silently miss the oldest ones. A guarantee with a quiet
     ceiling is not a guarantee.

Everything that reads calls from Vapi now goes through list_calls().
"""

import os

import requests

VAPI_API_KEY = os.environ.get("VAPI_API_KEY")
VAPI_ASSISTANT_ID = os.environ.get("VAPI_ASSISTANT_ID")

BASE = "https://api.vapi.ai"
PAGE_SIZE = 100
MAX_PAGES = 20  # 2,000 calls — far above any real day; a stop, not a limit


def _headers():
    if not VAPI_API_KEY:
        raise RuntimeError("VAPI_API_KEY is not set on this job.")
    return {"Authorization": f"Bearer {VAPI_API_KEY}"}


def _unwrap(payload):
    """Vapi returns either a bare list or a {'results': [...]} envelope."""
    if isinstance(payload, dict):
        return payload.get("results") or payload.get("data") or []
    return payload or []


def list_calls(created_at_ge: str = None, assistant_id: str = None) -> list:
    """
    Every call for this assistant since `created_at_ge` (a UTC ISO
    timestamp), following pagination.

    Pages by shrinking the time window: Vapi's list endpoint supports
    createdAtLt, so each page asks for calls older than the oldest one seen
    so far. This avoids depending on an offset/cursor field whose name has
    changed between versions.
    """
    assistant_id = assistant_id or VAPI_ASSISTANT_ID
    if not assistant_id:
        raise RuntimeError("VAPI_ASSISTANT_ID is not set on this job.")

    collected = []
    seen_ids = set()
    cursor_before = None

    for _ in range(MAX_PAGES):
        params = {"assistantId": assistant_id, "limit": PAGE_SIZE}
        if created_at_ge:
            params["createdAtGe"] = created_at_ge
        if cursor_before:
            params["createdAtLt"] = cursor_before

        response = requests.get(f"{BASE}/call", headers=_headers(), params=params, timeout=30)
        response.raise_for_status()
        page = _unwrap(response.json())

        fresh = [c for c in page if isinstance(c, dict) and c.get("id") not in seen_ids]
        if not fresh:
            break

        for call in fresh:
            seen_ids.add(call.get("id"))
        collected.extend(fresh)

        if len(page) < PAGE_SIZE:
            break

        oldest = min(
            (c.get("createdAt") for c in fresh if c.get("createdAt")),
            default=None,
        )
        if not oldest or oldest == cursor_before:
            break  # cannot advance the window — stop rather than loop forever
        cursor_before = oldest

    return collected


def get_call(call_id: str) -> dict:
    """Full detail for one call. The list endpoint may omit per-message
    tool-call results, so tool-health checking needs this."""
    response = requests.get(f"{BASE}/call/{call_id}", headers=_headers(), timeout=30)
    response.raise_for_status()
    return response.json()


def customer_number(call: dict) -> str:
    """
    The dialed number, defensively.

    `call.get("customer", {}).get("number")` looks safe but is not: if the
    key exists with a null value, .get returns None and the chained .get
    raises AttributeError. That pattern was live in dashboard.py.
    """
    return (call.get("customer") or {}).get("number")
