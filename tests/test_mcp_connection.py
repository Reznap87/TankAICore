from types import SimpleNamespace

import pytest

from tankai.web import mcp_connection as mcp
from tankai.web.connections import ConnectionError


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setenv("TANKAI_MCP_OWNER_ID", "owner")
    monkeypatch.setenv("TANKAI_MCP_WORKSPACE_ID", "workspace")
    monkeypatch.setenv("TANKAI_MCP_SERVER_URL", "https://mcp.example.com/tools")
    monkeypatch.setenv("TANKAI_MCP_READ_TOKEN", "synthetic-mcp-token-readonly")
    monkeypatch.setenv("TANKAI_MCP_ALLOWED_TOOLS", "lookup,mutate")
    monkeypatch.setattr(mcp, "_STATE", {})
    return SimpleNamespace(session_id="session", user_id="owner",
                           tenant_id="tenant", workspace_id="workspace", role="owner")


def test_discovery_and_explicit_allowlist(context, monkeypatch):
    calls = []

    def rpc(config, method, params, session=None, notification=False):
        calls.append((method, params, session, notification))
        if method == "initialize":
            return {"protocolVersion": mcp._VERSION, "capabilities": {"tools": {}}}, "session-id"
        if method == "notifications/initialized":
            assert notification and session == "session-id"
            return {}, session
        if method == "tools/list":
            return {"tools": [
                {"name": "lookup", "description": "Read", "annotations": {"readOnlyHint": True},
                 "inputSchema": {"type": "object"}},
                {"name": "mutate", "annotations": {"readOnlyHint": False}, "inputSchema": {"type": "object"}},
                {"name": "unknown", "annotations": {"readOnlyHint": True}, "inputSchema": {"type": "object"}},
            ]}, session
        if method == "tools/call":
            return {"content": [{"type": "text", "text": "result"}, {"type": "image", "data": "ignored"}]}, session
        raise AssertionError(method)

    monkeypatch.setattr(mcp, "_rpc", rpc)
    other = SimpleNamespace(**{**vars(context), "workspace_id": "other"})
    assert mcp.status(other) == {"status": "not_configured"}
    with pytest.raises(ConnectionError):
        mcp.start(other)
    with pytest.raises(ConnectionError):
        mcp.call(context, name="lookup", arguments={})
    assert not calls
    assert mcp.start(context) == {"status": "connected"}
    assert mcp.tools(context)["tools"] == [{"name": "lookup", "description": "Read"}]
    with pytest.raises(ConnectionError):
        mcp.call(context, name="mutate", arguments={})
    assert not any(method == "tools/call" for method, *_ in calls)
    assert mcp.call(context, name="lookup", arguments={"query": "synthetic"}) == {
        "status": "connected", "output": "result", "is_error": False}
    assert mcp.disconnect(context) == {"status": "disconnected"}
    with pytest.raises(ConnectionError):
        mcp.tools(context)


@pytest.mark.parametrize("url", ["http://mcp.example.com/tools", "https://127.0.0.1/tools",
                                 "https://host.local/tools", "https://mcp.example.com:8443/tools",
                                 "https://user:secret@mcp.example.com/tools",
                                 "https://mcp.example.com/tools?token=x"])
def test_invalid_server_urls(context, monkeypatch, url):
    monkeypatch.setenv("TANKAI_MCP_SERVER_URL", url)
    assert mcp.status(context) == {"status": "not_configured"}
    with pytest.raises(ConnectionError):
        mcp.start(context)


def test_unsupported_protocol_not_connected(context, monkeypatch):
    monkeypatch.setattr(mcp, "_rpc", lambda *a, **k: ({"protocolVersion": "other"}, None))
    with pytest.raises(ConnectionError):
        mcp.start(context)
    assert mcp.status(context) == {"status": "disconnected"}
