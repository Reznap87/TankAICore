"""Read-only Google connectors; session scoped, no token persistence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from urllib.parse import urlencode

from .connections import ConnectionError, _key, _request as _http_request


def _request(*args, **kwargs):
    try:
        return _http_request(*args, **kwargs)
    except ConnectionError:
        raise ConnectionError("Google-Verbindung derzeit nicht verfügbar") from None

SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
_SCOPES = {"drive": SCOPE, "gmail": GMAIL_SCOPE, "calendar": CALENDAR_SCOPE}


def _service_key(context, service):
    if service not in _SCOPES:
        raise ConnectionError("Unbekannter Google-Dienst")
    return (*_key(context), service)
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


def status(context, service="drive"):
    with _LOCK:
        _prune()
        if not configured():
            return {"status": "not_configured"}
        row = _STATE.get(_service_key(context, service))
        return {"status": row["status"] if row else "disconnected"}


def start(context, service="drive"):
    if not configured():
        raise ConnectionError("Google OAuth noch nicht konfiguriert")
    with _LOCK:
        _prune()
        key = _service_key(context, service)
        if key in _STATE or len(_STATE) >= 128:
            raise ConnectionError("Verbindung zuerst trennen oder später erneut versuchen")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        _STATE[key] = {"status": "pending", "state": state, "verifier": verifier,
                       "expires": time.monotonic() + 600}
        query = urlencode({"client_id": os.environ["TANKAI_GOOGLE_CLIENT_ID"],
                           "redirect_uri": CALLBACK, "response_type": "code",
                           "scope": _SCOPES[service], "access_type": "online", "prompt": "consent",
                           "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
        return {"status": "pending", "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?" + query}


def finish(context, *, state, code):
    if not configured() or type(state) is not str or type(code) is not str or not 1 <= len(code) <= 2048:
        raise ConnectionError("Google-Freigabe ungültig")
    with _LOCK:
        _prune()
        if not state.isascii() or len(state) > 128:
            raise ConnectionError("Google-Freigabe ungültig")
        matches = [(service, _service_key(context, service)) for service in _SCOPES
                   if (candidate := _STATE.get(_service_key(context, service)))
                   and candidate["status"] == "pending" and hmac.compare_digest(candidate["state"], state)]
        if len(matches) != 1:
            raise ConnectionError("Google-Freigabe ungültig oder abgelaufen")
        service, key = matches[0]
        row = _STATE.pop(key)
        result = _request("https://oauth2.googleapis.com/token", fields={
            "client_id": os.environ["TANKAI_GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["TANKAI_GOOGLE_CLIENT_SECRET"],
            "code": code, "code_verifier": row["verifier"],
            "redirect_uri": CALLBACK, "grant_type": "authorization_code"
        })
        token, expires = result.get("access_token"), result.get("expires_in")
        if (not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_.~-]{10,4096}", token)
                or result.get("scope") != _SCOPES[service] or str(result.get("token_type", "")).lower() != "bearer"
                or type(expires) is not int or expires <= 0):
            raise ConnectionError("Google hat den lesenden Zugriff nicht bestätigt")
        _STATE[key] = {"status": "connected", "token": token,
                       "expires": time.monotonic() + min(expires, _TTL)}
        return {"status": "connected"}


def disconnect(context, *, all_workspaces=False, service="drive"):
    with _LOCK:
        key = _service_key(context, service)
        for existing in list(_STATE):
            if existing == key or (all_workspaces and existing[:3] == key[:3]):
                del _STATE[existing]
    return status(context, service)


def files(context):
    if not configured():
        raise ConnectionError("Google OAuth noch nicht konfiguriert")
    with _LOCK:
        _prune()
        row = _STATE.get(_service_key(context, "drive"))
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


def messages(context):
    """Explicitly retrieve at most ten inbox headers, never bodies or attachments."""
    if not configured():
        raise ConnectionError("Google OAuth noch nicht konfiguriert")
    with _LOCK:
        _prune()
        row = _STATE.get(_service_key(context, "gmail"))
        if not row or row["status"] != "connected":
            raise ConnectionError("Gmail zuerst verbinden")
        endpoint = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        query = urlencode({"labelIds": "INBOX", "maxResults": 10, "fields": "messages(id),nextPageToken"})
        result = _request(endpoint + "?" + query, token=row["token"])
        records = result.get("messages", [])
        if not isinstance(records, list):
            raise ConnectionError("Nachrichtenliste ungültig")
        output = []
        for item in records[:10]:
            message_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(message_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", message_id):
                continue
            query = urlencode([("format", "metadata"), ("metadataHeaders", "From"),
                               ("metadataHeaders", "Subject"), ("metadataHeaders", "Date"),
                               ("fields", "payload(headers(name,value))")])
            # Request only explicitly selected metadata fields.
            message = _request(endpoint + "/" + message_id + "?" + query, token=row["token"])
            payload = message.get("payload")
            headers = payload.get("headers", []) if isinstance(payload, dict) else []
            if not isinstance(headers, list):
                continue
            clean = {}
            for header in headers:
                if not isinstance(header, dict):
                    continue
                name, value = header.get("name"), header.get("value")
                if isinstance(name, str) and name.lower() in ("from", "subject", "date") and isinstance(value, str):
                    clean[name.lower()] = value[:500]
            output.append(clean)
        return {"status": "connected", "messages": output, "has_more": bool(result.get("nextPageToken"))}


def events(context):
    """Explicitly read up to 20 upcoming primary-calendar events in the next 30 days."""
    if not configured():
        raise ConnectionError("Google OAuth noch nicht konfiguriert")
    with _LOCK:
        _prune()
        row = _STATE.get(_service_key(context, "calendar"))
        if not row or row["status"] != "connected":
            raise ConnectionError("Google Kalender zuerst verbinden")
        now = datetime.now(timezone.utc)
        query = urlencode({
            "timeMin": now.isoformat(timespec="seconds"),
            "timeMax": (now + timedelta(days=30)).isoformat(timespec="seconds"),
            "singleEvents": "true", "orderBy": "startTime", "maxResults": 20,
            "showDeleted": "false",
            "fields": "nextPageToken,items(summary,start(date,dateTime),end(date,dateTime),status)",
        })
        result = _request("https://www.googleapis.com/calendar/v3/calendars/primary/events?" + query,
                          token=row["token"])
        records = result.get("items", [])
        if not isinstance(records, list):
            raise ConnectionError("Terminliste ungültig")
        output = []
        for item in records[:20]:
            if not isinstance(item, dict) or item.get("status") == "cancelled":
                continue
            start, end = item.get("start"), item.get("end")
            if not isinstance(start, dict) or not isinstance(end, dict):
                continue
            start_value = start.get("dateTime") or start.get("date")
            end_value = end.get("dateTime") or end.get("date")
            if not isinstance(start_value, str) or not isinstance(end_value, str):
                continue
            title = item.get("summary")
            output.append({"title": title[:300] if isinstance(title, str) else "(Ohne Titel)",
                           "start": start_value[:64], "end": end_value[:64],
                           "all_day": "dateTime" not in start})
        return {"status": "connected", "events": output, "has_more": bool(result.get("nextPageToken"))}
