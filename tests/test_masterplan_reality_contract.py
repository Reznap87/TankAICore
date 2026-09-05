from pathlib import Path


MASTERPLAN = Path(__file__).resolve().parents[1] / "TANKAI_MASTERPLAN.md"


def test_current_reality_contract_tracks_repository_state_without_reopening_completed_work(
) -> None:
    text = MASTERPLAN.read_text(encoding="utf-8")
    current, separator, _history = text.partition("\n1. Ergebnis, das entstehen muss")

    assert separator
    assert "Version: 5.7.5" in current
    assert "Statusdatum: 5. September 2026" in current
    assert "62e8addc852d2178f7ec415b9c6184f3bef92a04" in current
    assert "f43f1bed75eb58eb46be05e4ad833bbb1a1df002" in current
    assert "TankAI Core CI Run #64" in current
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
    assert (
        "Die repositoryseitigen Vorarbeiten dafür sind durch PRs #26 bis #29 abgeschlossen"
        in current
    )
    assert "Der Rest dieses Gates ist deshalb EXTERN BLOCKIERT" in current
    assert "single_host_runner_bootstrap.readonly_doctor -> IMPLEMENTED" in current
    assert "Der konkrete Host bleibt OFFEN" in current
    assert "Für ops.production.live_provider_readiness einen read-only" not in current
