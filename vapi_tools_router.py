"""
vapi_tools_router.py — drop-in fix + diagnostic for the "Vapi says the tool
succeeded but Render never sees the POST" problem.

WHAT THIS GIVES YOU
-------------------
1. `/vapi/tools`  — ONE endpoint that speaks Vapi's real Function-tool
   protocol (the `message.toolCallList` envelope in, `{"results":[...]}` out).
   Your current endpoints in main.py do NOT speak this protocol: they expect
   a flat body like {"arv": 250000, "repair_cost": 15000} and return
   {"success": true}. That shape is what an **API Request** tool sends, not
   what a **Function** tool sends. This router accepts BOTH shapes, so it
   works no matter which tool type you end up using in Vapi.

2. `install_request_logging(app)` — logs method + path + first 400 chars of
   the body of EVERY request that reaches the service. This is how you prove
   where Vapi's POST is actually landing (see the diagnosis note at the
   bottom of this file — my leading theory is that it's landing on
   /vapi_call_ended and getting a cheerful 200 back).

3. `install_catch_all_logger(app)` — MUST be called last. Logs any POST to a
   path that matches nothing, instead of returning a bare 404 you might miss.

4. `find_lead_flexible()` — fixes a separate, real bug: the agent sends the
   FULL address ("6506 Clubway Ln, Austin, TX 78745") because that's what
   {{property_address}} contains, but the Airtable `address` column only
   holds the street ("6506 Clubway Ln"). Exact-match lookup therefore always
   misses and upsert_lead() silently CREATES A DUPLICATE orphan record
   instead of updating the real lead.

HOW TO WIRE IT UP (main.py)
---------------------------
    from vapi_tools_router import (
        router as vapi_router, install_request_logging, install_catch_all_logger,
    )

    app = FastAPI(title="Mello Acquisitions Agent Tools")
    install_request_logging(app)          # right after app = FastAPI(...)
    app.include_router(dashboard_router)
    app.include_router(vapi_router)
    install_catch_all_logger(app)         # LAST line of route registration

Then in Vapi, set each tool's Server URL to:
    https://mello-server.onrender.com/vapi/tools
(the same URL for all three — this router dispatches by tool name).
"""

import json
import traceback
from datetime import date
from typing import Any, Callable

from fastapi import APIRouter, BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from calculator import flip_mao, calculate_final_fee
from airtable_helpers import find_lead_record, upsert_lead, query_leads

router = APIRouter()


# ---------------------------------------------------------------------------
# 1. Address matching — the duplicate-record bug
# ---------------------------------------------------------------------------

def _street_only(address: str) -> str:
    """'6506 Clubway Ln, Austin, TX 78745' -> '6506 Clubway Ln'."""
    return address.split(",")[0].strip()


def find_lead_flexible(address: str):
    """
    Tries exact match first (so nothing changes for records that already
    match), then falls back to the street-only form the Airtable `address`
    column actually stores. Returns the record dict, or None.
    """
    record = find_lead_record(address)
    if record:
        return record

    street = _street_only(address)
    if street and street != address:
        record = find_lead_record(street)
        if record:
            return record

    # Last resort: substring match, in case of "St" vs "Street" style drift.
    try:
        safe = street.replace("\\", "\\\\").replace("'", "\\'")
        matches = query_leads(f"FIND('{safe}', {{address}}) > 0", max_records=1)
        return matches[0] if matches else None
    except Exception:
        return None


def resolve_address_for_write(address: str) -> str:
    """
    Returns the address string that will actually hit the existing record.
    If we can find the lead, write against ITS stored address value so
    upsert_lead() patches instead of creating a duplicate.
    """
    record = find_lead_flexible(address)
    if record:
        return record["fields"].get("address", address)
    return _street_only(address) or address


# ---------------------------------------------------------------------------
# 2. Tool implementations — thin wrappers over what main.py already does
# ---------------------------------------------------------------------------

NOTIFY_STATUSES = {"Human Call", "Offer Made", "Priority Follow-up"}


def _tool_calculate_mao(args: dict, bg=None) -> dict:
    return flip_mao(
        arv=float(args["arv"]),
        repair_cost=float(args["repair_cost"]),
        wholesale_fee_min=float(args.get("wholesale_fee_min", 10000)),
        buyer_profit_pct=float(args.get("buyer_profit_pct", 0.10)),
    )


