from types import SimpleNamespace

import pytest

from tankai.web import onedrive_connection as onedrive
from tankai.web.connections import ConnectionError


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv("TANKAI_ONEDRIVE_OWNER_ID", "owner")
    monkeypatch.setenv("TANKAI_ONEDRIVE_WORKSPACE_ID", "workspace")
    monkeypatch.setenv("TANKAI_ONEDRIVE_READ_TOKEN", "synthetic-onedrive-read-token")
    monkeypatch.setattr(onedrive, "_STATE", {})
    return SimpleNamespace(session_id="session", user_id="owner",
                           tenant_id="tenant", workspace_id="workspace", role="owner")


def test_onedrive_explicit_read_only_connection(context, monkeypatch):
    calls = []
    def search(token):
        calls.append(True)
        return {"value": [
            {"name": "Test.txt", "file": {}, "@microsoft.graph.downloadUrl": "ignored"},
            {"name": "Projects", "folder": {}},
            {"name": "Unknown"}
        ], "@odata.nextLink": "not-followed"}

    monkeypatch.setattr(onedrive, "_search", search)
    other = SimpleNamespace(**{**vars(context), "workspace_id": "other"})
    assert onedrive.status(other) == {"status": "not_configured"}
    with pytest.raises(ConnectionError):
        onedrive.start(other)
    with pytest.raises(ConnectionError):
        onedrive.files(context)
    assert calls == []
    assert onedrive.start(context) == {"status": "connected"}
    assert onedrive.files(context) == {"status": "connected", "files": [
        {"name": "Test.txt", "kind": "file"}, {"name": "Projects", "kind": "folder"}
    ], "has_more": True}
    assert len(calls) == 2
    assert onedrive.disconnect(context) == {"status": "disconnected"}
    with pytest.raises(ConnectionError):
        onedrive.files(context)


def test_onedrive_failed_verification(context, monkeypatch):
    def unavailable(token):
        raise ConnectionError("offline")
    monkeypatch.setattr(onedrive, "_search", unavailable)
    with pytest.raises(ConnectionError):
        onedrive.start(context)
    assert onedrive.status(context) == {"status": "disconnected"}
