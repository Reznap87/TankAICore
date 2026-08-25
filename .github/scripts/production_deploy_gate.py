#!/usr/bin/env python3
"""Fail-closed production deployment gate.

This gate reads public repository metadata only. It never mutates GitHub state,
reads secret values, or triggers deployment. It is intended to run immediately
before the production deployment step.
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

API_ROOT = "https://api.github.com"
REQUIRED_CHECKS = {"test", "cloudflare"}


def get_json(path: str) -> tuple[int, object]:
    request = Request(
        API_ROOT + path,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "tankai-production-deploy-gate",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except Exception:
            return exc.code, {"message": str(exc)}
    except (URLError, TimeoutError, OSError) as exc:
        return 0, {"message": str(exc)}


def evaluate_branch(payload: object, expected_sha: str) -> list[str]:
    failures: list[str] = []
    if not isinstance(payload, dict):
        return ["branch metadata is not an object"]

    commit = payload.get("commit")
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if sha != expected_sha:
        failures.append(f"main head mismatch: expected {expected_sha}, got {sha}")

    if payload.get("protected") is not True:
        failures.append("main is not protected")

    protection = payload.get("protection")
    if not isinstance(protection, dict):
        failures.append("branch protection summary missing")
        return failures

    required = protection.get("required_status_checks")
    if not isinstance(required, dict):
        failures.append("required status checks are not configured")
        return failures

    contexts = set(required.get("contexts") or [])
    for item in required.get("checks") or []:
        if isinstance(item, dict) and isinstance(item.get("context"), str):
            contexts.add(item["context"])
    missing = sorted(REQUIRED_CHECKS - contexts)
    if missing:
        failures.append("required CI checks missing: " + ", ".join(missing))
    return failures


def evaluate_ci(payload: object, expected_sha: str) -> list[str]:
    if not isinstance(payload, dict):
        return ["CI run metadata is not an object"]
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        return ["CI workflow run list missing"]
    matches = [
        run
        for run in runs
        if isinstance(run, dict)
        and run.get("head_sha") == expected_sha
        and run.get("event") == "push"
    ]
    if not matches:
        return [f"no push CI run found for {expected_sha}"]
    successful = [
        run
        for run in matches
        if run.get("status") == "completed" and run.get("conclusion") == "success"
    ]
    if not successful:
        return [f"CI for {expected_sha} is not completed/successful"]
    return []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, help="owner/name")
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--workflow", default="ci.yml")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    owner, sep, repo = args.repository.partition("/")
    if not sep or not owner or not repo:
        print("production gate FAIL: --repository must be owner/name", file=sys.stderr)
        return 2

    base = f"/repos/{quote(owner)}/{quote(repo)}"
    branch_path = f"{base}/branches/{quote(args.branch, safe='')}"
    branch_code, branch_payload = get_json(branch_path)

    failures: list[str] = []
    if branch_code != 200:
        failures.append(f"branch metadata unavailable: HTTP {branch_code or 'transport-error'}")
    else:
        failures.extend(evaluate_branch(branch_payload, args.expected_sha))

    workflow = quote(args.workflow, safe="")
    ci_path = f"{base}/actions/workflows/{workflow}/runs?branch={quote(args.branch)}&event=push&per_page=20"
    ci_code, ci_payload = get_json(ci_path)
    if ci_code != 200:
        failures.append(f"CI metadata unavailable: HTTP {ci_code or 'transport-error'}")
    else:
        failures.extend(evaluate_ci(ci_payload, args.expected_sha))

    if failures:
        print("production gate FAIL", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(f"production gate PASS: protected main + required CI + successful exact-SHA CI ({args.expected_sha})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