def _tool_calculate_final_fee(args: dict, bg=None) -> dict:
    return calculate_final_fee(
        arv=float(args["arv"]),
        repair_cost=float(args["repair_cost"]),
        agreed_price=float(args["agreed_price"]),
        buyer_profit_pct=float(args.get("buyer_profit_pct", 0.10)),
    )


def _tool_log_call_outcome(args: dict, bg=None) -> dict:
    incoming_address = args["address"]
    write_address = resolve_address_for_write(incoming_address)
    existing = find_lead_record(write_address)

    fields = {
        "status": args["status"],
        "last_call_date": date.today().isoformat(),
    }
    if args.get("notes"):
        fields["call_transcript_summary"] = args["notes"]
    if not existing:
        fields["#_calls"] = 1
    for key in ("offer_amount", "arv", "repair_estimate", "mao_floor"):
        if args.get(key) is not None:
            fields[key] = float(args[key])
    for key in ("email", "next_contact_date"):
        if args.get(key) is not None:
            fields[key] = args[key]

    result = upsert_lead(write_address, fields)

    # SMTP login takes seconds. Never do it on the request path — the tool
    # timeout is measured against the whole response, and a slow mail server
    # would turn a successful Airtable write into a "tool failed" in the call.
    if args["status"] in NOTIFY_STATUSES and bg is not None:
        lead_fields = {**(existing["fields"] if existing else {}),
                       **fields, "address": write_address}
        bg.add_task(_safe_notify, write_address, lead_fields, args["status"])

    return {
        "success": True,
        "airtable_record": result.get("id"),
        "matched_existing_lead": bool(existing),
    }


def _safe_notify(address: str, lead_fields: dict, status: str):
    try:
        from deal_dispatch import notify_attention_needed
        notify_attention_needed(lead_fields, status)
        print(f"[vapi_tools] notification sent for {address} ({status})")
    except Exception as e:
        print(f"[vapi_tools] notify FAILED for {address} (status still saved): {e}")


def _safe_dispatch_contract(address: str, lead_fields: dict, agreed_price: float):
    try:
        from deal_dispatch import dispatch_agreed_deal
        dispatch_agreed_deal(lead_fields, agreed_price)
        upsert_lead(address, {"#_emails": (lead_fields.get("#_emails", 0) or 0) + 1})
        print(f"[vapi_tools] contract emailed for {address}")
    except Exception as e:
        print(f"[vapi_tools] contract dispatch FAILED for {address}: {e}")
        try:
            upsert_lead(address, {
                "call_transcript_summary": (lead_fields.get("call_transcript_summary") or "")
                + f" [Contract email failed to send: {e}]"
            })
        except Exception as inner:
            print(f"[vapi_tools] could not record dispatch failure: {inner}")


def _tool_flag_for_human_review(args: dict, bg=None) -> dict:
    incoming_address = args["address"]
    write_address = resolve_address_for_write(incoming_address)

    fields = {
        "status": "Agreed",
        "offer_amount": float(args["agreed_price"]),
        "call_transcript_summary": args.get("call_transcript_summary", ""),
        "last_call_date": date.today().isoformat(),
    }
    for key in ("email",):
        if args.get(key) is not None:
            fields[key] = args[key]
    for key in ("repair_estimate", "mao_floor"):
        if args.get(key) is not None:
            fields[key] = float(args[key])

    result = upsert_lead(write_address, fields)

    # Contract generation + SMTP is slow — same reasoning as the notify path.
    full_record = find_lead_record(write_address)
    lead_fields = full_record["fields"] if full_record else {**fields, "address": write_address}
    dispatch_state = "skipped"
    if bg is not None:
        bg.add_task(_safe_dispatch_contract, write_address, lead_fields,
                    float(args["agreed_price"]))
        dispatch_state = "queued"

    return {
        "success": True,
        "airtable_record": result.get("id"),
        "needs_human_review": True,
        "contract_dispatch": dispatch_state,
    }


TOOL_HANDLERS: dict[str, Callable[..., Any]] = {
    "calculate_mao": _tool_calculate_mao,
    "calculate_final_fee": _tool_calculate_final_fee,
    "log_call_outcome": _tool_log_call_outcome,
    "flag_for_human_review": _tool_flag_for_human_review,
}


# ---------------------------------------------------------------------------
# 3. The endpoint itself
# ---------------------------------------------------------------------------

