from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / ".github" / "scripts" / "production_preflight_readonly.py"
SPEC = importlib.util.spec_from_file_location("production_preflight_readonly", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_workflow_contract_passes_for_repository_workflow() -> None:
    result = MODULE.workflow_check(
        Path(__file__).parents[1] / ".github" / "workflows" / "deploy-cloudflare.yml"
    )
    assert result.status == "PASS"


def test_protection_requires_both_ci_jobs() -> None:
    result = MODULE.protection_evaluator(
        {
            "required_status_checks": {
                "contexts": ["test"],
                "checks": [{"context": "cloudflare"}],
            }
        }
    )
    assert result.status == "PASS"

    result = MODULE.protection_evaluator(
        {"required_status_checks": {"contexts": ["test"], "checks": []}}
    )
    assert result.status == "FAIL"
    assert "cloudflare" in result.detail


def test_environment_requires_at_least_one_protection_rule() -> None:
    result = MODULE.environment_evaluator(
        {
            "protection_rules": [{"type": "required_reviewers"}],
            "deployment_branch_policy": {"protected_branches": True},
        }
    )
    assert result.status == "PASS"

    result = MODULE.environment_evaluator({"protection_rules": []})
    assert result.status == "FAIL"


def test_secret_check_reads_names_only() -> None:
    result = MODULE.secrets_evaluator(
        {
            "secrets": [
                {"name": "CLOUDFLARE_API_TOKEN", "updated_at": "2026-08-25T00:00:00Z"},
                {"name": "CLOUDFLARE_ACCOUNT_ID", "updated_at": "2026-08-25T00:00:00Z"},
            ]
        }
    )
    assert result.status == "PASS"
    assert "values not requested" in result.detail


def test_main_without_token_is_unknown_and_never_deploys(monkeypatch, tmp_path, capsys) -> None:
    workflow = tmp_path / "deploy.yml"
    workflow.write_text(
        """on:\n  workflow_dispatch:\n    inputs:\n      confirm_production:\n        options:\n          - DEPLOY\njobs:\n  deploy:\n    if: ${{ github.ref == 'refs/heads/main' && inputs.confirm_production == 'DEPLOY' }}\n    environment: production\nconcurrency:\n  group: cloudflare-production\n  cancel-in-progress: false\n""",
        encoding="utf-8",
    )
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        MODULE.sys,
        "argv",
        [
            str(SCRIPT),
            "--repository",
            "Reznap87/TankAICore",
            "--workflow",
            str(workflow),
        ],
    )

    assert MODULE.main() == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["overall"] == "UNKNOWN"
    assert receipt["read_only"] is True
    assert receipt["secret_values_requested"] is False
    assert receipt["deployment_triggered"] is False
    assert all(
        item["status"] == "UNKNOWN"
        for item in receipt["checks"]
        if item["name"] != "deploy_workflow_contract"
    )
