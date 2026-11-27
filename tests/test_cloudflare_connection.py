from types import SimpleNamespace

import pytest

from tankai.web import cloudflare_connection as cf
from tankai.web.connections import ConnectionError


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv("TANKAI_CF_CONNECTOR_OWNER_ID", "synthetic-owner")
    monkeypatch.setenv("TANKAI_CF_CONNECTOR_ACCOUNT_ID", "a" * 32)
    monkeypatch.setenv("TANKAI_CF_CONNECTOR_READ_TOKEN", "synthetic-read-token-12345")
    monkeypatch.setattr(cf, "_STATE", {})
    return SimpleNamespace(session_id="session", user_id="synthetic-owner",
                           tenant_id="tenant", workspace_id="workspace", role="owner")


def test_cf_owner_binding_and_read_only(context, monkeypatch):
    calls = []
    def request(url, **kwargs):
        calls.append(url)
        assert url == "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/workers/scripts"
        assert "fields" not in kwargs
        return {"success": True, "result": [{"id": "test-worker", "modified_on": "2026-11-26", "secret": "not returned"}]}
    monkeypatch.setattr(cf, "_request", request)
    other = SimpleNamespace(**{**vars(context), "user_id": "different-owner"})
    assert cf.status(other) == {"status": "not_configured"}
    with pytest.raises(ConnectionError):
        cf.start(other)
    with pytest.raises(ConnectionError):
        cf.workers(context)
    assert calls == []
    assert cf.start(context) == {"status": "connected"}
    assert cf.workers(context)["workers"] == [{"name": "test-worker", "modified_on": "2026-11-26"}]
    other_ws = SimpleNamespace(**{**vars(context), "workspace_id": "other"})
    with pytest.raises(ConnectionError):
        cf.workers(other_ws)
    assert cf.disconnect(context) == {"status": "disconnected"}
    assert len(calls) == 2


def test_cf_failed_verification_does_not_connect(context, monkeypatch):
    monkeypatch.setattr(cf, "_request", lambda *a, **k: {"success": False})
    with pytest.raises(ConnectionError):
        cf.start(context)
    assert cf.status(context) == {"status": "disconnected"}
