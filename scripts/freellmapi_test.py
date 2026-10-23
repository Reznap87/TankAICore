#!/usr/bin/env python3
"""Private FreeLLMAPI setup and explicit, bounded connectivity check.

No upstream account is created and no inference runs unless `smoke` is selected.
The gateway key is read with getpass, never accepted as a command-line argument.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".tankai" / "freellmapi.env"
BASE_URL = "http://127.0.0.1:3001"
ENCRYPTION = "FREELLMAPI_ENCRYPTION_KEY"
UNIFIED = "FREELLMAPI_UNIFIED_KEY"


def load_config(path: Path = CONFIG) -> dict[str, str]:
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Refusing a symlink configuration path")
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
        raise ValueError("Configuration must be a private regular file (mode 0600)")
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name not in {ENCRYPTION, UNIFIED} or name in result:
            raise ValueError("Invalid configuration format")
        result[name] = value
    if not re.fullmatch(r"[a-f0-9]{64}", result.get(ENCRYPTION, "")):
        raise ValueError("Missing or invalid encryption key; do not regenerate an existing key")
    return result


def write_config(values: dict[str, str], path: Path = CONFIG, *, create: bool = False) -> None:
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("Refusing a symlink configuration path")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = "".join(f"{name}={values[name]}\n" for name in (ENCRYPTION, UNIFIED) if name in values)
    if create:
        # O_EXCL preserves an existing encryption key, including concurrent init.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        return
    fd, temporary = tempfile.mkstemp(prefix=".freellmapi-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def initialize(path: Path = CONFIG) -> bool:
    if path.exists() or path.is_symlink():
        load_config(path)
        return False
    write_config({ENCRYPTION: secrets.token_hex(32)}, path, create=True)
    return True


def connect(key: str, path: Path = CONFIG) -> None:
    # Restrict to a dotenv-safe token; never interpolate shell syntax or URLs.
    if not re.fullmatch(r"[A-Za-z0-9_.-]{16,512}", key):
        raise ValueError("Expected the unified gateway key, not a provider key or URL")
    config = load_config(path)
    config[UNIFIED] = key
    write_config(config, path)


def request_json(path: str, key: str, payload: dict | None = None) -> tuple[dict, str]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        BASE_URL + path,
        data=data,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urlopen(request, timeout=60) as response:
        result = json.loads(response.read(2_000_000))
        return result, response.headers.get("X-Routed-Via", "")


def check(*, smoke: bool = False, path: Path = CONFIG) -> dict:
    key = load_config(path).get(UNIFIED, "")
    if not key:
        raise ValueError("No unified key stored; complete the gateway Keys page, then run connect")
    models, _ = request_json("/v1/models?available=true", key)
    # Virtual router entries are always listed, even when there are no keys.
    ready = [m for m in models.get("data", []) if m.get("execution_status") == "ready"
             and m.get("id") not in {"auto", "fusion"}
             and not str(m.get("id", "")).startswith("auto:")]
    result = {"gateway_authenticated": True, "ready_model_endpoints": len(ready),
              "inference_requested": False, "production_ready": False}
    if not ready:
        raise ValueError("Gateway reachable but no ready model endpoint; add an eligible provider key")
    if smoke:
        response, routed_via = request_json("/v1/chat/completions", key, {
            "model": "auto", "messages": [{"role": "user", "content": "Antworte nur mit TANKAI_OK."}],
            "max_tokens": 32, "temperature": 0, "stream": False,
        })
        choices = response.get("choices") or []
        content = choices[0].get("message", {}).get("content") if choices else None
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Gateway returned no textual model response")
        result.update(inference_requested=True, inference_response_received=True,
                      expected_reply=content.strip() == "TANKAI_OK",
                      routed_via=routed_via, model=response.get("model"))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "connect", "check", "smoke"))
    args = parser.parse_args()
    try:
        if args.action == "init":
            created = initialize()
            print("Private configuration created." if created else "Existing private configuration preserved.")
        elif args.action == "connect":
            connect(getpass.getpass("FreeLLMAPI unified key (hidden): "))
            print("Unified gateway key saved; encryption key preserved.")
        else:
            result = check(smoke=args.action == "smoke")
            print(json.dumps(result, ensure_ascii=False))
            if result.get("expected_reply") is False:
                return 1
    except HTTPError as exc:
        print(f"Gateway HTTP {exc.code}; no credentials or response body printed.")
        return 1
    except (ValueError, OSError, URLError, EOFError) as exc:
        # Do not print transport URLs, upstream bodies, or submitted key values.
        message = str(exc) if isinstance(exc, (ValueError, EOFError)) else "Local gateway or configuration unavailable"
        print(message)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
