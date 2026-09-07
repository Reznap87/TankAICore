from pathlib import Path


MASTERPLAN = Path(__file__).resolve().parents[1] / "TANKAI_MASTERPLAN.md"


def test_current_reality_contract_tracks_repository_state_without_reopening_completed_work(
) -> None:
    text = MASTERPLAN.read_text(encoding="utf-8")
    current, separator, _history = text.partition("\n1. Ergebnis, das entstehen muss")

    assert separator
    assert "Version: 5.7.7" in current
    assert "Statusdatum: 7. September 2026" in current
    assert "357e8bb51d0b5be0f285a8699916746f321b289a" in current
    assert "a18ecc459d4bf47380ca3b8312f00c335e13051e" in current
    assert "TankAI Core CI Run #68" in current
    assert "TankAI Core CI Run #69" in current
    assert "PR #39" in current
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
    assert "ops.ci.node24_action_runtime -> IMPLEMENTED" in current
    assert "development.local_qwen25_coder_runtime -> IMPLEMENTED" in current
    assert (
        "Die repositoryseitigen Vorarbeiten dafür sind durch PRs #26 bis #29 abgeschlossen"
        in current
    )
    assert "Der Rest dieses Gates ist deshalb EXTERN BLOCKIERT" in current
    assert "single_host_runner_bootstrap.readonly_doctor -> IMPLEMENTED" in current
    assert "Der konkrete Host bleibt OFFEN" in current
    assert "Für ops.production.live_provider_readiness einen read-only" not in current
