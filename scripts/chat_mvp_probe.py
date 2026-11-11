#!/usr/bin/env python3
"""Fail-closed chat MVP check. A live model call requires separate explicit consent.

Read-only mode logs in and checks authenticated /api/health. Live mode sends
exactly one /api/run only after both CLI and environment approval. This tool
never changes deployment, provider configuration or credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


class ProbeError(RuntimeError):
    """An MVP gate did not pass; details must not contain sensitive responses."""


def checked_origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ProbeError("An absolute HTTPS URL is required")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ProbeError("Base URL must contain only an origin (no credentials, path or query)")
    if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ProbeError("Remote HTTP is forbidden; use HTTPS")
    return url.rstrip("/")


def checked_login(data: dict, expected_email: str) -> str:
    token = data.get("csrf_token")
    user = data.get("user")
    workspace = data.get("workspace")
    if not isinstance(token, str) or not token:
        raise ProbeError("Login succeeded without a usable CSRF token")
    if (
        not isinstance(user, dict)
        or not isinstance(user.get("email"), str)
        or user["email"].strip().casefold() != expected_email.strip().casefold()
    ):
        raise ProbeError("Login response is not bound to the requested account")
    if (
        not isinstance(workspace, dict)
        or not isinstance(workspace.get("id"), str)
        or not workspace["id"].strip()
        or not isinstance(workspace.get("name"), str)
        or not workspace["name"].strip()
    ):
        raise ProbeError("Login response has no usable workspace")
    return token


def checked_health(data: dict, *, expected_email: str = "") -> dict:
    if data.get("ok") is not True:
        raise ProbeError("Core health check failed")
    if data.get("execution_mode") != "live" or data.get("live_provider") is not True:
        raise ProbeError("No confirmed live configuration; mock or mixed mode is not an MVP pass")
    if data.get("critic_independent") is not True:
        raise ProbeError("Independent live critic is not configured")
    if data.get("verification_ready") is not True:
        raise ProbeError("Core verification is not ready")
    user, workspace = data.get("user"), data.get("workspace")
    if (
        not isinstance(user, str)
        or not user.strip()
        or not isinstance(workspace, str)
        or not workspace.strip()
    ):
        raise ProbeError("Health response is not bound to an authenticated workspace")
    if expected_email and user.strip().casefold() != expected_email.strip().casefold():
        raise ProbeError("Health response is bound to a different account")
    main, critic = data.get("llm"), data.get("critic_llm")
    if not isinstance(main, str) or not main.strip() or not isinstance(critic, str) or not critic.strip():
        raise ProbeError("Main or critic model identity is missing")
    return {"main_llm": main, "critic_llm": critic, "mode": "live", "configured": True}


def checked_result(data: dict, models: dict) -> dict:
    if data.get("execution_mode") != "live" or data.get("status") != "completed":
        raise ProbeError("The chat did not complete as a live model run")
    if data.get("release_ready") is not True or data.get("verification_passed") is not True:
        raise ProbeError("Live response did not pass release and verification gates")
    if data.get("critic_independent") is not True:
        raise ProbeError("Run did not use an independent critic")
    if data.get("main_llm") != models["main_llm"] or data.get("critic_llm") != models["critic_llm"]:
        raise ProbeError("Model identity changed between readiness and run")
    answer = data.get("final_answer")
    if not isinstance(answer, str) or not answer.strip():
        raise ProbeError("No answer was returned")
    return {"live_answer_verified": True, "mode": "live", "main_llm": models["main_llm"], "critic_llm": models["critic_llm"]}


def request_json(opener, origin: str, path: str, *, body: dict | None = None, csrf: str = "") -> dict:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-CSRF-Token"] = csrf
    request = Request(origin + path, data=payload, headers=headers, method="POST" if body is not None else "GET")
    try:
        response = opener.open(request, timeout=25)
        with response:
            if response.geturl() != origin + path:
                raise ProbeError("Unexpected redirect")
            raw = response.read(128 * 1024 + 1)
            if len(raw) > 128 * 1024:
                raise ProbeError("Response too large")
    except HTTPError as exc:
        # Do not expose response bodies, authentication details or provider errors.
        raise ProbeError(f"{path}: HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise ProbeError(f"{path}: connection unavailable ({type(exc).__name__})") from None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ProbeError(f"{path}: invalid JSON response") from None
    if not isinstance(data, dict):
        raise ProbeError(f"{path}: expected JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Authenticated TankAI MVP readiness check")
    parser.add_argument("--base-url", default="https://tankaicore.com")
    parser.add_argument("--run", action="store_true", help="Send exactly one live run after verified configuration")
    parser.add_argument("--approve-provider-cost", action="store_true", help="Explicitly acknowledge external model charges")
    args = parser.parse_args(argv)
    try:
        origin = checked_origin(args.base_url)
        if args.run and (not args.approve_provider_cost or os.getenv("TANKAI_ALLOW_BILLED_SMOKE") != "1"):
            raise ProbeError("Live run requires --approve-provider-cost and TANKAI_ALLOW_BILLED_SMOKE=1")
        email, password = os.getenv("TANKAI_SMOKE_EMAIL", ""), os.getenv("TANKAI_SMOKE_PASSWORD", "")
        if not email or not password:
            raise ProbeError("TANKAI_SMOKE_EMAIL and TANKAI_SMOKE_PASSWORD must be set privately")
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        login = request_json(opener, origin, "/api/auth/login", body={"email": email, "password": password})
        token = checked_login(login, email)
        models = checked_health(
            request_json(opener, origin, "/api/health"),
            expected_email=email,
        )
        receipt = {"gate": "chat_mvp", "readiness": "passed", **models, "live_answer_verified": False}
        if args.run:
            # This application-level check cannot enforce provider-side billing.
            result = request_json(
                opener, origin, "/api/run", csrf=token,
                body={"goal": "Antworte in genau einem kurzen deutschen Satz: Ist TankAI erreichbar?",
                      "definition_of_done": "Ein geprüfter deutscher Satz, ohne erfundene Quellen.",
                      "parallel": False},
            )
            receipt.update(checked_result(result, models))
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    except ProbeError as exc:
        print(json.dumps({"gate": "chat_mvp", "readiness": "failed", "reason": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
