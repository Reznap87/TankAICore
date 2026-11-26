"""Explicit GitHub device authorization; session-local, read-only account connection."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.error import HTTPError, URLError

_LOCK = threading.RLock()
_STATE = {}
_LIMIT = 128
_TTL = 900


class ConnectionError(ValueError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def configured():
    return bool(re.fullmatch(r"[A-Za-z0-9_]{10,100}", os.getenv("TANKAI_GITHUB_CLIENT_ID", "")))


def _key(context):
    return (context.session_id, context.user_id, context.tenant_id, context.workspace_id)


def _prune():
    now = time.monotonic()
    for key in list(_STATE):
        if _STATE[key]["expires"] <= now:
            del _STATE[key]


def _request(url, *, fields=None, token=None):
    headers = {"Accept": "application/json", "User-Agent": "TankAI-Connections"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = urlencode(fields).encode("ascii") if fields is not None else None
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(
            Request(url, data=body, headers=headers), timeout=12
        ) as response:
            raw = response.read(262145)
            if len(raw) > 262144:
                raise ConnectionError("GitHub-Antwort zu groß")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ConnectionError("GitHub-Antwort ungültig")
            return result
    except (HTTPError, URLError, TimeoutError, UnicodeError, ValueError):
        raise ConnectionError("GitHub-Verbindung derzeit nicht verfügbar") from None


def status(context):
    with _LOCK:
        _prune()
        row = _STATE.get(_key(context))
        if not configured():
            return {"status": "not_configured"}
        if not row:
            return {"status": "disconnected"}
        return {"status": row["status"], **({"login": row["login"]} if row["status"] == "connected" else {})}


def disconnect(context, *, all_workspaces=False):
    with _LOCK:
        key = _key(context)
        for existing in list(_STATE):
            if existing == key or (all_workspaces and existing[:3] == key[:3]):
                del _STATE[existing]
    return status(context)


def start(context):
    if not configured():
        raise ConnectionError("GitHub-OAuth-App noch nicht konfiguriert")
    with _LOCK:
        _prune()
        key = _key(context)
        if key in _STATE:
            raise ConnectionError("Bestehende Verbindung zuerst trennen")
        if len(_STATE) >= _LIMIT:
            raise ConnectionError("Verbindungskapazität erreicht")
        result = _request("https://github.com/login/device/code", fields={
            "client_id": os.environ["TANKAI_GITHUB_CLIENT_ID"], "scope": ""
        })
        device, user = result.get("device_code"), result.get("user_code")
        interval, expires = result.get("interval", 5), result.get("expires_in", 900)
        if (not isinstance(device, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", device)
                or not isinstance(user, str) or not re.fullmatch(r"[A-Z0-9-]{4,20}", user)
                or type(interval) is not int or not 1 <= interval <= 60
                or type(expires) is not int or not 1 <= expires <= 1800):
            raise ConnectionError("GitHub-Gerätecode ungültig")
        now = time.monotonic()
        _STATE[key] = {"status": "pending", "device": device,
                       "interval": interval, "next": now + interval,
                       "expires": now + min(expires, _TTL)}
        return {"status": "pending", "user_code": user, "verification_uri": "https://github.com/login/device",
                "retry_after": interval}


def finish(context):
    if not configured():
        raise ConnectionError("GitHub-OAuth-App noch nicht konfiguriert")
    with _LOCK:
        _prune()
        key = _key(context)
        row = _STATE.get(key)
        if not row or row["status"] != "pending":
            raise ConnectionError("Keine offene GitHub-Freigabe")
        now = time.monotonic()
        if now < row["next"]:
            return {"status": "pending", "retry_after": max(1, int(row["next"] - now) + 1)}
        row["next"] = now + row["interval"]
        result = _request("https://github.com/login/oauth/access_token", fields={
            "client_id": os.environ["TANKAI_GITHUB_CLIENT_ID"], "device_code": row["device"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"
        })
        error = result.get("error")
        if error in ("authorization_pending", "slow_down"):
            if error == "slow_down":
                row["interval"] += 5
                row["next"] = time.monotonic() + row["interval"]
            return {"status": "pending", "retry_after": row["interval"]}
        token = result.get("access_token")
        if error or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_]{10,512}", token):
            _STATE.pop(key, None)
            raise ConnectionError("GitHub-Freigabe fehlgeschlagen oder abgelaufen")
        try:
            profile = _request("https://api.github.com/user", token=token)
            login = profile.get("login")
            if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", login):
                raise ConnectionError("GitHub-Konto nicht bestätigt")
        except ConnectionError:
            _STATE.pop(key, None)
            raise
        # No token persistence: this first integration proves account identity only.
        _STATE[key] = {"status": "connected", "login": login, "expires": time.monotonic() + _TTL}
        return {"status": "connected", "login": login}
