#!/usr/bin/env python3
"""One-shot protocol rehearsal through a local private-service bridge. Default: plan only."""
from __future__ import annotations
import argparse
import getpass
import json
from pathlib import Path
import re
import sys
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

HASH = re.compile(r"[0-9a-f]{64}\Z")


class RehearsalError(RuntimeError):
    """Fixed messages only: never include response bodies, headers or credentials."""


def require(condition, message="pilot response rejected"):
    if not condition:
        raise RehearsalError(message)


def uuid(value):
    try:
        parsed = UUID(value)
        return str(parsed) == value and parsed.version == 4
    except (ValueError, TypeError, AttributeError):
        return False


def integer(value):
    return type(value) is int and 0 <= value < 2 ** 53


def validate_state(value, image):
    require(isinstance(value, dict) and set(value) == {"phase", "revision", "operation_id", "automatic_resume", "recovery_required", "capsule"})
    require(value["phase"] in {"empty", "starting", "running", "capturing", "captured", "restoring", "staged", "blocked"}
            and integer(value["revision"]) and value["automatic_resume"] is False
            and type(value["recovery_required"]) is bool)
    require(value["operation_id"] is None or uuid(value["operation_id"]))
    saved = value["capsule"]
    if saved is not None:
        require(isinstance(saved, dict) and set(saved) == {"pin", "generation", "image", "root_id", "root_sha256", "created_at", "expires_at"})
        require(saved["image"] == image and uuid(saved["generation"]) and uuid(saved["root_id"])
                and isinstance(saved["pin"], str) and HASH.fullmatch(saved["pin"])
                and isinstance(saved["root_sha256"], str) and HASH.fullmatch(saved["root_sha256"])
                and integer(saved["created_at"]) and integer(saved["expires_at"])
                and saved["expires_at"] - saved["created_at"] == 86400000)
    return value


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RehearsalError("pilot redirect rejected")


class Rehearsal:
    def __init__(self, *, account, image, source_commit, token, port=8766):
        require(re.fullmatch(r"[0-9a-f]{32}", account) is not None, "invalid account")
        require(re.fullmatch(r"registry\.cloudflare\.com/" + account + r"/[A-Za-z0-9_./-]+@sha256:[0-9a-f]{64}", image) is not None, "reviewed image digest required")
        require(re.fullmatch(r"[0-9a-f]{40}", source_commit) is not None, "reviewed source commit required")
        require(re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token) is not None, "invalid operator token format")
        require(type(port) is int and 1024 <= port <= 65535, "invalid loopback port")
        self.image, self.token = image, token
        self.base = f"http://127.0.0.1:{port}"
        self.opener = build_opener(ProxyHandler({}), NoRedirects())
        self.deadline = time.monotonic() + 900
        self.report = {"format": "tankai-pilot-rehearsal-v1", "result": "RUNNING", "account_id": account,
                       "expected_image": image, "operator_supplied_source_commit": source_commit,
                       "evidence_scope": "http_protocol_and_root_match", "native_execution_independently_verified": False,
                       "requests_attempted": 0, "mutating_requests_attempted": 0, "cleanup_required": False, "steps": []}

    def call(self, action, *, previous=None, pin=None):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "local rehearsal deadline reached")
        require(self.report["requests_attempted"] < 7, "request limit reached")
        require(action in {"status", "inspection", "start", "capture", "restore"})
        mutating = action in {"start", "capture", "restore"}
        operation = str(uuid4()) if mutating else None
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json", "Connection": "close"}
        data = None
        if mutating:
            require(self.report["mutating_requests_attempted"] < 3, "mutation limit reached")
            require(previous is not None)
            headers.update({"Content-Type": "application/json", "If-Match": f'"{previous["revision"]}"', "Idempotency-Key": operation})
            data = json.dumps({"pin": pin} if action == "restore" else {}).encode()
            self.report["mutating_requests_attempted"] += 1
            self.report["cleanup_required"] = True
        self.report["requests_attempted"] += 1
        request = Request(self.base + "/_pilot/" + action, headers=headers, data=data, method="POST" if mutating else "GET")
        try:
            with self.opener.open(request, timeout=min(180, remaining)) as response:
                require(response.status == 200, "pilot operation not confirmed")
                require(response.headers.get_content_type() == "application/json")
                raw = response.read(8193)
                require(len(raw) <= 8192, "oversized pilot response")
                state = validate_state(json.loads(raw), self.image)
        except RehearsalError:
            raise
        except Exception:
            raise RehearsalError("pilot HTTP result uncertain; no retry") from None
        require(not state["recovery_required"], "pilot requires operator review; no retry")
        if mutating:
            require(state["operation_id"] == operation and state["revision"] == previous["revision"] + 2)
        self.report["steps"].append({"action": action, "phase": state["phase"], "revision": state["revision"], "operation_id": state["operation_id"]})
        return state

    def run(self):
        try:
            source = self.call("status")
            target = self.call("inspection")
            require(all(s["phase"] == "empty" and s["revision"] == 0 and s["capsule"] is None and s["operation_id"] is None for s in (source, target)), "pilot namespace already used; no start")
            running = self.call("start", previous=source)
            require(running["phase"] == "running" and running["capsule"] is None)
            captured = self.call("capture", previous=running)
            saved = captured["capsule"]
            require(captured["phase"] == "captured" and saved is not None and saved["generation"] == captured["operation_id"])
            staged = self.call("restore", previous=captured, pin=saved["pin"])
            require(staged["phase"] == "staged" and staged["capsule"] == saved)
            target = self.call("inspection")
            source = self.call("status")
            require(target["phase"] == "staged" and target["revision"] == 2 and target["operation_id"] == staged["operation_id"] and target["capsule"] == saved)
            require(source == staged)
            self.report.update(result="PASS", capsule=saved)
        except Exception as error:
            self.report.update(result="BLOCKED", error=str(error) if isinstance(error, RehearsalError) else "pilot response rejected")
        return self.report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Send exactly one bounded rehearsal through an already prepared bridge")
    parser.add_argument("--account-id")
    parser.add_argument("--image")
    parser.add_argument("--source-commit")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"result": "PLAN_ONLY", "network_requests": 0, "maximum_requests": 7, "maximum_mutations": 3,
                          "loopback_only": True, "automatic_retries": 0, "cleanup_is_separate": True}, sort_keys=True))
        return 0
    if not all((args.account_id, args.image, args.source_commit)):
        parser.error("--execute requires account, digest-pinned image and reviewed source commit")
    # Reserve evidence before any network effect; an existing receipt must prevent a run.
    try:
        output = args.output.open("x", encoding="utf-8") if args.output is not None else None
    except OSError:
        print(json.dumps({"result": "BLOCKED", "error": "output receipt unavailable; no requests sent"}))
        return 1
    try:
        token = (getpass.getpass("Pilot control token (hidden): ") if sys.stdin.isatty()
                 else sys.stdin.readline(130).removesuffix("\n").removesuffix("\r"))
        runner = Rehearsal(account=args.account_id, image=args.image, source_commit=args.source_commit, token=token, port=args.port)
        result = runner.run()
    except Exception:
        result = {"result": "BLOCKED", "error": "invalid rehearsal configuration"}
    serialized = json.dumps(result, sort_keys=True)
    if output is not None:
        with output:
            output.write(serialized + "\n")
    print(serialized)
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
