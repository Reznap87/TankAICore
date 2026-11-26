"""Notion read-only page discovery; one explicitly configured owner/workspace."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError, URLError

from .connections import ConnectionError, _NoRedirect, _key

_LOCK = threading.RLock()
_STATE = {}
_TTL = 900
_VERSION = "2026-03-11"


def _token(context):
    owner = os.getenv("TANKAI_NOTION_OWNER_ID", "")
    workspace = os.getenv("TANKAI_NOTION_WORKSPACE_ID", "")
    token = os.getenv("TANKAI_NOTION_READ_TOKEN", "")
    if (context.role != "owner" or not owner or not workspace
            or context.user_id != owner or context.workspace_id != workspace
            or not re.fullmatch(r"[A-Za-z0-9_-]{20,512}", token)):
        return None
    return token


def _prune():
    now = time.monotonic()
    for key in list(_STATE):
        if _STATE[key] <= now:
            del _STATE[key]


def status(context):
    with _LOCK:
        _prune()
        if not _token(context):
            return {"status": "not_configured"}
        return {"status": "connected" if _key(context) in _STATE else "disconnected"}


def _search(token):
    req = Request("https://api.notion.com/v1/search",
                  data=b'{"page_size":30,"filter":{"property":"object","value":"page"}}',
                  headers={"Authorization": "Bearer " + token, "Notion-Version": _VERSION,
                           "Accept": "application/json", "Content-Type": "application/json",
                           "User-Agent": "TankAI-Connections"})
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(req, timeout=12) as response:
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError()
            result = json.loads(raw)
        if not isinstance(result, dict) or result.get("object") != "list" or not isinstance(result.get("results"), list):
            raise ValueError()
        return result
    except (HTTPError, URLError, TimeoutError, ValueError, UnicodeError):
        raise ConnectionError("Notion-Lesezugriff nicht bestätigt") from None


def _pages(context):
    token = _token(context)
    if not token:
        raise ConnectionError("Notion-Lesezugang nicht eingerichtet")
    result = _search(token)
    pages = []
    for row in result["results"][:30]:
        if not isinstance(row, dict) or row.get("object") != "page" or row.get("in_trash") or row.get("is_archived") or row.get("archived"):
            continue
        page_id = row.get("id")
        if not isinstance(page_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", page_id):
            continue
        properties = row.get("properties")
        title = ""
        if isinstance(properties, dict):
            for prop in properties.values():
                if not isinstance(prop, dict) or prop.get("type") != "title":
                    continue
                parts = prop.get("title")
                if isinstance(parts, list):
                    title = "".join(p["plain_text"][:300] for p in parts[:30]
                                    if isinstance(p, dict) and isinstance(p.get("plain_text"), str))[:300]
                break
        pages.append({"id": page_id, "title": title or "(Ohne Titel)"})
    return {"status": "connected", "pages": pages, "has_more": result.get("has_more") is True}


def start(context):
    with _LOCK:
        _prune()
        if len(_STATE) >= 128 and _key(context) not in _STATE:
            raise ConnectionError("Verbindungskapazität erreicht")
        _pages(context)
        _STATE[_key(context)] = time.monotonic() + _TTL
        return {"status": "connected"}


def pages(context):
    with _LOCK:
        if status(context)["status"] != "connected":
            raise ConnectionError("Notion zuerst verbinden")
        try:
            return _pages(context)
        except ConnectionError:
            _STATE.pop(_key(context), None)
            raise


def disconnect(context, *, all_workspaces=False):
    with _LOCK:
        key = _key(context)
        for existing in list(_STATE):
            if existing == key or (all_workspaces and existing[:3] == key[:3]):
                del _STATE[existing]
        return status(context)
