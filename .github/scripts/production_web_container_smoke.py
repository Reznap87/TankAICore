#!/usr/bin/env python3
"""Local production-web container smoke test; never deploys."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

DOCKER = shutil.which("docker") or "docker"
MANAGED_LABEL = "com.tankai.ci.production-web-smoke"
SCOPE_LABEL = MANAGED_LABEL + ".scope"
HTTP_LIMIT = 4096
READY_TIMEOUT = 120.0
ENVIRONMENT = (
    ("TANKAI_HOST", "0.0.0.0"),
    ("TANKAI_PORT", "8765"),
    ("TANKAI_AUTH_MODE", "session"),
    ("TANKAI_DATA_ROOT", "/app/data"),
    ("TANKAI_AUTH_DB", "/app/data/auth.db"),
    ("TANKAI_SESSION_HOURS", "12"),
    ("TANKAI_COOKIE_SECURE", "1"),
    ("TANKAI_ALLOW_REGISTRATION", "0"),
    ("TANKAI_DEV_QUEUE_ENABLED", "0"),
    ("TANKAI_LLM", "mock"),
    ("TANKAI_EMBEDDER", "hashing"),
)
HEALTH_KEYS = {
    "ok",
    "version",
    "auth_mode",
    "auth_required",
    "registration_enabled",
    "production_ready",
    "development_queue_enabled",
}
HEALTH_BASE = {
    "Test": [
        "CMD-SHELL",
        (
            "python -c \"import json,urllib.request; "
            "d=json.load(urllib.request.urlopen("
            "'http://127.0.0.1:8765/api/health',timeout=3)); "
            "assert d['ok']\" || exit 1"
        ),
    ],
    "Interval": 30_000_000_000,
    "Timeout": 5_000_000_000,
    "StartPeriod": 20_000_000_000,
    "Retries": 3,
}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
CSP = re.compile(
    r"default-src 'self'; base-uri 'none'; object-src 'none'; "
    r"script-src 'nonce-[A-Za-z0-9_-]{24}'; "
    r"style-src 'self' 'unsafe-inline'; connect-src 'self'; "
    r"frame-ancestors 'none'; form-action 'self'\Z"
)
OPENER = build_opener(ProxyHandler({}))
REDACTIONS = {f"{key}={value}" for key, value in ENVIRONMENT}
REDACTIONS.update(value for _, value in ENVIRONMENT if len(value) >= 4)


class Failure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


def redact(text: str) -> str:
    for value in sorted(REDACTIONS, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return re.sub(
        r"(?i)\b((?:TANKAI|OPENAI|ANTHROPIC|BRAVE|TAVILY|GITHUB)_[A-Z0-9_]+)"
        r"\s*=\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )


def clipped(text: str, limit: int = 4096) -> str:
    text = redact(text.strip())
    return text if len(text) <= limit else text[-limit:]


def docker(
    args: list[str],
    *,
    operation: str,
    stdin: str | None = None,
    timeout: float = 30.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [DOCKER, *args],
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=max(0.1, timeout),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise Failure(f"{operation}: Docker timeout") from exc
    except OSError as exc:
        raise Failure(f"{operation}: Docker CLI unavailable") from exc
    if check and result.returncode != 0:
        detail = clipped(result.stderr or result.stdout)
        raise Failure(
            f"{operation}: Docker exit {result.returncode}"
            + (f": {detail}" if detail else "")
        )
    return result


def docker_text(
    args: list[str],
    *,
    operation: str,
    stdin: str | None = None,
    timeout: float = 30.0,
) -> str:
    return docker(
        args,
        operation=operation,
        stdin=stdin,
        timeout=timeout,
    ).stdout.strip()


def docker_doc(
    args: list[str],
    *,
    operation: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    try:
        value = json.loads(docker_text(args, operation=operation, timeout=timeout))
    except json.JSONDecodeError as exc:
        raise Failure(f"{operation}: invalid JSON") from exc
    require(
        isinstance(value, list)
        and len(value) == 1
        and isinstance(value[0], dict),
        f"{operation}: unexpected response",
    )
    return value[0]


def is_missing(result: subprocess.CompletedProcess[str]) -> bool:
    message = (result.stderr + result.stdout).casefold()
    return "no such" in message or "not found" in message


def inspect_resource(
    kind: str,
    name: str,
) -> tuple[dict[str, Any] | None, str | None]:
    command = (
        ["container", "inspect", name]
        if kind == "container"
        else ["volume", "inspect", name]
    )
    result = docker(
        command,
        operation=f"{kind} inspect",
        timeout=15,
        check=False,
    )
    if result.returncode:
        return (None, None) if is_missing(result) else (
            None,
            clipped(result.stderr or result.stdout),
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "invalid inspect JSON"
    if not (
        isinstance(value, list)
        and len(value) == 1
        and isinstance(value[0], dict)
    ):
        return None, "unexpected inspect response"
    return value[0], None


def resource_labels(kind: str, document: dict[str, Any]) -> dict[str, Any]:
    if kind == "volume":
        labels = document.get("Labels")
    else:
        config = document.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
    return labels if isinstance(labels, dict) else {}


def cleanup(container: str, volume: str, scope: str) -> list[str]:
    errors: list[str] = []
    resources = (
        ("container", container, ["container", "rm", "--force", container]),
        ("volume", volume, ["volume", "rm", "--force", volume]),
    )
    for kind, name, remove in resources:
        try:
            document, error = inspect_resource(kind, name)
        except Failure as exc:
            errors.append(f"{kind}: {clipped(str(exc))}")
            continue
        if error:
            errors.append(f"{kind}: inspect failed: {error}")
            continue
        if document is None:
            continue
        labels = resource_labels(kind, document)
        if (
            labels.get(MANAGED_LABEL) != "true"
            or labels.get(SCOPE_LABEL) != scope
        ):
            errors.append(f"{kind}: ownership labels differ")
            continue
        result = docker(
            remove,
            operation=f"{kind} cleanup",
            timeout=30,
            check=False,
        )
        if result.returncode and not is_missing(result):
            errors.append(f"{kind}: {clipped(result.stderr or result.stdout)}")
    return errors


def require_absent(kind: str, name: str) -> None:
    document, error = inspect_resource(kind, name)
    require(error is None, f"{kind} preflight inspect failed")
    require(document is None, f"{kind} name already exists")


def nonroot_user(value: object, source: str) -> str:
    require(isinstance(value, str) and bool(value.strip()), f"{source}: User empty")
    user = value.strip()
    identity = user.split(":", 1)[0].strip().casefold()
    require(
        identity != "root"
        and not (identity.isdecimal() and int(identity) == 0),
        f"{source}: root User",
    )
    return user


def health_signature(value: object, source: str) -> tuple[Any, ...]:
    require(isinstance(value, dict), f"{source}: healthcheck missing")
    require(
        {key: value.get(key) for key in HEALTH_BASE} == HEALTH_BASE,
        f"{source}: Dockerfile healthcheck changed",
    )
    start_interval = value.get("StartInterval")
    require(
        start_interval in (None, 0, 5_000_000_000),
        f"{source}: unexpected healthcheck start interval",
    )
    return (
        tuple(value["Test"]),
        value["Interval"],
        value["Timeout"],
        value["StartPeriod"],
        value["Retries"],
        start_interval,
    )


def size_bytes(value: str) -> int:
    match = re.fullmatch(r"([0-9]+)([kmgt]?)(?:i?b)?", value.lower())
    require(match is not None, "invalid tmpfs size")
    return int(match.group(1)) * {
        "": 1,
        "k": 1024,
        "m": 1024**2,
        "g": 1024**3,
        "t": 1024**4,
    }[match.group(2)]


def validate_tmpfs(host: dict[str, Any]) -> None:
    tmpfs = host.get("Tmpfs")
    require(
        isinstance(tmpfs, dict) and set(tmpfs) == {"/tmp"},
        "expected only /tmp tmpfs",
    )
    raw = tmpfs["/tmp"]
    require(isinstance(raw, str), "tmpfs options missing")
    flags: set[str] = set()
    values: dict[str, str] = {}
    for option in raw.split(","):
        key, separator, value = option.partition("=")
        (values if separator else flags).add(key) if not separator else None
        if separator:
            values[key] = value
    require(
        {"rw", "nosuid", "nodev", "noexec"}.issubset(flags),
        "tmpfs security options missing",
    )
    require(values.get("mode") == "1777", "tmpfs mode differs")
    require(
        "size" in values and size_bytes(values["size"]) == 128 * 1024**2,
        "tmpfs size differs",
    )


def validate_runtime(
    document: dict[str, Any],
    *,
    image_id: str,
    image_user: str,
    volume: str,
    labels: dict[str, str],
) -> tuple[str, int]:
    require(document.get("Image") == image_id, "running image ID differs")
    config = document.get("Config")
    host = document.get("HostConfig")
    network = document.get("NetworkSettings")
    require(
        isinstance(config, dict)
        and isinstance(host, dict)
        and isinstance(network, dict),
        "container inspect incomplete",
    )
    require(config.get("Image") == image_id, "Config.Image differs")
    require(
        nonroot_user(config.get("User"), "container") == image_user,
        "container User differs from image",
    )
    require(
        all((config.get("Labels") or {}).get(key) == value for key, value in labels.items()),
        "container labels missing",
    )
    require(host.get("ReadonlyRootfs") is True, "root filesystem writable")
    require(host.get("Privileged") is False, "container privileged")
    require(host.get("PidsLimit") == 256, "pids limit differs")
    require(
        {str(value).upper() for value in host.get("CapDrop") or []} == {"ALL"},
        "cap-drop differs",
    )
    require(
        "no-new-privileges:true" in (host.get("SecurityOpt") or []),
        "no-new-privileges missing",
    )
    validate_tmpfs(host)

    runtime_env: dict[str, str] = {}
    for item in config.get("Env") or []:
        key, separator, value = str(item).partition("=")
        if separator:
            runtime_env[key] = value
    require(
        all(runtime_env.get(key) == value for key, value in ENVIRONMENT),
        "explicit environment differs",
    )
    forbidden = {
        key
        for key in runtime_env
        if key.endswith("_API_KEY")
        or key.endswith("_TOKEN")
        or "PASSWORD" in key
        or "SECRET" in key
    }
    require(not forbidden, "secret environment present")

    exposed = config.get("ExposedPorts")
    require(
        isinstance(exposed, dict) and set(exposed) == {"8765/tcp"},
        "exposed ports differ",
    )
    port_bindings = host.get("PortBindings")
    require(
        isinstance(port_bindings, dict)
        and set(port_bindings) == {"8765/tcp"}
        and isinstance(port_bindings["8765/tcp"], list)
        and len(port_bindings["8765/tcp"]) == 1
        and isinstance(port_bindings["8765/tcp"][0], dict)
        and port_bindings["8765/tcp"][0].get("HostIp") == "127.0.0.1",
        "published ports differ",
    )

    mounts = document.get("Mounts")
    require(
        isinstance(mounts, list)
        and len(mounts) == 1
        and isinstance(mounts[0], dict),
        "mount count differs",
    )
    mount = mounts[0]
    require(
        mount.get("Type") == "volume"
        and mount.get("Name") == volume
        and mount.get("Destination") == "/app/data"
        and mount.get("RW") is True,
        "mount is not the RW named /app/data volume",
    )

    ports = network.get("Ports")
    require(
        isinstance(ports, dict)
        and set(ports) == {"8765/tcp"}
        and isinstance(ports["8765/tcp"], list)
        and len(ports["8765/tcp"]) == 1
        and isinstance(ports["8765/tcp"][0], dict),
        "network port bindings differ",
    )
    binding = ports["8765/tcp"][0]
    require(binding.get("HostIp") == "127.0.0.1", "host IP is not loopback")
    port = binding.get("HostPort")
    require(
        isinstance(port, str)
        and port.isdigit()
        and 1 <= int(port) <= 65535,
        "dynamic port invalid",
    )
    return "127.0.0.1", int(port)


def read_json(response: Any) -> dict[str, Any]:
    raw = response.read(HTTP_LIMIT + 1)
    require(len(raw) <= HTTP_LIMIT, "HTTP response exceeds 4096 bytes")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Failure("HTTP response is not JSON") from exc
    require(isinstance(value, dict), "HTTP JSON is not an object")
    return value


def request_json(
    base: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> tuple[int, Any, dict[str, Any]]:
    body = None
    headers = {"Accept": "application/json", "Connection": "close"}
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(base + path, data=body, headers=headers, method=method)
    try:
        with OPENER.open(request, timeout=max(0.1, timeout)) as response:
            return response.status, response.headers, read_json(response)
    except HTTPError as exc:
        return exc.code, exc.headers, read_json(exc)


def public_json_headers(headers: Any, endpoint: str) -> None:
    require(
        (headers.get_all("Content-Type") or [])
        == ["application/json; charset=utf-8"],
        f"{endpoint}: Content-Type differs",
    )
    require(
        not (headers.get_all("Set-Cookie") or []),
        f"{endpoint}: unexpected Set-Cookie",
    )


def wait_ready(container: str, base: str) -> tuple[Any, dict[str, Any]]:
    deadline = time.monotonic() + READY_TIMEOUT
    last_http = "not attempted"
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        document = docker_doc(
            ["container", "inspect", container],
            operation="readiness inspect",
            timeout=min(10.0, max(0.1, remaining)),
        )
        state = document.get("State")
        require(isinstance(state, dict), "container State missing")
        health = state.get("Health")
        health_status = health.get("Status") if isinstance(health, dict) else None
        if state.get("Status") not in {"created", "running"}:
            raise Failure("container exited during readiness")
        if health_status == "unhealthy":
            raise Failure("container health is unhealthy")
        if health_status == "healthy":
            try:
                code, headers, payload = request_json(
                    base,
                    "GET",
                    "/api/health",
                    timeout=min(5.0, max(0.1, remaining)),
                )
            except (URLError, TimeoutError, OSError) as exc:
                last_http = clipped(str(exc), 512)
            else:
                if code == 200:
                    return headers, payload
                last_http = f"HTTP {code}"
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1.0, remaining))
    raise Failure(f"not ready via Docker health and HTTP within 120s ({last_http})")


def security_headers(headers: Any) -> None:
    for key, value in SECURITY_HEADERS.items():
        require((headers.get_all(key) or []) == [value], f"{key} differs")
    csp = headers.get_all("Content-Security-Policy") or []
    require(len(csp) == 1 and CSP.fullmatch(csp[0]) is not None, "CSP differs")


def health_contract(headers: Any, payload: dict[str, Any], version: str) -> None:
    public_json_headers(headers, "/api/health")
    require(set(payload) == HEALTH_KEYS, "health does not have exactly seven keys")
    require(payload["ok"] is True, "health ok differs")
    require(payload["version"] == version, "health version differs")
    require(payload["auth_mode"] == "session", "auth mode differs")
    require(payload["auth_required"] is True, "auth requirement differs")
    require(payload["registration_enabled"] is False, "registration enabled")
    require(payload["production_ready"] is False, "production_ready not false")
    require(
        payload["development_queue_enabled"] is False,
        "development queue enabled",
    )


def unauthenticated_me(base: str) -> None:
    code, headers, payload = request_json(base, "GET", "/api/auth/me")
    public_json_headers(headers, "/api/auth/me")
    require(code == 401, "unauthenticated me is not 401")
    require(payload == {"error": "Nicht angemeldet"}, "me response differs")


def registration_disabled(base: str) -> None:
    code, headers, payload = request_json(
        base,
        "POST",
        "/api/auth/register",
        {},
    )
    public_json_headers(headers, "/api/auth/register")
    require(code == 403, "registration is not 403")
    require(
        payload == {"error": "Registrierung ist deaktiviert"},
        "registration response differs",
    )


def container_version(container: str) -> str:
    raw = docker_text(
        [
            "container",
            "exec",
            container,
            "python",
            "-c",
            "import json,tankai; print(json.dumps(tankai.__version__))",
        ],
        operation="read running version",
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Failure("running version is invalid") from exc
    require(isinstance(value, str) and bool(value), "running version is empty")
    return value


def create_user(container: str, scope: str) -> tuple[dict[str, str], str, str]:
    email = f"production-smoke-{scope}@example.invalid"
    password = secrets.token_urlsafe(36)
    REDACTIONS.update({email, password})
    raw = docker_text(
        [
            "container",
            "exec",
            "--interactive",
            container,
            "python",
            "-m",
            "tankai.web.auth_cli",
            "--db",
            "/app/data/auth.db",
            "create-user",
            "--email",
            email,
            "--name",
            "CI Smoke",
            "--tenant",
            "CI Smoke",
            "--workspace",
            "CI Smoke",
            "--password-stdin",
        ],
        operation="auth CLI create-user",
        stdin=password + "\n",
        timeout=45,
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Failure("auth CLI response invalid") from exc
    require(
        isinstance(value, dict)
        and set(value) == {"user_id", "tenant_id", "workspace_id"},
        "auth CLI keys differ",
    )
    identifiers: dict[str, str] = {}
    pattern = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    )
    for key in ("user_id", "tenant_id", "workspace_id"):
        require(
            isinstance(value[key], str) and pattern.fullmatch(value[key]) is not None,
            f"invalid {key}",
        )
        identifiers[key] = value[key]
    return identifiers, email, password


def login_contract(
    base: str,
    identifiers: dict[str, str],
    email: str,
    password: str,
) -> None:
    code, headers, payload = request_json(
        base,
        "POST",
        "/api/auth/login",
        {"email": email, "password": password},
    )
    require(code == 200, "login is not 200")
    require(
        (headers.get_all("Content-Type") or [])
        == ["application/json; charset=utf-8"],
        "login Content-Type differs",
    )
    require(
        set(payload)
        == {
            "user",
            "tenant",
            "workspace",
            "workspaces",
            "csrf_token",
            "session_expires_at",
        },
        "login keys differ",
    )
    require(
        payload["user"]
        == {
            "id": identifiers["user_id"],
            "email": email,
            "display_name": "CI Smoke",
        },
        "login user differs",
    )
    require(
        payload["tenant"] == {"id": identifiers["tenant_id"]},
        "login tenant differs",
    )
    require(
        payload["workspace"]
        == {
            "id": identifiers["workspace_id"],
            "name": "CI Smoke",
            "role": "owner",
        },
        "login workspace differs",
    )
    require(
        payload["workspaces"]
        == [
            {
                "id": identifiers["workspace_id"],
                "tenant_id": identifiers["tenant_id"],
                "name": "CI Smoke",
                "slug": "ci-smoke",
                "role": "owner",
            }
        ],
        "login workspace list differs",
    )
    require(
        isinstance(payload["csrf_token"], str) and bool(payload["csrf_token"]),
        "CSRF token missing",
    )
    try:
        expires = datetime.fromisoformat(payload["session_expires_at"])
    except (TypeError, ValueError) as exc:
        raise Failure("session expiry invalid") from exc
    require(
        expires.tzinfo is not None
        and expires.astimezone(timezone.utc) > datetime.now(timezone.utc),
        "session expiry is not in the future",
    )

    set_cookie = headers.get_all("Set-Cookie") or []
    require(len(set_cookie) == 1, "login does not set exactly one cookie")
    cookie = SimpleCookie()
    cookie.load(set_cookie[0])
    require(set(cookie) == {"tankai_session"}, "cookie name differs")
    morsel = cookie["tankai_session"]
    require(bool(morsel.value), "cookie value empty")
    REDACTIONS.add(morsel.value)
    require(
        {key for key in morsel.keys() if morsel[key]}
        == {"path", "max-age", "secure", "httponly", "samesite"},
        "cookie attributes differ",
    )
    require(morsel["path"] == "/", "cookie path differs")
    require(bool(morsel["httponly"]), "cookie is not HttpOnly")
    require(bool(morsel["secure"]), "cookie is not Secure")
    require(morsel["samesite"] == "Strict", "cookie SameSite differs")
    try:
        max_age = int(morsel["max-age"])
    except ValueError as exc:
        raise Failure("cookie max-age invalid") from exc
    require(1 <= max_age <= 43_200, "cookie max-age outside contract")

    # OPENER has no cookie processor. Never replay the Secure cookie over HTTP.
    unauthenticated_me(base)


FS_PROBE = r"""
import errno
import json
import os
import secrets
import stat

