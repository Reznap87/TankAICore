"""Real session/CSRF/HTTP/Core flow against in-process SDKs and synthetic tariffs."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
import threading
import subprocess
import sys
import hashlib
import re
from datetime import timedelta
from uuid import UUID

import pytest

from tankai.core.llm import AnthropicLLM, OpenAILLM
from tankai.core.monetary_budget import MonetaryBudget, PilotPolicy
from tankai.core.request_dispatch import DispatchDenied
from tankai.web import runtime as runtime_module, server as web_server
from tankai.web.owner_pilot import OwnerPilot, OwnerPilotForbidden
from tankai.core.supplied_sources import ApprovedSourceBundle, SuppliedSource
from tankai.web.source_manifest import WorkspaceSourceManifest
from test_request_dispatch import ENDPOINTS, NOW, sdks, tariff
from test_web_server import Client, _configure


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    store = _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("TANKAI_EMBEDDER", "hashing")
    monkeypatch.setenv("TANKAI_REQUIRE_RESEARCH_EVIDENCE", "0")
    monkeypatch.setenv("TANKAI_REQUIRE_INDEPENDENT_CRITIC", "1")
    monkeypatch.setenv("TANKAI_DEV_QUEUE_ENABLED", "0")
    owner, tenant, workspace = store.create_user_with_tenant(
        email="pilot-owner@example.test", password="Synthetic-pilot-password-123",
        display_name="Pilot Owner", tenant_name="Pilot Tenant")
    session = store.authenticate(email="pilot-owner@example.test", password="Synthetic-pilot-password-123")
    second = store.create_workspace(actor=session.context, name="ZZ second approved workspace")
    other, other_tenant, other_workspace = store.create_user_with_tenant(
        email="other-owner@example.test", password="Synthetic-pilot-password-123",
        display_name="Other Owner", tenant_name="Other Tenant")
    return SimpleNamespace(store=store, owner=owner, tenant=tenant, workspace=workspace,
                           second=second.id, context=session.context, other=other,
                           other_tenant=other_tenant, other_workspace=other_workspace)


@pytest.fixture
def ledger(accounts, tmp_path):
    with MonetaryBudget.create(tmp_path / "shared-pilot.sqlite",
            PilotPolicy(accounts.owner, NOW, NOW + 86_400), clock=lambda: NOW) as result:
        yield result


def source_grant(accounts, workspace=None, *, text="Reviewed pilot fixture: value 42.", **changes):
    workspace = workspace or accounts.workspace
    source = SuppliedSource("Reviewed pilot fixture", f"https://example.org/{workspace}", text,
                            hashlib.sha256(text.encode("utf-8")).hexdigest())
    values = dict(owner_id=accounts.owner, tenant_id=accounts.tenant, workspace_id=workspace,
                  valid_from=NOW, valid_until=NOW+86_400,
                  bundle=ApprovedSourceBundle("pilot-review-001", (source,)))
    values.update(changes)
    return WorkspaceSourceManifest(**values)


def pilot(accounts, ledger, **changes):
    options = dict(tenant_id=accounts.tenant,
                   workspace_ids=frozenset([accounts.workspace, accounts.second]),
                   tariffs={key: tariff(*key) for key in ENDPOINTS}, endpoints=ENDPOINTS,
                   input_token_bound=lambda request: 1000, clock=lambda: NOW)
    options.update(changes)
    options.setdefault("source_manifests", tuple(source_grant(accounts, workspace)
                                               for workspace in options["workspace_ids"]))
    return OwnerPilot(ledger, **options)


@pytest.fixture
def provider_factory(monkeypatch, sdks):
    import test_request_dispatch as fixture_module
    original = fixture_module.answer
    def answer_with_citations(parameters):
        result = original(parameters)
        if result == "step result":
            marker = re.search(r"\[(SRC-[A-F0-9]{8})\]", parameters["messages"][-1]["content"])
            if marker:
                return result + " [" + marker.group(1) + "]"
        return result
    monkeypatch.setattr(fixture_module, "answer", answer_with_citations)
    monkeypatch.setattr(runtime_module, "get_llm", lambda: OpenAILLM(
        model="main-v1", api_key="synthetic-offline-key", max_tokens=1000))
    monkeypatch.setattr(runtime_module, "get_critic_llm", lambda default: AnthropicLLM(
        model="critic-v1", api_key="synthetic-offline-key", max_tokens=1000))
    return sdks


@contextmanager
def serving(configuration):
    app = web_server.AppContext.from_env("127.0.0.1", owner_pilot=configuration)
    server = web_server.ThreadedHTTPServer(("127.0.0.1", 0), web_server.Handler, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.close()


def login(base, email="pilot-owner@example.test"):
    client = Client(base)
    status, _, body = client.post("/api/auth/login", {
        "email": email, "password": "Synthetic-pilot-password-123"})
    assert status == 200
    return client, body["csrf_token"]


def calls(sdks):
    return sum(len(sdk.calls) for sdk in sdks)


def test_authenticated_owner_http_core_run_binds_server_job_and_preserves_holds(accounts, ledger, provider_factory):
    with serving(pilot(accounts, ledger)) as (app, base):
        client, csrf = login(base)
        status, _, body = client.post("/api/run", {"goal":"Analyse this offline fixture"}, csrf=csrf)
        assert status == 200, body
        assert calls(provider_factory) == 5
        admission = body["monetary_admission"]
        assert admission["job_id"].startswith(accounts.tenant + ":" + accounts.workspace + ":")
        assert admission["held_micro_eur"] == 10_000
        assert admission["active_jobs"] == 0
        assert ledger.snapshot()["jobs"] == 1
        deferred = [receipt for receipt in body["receipts"]
                    if receipt["action"] == "memory_consolidation_deferred"]
        assert len(deferred) == 1 and deferred[0]["details"]["additional_model_calls"] == 0
        with app.runtimes.acquire(tenant_id=accounts.tenant, workspace_id=accounts.workspace) as runtime:
            assert runtime.tank.llm_call_budget.monetary_dispatch is None
            assert runtime.tank.tools.web_research_status() == "disabled"
            saved = runtime.tank.ltm._conn.execute("SELECT final_answer FROM runs").fetchall()
            assert len(saved) == 1 and saved[0][0] == body["final_answer"]
            assert runtime.tank.ltm._conn.execute(
                "SELECT COUNT(*) FROM receipts WHERE action='memory_consolidation_deferred'"
            ).fetchone()[0] == 1
            runtime.tank.llm_call_budget.reset()
            with pytest.raises(DispatchDenied):
                runtime.tank.llm.complete("outside owner/job scope")
        assert calls(provider_factory) == 5


def test_other_owner_cannot_spend_pilot_or_create_a_runtime(accounts, ledger, provider_factory):
    with serving(pilot(accounts, ledger)) as (app, base):
        client, csrf = login(base, "other-owner@example.test")
        status, _, _ = client.post("/api/run", {"goal":"unauthorized"}, csrf=csrf)
        assert status == 403
        assert not app.runtimes._runtimes
        assert calls(provider_factory) == 0 and ledger.snapshot()["jobs"] == 0


@pytest.mark.parametrize("role", ["admin", "member"])
def test_admin_or_member_demotion_cannot_use_existing_owner_context(accounts, ledger, role):
    configured = pilot(accounts, ledger)
    accounts.store.add_user_to_workspace(user_id=accounts.owner, workspace_id=accounts.workspace, role=role)
    with pytest.raises(OwnerPilotForbidden):
        configured.authorize(accounts.context, accounts.store)
    assert ledger.snapshot()["jobs"] == 0


def test_unapproved_workspace_cannot_borrow_same_owner_budget(accounts, ledger, provider_factory):
    with serving(pilot(accounts, ledger, workspace_ids=frozenset([accounts.workspace]))) as (_, base):
        client, csrf = login(base)
        status, _, body = client.post("/api/workspaces/select", {"workspace_id":accounts.second}, csrf=csrf)
        assert status == 200
        status, _, _ = client.post("/api/run", {"goal":"not approved"}, csrf=body["csrf_token"])
        assert status == 403 and calls(provider_factory) == 0
        assert ledger.snapshot()["jobs"] == 0


def test_workspace_switch_and_app_restart_do_not_reset_shared_budget(accounts, ledger, provider_factory):
    for index, workspace in enumerate([accounts.workspace, accounts.second]):
        with serving(pilot(accounts, ledger)) as (_, base):
            client, csrf = login(base)
            status, _, chosen = client.post("/api/workspaces/select", {"workspace_id":workspace}, csrf=csrf)
            assert status == 200
            status, _, body = client.post("/api/run", {"goal":"bounded offline fixture"}, csrf=chosen["csrf_token"])
            assert status == 200, body
            assert body["monetary_admission"]["held_micro_eur"] == (index + 1) * 10_000
            assert body["monetary_admission"]["job_id"].startswith(accounts.tenant + ":" + workspace + ":")
    with MonetaryBudget(ledger.path, clock=lambda: NOW) as reopened:
        assert reopened.snapshot()["held_micro_eur"] == 20_000
        assert reopened.snapshot()["jobs"] == 2
        assert reopened.snapshot()["attempts"] == 10
    assert calls(provider_factory) == 10


@pytest.mark.parametrize("override", [
    {"job_id":"forged"}, {"owner_id":"forged"}, {"budget":1000000},
    {"tariff":"free"}, {"max_tokens":999999}, {"parallel":True},
    {"supplied_sources":{"approval_id":"browser-forgery", "sources":[]}},
])
def test_browser_cannot_choose_identity_prices_limits_or_parallelism(accounts, ledger, provider_factory, override):
    with serving(pilot(accounts, ledger)) as (_, base):
        client, csrf = login(base)
        status, _, _ = client.post("/api/run", {"goal":"task", **override}, csrf=csrf)
        assert status == 400
        assert calls(provider_factory) == 0 and ledger.snapshot()["jobs"] == 0


def test_csrf_is_still_required_before_admission(accounts, ledger, provider_factory):
    with serving(pilot(accounts, ledger)) as (_, base):
        client, _ = login(base)
        status, _, _ = client.post("/api/run", {"goal":"task"})
        assert status == 403
        assert calls(provider_factory) == 0 and ledger.snapshot()["jobs"] == 0


def test_second_workspace_cannot_start_while_global_job_is_active(accounts, ledger, provider_factory):
    configured = pilot(accounts, ledger)
    with serving(configured) as (app, base):
        client, csrf = login(base)
        status, _, chosen = client.post("/api/workspaces/select", {"workspace_id":accounts.second}, csrf=csrf)
        assert status == 200
        with app.runtimes.acquire(tenant_id=accounts.tenant, workspace_id=accounts.workspace) as active:
            with configured.scope(accounts.context, accounts.store, active.tank):
                status, _, _ = client.post("/api/run", {"goal":"second job"}, csrf=chosen["csrf_token"])
                assert status == 429
                assert ledger.snapshot()["active_jobs"] == 1
        assert calls(provider_factory) == 0


def test_timeout_keeps_active_job_slot_and_cost_after_app_restart(accounts, ledger, provider_factory):
    with serving(pilot(accounts, ledger)) as (app, base):
        client, csrf = login(base)
        with app.runtimes.acquire(tenant_id=accounts.tenant, workspace_id=accounts.workspace) as runtime:
            runtime.tank.llm.delegate.client.error = TimeoutError("synthetic upstream timeout")
        client.post("/api/run", {"goal":"request that times out"}, csrf=csrf)
        assert calls(provider_factory) == 1
        assert ledger.snapshot()["active_jobs"] == 1
        assert ledger.snapshot()["held_micro_eur"] == 2000
    with MonetaryBudget(ledger.path, clock=lambda: NOW) as reopened:
        with serving(pilot(accounts, reopened)) as (_, base):
            client, csrf = login(base)
            status, _, _ = client.post("/api/run", {"goal":"do not overlap unknown work"}, csrf=csrf)
            assert status == 429
            assert calls(provider_factory) == 1


def test_session_authentication_cannot_be_disabled_for_pilot(accounts, ledger, monkeypatch):
    monkeypatch.setenv("TANKAI_AUTH_MODE", "disabled")
    with pytest.raises(RuntimeError, match="Session"):
        web_server.AppContext.from_env("127.0.0.1", owner_pilot=pilot(accounts, ledger))


def test_development_queue_is_not_an_alternate_unbudgeted_path(accounts, ledger, monkeypatch):
    monkeypatch.setenv("TANKAI_DEV_QUEUE_ENABLED", "1")
    with pytest.raises(RuntimeError, match="Development-Queue"):
        web_server.AppContext.from_env("127.0.0.1", owner_pilot=pilot(accounts, ledger))


def test_unknown_counter_fails_closed_over_http_without_freeing_job_quota(accounts, ledger, provider_factory):
    configured = pilot(accounts, ledger, input_token_bound=lambda request: None)
    with serving(configured) as (_, base):
        client, csrf = login(base)
        status, _, body = client.post("/api/run", {"goal":"cannot be safely counted"}, csrf=csrf)
        assert status == 503
        assert set(body) == {"error"}
        assert calls(provider_factory) == 0
        assert ledger.snapshot()["jobs"] == 1
        assert ledger.snapshot()["active_jobs"] == 0
        assert ledger.snapshot()["attempts"] == 0


def test_expired_pilot_is_not_renewed_by_another_http_request(accounts, ledger, provider_factory, monkeypatch):
    monkeypatch.setattr(ledger, "_clock", lambda: NOW + 86_400)
    with serving(pilot(accounts, ledger)) as (_, base):
        client, csrf = login(base)
        status, _, _ = client.post("/api/run", {"goal":"expired pilot"}, csrf=csrf)
        assert status == 429
        assert calls(provider_factory) == 0 and ledger.snapshot()["jobs"] == 0


def test_pilot_does_not_relax_evidence_requirement_or_enable_external_search(accounts, ledger, provider_factory, monkeypatch):
    monkeypatch.setenv("TANKAI_REQUIRE_RESEARCH_EVIDENCE", "1")
    with serving(pilot(accounts, ledger)) as (app, _):
        with app.runtimes.acquire(tenant_id=accounts.tenant, workspace_id=accounts.workspace) as runtime:
            assert runtime.tank.require_research_evidence is True
            assert runtime.tank.tools.web_research_status() == "disabled"
        assert calls(provider_factory) == 0


def test_session_revocation_before_job_admission_rejects_cached_context(accounts, ledger):
    configured = pilot(accounts, ledger)
    accounts.store.revoke_session(accounts.context.session_id, user_id=accounts.owner)
    with pytest.raises(OwnerPilotForbidden):
        configured.authorize(accounts.context, accounts.store)
    assert ledger.snapshot()["jobs"] == 0


def test_workspace_switch_before_job_admission_rejects_stale_context(accounts, ledger):
    configured = pilot(accounts, ledger)
    updated = accounts.store.select_workspace(accounts.context, accounts.second)
    with pytest.raises(OwnerPilotForbidden):
        configured.authorize(accounts.context, accounts.store)
    configured.authorize(updated, accounts.store)
    assert ledger.snapshot()["jobs"] == 0


def test_revocation_during_run_blocks_next_model_attempt(accounts, ledger, provider_factory, monkeypatch):
    with serving(pilot(accounts, ledger)) as (app, base):
        client, csrf = login(base)
        cookie = next(cookie for cookie in client.jar if cookie.name == "tankai_session")
        context = app.auth.resolve_session(cookie.value)
        with app.runtimes.acquire(tenant_id=accounts.tenant, workspace_id=accounts.workspace) as runtime:
            sdk_type = type(runtime.tank.llm.delegate.client)
        original_create = sdk_type.create

        def create_then_revoke(sdk, **parameters):
            result = original_create(sdk, **parameters)
            app.auth.revoke_session(context.session_id, user_id=accounts.owner)
            return result

        monkeypatch.setattr(sdk_type, "create", create_then_revoke)
        status, _, _ = client.post("/api/run", {"goal":"stop after revocation"}, csrf=csrf)
        assert status == 403
        assert calls(provider_factory) == 1
        assert ledger.snapshot()["attempts"] == 1
        assert ledger.snapshot()["held_micro_eur"] == 2000


def test_missing_critic_tariff_blocks_before_spending_on_main_model(accounts, ledger, provider_factory):
    identity = ("openai", "main-v1")
    configured = pilot(accounts, ledger, tariffs={identity:tariff(*identity)},
                       endpoints={identity:ENDPOINTS[identity]})
    with serving(configured) as (_, base):
        client, csrf = login(base)
        status, _, _ = client.post("/api/run", {"goal":"incomplete pricing"}, csrf=csrf)
        assert status == 503
        assert calls(provider_factory) == 0 and ledger.snapshot()["jobs"] == 0


def test_pilot_always_requires_existing_independent_critic_contract(accounts, ledger, provider_factory, monkeypatch):
    monkeypatch.setenv("TANKAI_REQUIRE_INDEPENDENT_CRITIC", "0")
    with serving(pilot(accounts, ledger)) as (app, _):
        with app.runtimes.acquire(tenant_id=accounts.tenant, workspace_id=accounts.workspace) as runtime:
            assert runtime.tank.require_independent_critic is True
            assert runtime.tank.critic_independent is True


def test_expired_session_cannot_admit_a_job(accounts, ledger, monkeypatch):
    from tankai.web import auth as auth_module
    monkeypatch.setattr(auth_module, "_utcnow", lambda: accounts.context.expires_at + timedelta(seconds=1))
    with pytest.raises(OwnerPilotForbidden):
        pilot(accounts, ledger).authorize(accounts.context, accounts.store)
    assert ledger.snapshot()["jobs"] == 0


def test_another_users_session_cannot_be_combined_with_owner_metadata(accounts, ledger):
    other_session = accounts.store.authenticate(email="other-owner@example.test", password="Synthetic-pilot-password-123")
    forged = replace(accounts.context, session_id=other_session.context.session_id)
    with pytest.raises(OwnerPilotForbidden):
        pilot(accounts, ledger).authorize(forged, accounts.store)
    assert ledger.snapshot()["jobs"] == 0


def test_revocation_during_offline_counting_blocks_before_money_reservation(accounts, ledger, provider_factory):
    with serving(pilot(accounts, ledger)) as (app, base):
        client, csrf = login(base)
        cookie = next(cookie for cookie in client.jar if cookie.name == "tankai_session")
        context = app.auth.resolve_session(cookie.value)
        def revoke_while_counting(request):
            app.auth.revoke_session(context.session_id, user_id=accounts.owner)
            return 1000
        app.owner_pilot._counter = revoke_while_counting
        status, _, _ = client.post("/api/run", {"goal":"authorization changed during counting"}, csrf=csrf)
        assert status == 403
        assert calls(provider_factory) == 0
        assert ledger.snapshot()["attempts"] == 0


def test_revocation_after_commit_blocks_sdk_and_conservatively_retains_job(accounts, ledger, provider_factory, monkeypatch):
    with serving(pilot(accounts, ledger)) as (app, base):
        client, csrf = login(base)
        cookie = next(cookie for cookie in client.jar if cookie.name == "tankai_session")
        context = app.auth.resolve_session(cookie.value)
        original = ledger.reserve
        def commit_then_revoke(*args, **kwargs):
            reservation = original(*args, **kwargs)
            app.auth.revoke_session(context.session_id, user_id=accounts.owner)
            return reservation
        monkeypatch.setattr(ledger, "reserve", commit_then_revoke)
        status, _, _ = client.post("/api/run", {"goal":"do not send after revocation"}, csrf=csrf)
        assert status == 403
        assert calls(provider_factory) == 0
        assert ledger.snapshot()["held_micro_eur"] == 2000
        assert ledger.snapshot()["active_jobs"] == 1
        assert ledger.snapshot()["unresolved_attempts"] == 1
    with MonetaryBudget(ledger.path, clock=lambda: NOW) as reopened:
        assert reopened.snapshot()["active_jobs"] == 1


def test_process_exit_inside_admitted_send_blocks_next_http_job_after_restart(accounts, ledger, provider_factory):
    code = r'''
import os, sys
from types import SimpleNamespace
from tankai import TankAI
from tankai.core.llm import OpenAILLM, AnthropicLLM
from tankai.core.monetary_budget import MonetaryBudget
from tankai.core.request_pricing import TextTariff
from tankai.web.auth import AuthStore
from tankai.web.owner_pilot import OwnerPilot
from tankai.core.supplied_sources import SuppliedSource, ApprovedSourceBundle
from tankai.web.source_manifest import WorkspaceSourceManifest
import hashlib
now=1790000000
path, auth_path, session_id, owner, tenant, workspace=sys.argv[1:]
ledger=MonetaryBudget(path, clock=lambda: now)
auth=AuthStore(auth_path)
context=auth.resolve_session_by_id(session_id)
endpoints={("openai","main-v1"):"https://openai.example.test/v1",
           ("anthropic","critic-v1"):"https://anthropic.example.test"}
tariffs={key:TextTariff("offline-tariff-v1",*key,now,now+86400,
    "https://example.test/pricing","EUR","1","1","0","1","0","0","0",None,True)
    for key in endpoints}
source=SuppliedSource("Offline fixture","https://example.org/fixture","offline",
    hashlib.sha256(b"offline").hexdigest())
grant=WorkspaceSourceManifest(owner,tenant,workspace,now,now+86400,ApprovedSourceBundle("review",(source,)))
pilot=OwnerPilot(ledger,tenant_id=tenant,workspace_ids=frozenset([workspace]),
    tariffs=tariffs,endpoints=endpoints,input_token_bound=lambda request:1000,clock=lambda:now,
    source_manifests=(grant,))
adapters=[]
for cls, key in ((OpenAILLM,("openai","main-v1")),(AnthropicLLM,("anthropic","critic-v1"))):
    adapter=object.__new__(cls)
    adapter.model=adapter.model_name=key[1]
    adapter.client=SimpleNamespace(base_url=endpoints[key])
    adapters.append(adapter)
tank=TankAI(llm=adapters[0],critic_llm=adapters[1],run_store_path=None,verbose=False,
    enable_tools=False,max_retries=0,verified_single_step=True,max_llm_calls_per_run=5)
with pilot.scope(context,auth,tank):
    tank.llm_call_budget.monetary_dispatch.execute(attempt_id="crashed-attempt",
        provider="openai",model="main-v1",endpoint=endpoints[("openai","main-v1")],
        parameters={"model":"main-v1","messages":[{"role":"user","content":"offline"}],
                    "max_tokens":1000,"temperature":0.2},sdk_max_retries=0,timeout_seconds=30,
        send_once=lambda parameters:os._exit(23))
'''
    completed = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(ledger.path),
        str(accounts.store.path), accounts.context.session_id, accounts.owner, accounts.tenant,
        accounts.workspace], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 23, completed.stderr
    with MonetaryBudget(ledger.path, clock=lambda: NOW) as reopened:
        assert reopened.snapshot()["active_jobs"] == 1
        assert reopened.snapshot()["held_micro_eur"] == 2000
        assert reopened.snapshot()["unresolved_attempts"] == 1
        with serving(pilot(accounts, reopened)) as (_, base):
            client, csrf = login(base)
            status, _, _ = client.post("/api/run", {"goal":"do not overlap crashed work"}, csrf=csrf)
            assert status == 429
            assert calls(provider_factory) == 0


def test_pilot_rejects_nonlocal_embedding_before_constructing_it(accounts, ledger, monkeypatch):
    monkeypatch.setenv("TANKAI_EMBEDDER", "unsupported-external-embedder")
    constructed = []
    monkeypatch.setattr(runtime_module, "LongTermMemory", lambda **kwargs: constructed.append(kwargs))
    with serving(pilot(accounts, ledger)) as (app, _):
        with pytest.raises(DispatchDenied, match="hashing"):
            app.runtimes.get(tenant_id=accounts.tenant, workspace_id=accounts.workspace)
    assert not constructed


def test_repeated_server_nonce_cannot_resume_unknown_job_after_restart(accounts, ledger, provider_factory, monkeypatch):
    from tankai.web import owner_pilot as owner_module
    monkeypatch.setattr(owner_module, "uuid4", lambda: UUID(int=42))
    with serving(pilot(accounts, ledger)) as (app, base):
        client, csrf = login(base)
        with app.runtimes.acquire(tenant_id=accounts.tenant, workspace_id=accounts.workspace) as runtime:
            runtime.tank.llm.delegate.client.error = TimeoutError("synthetic uncertain work")
        client.post("/api/run", {"goal":"uncertain attempt"}, csrf=csrf)
        assert ledger.snapshot()["active_jobs"] == 1
        assert calls(provider_factory) == 1
    with MonetaryBudget(ledger.path, clock=lambda: NOW) as reopened:
        with serving(pilot(accounts, reopened)) as (_, base):
            client, csrf = login(base)
            status, _, _ = client.post("/api/run", {"goal":"never resume with a repeated nonce"}, csrf=csrf)
            assert status == 503
            assert calls(provider_factory) == 1
            assert reopened.snapshot()["attempts"] == 1
            assert reopened.snapshot()["held_micro_eur"] == 2000
