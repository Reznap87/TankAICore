"""Die kostenlose Route muss ohne Login am Health-Endpunkt erkennbar sein.

Betreiber und CI muessen feststellen koennen, ob der Free-Chat aktiv ist,
ohne ein Konto oder einen Provider-Key zu benötigen. Es werden ausschließlich
Booleans veroeffentlicht - niemals Schluessel, Endpunkte oder Nutzerdaten.
"""
from __future__ import annotations

import threading

import pytest

from tankai.web import server as web_server
from test_web_server import Client


@pytest.mark.parametrize("free_route", [False, True])
def test_public_health_reports_free_route_state(tmp_path, monkeypatch, free_route):
    monkeypatch.setenv("TANKAI_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("TANKAI_AUTH_DB", str(tmp_path / "auth.db"))
    monkeypatch.setenv("TANKAI_AUTH_MODE", "session")
    monkeypatch.setenv("TANKAI_EMBEDDER", "hashing")

    flags = ("TANKAI_LIVE_PROVIDER_ENABLED", "TANKAI_OPENROUTER_FREE_ENABLED", "TANKAI_LLM")
    for key in flags:
        monkeypatch.delenv(key, raising=False)
    if free_route:
        monkeypatch.setenv("TANKAI_LIVE_PROVIDER_ENABLED", "1")
        monkeypatch.setenv("TANKAI_OPENROUTER_FREE_ENABLED", "1")
        monkeypatch.setenv("TANKAI_LLM", "openrouter")

    app = web_server.AppContext.from_env("127.0.0.1")
    assert app.free_route_enabled is free_route

    server = web_server.ThreadedHTTPServer(("127.0.0.1", 0), web_server.Handler, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, payload = Client(f"http://127.0.0.1:{server.server_port}").get("/api/health")
        assert status == 200
        assert payload["free_route_enabled"] is free_route
        # Kein Secret, kein Endpunkt, keine Nutzerinformation im oeffentlichen Health.
        for key in ("OPENROUTER_API_KEY", "OPENROUTER_MODEL", "user", "workspace"):
            assert key not in payload
        assert payload["monetary_admission_configured"] is False
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()


@pytest.mark.parametrize("configured", [False, True])
def test_public_health_reports_nvidia_configuration_without_key(tmp_path, monkeypatch, configured):
    monkeypatch.setenv("TANKAI_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("TANKAI_AUTH_DB", str(tmp_path / "auth.db"))
    monkeypatch.setenv("TANKAI_AUTH_MODE", "session")
    monkeypatch.setenv("TANKAI_EMBEDDER", "hashing")
    monkeypatch.delenv("TANKAI_NVIDIA_FREE_ENABLED", raising=False)
    monkeypatch.delenv("TANKAI_NVIDIA_KEY_SLOT", raising=False)
    for slot in "abcdefgh":
        monkeypatch.delenv("NVIDIA_API_KEY" + slot, raising=False)
    if configured:
        monkeypatch.setenv("TANKAI_NVIDIA_FREE_ENABLED", "1")
        monkeypatch.setenv("TANKAI_NVIDIA_KEY_SLOT", "a")
        monkeypatch.setenv("NVIDIA_API_KEYa", "synthetic-key-never-exposed")

    app = web_server.AppContext.from_env("127.0.0.1")
    server = web_server.ThreadedHTTPServer(("127.0.0.1", 0), web_server.Handler, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, payload = Client(f"http://127.0.0.1:{server.server_port}").get("/api/health")
        assert status == 200
        assert payload["nvidia_chat_configured"] is configured
        body = str(payload)
        assert "synthetic-key-never-exposed" not in body
        assert "NVIDIA_API_KEYa" not in body
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()
