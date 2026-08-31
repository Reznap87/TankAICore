from pathlib import Path


MASTERPLAN = Path(__file__).resolve().parents[1] / "TANKAI_MASTERPLAN.md"


def test_current_reality_contract_tracks_repository_state_without_reopening_completed_work(
) -> None:
    text = MASTERPLAN.read_text(encoding="utf-8")
    current, separator, _history = text.partition("\n1. Ergebnis, das entstehen muss")

    assert separator
    assert "Version: 5.7.0" in current
    assert "Statusdatum: 31. August 2026" in current
    assert "e43baadf3675af21d82aacde814f9fedf3739cf8" in current
    assert "1af824bb7b7aaa73bc893037179dd720023c8001" in current
    assert "TankAI Core CI Run #53" in current
    assert (
        "development.external_agent_gateway.v1 -> IMPLEMENTED UND CI-VERIFIZIERT"
        in current
    )
    assert (
        "development.external_agent_operator_cli -> IMPLEMENTED UND CI-VERIFIZIERT"
        in current
    )
    assert (
        "Die repositoryseitigen Vorarbeiten dafür sind durch PRs #26 bis #29 abgeschlossen"
        in current
    )
    assert "Der Rest dieses Gates ist deshalb EXTERN BLOCKIERT" in current
    assert "ops.development.single_host_runner_bootstrap BEREIT" in current
    assert "Für ops.production.live_provider_readiness einen read-only" not in current
