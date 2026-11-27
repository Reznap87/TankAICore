"""OneDrive metadata listing using a separately provisioned delegated Files.Read token."""
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
    owner = os.getenv("TANKAI_ONEDRIVE_OWNER_ID", "")
    workspace = os.getenv("TANKAI_ONEDRIVE_WORKSPACE_ID", "")
    token = os.getenv("TANKAI_ONEDRIVE_READ_TOKEN", "")
    if (context.role != "owner" or not owner or not workspace
            or context.user_id != owner or context.workspace_id != workspace
            or not re.fullmatch(r"[A-Za-z0-9_.~+/=-]{20,16384}", token)):
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
    req = Request("https://graph.microsoft.com/v1.0/me/drive/root/children?$select=name,file,folder&$top=30",
                  headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                           "User-Agent": "TankAI-Connections"})
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(req, timeout=12) as response:
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ValueError()
            result = json.loads(raw)
        if not isinstance(result, dict) or not isinstance(result.get("value"), list):
            raise ValueError()
        return result
    except (HTTPError, URLError, TimeoutError, ValueError, UnicodeError):
        raise ConnectionError("OneDrive-Lesezugriff nicht bestätigt") from None


def _files(context):
    token = _token(context)
    if not token:
        raise ConnectionError("OneDrive-Lesezugang nicht eingerichtet")
    result = _search(token)
    files = []
    for row in result["value"][:30]:
        if not isinstance(row, dict):
            continue
        kind = "folder" if isinstance(row.get("folder"), dict) else "file" if isinstance(row.get("file"), dict) else None
        name = row.get("name")
        if kind and isinstance(name, str) and name.strip():
            files.append({"name": name[:300], "kind": kind})
    return {"status": "connected", "files": files,
            "has_more": bool(result.get("@odata.nextLink")) or len(result["value"]) > 30}


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
            raise ConnectionError("OneDrive zuerst verbinden")
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
