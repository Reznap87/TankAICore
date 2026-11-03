"""A response-limit rejection keeps the owner pilot stopped across app restart."""
import json

from tankai.core.monetary_budget import MonetaryBudget
from test_web_owner_pilot import accounts, ledger, provider_factory, pilot, serving, login, calls
from test_request_dispatch import NOW, sdks


def test_oversized_provider_output_cannot_free_the_http_job_on_restart(
    accounts, ledger, provider_factory, monkeypatch,
):
    import test_request_dispatch as sdk_fixture

    oversized = "x" * 128_001
    monkeypatch.setattr(sdk_fixture, "answer", lambda parameters: oversized)
    with serving(pilot(accounts, ledger)) as (_, base):
        client, csrf = login(base)
        status, _, body = client.post("/api/run", {"goal":"response limit fixture"}, csrf=csrf)
        assert status == 503
        assert oversized not in str(body)
        assert len(json.dumps(body).encode("utf-8")) < 128_000
        assert calls(provider_factory) == 1
        assert ledger.snapshot()["active_jobs"] == 1
        assert ledger.snapshot()["held_micro_eur"] == 2000
    with MonetaryBudget(ledger.path, clock=lambda: NOW) as reopened:
        with serving(pilot(accounts, reopened)) as (_, base):
            client, csrf = login(base)
            status, _, _ = client.post("/api/run", {"goal":"must stay stopped"}, csrf=csrf)
            assert status == 429
            assert calls(provider_factory) == 1
