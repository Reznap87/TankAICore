from types import SimpleNamespace

import pytest

from tankai.web import notion_connection as notion
from tankai.web.connections import ConnectionError


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv("TANKAI_NOTION_OWNER_ID", "owner")
    monkeypatch.setenv("TANKAI_NOTION_WORKSPACE_ID", "workspace")
    monkeypatch.setenv("TANKAI_NOTION_READ_TOKEN", "synthetic-notion-read-token")
    monkeypatch.setattr(notion, "_STATE", {})
    return SimpleNamespace(session_id="session", user_id="owner",
                           tenant_id="tenant", workspace_id="workspace", role="owner")


def test_notion_requires_owner_workspace_and_explicit_connection(context, monkeypatch):
    calls = []
    page_id = "a" * 8 + "-aaaa-aaaa-aaaa-" + "a" * 12

    def search(token):
        calls.append(True)
        return {"object": "list", "results": [
            {"object": "page", "id": page_id, "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "Test"}]},
                "Private": {"type": "rich_text", "rich_text": [{"plain_text": "Do not return"}]}
            }},
            {"object": "page", "id": page_id, "in_trash": True}
        ], "has_more": False}

    monkeypatch.setattr(notion, "_search", search)
    other = SimpleNamespace(**{**vars(context), "workspace_id": "other"})
    assert notion.status(other) == {"status": "not_configured"}
    with pytest.raises(ConnectionError):
        notion.start(other)
    with pytest.raises(ConnectionError):
        notion.pages(context)
    assert calls == []
    assert notion.start(context) == {"status": "connected"}
    assert notion.pages(context)["pages"] == [{"id": page_id, "title": "Test"}]
    assert len(calls) == 2
    assert notion.disconnect(context) == {"status": "disconnected"}
    with pytest.raises(ConnectionError):
        notion.pages(context)


def test_notion_fails_closed(context, monkeypatch):
    def unavailable(token):
        raise ConnectionError("offline")
    monkeypatch.setattr(notion, "_search", unavailable)
    with pytest.raises(ConnectionError):
        notion.start(context)
    assert notion.status(context) == {"status": "disconnected"}
