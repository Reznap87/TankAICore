"""Short-lived signed receipts: no server-side storage of temporary chat text."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

_KEY = secrets.token_bytes(32)
TTL_SECONDS = 600
MAX_RECEIPT_LENGTH = 190_000


def _scope(context):
    return [context.session_id, context.user_id, context.tenant_id, context.workspace_id]


def _signature(context, token: str) -> str:
    scope = json.dumps(_scope(context), separators=(",", ":")).encode("utf-8")
    return hmac.new(_KEY, scope + b"\x00" + token.encode("ascii"), hashlib.sha256).hexdigest()


def issue_receipt(context, payload: dict) -> str:
    raw = json.dumps([int(time.time()) + TTL_SECONDS, payload],
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    token = base64.urlsafe_b64encode(raw).decode("ascii")
    if len(token) + 65 > MAX_RECEIPT_LENGTH:
        raise ValueError("Schnellantwort zu lang")
    return token + "." + _signature(context, token)


def read_receipt(context, receipt: str) -> dict:
    if type(receipt) is not str or len(receipt) > MAX_RECEIPT_LENGTH:
        raise ValueError("Ungültige Schnellantwort")
    try:
        token, signature = receipt.split(".")
        expected = hmac.new(_KEY, token.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        expires, payload = json.loads(base64.urlsafe_b64decode(token))
        if (type(expires) is not int or expires <= time.time()
                or type(payload) is not dict or payload.get("workspace_id") != context.workspace_id
                or payload.get("answer_mode") != "direct" or payload.get("reply_to") is not None
                or payload.get("model_identity_verified") is not True):
            raise ValueError()
        return payload
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("Schnellantwort ungültig oder abgelaufen") from None
