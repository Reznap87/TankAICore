from types import SimpleNamespace

import pytest

from tankai.web import outlook_connection as outlook
from tankai.web.connections import ConnectionError


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv("TANKAI_OUTLOOK_OWNER_ID", "owner")
    monkeypatch.setenv("TANKAI_OUTLOOK_WORKSPACE_ID", "workspace")
    monkeypatch.setenv("TANKAI_OUTLOOK_READ_TOKEN", "synthetic-outlook-read-token")
    monkeypatch.setattr(outlook, "_STATE", {})
    return SimpleNamespace(session_id="session", user_id="owner",
                           tenant_id="tenant", workspace_id="workspace", role="owner")


def test_outlook_inbox_metadata_only(context, monkeypatch):
    calls = []
    def search(token):
        calls.append(True)
        return {"value": [
            {"subject": "Test", "from": {"emailAddress": {"address": "test@example.invalid"}},
             "receivedDateTime": "2026-11-27T00:00:00Z", "body": {"content": "ignored"},
             "attachments": [{"name": "ignored"}]},
            {"subject": None, "from": None},
        ], "@odata.nextLink": "not-followed"}

    monkeypatch.setattr(outlook, "_search", search)
    other = SimpleNamespace(**{**vars(context), "workspace_id": "other"})
    assert outlook.status(other) == {"status": "not_configured"}
    with pytest.raises(ConnectionError):
        outlook.start(other)
    with pytest.raises(ConnectionError):
        outlook.messages(context)
    assert calls == []
    assert outlook.start(context) == {"status": "connected"}
    assert outlook.messages(context) == {"status": "connected", "messages": [
        {"subject": "Test", "from": "test@example.invalid", "date": "2026-11-27T00:00:00Z"},
        {"subject": "", "from": "", "date": ""}
    ], "has_more": True}
    assert len(calls) == 2
    assert outlook.disconnect(context) == {"status": "disconnected"}
    with pytest.raises(ConnectionError):
        outlook.messages(context)


def test_outlook_failed_verification(context, monkeypatch):
    def unavailable(token):
        raise ConnectionError("offline")
    monkeypatch.setattr(outlook, "_search", unavailable)
    with pytest.raises(ConnectionError):
        outlook.start(context)
    assert outlook.status(context) == {"status": "disconnected"}
