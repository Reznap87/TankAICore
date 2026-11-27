from types import SimpleNamespace

import pytest

from tankai.web import dropbox_connection as dropbox
from tankai.web.connections import ConnectionError


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv("TANKAI_DROPBOX_OWNER_ID", "owner")
    monkeypatch.setenv("TANKAI_DROPBOX_WORKSPACE_ID", "workspace")
    monkeypatch.setenv("TANKAI_DROPBOX_READ_TOKEN", "synthetic-dropbox-read-token")
    monkeypatch.setattr(dropbox, "_STATE", {})
    return SimpleNamespace(session_id="session", user_id="owner",
                           tenant_id="tenant", workspace_id="workspace", role="owner")


def test_dropbox_explicit_read_only_connection(context, monkeypatch):
    calls = []
    def search(token):
        calls.append(True)
        return {"entries": [
            {".tag": "file", "name": "Test.txt", "content": "ignored"},
            {".tag": "folder", "name": "Projects"},
            {".tag": "deleted", "name": "Deleted"}
        ], "has_more": True}

    monkeypatch.setattr(dropbox, "_search", search)
    other = SimpleNamespace(**{**vars(context), "workspace_id": "other"})
    assert dropbox.status(other) == {"status": "not_configured"}
    with pytest.raises(ConnectionError):
        dropbox.start(other)
    with pytest.raises(ConnectionError):
        dropbox.files(context)
    assert calls == []
    assert dropbox.start(context) == {"status": "connected"}
    assert dropbox.files(context) == {"status": "connected", "files": [
        {"name": "Test.txt", "kind": "file"}, {"name": "Projects", "kind": "folder"}
    ], "has_more": True}
    assert len(calls) == 2
    assert dropbox.disconnect(context) == {"status": "disconnected"}
    with pytest.raises(ConnectionError):
        dropbox.files(context)


def test_dropbox_failed_verification(context, monkeypatch):
    def unavailable(token):
        raise ConnectionError("offline")
    monkeypatch.setattr(dropbox, "_search", unavailable)
    with pytest.raises(ConnectionError):
        dropbox.start(context)
    assert dropbox.status(context) == {"status": "disconnected"}
