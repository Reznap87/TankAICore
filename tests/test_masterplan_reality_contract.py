from pathlib import Path


MASTERPLAN = Path(__file__).resolve().parents[1] / "TANKAI_MASTERPLAN.md"


def test_current_reality_contract_tracks_repository_state_without_reopening_completed_work(
) -> None:
    text = MASTERPLAN.read_text(encoding="utf-8")
    current, separator, _history = text.partition("\n1. Ergebnis, das entstehen muss")

    assert separator
    assert "Version: 5.7.10" in current
    assert "Statusdatum: 10. September 2026" in current
    assert "e1548701a6acce5d71cc7246deec0d88ce985d83" in current
    assert "aae4b0ae2177de051c61fca74dfdfc82389c46b7" in current
    assert "kein offener Pull Request" in current
    assert "TankAI Core CI Run #70" in current
    assert "TankAI Core CI Run #71" in current
    assert "TankAI Core CI Run #72" in current
    assert "TankAI Core CI Run #73" in current
    assert "TankAI Core CI Run #74" in current
    assert "TankAI Core CI Run #75" in current
    assert "TankAI Core CI Run #76" in current
    assert "TankAI Core CI Run #77" in current
    assert "TankAI Core CI Run #69" in current
    assert "PR #39" in current
    assert "PR #42" in current
    assert (
        "development.external_agent_gateway.v1 -> IMPLEMENTED UND CI-VERIFIZIERT"
        in current
    )
    assert (
        "development.external_agent_operator_cli -> IMPLEMENTED UND CI-VERIFIZIERT"
        in current
    )
    assert (
        "development.external_agent_job_schema.v1 -> IMPLEMENTED UND CI-VERIFIZIERT"
        in current
    )
    assert "development.external_agent_validation_errors.v1 -> IMPLEMENTED" in current
    assert "development.external_agent_job_preflight.v1 -> IMPLEMENTED" in current
    assert "development.external_agent_job_history.v1 -> IMPLEMENTED" in current
    assert "ops.ci.node24_action_runtime -> IMPLEMENTED" in current
    assert "development.local_qwen25_coder_runtime -> IMPLEMENTED" in current
    assert "development.local_qwen25_coder_model_integrity -> IMPLEMENTED" in current
    assert (
        "Die repositoryseitigen Vorarbeiten dafür sind durch PRs #26 bis #29 abgeschlossen"
        in current
    )
    assert "Der Rest dieses Gates ist deshalb EXTERN BLOCKIERT" in current
    assert "single_host_runner_bootstrap.readonly_doctor -> IMPLEMENTED" in current
    assert (
        "single_host_runner_bootstrap.configuration_readiness -> IMPLEMENTED"
        in current
    )
    assert "Der konkrete Host und" in current
    assert "bleiben OFFEN" in current
    assert "Für ops.production.live_provider_readiness einen read-only" not in current
