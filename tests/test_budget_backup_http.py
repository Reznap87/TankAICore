"""Encrypted recovery retains the unknown-job barrier at actual HTTP admission."""
import secrets

from tankai.core.budget_backup import create_backup, restore_backup
from tankai.core.monetary_budget import MonetaryBudget, PriceQuote
from test_owner_source_evidence import only_loopback
from test_request_dispatch import NOW, sdks
from test_web_owner_pilot import accounts, ledger, provider_factory, calls, login, pilot, serving


def test_encrypted_recovered_hold_blocks_new_owner_app_sends(accounts, ledger, provider_factory, tmp_path):
    ledger.begin_job("uncertain",owner_id=accounts.owner)
    ledger.reserve("uncertain","held",owner_id=accounts.owner,
        quote=PriceQuote("synthetic","openai","main-v1",NOW,NOW+86_400,2000,
                         "https://example.test/pricing","synthetic all-in ceiling"))
    ledger.settle("held",owner_id=accounts.owner,actual_micro_eur=None)
    key = secrets.token_bytes(32)
    path = tmp_path/"private.enc"
    receipt = create_backup(ledger,path,key)
    recovered_path = tmp_path/"recovered.sqlite"
    restore_backup(path,recovered_path,key,expected_sha256=receipt["encrypted_sha256"],
                   expected_policy=ledger.policy,clock=lambda:NOW)
    for _ in range(2):
        with MonetaryBudget(recovered_path,clock=lambda:NOW) as recovered:
            assert recovered.snapshot() == ledger.snapshot()
            with serving(pilot(accounts,recovered)) as (_,base):
                client,csrf = login(base)
                status,_,_ = client.post("/api/run",{"goal":"do not unlock restored uncertain work"},csrf=csrf)
                assert status == 429 and calls(provider_factory) == 0
                assert recovered.snapshot()["active_jobs"] == 1
                assert recovered.snapshot()["held_micro_eur"] == 2000
