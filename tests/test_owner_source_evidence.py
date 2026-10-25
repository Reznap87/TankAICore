"""Pinned synthetic manifests through actual owner/session/HTTP/Core admission.

Only the loopback test server may be reached. SDK endpoints remain in-process
test doubles; no credentials, public documents or live provider requests.
"""
from dataclasses import asdict, replace
import hashlib
import ipaddress
import json
import socket

import pytest

from tankai.core.monetary_budget import MonetaryBudget
from tankai.core.request_dispatch import DispatchDenied
from tankai.web.source_manifest import load_source_manifest
from test_request_dispatch import NOW, sdks
from test_web_owner_pilot import (
    accounts, calls, ledger, login, pilot, provider_factory, serving, source_grant,
)


@pytest.fixture(autouse=True)
def only_loopback(monkeypatch):
    resolve, connect, connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex
    def check(address):
        try:
            allowed = ipaddress.ip_address(address[0] if isinstance(address, tuple) else address).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            pytest.fail("source pilot tests may only reach their loopback server")
    def local_resolve(host, *args, **kwargs):
        check(host)
        return resolve(host, *args, **kwargs)
    def local_connect(sock, address):
        check(address)
        return connect(sock, address)
    def local_connect_ex(sock, address):
        check(address)
        return connect_ex(sock, address)
    monkeypatch.setattr(socket, "getaddrinfo", local_resolve)
    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", local_connect_ex)


def test_pinned_manifest_reaches_specialist_and_independent_critic_with_provenance(
        accounts, ledger, provider_factory, tmp_path):
    grant = source_grant(accounts)
    data = {key:getattr(grant, key) for key in
            ("owner_id", "tenant_id", "workspace_id", "valid_from", "valid_until")}
    data.update(schema_version=1, approval_id=grant.bundle.approval_id,
                sources=[asdict(source) for source in grant.bundle.sources])
    raw = json.dumps(data).encode()
    path = tmp_path / "approved-synthetic-source.json"
    path.write_bytes(raw)
    loaded = load_source_manifest(path, expected_sha256=hashlib.sha256(raw).hexdigest())
    configured = pilot(accounts, ledger, source_manifests=(loaded,))
    with serving(configured) as (app, base):
        client, csrf = login(base)
        status, _, body = client.post("/api/run", {"goal":"Review the approved fixture"}, csrf=csrf)
        assert status == 200, body
        assert body["verification_passed"] and body["release_ready"]
        assert body["source_ids"] == [grant.bundle.sources[0].source_id]
        assert body["source_approval_id"] == grant.bundle.approval_id
        assert body["evidence_mode"] == "supplied" and body["web_research"] == "disabled"
        assert "kein aktueller Webabruf" in body["final_answer"]
        source_receipts = [r for r in body["receipts"] if r["action"]=="execute_step:analysis"]
        assert len(source_receipts) == 1
        record = source_receipts[0]["details"]["sources"][0]
        assert record["content_sha256"] == grant.bundle.sources[0].sha256 and record["fetched"] is False
        assert calls(provider_factory) == 5 and ledger.snapshot()["held_micro_eur"] == 10_000
        main, critic = provider_factory
        text = grant.bundle.sources[0].text
        assert sum(text in p["messages"][-1]["content"] for p in main.calls) == 1
        assert sum(text in p["messages"][-1]["content"] for p in critic.calls) == 2
        with app.runtimes.acquire(tenant_id=accounts.tenant, workspace_id=accounts.workspace) as runtime:
            assert runtime.tank.require_research_evidence is True  # env explicitly says 0
            assert runtime.tank.tools.web_research_status() == "disabled"
            assert runtime.tank._supplied_sources is None


@pytest.mark.parametrize("kind", ["missing", "another-workspace", "expired", "future"])
def test_missing_or_inactive_workspace_grant_blocks_before_runtime_job_or_sdk(
        accounts, ledger, provider_factory, kind):
    grant = source_grant(accounts)
    options = {}
    manifests = (grant,)
    if kind == "missing": manifests = ()
    if kind == "another-workspace": manifests = (source_grant(accounts, accounts.second),)
    if kind == "expired":
        manifests = (replace(grant, valid_until=NOW+1),)
        options["clock"] = lambda: NOW+1
    if kind == "future": manifests = (replace(grant, valid_from=NOW+1),)
    with serving(pilot(accounts, ledger, source_manifests=manifests, **options)) as (app, base):
        client, csrf = login(base)
        status, _, _ = client.post("/api/run", {"goal":"blocked source fixture"}, csrf=csrf)
        assert status == 403
        assert not app.runtimes._runtimes
        assert calls(provider_factory) == 0 and ledger.snapshot()["jobs"] == 0


@pytest.mark.parametrize("kind", ["owner", "tenant", "workspace", "duplicate", "list", "before", "after"])
def test_grant_configuration_must_match_pilot_identity_workspaces_and_lifetime(accounts, ledger, kind):
    grant = source_grant(accounts)
    values = {
        "owner": (replace(grant, owner_id=accounts.other),),
        "tenant": (replace(grant, tenant_id=accounts.other_tenant),),
        "workspace": (replace(grant, workspace_id=accounts.other_workspace),),
        "duplicate": (grant, grant), "list": [grant],
        "before": (replace(grant, valid_from=NOW-1),),
        "after": (replace(grant, valid_until=NOW+86_401),),
    }
    with pytest.raises(DispatchDenied):
        pilot(accounts, ledger, source_manifests=values[kind])
    assert ledger.snapshot()["jobs"] == 0 and ledger.snapshot()["attempts"] == 0