def info(path):
    item = os.stat(path)
    return {
        "uid": item.st_uid,
        "gid": item.st_gid,
        "mode": stat.S_IMODE(item.st_mode),
        "directory": stat.S_ISDIR(item.st_mode),
        "regular": stat.S_ISREG(item.st_mode),
    }

def write(parent):
    path = parent + "/.tankai-smoke-" + secrets.token_hex(8)
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except OSError as exc:
        return {"writable": False, "errno": exc.errno}
    os.close(descriptor)
    os.unlink(path)
    return {"writable": True, "errno": None}

with open("/proc/1/status", encoding="utf-8") as handle:
    uid_line = next(line for line in handle if line.startswith("Uid:"))

print(json.dumps({
    "pid1_uids": [int(value) for value in uid_line.split()[1:]],
    "effective_uid": os.geteuid(),
    "effective_gid": os.getegid(),
    "data": info("/app/data"),
    "auth_db": info("/app/data/auth.db"),
    "data_write": write("/app/data"),
    "app_write": write("/app"),
}))
"""


def filesystem_contract(container: str) -> None:
    raw = docker_text(
        ["container", "exec", container, "python", "-c", FS_PROBE],
        operation="filesystem probe",
    )
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Failure("filesystem probe invalid") from exc
    require(
        value.get("pid1_uids") == [10001, 10001, 10001, 10001],
        "PID 1 UIDs differ",
    )
    require(value.get("effective_uid") == 10001, "exec UID differs")
    require(value.get("effective_gid") == 10001, "exec GID differs")
    require(
        value.get("data")
        == {
            "uid": 10001,
            "gid": 10001,
            "mode": 0o700,
            "directory": True,
            "regular": False,
        },
        "data ownership or mode differs",
    )
    require(
        value.get("auth_db")
        == {
            "uid": 10001,
            "gid": 10001,
            "mode": 0o600,
            "directory": False,
            "regular": True,
        },
        "auth.db ownership or mode differs",
    )
    require(
        value.get("data_write") == {"writable": True, "errno": None},
        "data is not writable",
    )
    app = value.get("app_write")
    require(
        isinstance(app, dict)
        and app.get("writable") is False
        and app.get("errno") == errno.EROFS,
        "/app is not read-only",
    )


def diagnostics(container: str) -> None:
    try:
        document, error = inspect_resource("container", container)
        if error:
            print(f"diagnostic inspect failed: {error}", file=sys.stderr)
        elif document:
            state = document.get("State")
            state = state if isinstance(state, dict) else {}
            health = state.get("Health")
            safe = {
                "status": state.get("Status"),
                "health": (
                    health.get("Status") if isinstance(health, dict) else None
                ),
                "exit_code": state.get("ExitCode"),
                "oom_killed": state.get("OOMKilled"),
            }
            print(
                "diagnostic state: "
                + clipped(json.dumps(safe, separators=(",", ":"))),
                file=sys.stderr,
            )
        logs = docker(
            ["container", "logs", "--tail", "80", container],
            operation="diagnostic logs",
            timeout=15,
            check=False,
        )
        if logs.returncode == 0 and (logs.stdout or logs.stderr):
            print(
                "diagnostic logs (redacted):\n"
                + clipped(logs.stdout + logs.stderr),
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"diagnostics unavailable: {clipped(str(exc))}", file=sys.stderr)


def smoke(
    image: str,
    iid_file: Path,
    *,
    scope: str,
    container: str,
    volume: str,
) -> None:
    try:
        engine_os = json.loads(
            docker_text(
                ["info", "--format", "{{json .OSType}}"],
                operation="Docker engine OS",
            )
        )
    except json.JSONDecodeError as exc:
        raise Failure("Docker engine OS invalid") from exc
    require(engine_os == "linux", "Linux Docker engine required")

    try:
        image_id = iid_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise Failure("IID file unreadable") from exc
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is not None,
        "IID file invalid",
    )
    image_document = docker_doc(
        ["image", "inspect", image],
        operation="image inspect",
    )
    require(image_document.get("Id") == image_id, "tag does not match built IID")
    require(image_document.get("Os") == "linux", "image is not Linux")
    image_config = image_document.get("Config")
    require(isinstance(image_config, dict), "image Config missing")
    image_user = nonroot_user(image_config.get("User"), "image")
    image_health = health_signature(image_config.get("Healthcheck"), "image")

    require_absent("container", container)
    require_absent("volume", volume)
    labels = {MANAGED_LABEL: "true", SCOPE_LABEL: scope}

    created = docker_text(
        [
            "volume",
            "create",
            "--label",
            f"{MANAGED_LABEL}=true",
            "--label",
            f"{SCOPE_LABEL}={scope}",
            volume,
        ],
        operation="volume create",
    )
    require(created == volume, "unexpected created volume")
    volume_document = docker_doc(
        ["volume", "inspect", volume],
        operation="volume inspect",
    )
    require(
        all(
            (volume_document.get("Labels") or {}).get(key) == value
            for key, value in labels.items()
        ),
        "volume labels missing",
    )

    command = [
        "container",
        "run",
        "--detach",
        "--name",
        container,
        "--label",
        f"{MANAGED_LABEL}=true",
        "--label",
        f"{SCOPE_LABEL}={scope}",
        "--mount",
        f"type=volume,source={volume},target=/app/data",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=128m,mode=1777",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--pids-limit",
        "256",
        "--publish",
        "127.0.0.1::8765",
    ]
    for key, value in ENVIRONMENT:
        command.extend(["--env", f"{key}={value}"])
    command.append(image_id)
    container_id = docker_text(
        command,
        operation="container run",
        timeout=45,
    )
    require(
        re.fullmatch(r"[0-9a-f]{64}", container_id) is not None,
        "container ID invalid",
    )

    container_document = docker_doc(
        ["container", "inspect", container],
        operation="container inspect",
    )
    container_health = health_signature(
        (container_document.get("Config") or {}).get("Healthcheck"),
        "container",
    )
    require(container_health == image_health, "container healthcheck differs from image")
    host, port = validate_runtime(
        container_document,
        image_id=image_id,
        image_user=image_user,
        volume=volume,
        labels=labels,
    )
    base = f"http://{host}:{port}"

    headers, health = wait_ready(container, base)
    version = container_version(container)
    security_headers(headers)
    health_contract(headers, health, version)
    unauthenticated_me(base)
    registration_disabled(base)
    identifiers, email, password = create_user(container, scope)
    filesystem_contract(container)
    login_contract(base, identifiers, email, password)


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image")
    parser.add_argument("--image-id-file", type=Path)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--cleanup", action="store_true")
    value = parser.parse_args()
    if not value.scope.strip() or len(value.scope) > 512:
        parser.error("--scope must contain 1 to 512 characters")
    if not value.cleanup and (not value.image or value.image_id_file is None):
        parser.error("--image and --image-id-file are required")
    return value


def main() -> int:
    args = arguments()
    scope = hashlib.sha256(args.scope.encode("utf-8")).hexdigest()[:24]
    container = f"tankai-production-web-smoke-{scope}"
    volume = f"tankai-production-web-smoke-data-{scope}"

    if args.cleanup:
        errors = cleanup(container, volume, scope)
        if errors:
            print("cleanup failed: " + "; ".join(errors), file=sys.stderr)
            return 1
        print("production web smoke cleanup: PASS")
        return 0

    primary: Exception | None = None
    cleanup_errors: list[str] = []
    try:
        smoke(
            args.image,
            args.image_id_file,
            scope=scope,
            container=container,
            volume=volume,
        )
    except Exception as exc:
        primary = exc
        diagnostics(container)
    finally:
        cleanup_errors = cleanup(container, volume, scope)

    if primary:
        message = clipped(str(primary))
        if cleanup_errors:
            message += "; cleanup also failed: " + "; ".join(cleanup_errors)
        print("production web container smoke: FAIL: " + message, file=sys.stderr)
        return 1
    if cleanup_errors:
        print(
            "production web container smoke: FAIL: cleanup: "
            + "; ".join(cleanup_errors),
            file=sys.stderr,
        )
        return 1
    print("production web container smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
