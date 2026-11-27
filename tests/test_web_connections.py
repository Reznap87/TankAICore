from types import SimpleNamespace

import pytest

from tankai.web import connections


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv("TANKAI_GITHUB_CLIENT_ID", "synthetic_client_id")
    monkeypatch.setattr(connections, "_STATE", {})
    return SimpleNamespace(session_id="session", user_id="user", tenant_id="tenant", workspace_id="workspace")


def test_connector_requires_configuration_and_explicit_start(context, monkeypatch):
    monkeypatch.delenv("TANKAI_GITHUB_CLIENT_ID")
    assert connections.status(context)["status"] == "not_configured"
    with pytest.raises(connections.ConnectionError):
        connections.start(context)


def test_device_authorization_scoped_and_read_only(context, monkeypatch):
    now = [1000.0]
    calls = []
    monkeypatch.setattr(connections.time, "monotonic", lambda: now[0])

    def fake_request(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/device/code"):
            assert kwargs["fields"]["scope"] == ""
            return {"device_code": "synthetic_device_code", "user_code": "ABCD-EFGH", "interval": 5, "expires_in": 900}
        if url.endswith("/access_token"):
            return {"access_token": "synthetic_token", "scope": ""}
        if url.endswith("/user"):
            return {"login": "synthetic-user"}
        return [{"full_name": "synthetic-user/public", "private": False},
                {"full_name": "synthetic-user/private", "private": True}]

    monkeypatch.setattr(connections, "_request", fake_request)
    assert connections.status(context)["status"] == "disconnected"
    pending = connections.start(context)
    assert pending["user_code"] == "ABCD-EFGH"
    assert "device_code" not in pending
    assert connections.finish(context)["status"] == "pending"
    assert len(calls) == 1
    other = SimpleNamespace(**{**vars(context), "workspace_id": "other"})
    assert connections.status(other)["status"] == "disconnected"
    with pytest.raises(connections.ConnectionError):
        connections.repositories(other)
    now[0] += 5
    assert connections.finish(context) == {"status": "connected", "login": "synthetic-user"}
    assert connections.repositories(context)["repositories"] == [
        {"name": "synthetic-user/public", "url": "https://github.com/synthetic-user/public"}
    ]
    assert connections.status(context) == {"status": "connected", "login": "synthetic-user"}
    connections.disconnect(context)
    assert connections.status(context)["status"] == "disconnected"
    with pytest.raises(connections.ConnectionError):
        connections.repositories(context)


def test_no_elevated_oauth_scope(context, monkeypatch):
    monkeypatch.setattr(connections, "_request", lambda *args, **kwargs: {"access_token": "synthetic_token", "scope": "repo"})
    connections._STATE[connections._key(context)] = {"status": "pending", "device": "synthetic_device", "interval": 5, "next": 0, "expires": connections.time.monotonic() + 60}
    with pytest.raises(connections.ConnectionError):
        connections.finish(context)
    assert connections.status(context)["status"] == "disconnected"