@pytest.mark.parametrize("kind", ["exception", "boolean", "float"])
def test_source_clock_failure_is_safe_service_unavailable(accounts, ledger, provider_factory, kind):
    def clock():
        if kind == "exception": raise RuntimeError("synthetic clock failure")
        return True if kind == "boolean" else float(NOW)
    with serving(pilot(accounts, ledger, clock=clock)) as (app, base):
        client, csrf = login(base)
        status, _, body = client.post("/api/run", {"goal":"clock fixture"}, csrf=csrf)
        assert status == 503 and "clock failure" not in str(body)
        assert not app.runtimes._runtimes
        assert calls(provider_factory) == 0 and ledger.snapshot()["jobs"] == 0


@pytest.mark.parametrize("phase,successful_responses", [
    ("after-response", 1), ("after-response", 3), ("after-response", 4),
    ("during-count", 0), ("after-commit", 0),
])
def test_grant_expiry_between_admission_and_send_never_authorizes_another_sdk_call(
        accounts, ledger, provider_factory, monkeypatch, phase, successful_responses):
    now = [NOW]
    configured = pilot(accounts, ledger, clock=lambda:now[0], source_manifests=(
        replace(source_grant(accounts), valid_until=NOW+1),))
    with serving(configured) as (app, base):
        client, csrf = login(base)
        if phase == "after-response":
            with app.runtimes.acquire(tenant_id=accounts.tenant, workspace_id=accounts.workspace) as runtime:
                sdk_type = type(runtime.tank.llm.delegate.client)
            original_create = sdk_type.create
            def create_then_expire(sdk, **parameters):
                result = original_create(sdk, **parameters)
                if calls(provider_factory) == successful_responses:
                    now[0] = NOW+1
                return result
            monkeypatch.setattr(sdk_type, "create", create_then_expire)
        elif phase == "during-count":
            def count_then_expire(request):
                now[0] = NOW+1
                return 1000
            configured._counter = count_then_expire
        else:
            original_reserve = ledger.reserve
            def reserve_then_expire(*args, **kwargs):
                result = original_reserve(*args, **kwargs)
                now[0] = NOW+1
                return result
            monkeypatch.setattr(ledger, "reserve", reserve_then_expire)
        status, _, _ = client.post("/api/run", {"goal":"stop at source expiry"}, csrf=csrf)
        assert status == 403
        assert calls(provider_factory) == successful_responses
        snapshot = ledger.snapshot()
        attempts = 0 if phase=="during-count" else max(1, successful_responses)
        assert snapshot["attempts"] == attempts
        assert snapshot["held_micro_eur"] == attempts * 2000
        assert snapshot["active_jobs"] == (1 if phase=="after-commit" else 0)
    with MonetaryBudget(ledger.path, clock=lambda:NOW) as reopened:
        assert reopened.snapshot()["held_micro_eur"] == snapshot["held_micro_eur"]
        assert reopened.snapshot()["active_jobs"] == snapshot["active_jobs"]


def test_workspace_switch_cannot_borrow_previous_source_snapshot(accounts, ledger, provider_factory):
    first = source_grant(accounts, text="Reviewed first workspace document.")
    second = source_grant(accounts, accounts.second, text="Reviewed second workspace document.")
    with serving(pilot(accounts, ledger, source_manifests=(first, second))) as (_, base):
        client, csrf = login(base)
        for grant, absent in ((first, second), (second, first)):
            _, _, selected = client.post("/api/workspaces/select", {"workspace_id":grant.workspace_id}, csrf=csrf)
            csrf = selected["csrf_token"]
            start = {id(sdk):len(sdk.calls) for sdk in provider_factory}
            status, _, body = client.post("/api/run", {"goal":"review workspace"}, csrf=csrf)
            assert status == 200 and body["verification_passed"], body
            assert body["source_ids"] == [grant.bundle.sources[0].source_id]
            sent = [parameters for sdk in provider_factory for parameters in sdk.calls[start.get(id(sdk),0):]]
            prompts = [p["messages"][-1]["content"] for p in sent]
            assert sum(grant.bundle.sources[0].text in p for p in prompts) == 3
            assert all(absent.bundle.sources[0].text not in p for p in prompts)
    assert calls(provider_factory) == 10 and ledger.snapshot()["held_micro_eur"] == 20_000


@pytest.mark.parametrize("answer", ["no citation", "valid and unknown", "valid and malformed"])
def test_http_analysis_cannot_pass_by_avoiding_the_research_role(
        accounts, ledger, provider_factory, monkeypatch, answer):
    import test_request_dispatch as fixture
    grant = source_grant(accounts)
    original = fixture.answer
    def invalid_answer(parameters):
        result = original(parameters)
        if result.startswith("step result"):
            if answer == "no citation": return "unsupported claim"
            marker = "SRC-DEADBEEF" if answer == "valid and unknown" else "SRC-invalid"
            return f"claim [{grant.bundle.sources[0].source_id}] [{marker}]"
        return result
    monkeypatch.setattr(fixture, "answer", invalid_answer)
    with serving(pilot(accounts, ledger, source_manifests=(grant,))) as (_, base):
        client, csrf = login(base)
        status, _, body = client.post("/api/run", {"goal":"validate cited analysis"}, csrf=csrf)
        assert status == 200, body
        assert not body["verification_passed"] and not body["release_ready"]
        assert calls(provider_factory) <= 5
