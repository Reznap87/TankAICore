#!/usr/bin/env python3
"""Read-only GitHub production preflight.

The runner reads repository governance metadata and local deployment workflow content.
It never triggers workflows, mutates repository settings, or reads secret values.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API_ROOT = "https://api.github.com"
REQUIRED_SECRET_NAMES = {"CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"}
REQUIRED_CHECKS = {"test", "cloudflare"}


@dataclass
class Check:
    name: str
    status: str
    detail: str


def request_json(path: str, token: str) -> tuple[int, Any]:
    request = Request(
        API_ROOT + path,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "tankai-production-preflight",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        try:
            payload = json.load(exc)
        except Exception:
            payload = {"message": str(exc)}
        return exc.code, payload
    except (URLError, TimeoutError, OSError) as exc:
        return 0, {"message": str(exc)}


def api_check(
    name: str,
    path: str,
    token: str,
    evaluator: Any,
) -> tuple[Check, Any | None]:
    code, payload = request_json(path, token)
    if code != 200:
        reason = payload.get("message") if isinstance(payload, dict) else None
        detail = f"HTTP {code or 'transport-error'}"
        if reason:
            detail += f": {reason}"
        return Check(name, "UNKNOWN", detail), None
    try:
        return evaluator(payload), payload
    except Exception as exc:
        return Check(name, "FAIL", f"invalid response: {exc}"), payload


def branch_evaluator(expected_sha: str | None):
    def evaluate(payload: Any) -> Check:
        sha = payload["commit"]["sha"]
        if expected_sha and sha != expected_sha:
            return Check("branch_head", "FAIL", f"main={sha}, expected={expected_sha}")
        return Check("branch_head", "PASS", f"main={sha}")

    return evaluate


def protection_evaluator(payload: Any) -> Check:
    checks = payload.get("required_status_checks") or {}
    contexts = set(checks.get("contexts") or [])
    for item in checks.get("checks") or []:
        if isinstance(item, dict) and isinstance(item.get("context"), str):
            contexts.add(item["context"])
    missing = sorted(REQUIRED_CHECKS - contexts)
    if missing:
        return Check(
            "main_protection",
            "FAIL",
            "required status checks missing: " + ", ".join(missing),
        )
    return Check(
        "main_protection",
        "PASS",
        "required checks present: " + ", ".join(sorted(REQUIRED_CHECKS)),
    )


def environment_evaluator(payload: Any) -> Check:
    rules = payload.get("protection_rules")
    if not isinstance(rules, list):
        return Check("production_environment", "FAIL", "protection_rules missing")
    rule_types = sorted(
        str(item.get("type"))
        for item in rules
        if isinstance(item, dict) and item.get("type")
    )
    if not rule_types:
        return Check("production_environment", "FAIL", "no protection rules configured")
    branch_policy = payload.get("deployment_branch_policy")
    return Check(
        "production_environment",
        "PASS",
        f"protection_rules={rule_types}; deployment_branch_policy={branch_policy}",
    )


def secrets_evaluator(payload: Any) -> Check:
    secrets = payload.get("secrets")
    if not isinstance(secrets, list):
        return Check("production_secret_names", "FAIL", "secret metadata list missing")
    names = {
        item.get("name")
        for item in secrets
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    missing = sorted(REQUIRED_SECRET_NAMES - names)
    if missing:
        return Check(
            "production_secret_names",
            "FAIL",
            "missing secret names: " + ", ".join(missing),
        )
    return Check(
        "production_secret_names",
        "PASS",
        "required secret names present; values not requested",
    )


def workflow_check(path: Path) -> Check:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return Check("deploy_workflow_contract", "FAIL", f"unreadable: {exc}")

    requirements = {
        "manual dispatch": r"(?m)^\s*workflow_dispatch:\s*$",
        "explicit DEPLOY option": r"(?m)^\s*-\s*DEPLOY\s*$",
        "main ref guard": r"github\.ref\s*==\s*'refs/heads/main'",
        "DEPLOY guard": r"inputs\.confirm_production\s*==\s*'DEPLOY'",
        "production environment": r"(?m)^\s*environment:\s*production\s*$",
        "serial concurrency": r"(?m)^\s*group:\s*cloudflare-production\s*$",
        "no cancellation": r"(?m)^\s*cancel-in-progress:\s*false\s*$",
    }
    missing = [label for label, pattern in requirements.items() if re.search(pattern, text) is None]
    if missing:
        return Check("deploy_workflow_contract", "FAIL", "missing: " + ", ".join(missing))
    return Check(
        "deploy_workflow_contract",
        "PASS",
        "manual main-only explicit DEPLOY gate and serial production environment binding present",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, help="owner/name")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--environment", default="production")
    parser.add_argument("--expected-sha")
    parser.add_argument(
        "--workflow",
        type=Path,
        default=Path(".github/workflows/deploy-cloudflare.yml"),
    )
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get(args.token_env, "").strip()
    checks: list[Check] = [workflow_check(args.workflow)]

    if not token:
        checks.extend(
            [
                Check("branch_head", "UNKNOWN", f"{args.token_env} is not set"),
                Check("main_protection", "UNKNOWN", f"{args.token_env} is not set"),
                Check("production_environment", "UNKNOWN", f"{args.token_env} is not set"),
                Check("production_secret_names", "UNKNOWN", f"{args.token_env} is not set"),
            ]
        )
    else:
        owner, sep, repo = args.repository.partition("/")
        if not sep or not owner or not repo:
            print(json.dumps({"error": "--repository must be owner/name"}))
            return 2
        base = f"/repos/{quote(owner)}/{quote(repo)}"
        branch = quote(args.branch, safe="")
        environment = quote(args.environment, safe="")

        check, _ = api_check(
            "branch_head",
            f"{base}/branches/{branch}",
            token,
            branch_evaluator(args.expected_sha),
        )
        checks.append(check)

        check, _ = api_check(
            "main_protection",
            f"{base}/branches/{branch}/protection",
            token,
            protection_evaluator,
        )
        checks.append(check)

        check, _ = api_check(
            "production_environment",
            f"{base}/environments/{environment}",
            token,
            environment_evaluator,
        )
        checks.append(check)

        check, _ = api_check(
            "production_secret_names",
            f"{base}/environments/{environment}/secrets",
            token,
            secrets_evaluator,
        )
        checks.append(check)

    statuses = [check.status for check in checks]
    overall = "PASS" if all(item == "PASS" for item in statuses) else (
        "FAIL" if "FAIL" in statuses else "UNKNOWN"
    )
    receipt = {
        "schema": 1,
        "repository": args.repository,
        "branch": args.branch,
        "environment": args.environment,
        "expected_sha": args.expected_sha,
        "read_only": True,
        "secret_values_requested": False,
        "deployment_triggered": False,
        "overall": overall,
        "checks": [asdict(check) for check in checks],
    }
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
