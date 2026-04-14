"""sevDesk MCP Server - HTTP/SSE transport"""
import json
import os

import httpx
import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

import ipaddress
import sys
import time
import uuid as uuid_mod

SEVDESK_BASE_URL = os.environ.get("SEVDESK_BASE_URL", "https://my.sevdesk.de/api/v1")
SEVDESK_API_TOKEN = os.environ.get("SEVDESK_API_TOKEN", "")
MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID", "")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET", "")

_oauth_codes: dict[str, float] = {}
_oauth_tokens: set[str] = set()


def _audit_log(event: dict) -> None:
    """Write single-line JSON audit record to stdout."""
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "svc": "sevdesk-mcp", **event}
    print(f"[MCP-AUDIT] {json.dumps(record, separators=(',', ':'))}", flush=True, file=sys.stdout)

mcp_server = Server("sevdesk-mcp")


def api_headers() -> dict:
    return {
        "Authorization": SEVDESK_API_TOKEN,
        "Content-Type": "application/json",
    }


def sevdesk_get(path: str, params: dict | None = None) -> dict:
    url = f"{SEVDESK_BASE_URL}/{path.lstrip('/')}"
    with httpx.Client(timeout=30) as client:
        r = client.get(url, headers=api_headers(), params=params or {})
        r.raise_for_status()
        return r.json()


def sevdesk_post(path: str, body: dict) -> dict:
    url = f"{SEVDESK_BASE_URL}/{path.lstrip('/')}"
    with httpx.Client(timeout=30) as client:
        r = client.post(url, headers=api_headers(), json=body)
        r.raise_for_status()
        return r.json()


def sevdesk_put(path: str, body: dict) -> dict:
    url = f"{SEVDESK_BASE_URL}/{path.lstrip('/')}"
    with httpx.Client(timeout=30) as client:
        r = client.put(url, headers=api_headers(), json=body)
        r.raise_for_status()
        return r.json()


def sevdesk_delete(path: str) -> dict:
    url = f"{SEVDESK_BASE_URL}/{path.lstrip('/')}"
    with httpx.Client(timeout=30) as client:
        r = client.delete(url, headers=api_headers())
        r.raise_for_status()
        return r.json()