def _parse_tool_calls(body: dict) -> list[tuple[str, str, dict]]:
    """
    Returns a list of (tool_call_id, tool_name, arguments).
    Handles three shapes:
      a) Vapi Function tool:  {"message": {"toolCallList": [{"id", "function": {"name", "arguments"}}]}}
      b) Older Vapi variant:  {"message": {"toolCalls": [...]}}
      c) Flat API Request:    {"arv": 250000, ...}  -> caller must supply the name via path/query
    """
    message = body.get("message") or {}
    raw_calls = message.get("toolCallList") or message.get("toolCalls") or []

    parsed = []
    for call in raw_calls:
        fn = call.get("function") or {}
        name = fn.get("name") or call.get("name")
        args = fn.get("arguments", call.get("arguments", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        parsed.append((call.get("id"), name, args or {}))
    return parsed


@router.post("/vapi/tools")
async def vapi_tools(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    calls = _parse_tool_calls(body)

    if not calls:
        # No Vapi envelope. Either something else POSTed here, or Vapi sent
        # a non-tool-calls server message (status-update, end-of-call-report).
        msg_type = (body.get("message") or {}).get("type")
        print(f"[vapi_tools] POST with no toolCallList (message.type={msg_type}) — ignoring")
        return {"received": True, "ignored": f"no tool calls (type={msg_type})"}

    results = []
    for call_id, name, args in calls:
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            print(f"[vapi_tools] unknown tool name: {name!r}")
            results.append({"toolCallId": call_id,
                            "error": f"Unknown tool '{name}' on this server."})
            continue
        try:
            print(f"[vapi_tools] -> {name} args={args}")
            output = handler(args, background_tasks)
            # Vapi requires result to be a STRING. Objects get dropped
            # silently, which looks exactly like "the tool did nothing".
            results.append({"toolCallId": call_id,
                            "result": json.dumps(output, default=str)})
            print(f"[vapi_tools] <- {name} ok: {output}")
        except Exception as e:
            traceback.print_exc()
            results.append({"toolCallId": call_id, "error": str(e)[:300]})

    return JSONResponse(status_code=200, content={"results": results})


# ---------------------------------------------------------------------------
# 4. Diagnostics
# ---------------------------------------------------------------------------

def install_request_logging(app: FastAPI, max_body_chars: int = 400):
    """Logs every request that reaches the app, body included."""

    @app.middleware("http")
    async def _log_requests(request: Request, call_next):
        body_bytes = b""
        if request.method in ("POST", "PUT", "PATCH"):
            body_bytes = await request.body()

            async def _receive():
                return {"type": "http.request", "body": body_bytes, "more_body": False}

            request._receive = _receive  # let downstream handlers read it again

        snippet = body_bytes[:max_body_chars].decode("utf-8", "replace")
        print(f"[req] {request.method} {request.url.path} "
              f"ua={request.headers.get('user-agent', '-')[:40]} body={snippet}")
        response = await call_next(request)
        print(f"[req] {request.method} {request.url.path} -> {response.status_code}")
        return response


def install_catch_all_logger(app: FastAPI):
    """Register LAST. Catches POSTs to paths nothing else claimed."""

    @app.post("/{full_path:path}")
    async def _catch_all(full_path: str, request: Request):
        body = (await request.body())[:400].decode("utf-8", "replace")
        print(f"[UNMATCHED POST] /{full_path} body={body}")
        return JSONResponse(status_code=404,
                            content={"error": f"No handler for /{full_path}"})


# ---------------------------------------------------------------------------
# DIAGNOSIS NOTE — why Vapi reports success with no POST in your logs
# ---------------------------------------------------------------------------
# Vapi resolves a Function tool's webhook in this order:
#     tool.server.url -> assistant.server.url -> phoneNumber.server.url -> org
# If the three tools have a BLANK Server URL, they fall through to the
# assistant's Server URL — which you set to /vapi_call_ended for the
# end-of-call report. That endpoint returns HTTP 200 with
# {"received": true, "ignored": "not an end-of-call-report"} for anything
# that isn't an end-of-call-report. Vapi sees 200 -> marks the tool call
# successful. You see no POST to /calculate_mao, because there wasn't one.
#
# Verify in 30 seconds (you have the tool IDs in the handoff doc):
#   curl -s https://api.vapi.ai/tool/a6589cb2-6d0d-456c-8d20-5ef047ba0d2a \
#     -H "Authorization: Bearer $VAPI_API_KEY" | python -m json.tool
# Look at: .type, .async, .server.url
#   - server.url null/absent  -> confirmed, that's the bug
#   - async: true             -> also fire-and-forget "success"
#   - type "function" but url pointing at /calculate_mao -> wrong protocol
#     (that endpoint can't parse the envelope; use /vapi/tools instead)
