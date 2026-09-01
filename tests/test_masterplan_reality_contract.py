from pathlib import Path


MASTERPLAN = Path(__file__).resolve().parents[1] / "TANKAI_MASTERPLAN.md"


def test_current_reality_contract_tracks_repository_state_without_reopening_completed_work(
) -> None:
    text = MASTERPLAN.read_text(encoding="utf-8")
    current, separator, _history = text.partition("\n1. Ergebnis, das entstehen muss")

    assert separator
    assert "Version: 5.7.1" in current
    assert "Statusdatum: 1. September 2026" in current
    assert "48ce98729d33254a21e484c8a69f0a1661d64458" in current
    assert "756abe0dfb68c4b48db57e7a0c757ad6bff17013" in current
    assert "TankAI Core CI Run #55" in current
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
    assert "single_host_runner_bootstrap.readonly_doctor -> IMPLEMENTED" in current
    assert "Der konkrete Host bleibt OFFEN" in current
    assert "Für ops.production.live_provider_readiness einen read-only" not in current
