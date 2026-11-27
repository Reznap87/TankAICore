"""Operator-configured MCP Streamable HTTP client (2025-11-25, JSON responses).

No arbitrary URLs, automatic tool calls, model data forwarding or server-driven actions.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError

from .connections import ConnectionError, _NoRedirect, _key

_VERSION = "2025-11-25"
_LOCK = threading.RLock()
_STATE = {}
_TTL = 900
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def _config(context):
    owner = os.getenv("TANKAI_MCP_OWNER_ID", "")
    workspace = os.getenv("TANKAI_MCP_WORKSPACE_ID", "")
    url = os.getenv("TANKAI_MCP_SERVER_URL", "")
    token = os.getenv("TANKAI_MCP_READ_TOKEN", "")
    allow = frozenset(filter(None, (x.strip() for x in os.getenv("TANKAI_MCP_ALLOWED_TOOLS", "").split(","))))
    if (context.role != "owner" or not owner or not workspace
            or context.user_id != owner or context.workspace_id != workspace
            or not allow or len(allow) > 32 or any(not _NAME.fullmatch(x) for x in allow)
            or not re.fullmatch(r"[A-Za-z0-9_.~+/=-]{20,16384}", token)):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if (parts.scheme != "https" or parts.username or parts.password or parts.query
                or parts.fragment or parts.port not in (None, 443)
                or not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host)
                or host.endswith((".local", ".localhost", ".internal", ".test"))
                or not parts.path.startswith("/") or len(url) > 1000
                or any(ord(c) < 33 or ord(c) > 126 for c in url)):
            return None
        try:
            ipaddress.ip_address(host)
            return None
        except ValueError:
            pass
    except ValueError:
        return None
    return url, token, allow


def _prune():
    now = time.monotonic()
    for key in list(_STATE):
        if _STATE[key]["expires"] <= now:
            del _STATE[key]


def status(context):
    with _LOCK:
        _prune()
        config = _config(context)
        if not config:
            return {"status": "not_configured"}
        row = _STATE.get(_key(context))
        return {"status": "connected" if row and row["config"] == config else "disconnected"}


def _rpc(config, method, params, session=None, notification=False):
    url, token, _ = config
    request_id = secrets.token_hex(12)
    payload = {"jsonrpc": "2.0", "method": method, "params": params}
    if not notification:
        payload["id"] = request_id
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream", "User-Agent": "TankAI-Connections"}
    if method != "initialize":
        headers["MCP-Protocol-Version"] = _VERSION
    if session:
        headers["Mcp-Session-Id"] = session
    try:
        body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("ascii")
        if len(body) > 8192:
            raise ValueError()
        with build_opener(ProxyHandler({}), _NoRedirect()).open(Request(url, data=body, headers=headers), timeout=10) as response:
            if notification:
                if response.status != 202:
                    raise ValueError()
                return {}, session
            if response.headers.get_content_type() != "application/json":
                raise ValueError()
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError()
            reply = json.loads(raw)
            sid = response.headers.get("Mcp-Session-Id")
        if sid and (not re.fullmatch(r"[!-~]{1,256}", sid) or (session and sid != session)):
            raise ValueError()
        if not isinstance(reply, dict) or reply.get("jsonrpc") != "2.0" or reply.get("id") != request_id or "error" in reply or not isinstance(reply.get("result"), dict):
            raise ValueError()
        return reply["result"], sid or session
    except (HTTPError, URLError, TimeoutError, UnicodeError, ValueError, RecursionError):
        raise ConnectionError("MCP-Server nicht erreichbar oder Antwort nicht unterstützt") from None


def _list(config, session):
    result, _ = _rpc(config, "tools/list", {}, session)
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise ConnectionError("MCP-Werkzeugliste ungültig")
    output = {}
    for item in tools[:100]:
        if not isinstance(item, dict):
            continue
        name, annotations = item.get("name"), item.get("annotations")
        if (not isinstance(name, str) or name not in config[2]
                or not isinstance(annotations, dict) or annotations.get("readOnlyHint") is not True):
            continue
        schema = item.get("inputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            continue
        description = item.get("description")
        output[name] = {"name": name, "description": description[:300] if isinstance(description, str) else ""}
    return output


def start(context):
    with _LOCK:
        _prune()
        config = _config(context)
        if not config:
            raise ConnectionError("MCP-Server nicht eingerichtet")
        key = _key(context)
        if key in _STATE or len(_STATE) >= 128:
            raise ConnectionError("Verbindung zuerst trennen oder später erneut versuchen")
        result, session = _rpc(config, "initialize", {
            "protocolVersion": _VERSION, "capabilities": {},
            "clientInfo": {"name": "TankAI", "version": "1"}
        })
        if result.get("protocolVersion") != _VERSION or not isinstance(result.get("capabilities"), dict) or not isinstance(result["capabilities"].get("tools"), dict):
            raise ConnectionError("MCP-Protokoll oder Werkzeugfähigkeit nicht unterstützt")
        _rpc(config, "notifications/initialized", {}, session, notification=True)
        tools = _list(config, session)
        _STATE[key] = {"config": config, "session": session, "tools": tools, "expires": time.monotonic() + _TTL}
        return {"status": "connected"}


def _connected(context):
    _prune()
    row = _STATE.get(_key(context))
    if not row or row["config"] != _config(context):
        raise ConnectionError("MCP zuerst verbinden")
    return row


def tools(context):
    with _LOCK:
        row = _connected(context)
        row["tools"] = _list(row["config"], row["session"])
        return {"status": "connected", "tools": list(row["tools"].values())}


def call(context, *, name, arguments):
    if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(arguments, dict):
        raise ConnectionError("Ungültige MCP-Parameter")
    with _LOCK:
        row = _connected(context)
        # Re-read the tool list before dispatch; no trust in client-supplied tool metadata.
        row["tools"] = _list(row["config"], row["session"])
        if name not in row["tools"]:
            raise ConnectionError("MCP-Werkzeug nicht freigegeben")
        result, _ = _rpc(row["config"], "tools/call", {"name": name, "arguments": arguments}, row["session"])
        content = result.get("content")
        if not isinstance(content, list):
            raise ConnectionError("MCP-Ergebnis nicht unterstützt")
        text = []
        for block in content[:20]:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                text.append(block["text"][:4000])
        return {"status": "connected", "output": "\n".join(text)[:8000], "is_error": result.get("isError") is True}


def disconnect(context, *, all_workspaces=False):
    with _LOCK:
        key = _key(context)
        for existing in list(_STATE):
            if existing == key or (all_workspaces and existing[:3] == key[:3]):
                del _STATE[existing]
        return status(context)
