"""Dropbox read-only root metadata listing for a configured owner and workspace."""
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


def _token(context):
    owner = os.getenv("TANKAI_DROPBOX_OWNER_ID", "")
    workspace = os.getenv("TANKAI_DROPBOX_WORKSPACE_ID", "")
    token = os.getenv("TANKAI_DROPBOX_READ_TOKEN", "")
    if (context.role != "owner" or not owner or not workspace
            or context.user_id != owner or context.workspace_id != workspace
            or not re.fullmatch(r"[A-Za-z0-9_.~-]{20,4096}", token)):
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
    req = Request("https://api.dropboxapi.com/2/files/list_folder",
                  data=b'{"path":"","recursive":false,"limit":30,"include_deleted":false}',
                  headers={"Authorization": "Bearer " + token, "Accept": "application/json", "Content-Type": "application/json",
                           "User-Agent": "TankAI-Connections"})
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(req, timeout=12) as response:
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError()
            result = json.loads(raw)
        if not isinstance(result, dict) or not isinstance(result.get("entries"), list):
            raise ValueError()
        return result
    except (HTTPError, URLError, TimeoutError, ValueError, UnicodeError):
        raise ConnectionError("Dropbox-Lesezugriff nicht bestätigt") from None


def _files(context):
    token = _token(context)
    if not token:
        raise ConnectionError("Dropbox-Lesezugang nicht eingerichtet")
    result = _search(token)
    files = []
    for row in result["entries"][:30]:
        if not isinstance(row, dict) or row.get(".tag") not in ("file", "folder"):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        files.append({"name": name[:300], "kind": row[".tag"]})
    return {"status": "connected", "files": files, "has_more": result.get("has_more") is True}


def start(context):
    with _LOCK:
        _prune()
        if len(_STATE) >= 128 and _key(context) not in _STATE:
            raise ConnectionError("Verbindungskapazität erreicht")
        _files(context)
        _STATE[_key(context)] = time.monotonic() + _TTL
        return {"status": "connected"}


def files(context):
    with _LOCK:
        if status(context)["status"] != "connected":
            raise ConnectionError("Dropbox zuerst verbinden")
        try:
            return _files(context)
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
