from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "production_deploy_gate.py"
SPEC = importlib.util.spec_from_file_location("production_deploy_gate", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_branch_gate_requires_protection_exact_sha_and_checks() -> None:
    payload = {
        "commit": {"sha": "abc"},
        "protected": True,
        "protection": {
            "required_status_checks": {
                "contexts": ["test"],
                "checks": [{"context": "cloudflare"}],
            }
        },
    }
    assert MODULE.evaluate_branch(payload, "abc") == []

    failures = MODULE.evaluate_branch(
        {
            "commit": {"sha": "wrong"},
            "protected": False,
            "protection": {"required_status_checks": {"contexts": ["test"], "checks": []}},
        },
        "abc",
    )
    assert any("head mismatch" in item for item in failures)
    assert "main is not protected" in failures
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
