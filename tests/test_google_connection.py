from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tankai.web import google_connection as drive
from tankai.web.connections import ConnectionError


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv("TANKAI_GOOGLE_CLIENT_ID", "synthetic-client-id.apps.googleusercontent.com")
    monkeypatch.setenv("TANKAI_GOOGLE_CLIENT_SECRET", "synthetic-secret")
    monkeypatch.setattr(drive, "_STATE", {})
    return SimpleNamespace(session_id="session", user_id="user", tenant_id="tenant", workspace_id="workspace")


def test_google_drive_authorization_and_metadata(context, monkeypatch):
    calls = []

    def fake_request(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/token"):
            assert kwargs["fields"]["code_verifier"]
            assert kwargs["fields"]["redirect_uri"] == drive.CALLBACK
            return {"access_token": "synthetic-token", "scope": drive.SCOPE, "expires_in": 3600, "token_type": "Bearer"}
        assert url.startswith("https://www.googleapis.com/drive/v3/files?")
        return {"files": [{"id": "doc-1", "name": "Test", "content": "not returned"}]}

    monkeypatch.setattr(drive, "_request", fake_request)
    assert drive.status(context) == {"status": "disconnected"}
    pending = drive.start(context)
    params = parse_qs(urlsplit(pending["authorization_url"]).query)
    assert params["scope"] == [drive.SCOPE]
    assert params["access_type"] == ["online"]
    assert params["code_challenge_method"] == ["S256"]
    other = SimpleNamespace(**{**vars(context), "workspace_id": "other"})
    with pytest.raises(ConnectionError):
        drive.finish(other, state=params["state"][0], code="synthetic")
    with pytest.raises(ConnectionError):
        drive.finish(context, state="wrong", code="synthetic")
    assert calls == []
    assert drive.finish(context, state=params["state"][0], code="synthetic") == {"status": "connected"}
    with pytest.raises(ConnectionError):
        drive.finish(context, state=params["state"][0], code="synthetic")
    assert drive.files(context)["files"] == [{"id": "doc-1", "name": "Test"}]
    with pytest.raises(ConnectionError):
        drive.files(other)
    assert drive.disconnect(context) == {"status": "disconnected"}
    with pytest.raises(ConnectionError):
        drive.files(context)


def test_google_drive_rejects_broader_scope(context, monkeypatch):
    pending = drive.start(context)
    state = parse_qs(urlsplit(pending["authorization_url"]).query)["state"][0]
    monkeypatch.setattr(drive, "_request", lambda *a, **k: {"access_token": "synthetic-token", "scope": drive.SCOPE + " extra", "expires_in": 3600, "token_type": "Bearer"})
    with pytest.raises(ConnectionError):
        drive.finish(context, state=state, code="synthetic")
    assert drive.status(context) == {"status": "disconnected"}


def test_gmail_is_separate_and_only_reads_headers(context, monkeypatch):
    calls = []
    def fake_request(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/token"):
            return {"access_token": "synthetic-gmail-token", "scope": drive.GMAIL_SCOPE,
                    "expires_in": 3600, "token_type": "Bearer"}
        if "/messages?" in url:
            assert parse_qs(urlsplit(url).query)["labelIds"] == ["INBOX"]
            return {"messages": [{"id": "test-message"}]}
        assert "/messages/test-message?" in url
        assert parse_qs(urlsplit(url).query)["format"] == ["metadata"]
        return {"payload": {"headers": [
            {"name": "Subject", "value": "Synthetic"},
            {"name": "From", "value": "test@example.invalid"},
            {"name": "Date", "value": "Today"},
            {"name": "Other", "value": "Ignored"}], "body": {"data": "ignored"}}}

    monkeypatch.setattr(drive, "_request", fake_request)
    pending = drive.start(context, "gmail")
    params = parse_qs(urlsplit(pending["authorization_url"]).query)
    assert params["scope"] == [drive.GMAIL_SCOPE]
    assert drive.status(context) == {"status": "disconnected"}
    assert drive.finish(context, state=params["state"][0], code="synthetic") == {"status": "connected"}
    assert drive.status(context, "gmail") == {"status": "connected"}
    with pytest.raises(ConnectionError):
        drive.files(context)
    assert drive.messages(context)["messages"] == [
        {"subject": "Synthetic", "from": "test@example.invalid", "date": "Today"}
    ]
    assert all("fields" not in kwargs for _, kwargs in calls[1:])
    drive.disconnect(context, service="gmail")
    with pytest.raises(ConnectionError):
        drive.messages(context)


def test_calendar_separate_consent_and_read_only_event_list(context, monkeypatch):
    calls = []

    def fake_request(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/token"):
            return {"access_token": "synthetic-calendar-token", "scope": drive.CALENDAR_SCOPE,
                    "expires_in": 3600, "token_type": "Bearer"}
        assert url.startswith("https://www.googleapis.com/calendar/v3/calendars/primary/events?")
        assert "fields" not in kwargs
        query = parse_qs(urlsplit(url).query)
        assert query["maxResults"] == ["20"]
        assert query["singleEvents"] == ["true"]
        assert query["orderBy"] == ["startTime"]
        return {"items": [
            {"summary": "Synthetic event", "start": {"date": "2026-12-01"},
             "end": {"date": "2026-12-02"}, "description": "Ignored"},
            {"summary": "Cancelled", "status": "cancelled",
             "start": {"date": "2026-12-03"}, "end": {"date": "2026-12-04"}}
        ]}

    monkeypatch.setattr(drive, "_request", fake_request)
    pending = drive.start(context, "calendar")
    params = parse_qs(urlsplit(pending["authorization_url"]).query)
    assert params["scope"] == [drive.CALENDAR_SCOPE]
    assert drive.status(context, "gmail") == {"status": "disconnected"}
    assert drive.finish(context, state=params["state"][0], code="synthetic") == {"status": "connected"}
    assert drive.events(context)["events"] == [
        {"title": "Synthetic event", "start": "2026-12-01", "end": "2026-12-02", "all_day": True}
    ]
    other = SimpleNamespace(**{**vars(context), "workspace_id": "other"})
    with pytest.raises(ConnectionError):
        drive.events(other)
    drive.disconnect(context, service="calendar")
    with pytest.raises(ConnectionError):
        drive.events(context)
