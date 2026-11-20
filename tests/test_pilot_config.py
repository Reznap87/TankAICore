"""Konfiguration des Geldpiloten: fail-closed, Grenzen, Neustart, Moditaet.

Die Tests setzen die Umgebung komplett und isoliert (``environ=``), damit kein
Test von zufaelligen ``TANKAI_PILOT_*``-Werten abhaengt.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time

import pytest

from tankai.core.monetary_budget import (
    BudgetDenied,
    DAY_SECONDS,
    MonetaryBudget,
    PilotPolicy,
    PriceQuote,
)
from tankai.web.pilot_config import _input_token_bound, load_owner_pilot_from_env
from tankai.core.request_dispatch import DispatchDenied, FrozenTextRequest, _freeze

#: Laufzeitgesteuert, weil der Ledger die echte Zeit prueft: Eine statische
#: Vergangenheitsangabe wuerde jeden Piloten sofort als abgelaufen markieren.
NOW = int(time.time())
OWNER = "00000000-0000-4000-8000-000000000001"
TENANT = "00000000-0000-4000-8000-000000000002"
WORKSPACE = "00000000-0000-4000-8000-000000000003"


def _approvals() -> str:
    return (
        '{"openai:main-v1": {"endpoint": "https://openai.example.test/v1",'
        ' "tariff": {"tariff_id": "offline-tariff-v1", "valid_from": "%d",'
        ' "valid_until": "%d", "source_url": "https://example.test/pricing",'
        ' "billing_currency": "EUR", "input_per_million": "1",'
        ' "output_per_million": "1", "request_fee": "0",'
        ' "eur_per_currency_unit": "1", "tax_rate": "0",'
        ' "surcharge_rate": "0", "fixed_surcharge_eur": "0",'
        ' "fx_source_url": null,'
        ' "all_billable_usage_and_fees_verified": true}}}'
        % (NOW, NOW + DAY_SECONDS)
    )


def _env(tmp_path: Path, **over: str) -> dict[str, str]:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    base = {
        "TANKAI_PILOT_OWNER_ID": OWNER,
        "TANKAI_PILOT_TENANT_ID": TENANT,
        "TANKAI_PILOT_WORKSPACE_IDS": WORKSPACE,
        "TANKAI_PILOT_STARTS_AT": str(NOW),
        "TANKAI_PILOT_EXPIRES_AT": str(NOW + DAY_SECONDS),
        "TANKAI_PILOT_TOTAL_MICRO_EUR": "1000000",
        "TANKAI_PILOT_DAILY_MICRO_EUR": "200000",
        "TANKAI_PILOT_LEDGER_PATH": str(data / "monetary.sqlite"),
        "TANKAI_PILOT_APPROVALS_JSON": _approvals(),
        "TANKAI_PILOT_SPEND_APPROVED": "1",
    }
    base.update(over)
    return base


def _quote(amount: int) -> PriceQuote:
    """Tarif gueltig ueber einen Tageswechsel hinweg (Gesamtgrenze-Test)."""
    return PriceQuote(
        "test-tariff-v1", "openai", "main-v1", NOW, NOW + 2 * DAY_SECONDS,
        amount, "https://example.test/pricing",
        "Deterministic test tariff only; fixed all-in euro ceiling.",
    )


def _reserve(ledger: MonetaryBudget, attempt: str, amount: int, job: str = "job") -> None:
    ledger.reserve(job, attempt, owner_id=OWNER, quote=_quote(amount))


def test_keine_pilot_umgebung_ist_fail_closed():
    assert load_owner_pilot_from_env(environ={}) is None


def test_unvollstaendige_umgebung_bleibt_fail_closed():
    env = _env(Path("/tmp"))  # nur fuer das Ledger; bleibt hier unbenutzt
    env.pop("TANKAI_PILOT_DAILY_MICRO_EUR")
    warnings: list[str] = []
    assert load_owner_pilot_from_env(environ=env, log=warnings.append) is None
    assert warnings and "TANKAI_PILOT_DAILY_MICRO_EUR" in warnings[0]



def test_explicit_spending_approval_required_even_with_valid_budget(tmp_path):
    env = _env(tmp_path)
    ledger = Path(env["TANKAI_PILOT_LEDGER_PATH"])
    env.pop("TANKAI_PILOT_SPEND_APPROVED")
    assert load_owner_pilot_from_env(environ=env) is None
    assert not ledger.exists()  # no side effect when approval is absent


def test_nonliteral_spending_approval_is_denied(tmp_path):
    env = _env(tmp_path, TANKAI_PILOT_SPEND_APPROVED="true")
    assert load_owner_pilot_from_env(environ=env) is None


def test_empty_tariff_approvals_are_denied_before_ledger_create(tmp_path):
    env = _env(tmp_path, TANKAI_PILOT_APPROVALS_JSON="{}")
    assert load_owner_pilot_from_env(environ=env) is None
    assert not Path(env["TANKAI_PILOT_LEDGER_PATH"]).exists()


def test_unpinned_source_manifest_is_denied_before_ledger_create(tmp_path):
    env = _env(tmp_path, TANKAI_PILOT_SOURCE_MANIFESTS_JSON='[{"path": "local.json"}]')
    assert load_owner_pilot_from_env(environ=env) is None
    assert not Path(env["TANKAI_PILOT_LEDGER_PATH"]).exists()


def test_token_ceiling_is_not_unsafe_character_estimate():
    req = _freeze("openai", "main-v1", "https://openai.example.test/v1", {
        "model": "main-v1", "messages": [{"role": "user", "content": "😀" * 100}],
        "max_tokens": 128, "temperature": 0,
    })
    assert _input_token_bound(req) == 12_000  # upper bound, not len(text)//4


def test_oversized_request_denied_instead_of_underreserved():
    req = _freeze("openai", "main-v1", "https://openai.example.test/v1", {
        "model": "main-v1", "messages": [{"role": "user", "content": "😀" * 5_000}],
        "max_tokens": 128, "temperature": 0,
    })
    with pytest.raises(DispatchDenied, match="offline token ceiling"):
        _input_token_bound(req)


def test_unencodable_counter_input_fails_closed():
    invalid = FrozenTextRequest("openai", "main-v1",
                                "https://openai.example.test/v1", "\\ud800")
    with pytest.raises(DispatchDenied, match="token ceiling unavailable"):
        _input_token_bound(invalid)


def test_vollstaendige_umgebung_baut_piloten(tmp_path):
    pilot = load_owner_pilot_from_env(environ=_env(tmp_path))
    assert pilot is not None
    assert pilot.tenant_id == TENANT
    assert pilot.workspace_ids == frozenset({WORKSPACE})
    assert pilot.ledger.policy.owner_id == OWNER
    assert pilot.ledger.policy.total_micro_eur == 1_000_000
    assert pilot.ledger.policy.daily_micro_eur == 200_000


def test_ohne_freigabe_gibt_es_keinen_piloten(tmp_path):
    """Fail-closed fuer Anspruch 8: keine Freigabe, kein Pilot, kein bezahlter Aufruf."""
    env = _env(tmp_path)
    env.pop("TANKAI_PILOT_APPROVALS_JSON")
    assert load_owner_pilot_from_env(environ=env) is None


def test_freigabe_ueber_nominalen_endpunkt_und_tarif(tmp_path):
    pilot = load_owner_pilot_from_env(environ=_env(tmp_path))
    assert pilot is not None
    assert pilot.approved_identities == frozenset({("openai", "main-v1")})


def test_tagesgrenze_sperrt(tmp_path):
    env = _env(tmp_path, TANKAI_PILOT_DAILY_MICRO_EUR="200000")
    pilot = load_owner_pilot_from_env(environ=env)
    assert pilot is not None
    pilot.ledger.begin_job("job", owner_id=OWNER)
    _reserve(pilot.ledger, "first", 200_000)
    # Ersten Auftrag abschliessen, damit kein aktiver Auftrag mehr blockiert.
    pilot.ledger.settle("first", owner_id=OWNER, actual_micro_eur=200_000)
    pilot.ledger.close_job("job", owner_id=OWNER)
    pilot.ledger.begin_job("job2", owner_id=OWNER)
    with pytest.raises(BudgetDenied, match="daily_budget"):
        _reserve(pilot.ledger, "second", 1, job="job2")


class _Clock:
    def __init__(self, now: int):
        self.now = now

    def __call__(self) -> int:
        return self.now


def test_gesamtgrenze_sperrt(tmp_path):
    """Die Gesamtgrenze ist nur ueber einen Tageswechsel hinweg erreichbar."""
    policy = replace(
        PilotPolicy(OWNER, NOW, NOW + 2 * DAY_SECONDS),
        total_micro_eur=150_000, daily_micro_eur=100_000, per_job_micro_eur=100_000,
    )
    clock = _Clock(NOW)
    with MonetaryBudget.create(tmp_path / "ledger.sqlite", policy, clock=clock) as ledger:
        ledger.begin_job("job", owner_id=OWNER)
        ledger.reserve("job", "first", owner_id=OWNER, quote=_quote(100_000))
        ledger.settle("first", owner_id=OWNER, actual_micro_eur=100_000)
        ledger.close_job("job", owner_id=OWNER)
        clock.now = NOW + DAY_SECONDS  # neuer Tag: Tagesgrenze zurueckgesetzt
        ledger.begin_job("job2", owner_id=OWNER)
        with pytest.raises(BudgetDenied, match="total_budget"):
            ledger.reserve("job2", "second", owner_id=OWNER, quote=_quote(100_000))


def test_ablaufdatum_sperrt(tmp_path):
    env = _env(
        tmp_path,
        TANKAI_PILOT_STARTS_AT=str(NOW - 2 * DAY_SECONDS),
        TANKAI_PILOT_EXPIRES_AT=str(NOW - DAY_SECONDS),
    )
    pilot = load_owner_pilot_from_env(environ=env)
    assert pilot is not None
    with pytest.raises(BudgetDenied, match="pilot_expired"):
        pilot.ledger.begin_job("job", owner_id=OWNER)


def test_neustart_oeffnet_ledigen_ledger_erneut(tmp_path):
    env = _env(tmp_path)
    first = load_owner_pilot_from_env(environ=env)
    assert first is not None
    first.ledger.begin_job("job", owner_id=OWNER)
    _reserve(first.ledger, "attempt", 25_000)
    held = first.ledger.snapshot()["held_micro_eur"]
    del first

    restarted = load_owner_pilot_from_env(environ=env)
    assert restarted is not None
    assert restarted.ledger.snapshot()["held_micro_eur"] == held


def test_abweichende_policy_bleibt_fail_closed(tmp_path):
    load_owner_pilot_from_env(environ=_env(tmp_path))
    changed = _env(tmp_path, TANKAI_PILOT_DAILY_MICRO_EUR="199999")
    assert load_owner_pilot_from_env(environ=changed) is None


def test_openrouter_free_und_geldpilot_sind_ausgeschlossen(tmp_path, monkeypatch):
    """Strikte Trennung: beide Betriebsmodi zusammen sind ungueltig."""
    from tankai.web.server import AppContext

    data = tmp_path / "data"
    monkeypatch.setenv("TANKAI_DATA_ROOT", str(data))
    monkeypatch.setenv("TANKAI_AUTH_DB", str(data / "auth.db"))
    monkeypatch.setenv("TANKAI_AUTH_MODE", "session")
    monkeypatch.setenv("TANKAI_EMBEDDER", "hashing")
    monkeypatch.setenv("TANKAI_OPENROUTER_FREE_ENABLED", "1")
    monkeypatch.setenv("TANKAI_LIVE_PROVIDER_ENABLED", "1")
    pilot = load_owner_pilot_from_env(environ=_env(tmp_path))
    assert pilot is not None
    with pytest.raises(RuntimeError, match="OpenRouter-Free"):
        AppContext.from_env("127.0.0.1", owner_pilot=pilot)


def test_default_policy_ist_nicht_stillerweise_gesetzt(tmp_path):
    """Die Laedertyp-Defaults gelten nur, wenn sie explizit gesetzt werden."""
    policy = replace(PilotPolicy(OWNER, NOW, NOW + DAY_SECONDS))
    assert policy.total_micro_eur == 10_000_000  # Typ-Default
    pilot = load_owner_pilot_from_env(environ=_env(tmp_path))
    assert pilot is not None
    assert pilot.ledger.policy.total_micro_eur == 1_000_000  # aus der Umgebung
