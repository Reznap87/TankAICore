from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "production_deploy_gate.py"
SPEC = importlib.util.spec_from_file_location("production_deploy_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_branch_gate_requires_protection_and_exact_sha() -> None:
    assert MODULE.evaluate_branch({"commit": {"sha": "abc"}, "protected": True}, "abc") == []

    failures = MODULE.evaluate_branch(
        {"commit": {"sha": "wrong"}, "protected": False},
        "abc",
    )
    assert any("head mismatch" in item for item in failures)
    assert "main is not protected" in failures


def test_rules_gate_requires_test_and_cloudflare() -> None:
    payload = [
        {"type": "pull_request", "parameters": {}},
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": "test", "integration_id": 15368},
                    {"context": "cloudflare", "integration_id": 15368},
                ],
                "strict_required_status_checks_policy": False,
            },
        },
        {"type": "non_fast_forward"},
    ]
    assert MODULE.evaluate_rules(payload) == []

    failures = MODULE.evaluate_rules(
        [
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": [{"context": "test"}]},
            }
        ]
    )
    assert any("cloudflare" in item for item in failures)


def test_ci_gate_requires_successful_push_run_for_exact_sha() -> None:
    assert MODULE.evaluate_ci(
        {
            "workflow_runs": [
                {
                    "head_sha": "abc",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        },
        "abc",
    ) == []

    failures = MODULE.evaluate_ci(
        {
            "workflow_runs": [
                {
                    "head_sha": "abc",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "failure",
                }
            ]
        },
        "abc",
    )
    assert any("not completed/successful" in item for item in failures)


def test_deploy_workflow_is_fail_closed() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github" / "workflows" / "deploy-cloudflare.yml"
    ).read_text(encoding="utf-8")
    assert "expected_sha:" in workflow
    assert "github.sha == inputs.expected_sha" in workflow
    assert "production_deploy_gate.py" in workflow
    assert "Verify production secret presence" in workflow
    assert "CLOUDFLARE_API_TOKEN is not configured" in workflow
    assert "CLOUDFLARE_ACCOUNT_ID is not configured" in workflow
    assert "environment: production" in workflow
    assert "group: cloudflare-production" in workflow
