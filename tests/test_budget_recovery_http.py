"""A restored uncertain ledger must still block actual owner HTTP admission."""
import hashlib

from tankai.core.monetary_budget import MonetaryBudget, PriceQuote
from test_owner_source_evidence import only_loopback
from test_request_dispatch import NOW, sdks
from test_web_owner_pilot import accounts, ledger, provider_factory, calls, login, pilot, serving


def test_restored_unknown_hold_blocks_sdk_after_fresh_app_and_ledger_instances(
        accounts, ledger, provider_factory, tmp_path):
    ledger.begin_job("interrupted-owner-work", owner_id=accounts.owner)
    ledger.reserve("interrupted-owner-work", "interrupted-attempt", owner_id=accounts.owner,
        quote=PriceQuote("synthetic-price", "openai", "main-v1", NOW, NOW+86_400,
                         2000, "https://example.test/pricing", "Synthetic all-in fixture"))
    ledger.settle("interrupted-attempt", owner_id=accounts.owner, actual_micro_eur=None)
    image = ledger.export_snapshot()
    path = tmp_path/"isolated-restored-budget.sqlite"
    with MonetaryBudget.restore_new(path, data=image, expected_sha256=hashlib.sha256(image).hexdigest(),
                                     expected_policy=ledger.policy, clock=lambda:NOW) as restored:
        assert restored.snapshot() == ledger.snapshot()
    for _ in range(2):
        with MonetaryBudget(path, clock=lambda:NOW) as reopened:
            with serving(pilot(accounts, reopened)) as (_, base):
                client, csrf = login(base)
                status, _, _ = client.post("/api/run", {"goal":"do not resume uncertain restored work"}, csrf=csrf)
                assert status == 429
                assert calls(provider_factory) == 0
                assert reopened.snapshot()["held_micro_eur"] == 2000
                assert reopened.snapshot()["active_jobs"] == 1
                assert reopened.snapshot()["attempts"] == 1
