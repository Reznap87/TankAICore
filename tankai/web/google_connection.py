"""Google Drive metadata-only OAuth connection; session scoped, no token persistence."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from urllib.parse import urlencode

from .connections import ConnectionError, _key, _request

SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
CALLBACK = "https://tankaicore.com/api/connections/google/callback"
_LOCK = threading.RLock()
_STATE = {}
_TTL = 900


def configured():
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]{10,250}\.apps\.googleusercontent\.com",
                             os.getenv("TANKAI_GOOGLE_CLIENT_ID", ""))
                and os.getenv("TANKAI_GOOGLE_CLIENT_SECRET", "").strip())


def _prune():
    now = time.monotonic()
    for key in list(_STATE):
        if _STATE[key]["expires"] <= now:
            del _STATE[key]


def status(context):
    with _LOCK:
        _prune()
        if not configured():
            return {"status": "not_configured"}
        row = _STATE.get(_key(context))
        return {"status": row["status"] if row else "disconnected"}


def start(context):
    if not configured():
        raise ConnectionError("Google OAuth noch nicht konfiguriert")
    with _LOCK:
        _prune()
        key = _key(context)
        if key in _STATE or len(_STATE) >= 128:
            raise ConnectionError("Verbindung zuerst trennen oder später erneut versuchen")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        _STATE[key] = {"status": "pending", "state": state, "verifier": verifier,
                       "expires": time.monotonic() + 600}
        query = urlencode({"client_id": os.environ["TANKAI_GOOGLE_CLIENT_ID"],
                           "redirect_uri": CALLBACK, "response_type": "code",
                           "scope": SCOPE, "access_type": "online", "prompt": "consent",
                           "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        return {"status": "pending", "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?" + query}


def finish(context, *, state, code):
    if not configured() or type(state) is not str or type(code) is not str or not 1 <= len(code) <= 2048:
        raise ConnectionError("Google-Freigabe ungültig")
    with _LOCK:
        _prune()
        key = _key(context)
        row = _STATE.get(key)
        if not row or row["status"] != "pending" or not state.isascii() or not hmac.compare_digest(row["state"], state):
            raise ConnectionError("Google-Freigabe ungültig oder abgelaufen")
        del _STATE[key]
        result = _request("https://oauth2.googleapis.com/token", fields={
            "client_id": os.environ["TANKAI_GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["TANKAI_GOOGLE_CLIENT_SECRET"],
            "code": code, "code_verifier": row["verifier"],
            "redirect_uri": CALLBACK, "grant_type": "authorization_code"
        })
        token, expires = result.get("access_token"), result.get("expires_in")
        if (not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.~-]{10,4096}", token)
                or result.get("scope") != SCOPE or str(result.get("token_type", "")).lower() != "bearer"
                or type(expires) is not int or expires <= 0):
            raise ConnectionError("Google hat den lesenden Zugriff nicht bestätigt")
        _STATE[key] = {"status": "connected", "token": token,
                       "expires": time.monotonic() + min(expires, _TTL)}
        return {"status": "connected"}


def disconnect(context, *, all_workspaces=False):
    with _LOCK:
        key = _key(context)
        for existing in list(_STATE):
            if existing == key or (all_workspaces and existing[:3] == key[:3]):
                del _STATE[existing]
    return status(context)


def files(context):
    if not configured():
        raise ConnectionError("Google OAuth noch nicht konfiguriert")
    with _LOCK:
        _prune()
        row = _STATE.get(_key(context))
        if not row or row["status"] != "connected":
            raise ConnectionError("Google Drive zuerst verbinden")
        query = urlencode({"q": "trashed = false", "pageSize": 30,
                           "fields": "nextPageToken,files(id,name,mimeType)",
                           "orderBy": "modifiedTime desc"})
        result = _request("https://www.googleapis.com/drive/v3/files?" + query, token=row["token"])
        records = result.get("files")
        if not isinstance(records, list):
            raise ConnectionError("Dateiliste ungültig")
        output = []
        for item in records[:30]:
            if not isinstance(item, dict):
                continue
            file_id, name = item.get("id"), item.get("name")
            if isinstance(file_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", file_id) and isinstance(name, str):
                output.append({"name": name[:300], "id": file_id})
        return {"status": "connected", "files": output, "has_more": bool(result.get("nextPageToken"))}