TOOLS = [
    # ---- Contacts ----
    Tool(
        name="list_contacts",
        description="List contacts from sevDesk. Optionally filter by name, customer number or type.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100, "description": "Max results"},
                "offset": {"type": "integer", "default": 0},
                "depth": {"type": "integer", "default": 1, "description": "1 = include address etc."},
                "customerNumber": {"type": "string", "description": "Filter by customer number"},
            },
        },
    ),
    Tool(
        name="get_contact",
        description="Get a single contact by its sevDesk ID.",
        inputSchema={
            "type": "object",
            "properties": {"contact_id": {"type": "integer"}},
            "required": ["contact_id"],
        },
    ),
    Tool(
        name="create_contact",
        description="Create a new contact in sevDesk.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Company name (for organisations)"},
                "surename": {"type": "string", "description": "First name (for persons)"},
                "familyname": {"type": "string", "description": "Last name (for persons)"},
                "category_id": {"type": "integer", "description": "3 = Customer, 4 = Supplier, 5 = Partner, 6 = Prospect"},
                "customerNumber": {"type": "string"},
                "description": {"type": "string"},
                "vatNumber": {"type": "string"},
            },
        },
    ),
    # ---- Invoices ----
    Tool(
        name="list_invoices",
        description="List invoices from sevDesk.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100},
                "offset": {"type": "integer", "default": 0},
                "status": {"type": "string", "description": "100=Draft, 200=Delivered, 1000=Cancelled"},
                "invoiceNumber": {"type": "string"},
                "startDate": {"type": "integer", "description": "Unix timestamp"},
                "endDate": {"type": "integer", "description": "Unix timestamp"},
                "contact_id": {"type": "integer"},
            },
        },
    ),
    Tool(
        name="get_invoice",
        description="Get a single invoice by ID.",
        inputSchema={
            "type": "object",
            "properties": {"invoice_id": {"type": "integer"}},
            "required": ["invoice_id"],
        },
    ),
    Tool(
        name="get_invoice_positions",
        description="Get line items / positions of an invoice.",
        inputSchema={
            "type": "object",
            "properties": {"invoice_id": {"type": "integer"}},
            "required": ["invoice_id"],
        },
    ),
    Tool(
        name="create_invoice",
        description="Create a new invoice in sevDesk.",
        inputSchema={
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer"},
                "invoiceDate": {"type": "string", "description": "ISO date e.g. 2024-01-15"},
                "header": {"type": "string"},
                "headText": {"type": "string"},
                "footText": {"type": "string"},
                "timeToPay": {"type": "integer", "description": "Days to pay"},
                "discount": {"type": "number"},
                "address": {"type": "string"},
                "currency": {"type": "string", "default": "EUR"},
                "taxRate": {"type": "number"},
                "taxText": {"type": "string"},
                "taxType": {"type": "string", "default": "default"},
                "invoiceType": {"type": "string", "default": "RE"},
                "positions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "quantity": {"type": "number"},
                            "price": {"type": "number"},
                            "unity_id": {"type": "integer"},
                            "taxRate": {"type": "number"},
                        },
                    },
                },
            },
            "required": ["contact_id", "invoiceDate"],
        },
    ),
    Tool(
        name="send_invoice_by_email",
        description="Send an invoice via email.",
        inputSchema={
            "type": "object",
            "properties": {
                "invoice_id": {"type": "integer"},
                "toEmail": {"type": "string"},
                "subject": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["invoice_id", "toEmail", "subject", "text"],
        },
    ),
    Tool(
        name="mark_invoice_sent",
        description="Mark an invoice as sent (status 200).",
        inputSchema={
            "type": "object",
            "properties": {"invoice_id": {"type": "integer"}},
            "required": ["invoice_id"],
        },
    ),
    Tool(
        name="cancel_invoice",
        description="Cancel an invoice.",
        inputSchema={
            "type": "object",
            "properties": {"invoice_id": {"type": "integer"}},
            "required": ["invoice_id"],
        },
    ),
    # ---- Orders ----
    Tool(
        name="list_orders",
        description="List orders (Auftraege) from sevDesk.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100},
                "offset": {"type": "integer", "default": 0},
                "status": {"type": "string"},
                "contact_id": {"type": "integer"},
            },
        },
    ),
    Tool(
        name="get_order",
        description="Get a single order by ID.",
        inputSchema={
            "type": "object",
            "properties": {"order_id": {"type": "integer"}},
            "required": ["order_id"],
        },
    ),
    # ---- Vouchers ----
    Tool(
        name="list_vouchers",
        description="List vouchers (incoming invoices / receipts) from sevDesk.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100},
                "offset": {"type": "integer", "default": 0},
                "status": {"type": "string", "description": "50=Draft, 100=Open, 1000=Paid"},
                "startDate": {"type": "integer"},
                "endDate": {"type": "integer"},
                "contact_id": {"type": "integer"},
            },
        },
    ),
    Tool(
        name="get_voucher",
        description="Get a single voucher by ID.",
        inputSchema={
            "type": "object",
            "properties": {"voucher_id": {"type": "integer"}},
            "required": ["voucher_id"],
        },
    ),
    Tool(
        name="create_voucher",
        description="Create a new voucher (incoming receipt) in sevDesk.",
        inputSchema={
            "type": "object",
            "properties": {
                "voucherDate": {"type": "string", "description": "ISO date"},
                "supplier_contact_id": {"type": "integer"},
                "description": {"type": "string"},
                "voucherType": {"type": "string", "default": "VOU"},
                "creditDebit": {"type": "string", "default": "C", "description": "C=Credit, D=Debit"},
                "taxType": {"type": "string", "default": "default"},
                "currency": {"type": "string", "default": "EUR"},
                "positions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "accountingType_id": {"type": "integer"},
                            "taxRate": {"type": "number"},
                            "sum": {"type": "number"},
                            "net": {"type": "boolean"},
                        },
                    },
                },
            },
            "required": ["voucherDate"],
        },
    ),
    # ---- Accounting / Transactions ----
    Tool(
        name="list_check_accounts",
        description="List bank / cash accounts (Bankkonten) in sevDesk.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_transactions",
        description="List transactions for a check account.",
        inputSchema={
            "type": "object",
            "properties": {
                "check_account_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 100},
                "offset": {"type": "integer", "default": 0},
                "startDate": {"type": "string", "description": "ISO date"},
                "endDate": {"type": "string", "description": "ISO date"},
            },
        },
    ),
    Tool(
        name="get_transaction",
        description="Get a single bank transaction by ID.",
        inputSchema={
            "type": "object",
            "properties": {"transaction_id": {"type": "integer"}},
            "required": ["transaction_id"],
        },
    ),
    # ---- Documents / Parts ----
    Tool(
        name="list_parts",
        description="List parts / products (Artikel) from sevDesk.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100},
                "offset": {"type": "integer", "default": 0},
                "name": {"type": "string"},
            },
        },
    ),
    Tool(
        name="get_part",
        description="Get a single part / product by ID.",
        inputSchema={
            "type": "object",
            "properties": {"part_id": {"type": "integer"}},
            "required": ["part_id"],
        },
    ),
    # ---- Credit Notes ----
    Tool(
        name="list_credit_notes",
        description="List credit notes (Gutschriften) from sevDesk.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100},
                "offset": {"type": "integer", "default": 0},
                "status": {"type": "string"},
                "contact_id": {"type": "integer"},
            },
        },
    ),
    # ---- Accounting Types ----
    Tool(
        name="list_accounting_types",
        description="List accounting types (Buchungskonten/Kategorien) from sevDesk.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 500},
                "useClientAccountingChart": {"type": "integer", "default": 1},
            },
        },
    ),
    # ---- Unity (units of measure) ----
    Tool(
        name="list_unity",
        description="List available units of measure in sevDesk.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # ---- Tags ----
    Tool(
        name="list_tags",
        description="List all tags in sevDesk.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # ---- Export / Reports ----
    Tool(
        name="get_invoice_pdf",
        description="Get the PDF download URL for an invoice.",
        inputSchema={
            "type": "object",
            "properties": {"invoice_id": {"type": "integer"}},
            "required": ["invoice_id"],
        },
    ),
    # ---- Contact Addresses ----
    Tool(
        name="list_contact_addresses",
        description="List addresses for a contact.",
        inputSchema={
            "type": "object",
            "properties": {"contact_id": {"type": "integer"}},
            "required": ["contact_id"],
        },
    ),
    # ---- Communication Ways ----
    Tool(
        name="list_communication_ways",
        description="List communication ways (email, phone, etc.) for a contact.",
        inputSchema={
            "type": "object",
            "properties": {"contact_id": {"type": "integer"}},
            "required": ["contact_id"],
        },
    ),
    # ---- Bookkeeping ----
    Tool(
        name="book_invoice",
        description="Book / reconcile a payment for an invoice.",
        inputSchema={
            "type": "object",
            "properties": {
                "invoice_id": {"type": "integer"},
                "amount": {"type": "number"},
                "date": {"type": "string", "description": "ISO date"},
                "type": {"type": "string", "default": "N", "description": "N=normal"},
                "check_account_id": {"type": "integer"},
                "check_account_transaction_id": {"type": "integer"},
                "createFeed": {"type": "boolean", "default": True},
            },
            "required": ["invoice_id", "amount", "date"],
        },
    ),
    Tool(
        name="book_voucher",
        description="Book / reconcile a payment for a voucher.",
        inputSchema={
            "type": "object",
            "properties": {
                "voucher_id": {"type": "integer"},
                "amount": {"type": "number"},
                "date": {"type": "string"},
                "type": {"type": "string", "default": "N"},
                "check_account_id": {"type": "integer"},
                "check_account_transaction_id": {"type": "integer"},
                "createFeed": {"type": "boolean", "default": True},
            },
            "required": ["voucher_id", "amount", "date"],
        },
    ),
    # ---- Misc ----
    Tool(
        name="health_check",
        description="Check if the sevDesk MCP server is running and the API token is set.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="sevdesk_api_request",
        description="Make a raw GET request to any sevDesk API endpoint for advanced use.",
        inputSchema={
            "type": "object",
            "properties": {
                "endpoint": {"type": "string", "description": "API path, e.g. /Contact or /Invoice/100"},
                "params": {"type": "object", "description": "Query parameters"},
            },
            "required": ["endpoint"],
        },
    ),
]


@mcp_server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@mcp_server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    _audit_log({"event": "tool_call", "tool": name,
                "arg_keys": sorted(arguments.keys()) if isinstance(arguments, dict) else []})
    try:
        result = await _dispatch(name, arguments)
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except httpx.HTTPStatusError as e:
        msg = f"sevDesk API error {e.response.status_code}: {e.response.text}"
        _audit_log({"event": "tool_error", "tool": name, "err": f"http_{e.response.status_code}"})
        return [TextContent(type="text", text=msg)]
    except Exception as e:
        _audit_log({"event": "tool_error", "tool": name, "err": type(e).__name__})
        return [TextContent(type="text", text=f"Error: {type(e).__name__}: {e}")]


async def _dispatch(name: str, args: dict) -> object:  # noqa: PLR0912 PLR0915
    # ---- Contacts ----
    if name == "list_contacts":
        params = {}
        if "limit" in args:
            params["limit"] = args["limit"]
        if "offset" in args:
            params["offset"] = args["offset"]
        if "depth" in args:
            params["depth"] = args["depth"]
        if "customerNumber" in args:
            params["customerNumber"] = args["customerNumber"]
        return sevdesk_get("/Contact", params)

    if name == "get_contact":
        return sevdesk_get(f"/Contact/{args['contact_id']}")

    if name == "create_contact":
        body = {"objectName": "Contact", "mapAll": True}
        for k in ("name", "surename", "familyname", "description", "customerNumber", "vatNumber"):
            if k in args:
                body[k] = args[k]
        if "category_id" in args:
            body["category"] = {"id": args["category_id"], "objectName": "Category"}
        return sevdesk_post("/Contact", body)

    # ---- Invoices ----
    if name == "list_invoices":
        params = {}
        for k in ("limit", "offset", "status", "invoiceNumber", "startDate", "endDate"):
            if k in args:
                params[k] = args[k]
        if "contact_id" in args:
            params["contact[id]"] = args["contact_id"]
            params["contact[objectName]"] = "Contact"
        return sevdesk_get("/Invoice", params)

    if name == "get_invoice":
        return sevdesk_get(f"/Invoice/{args['invoice_id']}")

    if name == "get_invoice_positions":
        return sevdesk_get(f"/Invoice/{args['invoice_id']}/getPositions")

    if name == "create_invoice":
        body = {
            "objectName": "Invoice",
            "mapAll": True,
            "invoice": {
                "objectName": "Invoice",
                "invoiceDate": args["invoiceDate"],
                "status": "100",
                "invoiceType": args.get("invoiceType", "RE"),
                "currency": args.get("currency", "EUR"),
                "taxType": args.get("taxType", "default"),
                "contact": {"id": args["contact_id"], "objectName": "Contact"},
                "contactPerson": {"id": "0", "objectName": "SevUser"},
            },
            "invoicePosSave": [],
            "invoicePosDelete": None,
        }
        for k in ("header", "headText", "footText", "timeToPay", "discount", "address", "taxRate", "taxText"):
            if k in args:
                body["invoice"][k] = args[k]
        for pos in args.get("positions", []):
            body["invoicePosSave"].append({
                "objectName": "InvoicePos",
                "mapAll": True,
                "name": pos.get("name", ""),
                "quantity": pos.get("quantity", 1),
                "price": pos.get("price", 0),
                "taxRate": pos.get("taxRate", 19),
                "unity": {"id": pos.get("unity_id", 1), "objectName": "Unity"},
            })
        return sevdesk_post("/Invoice/Factory/saveInvoice", body)

    if name == "send_invoice_by_email":
        body = {
            "toEmail": args["toEmail"],
            "subject": args["subject"],
            "text": args["text"],
        }
        return sevdesk_post(f"/Invoice/{args['invoice_id']}/sendViaEmail", body)

    if name == "mark_invoice_sent":
        return sevdesk_put(f"/Invoice/{args['invoice_id']}/changeStatus", {"value": "200"})

    if name == "cancel_invoice":
        return sevdesk_post(f"/Invoice/{args['invoice_id']}/cancel", {})

    # ---- Orders ----
    if name == "list_orders":
        params = {}
        for k in ("limit", "offset", "status"):
            if k in args:
                params[k] = args[k]
        if "contact_id" in args:
            params["contact[id]"] = args["contact_id"]
            params["contact[objectName]"] = "Contact"
        return sevdesk_get("/Order", params)

    if name == "get_order":
        return sevdesk_get(f"/Order/{args['order_id']}")

    # ---- Vouchers ----
    if name == "list_vouchers":
        params = {}
        for k in ("limit", "offset", "status", "startDate", "endDate"):
            if k in args:
                params[k] = args[k]
        if "contact_id" in args:
            params["contact[id]"] = args["contact_id"]
            params["contact[objectName]"] = "Contact"
        return sevdesk_get("/Voucher", params)

    if name == "get_voucher":
        return sevdesk_get(f"/Voucher/{args['voucher_id']}")

    if name == "create_voucher":
        body = {
            "objectName": "Voucher",
            "mapAll": True,
            "voucher": {
                "objectName": "Voucher",
                "voucherDate": args["voucherDate"],
                "status": "50",
                "voucherType": args.get("voucherType", "VOU"),
                "creditDebit": args.get("creditDebit", "C"),
                "taxType": args.get("taxType", "default"),
                "currency": args.get("currency", "EUR"),
            },
            "voucherPosSave": [],
            "voucherPosDelete": None,
        }
        if "supplier_contact_id" in args:
            body["voucher"]["supplier"] = {"id": args["supplier_contact_id"], "objectName": "Contact"}
        if "description" in args:
            body["voucher"]["description"] = args["description"]
        for pos in args.get("positions", []):
            body["voucherPosSave"].append({
                "objectName": "VoucherPos",
                "mapAll": True,
                "accountingType": {"id": pos.get("accountingType_id", 1), "objectName": "AccountingType"},
                "taxRate": pos.get("taxRate", 19),
                "sum": pos.get("sum", 0),
                "net": pos.get("net", False),
            })
        return sevdesk_post("/Voucher/Factory/saveVoucher", body)

    # ---- Check Accounts / Transactions ----
    if name == "list_check_accounts":
        return sevdesk_get("/CheckAccount")

    if name == "list_transactions":
        params = {}
        for k in ("limit", "offset", "startDate", "endDate"):
            if k in args:
                params[k] = args[k]
        if "check_account_id" in args:
            params["checkAccount[id]"] = args["check_account_id"]
            params["checkAccount[objectName]"] = "CheckAccount"
        return sevdesk_get("/CheckAccountTransaction", params)

    if name == "get_transaction":
        return sevdesk_get(f"/CheckAccountTransaction/{args['transaction_id']}")

    # ---- Parts ----
    if name == "list_parts":
        params = {}
        for k in ("limit", "offset", "name"):
            if k in args:
                params[k] = args[k]
        return sevdesk_get("/Part", params)

    if name == "get_part":
        return sevdesk_get(f"/Part/{args['part_id']}")

    # ---- Credit Notes ----
    if name == "list_credit_notes":
        params = {}
        for k in ("limit", "offset", "status"):
            if k in args:
                params[k] = args[k]
        if "contact_id" in args:
            params["contact[id]"] = args["contact_id"]
            params["contact[objectName]"] = "Contact"
        return sevdesk_get("/CreditNote", params)

    # ---- Accounting Types ----
    if name == "list_accounting_types":
        params = {
            "limit": args.get("limit", 500),
            "useClientAccountingChart": args.get("useClientAccountingChart", 1),
        }
        return sevdesk_get("/AccountingType", params)

    # ---- Unity ----
    if name == "list_unity":
        return sevdesk_get("/Unity")

    # ---- Tags ----
    if name == "list_tags":
        return sevdesk_get("/Tag")

    # ---- Invoice PDF ----
    if name == "get_invoice_pdf":
        return sevdesk_get(f"/Invoice/{args['invoice_id']}/getPdf")

    # ---- Contact Addresses ----
    if name == "list_contact_addresses":
        return sevdesk_get("/ContactAddress", {"contact[id]": args["contact_id"], "contact[objectName]": "Contact"})

    # ---- Communication Ways ----
    if name == "list_communication_ways":
        return sevdesk_get("/CommunicationWay", {"contact[id]": args["contact_id"], "contact[objectName]": "Contact"})

    # ---- Booking ----
    if name == "book_invoice":
        body = {
            "amount": args["amount"],
            "date": args["date"],
            "type": args.get("type", "N"),
            "createFeed": args.get("createFeed", True),
        }
        if "check_account_id" in args:
            body["checkAccount"] = {"id": args["check_account_id"], "objectName": "CheckAccount"}
        if "check_account_transaction_id" in args:
            body["checkAccountTransaction"] = {
                "id": args["check_account_transaction_id"],
                "objectName": "CheckAccountTransaction",
            }
        return sevdesk_post(f"/Invoice/{args['invoice_id']}/bookAmount", body)

    if name == "book_voucher":
        body = {
            "amount": args["amount"],
            "date": args["date"],
            "type": args.get("type", "N"),
            "createFeed": args.get("createFeed", True),
        }
        if "check_account_id" in args:
            body["checkAccount"] = {"id": args["check_account_id"], "objectName": "CheckAccount"}
        if "check_account_transaction_id" in args:
            body["checkAccountTransaction"] = {
                "id": args["check_account_transaction_id"],
                "objectName": "CheckAccountTransaction",
            }
        return sevdesk_post(f"/Voucher/{args['voucher_id']}/bookAmount", body)

    # ---- Misc ----
    if name == "health_check":
        token_set = bool(SEVDESK_API_TOKEN)
        return {"status": "ok", "server": "sevdesk-mcp", "api_token_set": token_set}

    if name == "sevdesk_api_request":
        return sevdesk_get(args["endpoint"], args.get("params", {}))

    return {"error": f"Unknown tool: {name}"}


# ---- SSE / HTTP transport ----

sse_transport = SseServerTransport("/messages/")


async def check_auth(request: Request) -> tuple[str | None, JSONResponse | None]:
    """Return (auth_method, None) if OK, or (None, 401-response) if not."""
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""

    if MCP_AUTH_TOKEN and token == MCP_AUTH_TOKEN:
        return "bearer", None
    if token in _oauth_tokens:
        return "oauth", None

    return None, JSONResponse({"error": "Unauthorized"}, status_code=401)


async def oauth_authorize(request: Request):
    client_id = request.query_params.get("client_id", "")
    redirect_uri = request.query_params.get("redirect_uri", "")
    state = request.query_params.get("state", "")

    if client_id != OAUTH_CLIENT_ID or not redirect_uri:
        return JSONResponse({"error": "invalid_client"}, status_code=400)

    code = uuid_mod.uuid4().hex
    _oauth_codes[code] = time.time() + 60
    from starlette.responses import RedirectResponse
    from urllib.parse import quote
    return RedirectResponse(f"{redirect_uri}?code={code}&state={quote(state)}")


async def oauth_token(request: Request):
    body = (await request.body()).decode()
    from urllib.parse import parse_qs
    params = {k: v[0] for k, v in parse_qs(body).items()}

    if params.get("client_id") != OAUTH_CLIENT_ID or params.get("client_secret") != OAUTH_CLIENT_SECRET:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    code = params.get("code", "")
    if params.get("grant_type") == "authorization_code" and code:
        expiry = _oauth_codes.pop(code, 0)
        if time.time() > expiry:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

    access_token = uuid_mod.uuid4().hex
    _oauth_tokens.add(access_token)
    return JSONResponse({"access_token": access_token, "token_type": "Bearer"})


async def handle_sse(request: Request):
    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    auth_method, denied = await check_auth(request)
    if denied:
        _audit_log({"event": "sse_connect", "ip": client_ip, "result": "401_unauthorized"})
        return denied
    _audit_log({"event": "sse_connect", "ip": client_ip, "auth": auth_method})
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp_server.run(
            streams[0],
            streams[1],
            mcp_server.create_initialization_options(),
        )


async def health(request: Request):
    return JSONResponse({"status": "ok", "server": "sevdesk-mcp"})


routes = [
    Route("/health", health),
    Route("/sse", handle_sse),
    Mount("/messages/", app=sse_transport.handle_post_message),
]
if OAUTH_CLIENT_ID:
    routes.insert(0, Route("/authorize", oauth_authorize))
    routes.insert(1, Route("/token", oauth_token, methods=["POST"]))

app = Starlette(routes=routes)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8090"))
    uvicorn.run(app, host="0.0.0.0", port=port)
