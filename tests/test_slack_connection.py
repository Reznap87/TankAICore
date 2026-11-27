from types import SimpleNamespace

import pytest

from tankai.web import slack_connection as slack
from tankai.web.connections import ConnectionError


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv("TANKAI_SLACK_OWNER_ID", "owner")
    monkeypatch.setenv("TANKAI_SLACK_WORKSPACE_ID", "workspace")
    monkeypatch.setenv("TANKAI_SLACK_READ_TOKEN", "synthetic-slack-read-token")
    monkeypatch.setattr(slack, "_STATE", {})
    return SimpleNamespace(session_id="session", user_id="owner",
                           tenant_id="tenant", workspace_id="workspace", role="owner")


def test_slack_public_channels_only(context, monkeypatch):
    calls = []
    def search(token):
        calls.append(True)
        return {"ok": True, "channels": [
            {"name": "general", "is_channel": True, "is_private": False, "is_archived": False, "topic": "ignored"},
            {"name": "private", "is_channel": True, "is_private": True, "is_archived": False},
            {"name": "archive", "is_channel": True, "is_private": False, "is_archived": True},
            {"name": "unknown"},
        ], "response_metadata": {"next_cursor": "not-followed"}}

    monkeypatch.setattr(slack, "_search", search)
    other = SimpleNamespace(**{**vars(context), "workspace_id": "other"})
    assert slack.status(other) == {"status": "not_configured"}
    with pytest.raises(ConnectionError):
        slack.start(other)
    with pytest.raises(ConnectionError):
        slack.channels(context)
    assert not calls
    assert slack.start(context) == {"status": "connected"}
    assert slack.channels(context) == {"status": "connected", "channels": [{"name": "general"}], "has_more": True}
    assert len(calls) == 2
    assert slack.disconnect(context) == {"status": "disconnected"}
    with pytest.raises(ConnectionError):
        slack.channels(context)


def test_slack_failed_verification(context, monkeypatch):
    def unavailable(token):
        raise ConnectionError("offline")
    monkeypatch.setattr(slack, "_search", unavailable)
    with pytest.raises(ConnectionError):
        slack.start(context)
    assert slack.status(context) == {"status": "disconnected"}
