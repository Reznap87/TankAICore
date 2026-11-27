"""Explicit read-only Cloudflare connection for one configured TankAI owner."""
from __future__ import annotations

import os
import re
import threading
import time

from .connections import ConnectionError, _key, _request

_LOCK = threading.RLock()
_STATE = {}
_TTL = 900


def _config(context):
    if context.role != "owner" or not os.getenv("TANKAI_CF_CONNECTOR_OWNER_ID"):
        return None
    if context.user_id != os.getenv("TANKAI_CF_CONNECTOR_OWNER_ID"):
        return None
    account = os.getenv("TANKAI_CF_CONNECTOR_ACCOUNT_ID", "")
    token = os.getenv("TANKAI_CF_CONNECTOR_READ_TOKEN", "")
    if not re.fullmatch(r"[a-f0-9]{32}", account) or not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", token):
        return None
    return account, token


def _prune():
    now = time.monotonic()
    for key in list(_STATE):
        if _STATE[key] <= now:
            del _STATE[key]


def status(context):
    with _LOCK:
        _prune()
        if not _config(context):
            return {"status": "not_configured"}
        return {"status": "connected" if _key(context) in _STATE else "disconnected"}


def _workers(context):
    config = _config(context)
    if not config:
        raise ConnectionError("Cloudflare-Lesezugang nicht eingerichtet")
    account, token = config
    try:
        result = _request("https://api.cloudflare.com/client/v4/accounts/" + account + "/workers/scripts", token=token)
    except ConnectionError:
        raise ConnectionError("Cloudflare-Lesezugriff fehlgeschlagen") from None
    if result.get("success") is not True or not isinstance(result.get("result"), list):
        raise ConnectionError("Cloudflare-Lesezugriff nicht bestätigt")
    records = result["result"]
    workers = []
    for row in records[:100]:
        if not isinstance(row, dict):
            continue
        name = row.get("id")
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", name):
            workers.append({"name": name, "modified_on": str(row.get("modified_on") or "")[:64]})
    return {"status": "connected", "workers": workers, "truncated": len(records) > 100}


def start(context):
    with _LOCK:
        _prune()
        if len(_STATE) >= 128 and _key(context) not in _STATE:
            raise ConnectionError("Verbindungskapazität erreicht")
        _workers(context)
        _STATE[_key(context)] = time.monotonic() + _TTL
        return {"status": "connected"}


def workers(context):
    with _LOCK:
        if status(context)["status"] != "connected":
            raise ConnectionError("Cloudflare zuerst verbinden")
        try:
            return _workers(context)
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
