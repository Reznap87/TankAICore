"""Provider-neutral usage records. Missing data never implies zero cost."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any


@dataclass(frozen=True)
class CompletionResult:
    text: str
    reported_model: str | None = None
    tokens: dict[str, int] = field(default_factory=dict)


def safe_label(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,159}", value):
        return None
    if value.lower().startswith(("sk-", "bearer", "http:", "https:")):
        return None
    return value


def field_value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def safe_tokens(tokens: dict) -> dict[str, int]:
    allowed = {"input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
               "reasoning_output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"}
    if not isinstance(tokens, dict):
        return {}
    return {key: value for key, value in tokens.items() if key in allowed
            and type(value) is int and 0 <= value <= 10**12}


def token_counts(usage: Any, *, anthropic: bool = False) -> dict[str, int]:
    paths = ({"input_tokens": ("input_tokens",), "output_tokens": ("output_tokens",),
              "cache_read_input_tokens": ("cache_read_input_tokens",),
              "cache_creation_input_tokens": ("cache_creation_input_tokens",)} if anthropic else
             {"input_tokens": ("prompt_tokens",), "output_tokens": ("completion_tokens",),
              "total_tokens": ("total_tokens",),
              "cached_input_tokens": ("prompt_tokens_details", "cached_tokens"),
              "reasoning_output_tokens": ("completion_tokens_details", "reasoning_tokens")})
    result = {}
    for name, path in paths.items():
        value = usage
        for key in path:
            value = field_value(value, key)
        if type(value) is int and 0 <= value <= 10**12:
            result[name] = value
    return result


def usage_summary(attempts: list[dict]) -> dict:
    totals: dict[str, int] = {}
    missing = 0
    for attempt in attempts:
        tokens = attempt.get("tokens", {})
        if not {"input_tokens", "output_tokens"}.issubset(tokens):
            missing += 1
        for key, value in tokens.items():
            totals[key] = totals.get(key, 0) + value
    return {"adapter_attempts": len(attempts), "attempts_with_incomplete_usage": missing,
            "reported_tokens": totals, "cost_status": "not_priced",
            "upstream_attempts_confirmed": False, "attempts": attempts}


class UsageLedger:
    """Workspace-local accounting before/after each SDK attempt, without text.

    Started calls surviving a crash remain unresolved. Adapter calls and reported
    tokens do not establish the cost or number of internal upstream attempts.
    """
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.execute("""CREATE TABLE IF NOT EXISTS usage_attempts (
            attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL)""")
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_run ON usage_attempts(run_id)")
        self._conn.commit()

    def record(self, event: dict) -> None:
        allowed = {key: event[key] for key in (
            "attempt_id", "run_id", "adapter_attempt", "provider", "requested_model", "reported_model",
            "status", "tokens", "simulation", "error_type", "duration_ms", "created_at",
        ) if key in event}
        allowed["tokens"] = safe_tokens(allowed.get("tokens", {}))
        for label in ("provider", "requested_model", "reported_model", "error_type"):
            if label in allowed:
                allowed[label] = safe_label(allowed[label])
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO usage_attempts VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(attempt_id) DO UPDATE SET status=excluded.status, payload=excluded.payload",
                (allowed["attempt_id"], allowed["run_id"], allowed["status"],
                 allowed["created_at"], json.dumps(allowed, allow_nan=False)),
            )

    def summary(self, run_id: str) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM usage_attempts WHERE run_id=? ORDER BY created_at, attempt_id", (run_id,),
            ).fetchall()
        return usage_summary([json.loads(row[0]) for row in rows])

    def close(self):
        with self._lock:
            self._conn.close()
