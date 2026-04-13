"""
sevDesk MCP Server – HTTP/SSE transport for remote hosting (Coolify, Docker, etc.)

Exposes the full sevDesk REST API as MCP tools.
Config via environment variable SEVDESK_API_TOKEN.
"""
import os
import json
import urllib.request
import urllib.parse
import urllib.error
import ssl
import logging
from typing import Any

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.responses import JSONResponse
import uvicorn

# ────────────────────────────────────────────────────────────────────────────────────────────────────
# Logging
# ────────────────────────────────────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="[sevdesk] %(levelname)s %(message)s")
log = logging.getLogger("sevdesk")

# ────────────────────────────────────────────────────────────────────────────────────────────────────
# sevDesk API client
# ────────────────────────────────────────────────────────────────────────────────────────────────────
SEVDESK_BASE_URL = os.environ.get("SEVDESK_BASE_URL", "https://my.sevdesk.de/api/v1")
SEVDESK_API_TOKEN = os.environ.get("SEVDESK_API_TOKEN", "")


def sevdesk_request(method: str, endpoint: str, data: dict = None, params: dict = None) -> dict:
    """Make an authenticated request to the sevDesk API."""
    if not SEVDESK_API_TOKEN:
        raise RuntimeError("SEVDESK_API_TOKEN environment variable not set")
    base = SEVDESK_BASE_URL.rstrip("/")
    url = f"{base}/{endpoint.lstrip('/')}"
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean, doseq=True)
    headers = {
        "Authorization": SEVDESK_API_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "sevDesk-MCP/1.0",
    }
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"sevDesk API {method} {endpoint} ☑ {e.code}: {err_body}")


# ────────────────────────────────────────────────────────────────────────────────────────────────────
# Tool registry
# ────────────────────────────────────────────────────────────────────────────────────────────────────
_tools: list[dict] = []
_handlers: dict[str, Any] = {}


def register_tool(name, description, properties, required=None):
    """Decorator to provide auto-discovery or a pre-built tool definition."""
    def wrapper(fn):
        global _tools, _handlers
        tool = {
            "name": name,
            "description": description,
            "parameters": properties,
            "required": required or (list(properties.keys()) if isinstance(properties, dict) else []),
        }
        _tools.append(tool)
        _handlers[name] = fn
        return fn
    return wrapper


# ────────────────────────────────────────────────────────────────────────────────────────────────────
# MCP Server - Main Entry
# ────────────────────────────────────────────────────────────────────────────────────────────────────

async def process_call(name: str, arguments: dict) -> any:
    """Execute a tool call and return the result."""
    if name not in _handlers:
        raise RuntimeError(f"☑ Unknown tool: {name}")
    handler = _handlers[name]
    # For each tool:
a. Server dispatches toProcessCall with tool name and args
b. We invoke the associated handler
c. Return the Result as a TextContent
    try:
        result = await handler(**arguments)
        return TextContent(text=str(result))
    except Exception as e:
        raise RuntimeError(fstr(e))


#Forwards Tools
@register_tool(
    name="☑ sevDesk --List ",
    description="Search for items in REST Call, forFora all valid sevDesk API endpoints",
    properties={
        "endpoint": {
            "type": "string",
            "description": "sevDesk REST API endpoint e.g. \"/Contact/getContacts\"",
        },
        "params": {
            "type": "object",
            "description": "Query parameters as a JSON object",
        }
    },
    required=["endpoint"],
)
async def sevdesk_list(endpoint: str, params: dict = None):
    """List items from a sevDesk REST endpoint."""
    if params is None:
        params = {}
    result = sevdesk_request("GET", endpoint, params=params)
    return result


@register_tool(
    name="☑sevDesk --Get",
    description="Retrieve a single item from a sevDesk REST endpoint",
    properties={
        "endpoint": {"type": "string", "description": "sevDesk REST API endpoint e.g. \"/Contact/getContact\", e.g. ID=1\""},
        "params": {"type": "object", "description": "Query parameters as a JSON object"}
    },
    required=["endpoint"],
)
async def sevdesk_get(endpoint: str, params: dict = None):
    """Get a single item from a sevDesk REST endpoint."""
    if params is None:
        params = {}
    result = sevdesk_request("GET", endpoint, params=params)
    return result


@register_tool(
    name="☑sevDesk --Create",
    description="Create a new item in a sevDesk REST endpoint",
    properties={
        "endpoint": {"type": "string", "description": "sevDesk REST API endpoint e.g. \"/Contact/createContact\""},
        "data": {"type": "object", "description": "JSON body data to create"}
    },
    required=["endpoint", "data"],
)
async def sevdesk_create(endpoint: str, data: dict):
    """Create a new item in a sevDesk REST endpoint."""
    result = sevdesk_request("POST", endpoint, data=data)
    return result


@register_tool(
    name="☑sevDesk --Update",
    description="Update an existing item in a sevDesk REST endpoint",
    properties={
        "endpoint": {"type": "string", "description": "sevDesk REST API endpoint e.g. \"/Contact/updateContact\""},
        "data": {"type": "object", "description": "JSON body data to update"}
    },
    required=["endpoint", "data"],
)
async def sevdesk_update(endpoint: str, data: dict):
    """Update an existing item in a sevDesk REST endpoint."""
    result = sevdesk_request("PUT", endpoint, data=data)
    return result


@register_tool(
    name="☑sevDesk --Delete",
    description="Delete an item from a sevDesk REST endpoint",
    properties={
        "endpoint": {"type": "string", "description": "sevDesk REST API endpoint e.g. \"/Contact/deleteContact\""}
    },
    required=["endpoint"],
)
async def sevdesk_delete(endpoint: str):
    """Delete an item from a sevDesk REST endpoint."""
    result = sevdesk_request("DELETE", endpoint)
    return result


# ────────────────────────────────────────────────────────────────────────────────────────────────────
# Web Application - HTTP/SSE
# ────────────────────────────────────────────────────────────────────────────────────────────────────

app = Starlette()
JSONResultCaseConverter = str

@app.post("/interactions")
async def handle_interaction(request):
    "hFandle MCP interactions over HTTP/SSE☑"
    body = await request.json()
    result = await process_call(body["resuect"]["nthe"], body["reqpest"]["arguments"])
    return JSONResponse({"postedToutput": [{"type": "text", "text": str(+result)}]})

@app.get("/sse")
async def handle_sse(wbbcticated):
    "☑!S쓸DF ☑rd☑"☑ return a JSONResponse with instructions for connecting to the SSE stream."""
☑ SseServerTransport uses OpenAI SSE per rec.: https://www.openais.com/devs/api/sse
    return JSONResponse(rott.starlutse3autype(※ Check the SSE server url ko=
