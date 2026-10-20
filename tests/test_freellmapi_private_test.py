import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("freellmapi_test", ROOT / "scripts/freellmapi_test.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def test_setup_preserves_encryption_and_keeps_credentials_private(tmp_path):
    path = tmp_path / "private" / "gateway.env"
    assert helper.initialize(path)
    original = helper.load_config(path)[helper.ENCRYPTION]
    assert path.stat().st_mode & 0o777 == 0o600
    assert not helper.initialize(path)
    helper.connect("freellmapi-test-unified-key", path)
    assert helper.load_config(path)[helper.ENCRYPTION] == original
    assert helper.load_config(path)[helper.UNIFIED] == "freellmapi-test-unified-key"
    assert path.stat().st_mode & 0o777 == 0o600


def test_setup_rejects_symlink_and_public_secret_file(tmp_path):
    real = tmp_path / "real"
    helper.initialize(real)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        helper.initialize(link)
    real.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        helper.load_config(real)


def test_connect_rejects_env_injection_without_changing_file(tmp_path):
    path = tmp_path / "gateway.env"
    helper.initialize(path)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        helper.connect("gateway-token\nEXTRA=bad", path)
    assert path.read_bytes() == original


def configured(tmp_path):
    path = tmp_path / "gateway.env"
    helper.initialize(path)
    helper.connect("freellmapi-test-unified-key", path)
    return path


def test_virtual_router_entries_cannot_pass_readiness(tmp_path, monkeypatch):
    path = configured(tmp_path)
    monkeypatch.setattr(helper, "request_json", lambda *args: ({"data": [
        {"id": "auto", "execution_status": "ready"},
        {"id": "auto:coding", "execution_status": "ready"},
        {"id": "fusion", "execution_status": "ready"},
        {"id": "real-model", "execution_status": "needsKey"},
    ]}, ""))
    with pytest.raises(ValueError, match="no ready model"):
        helper.check(path=path)


def test_check_never_requests_inference(tmp_path, monkeypatch):
    path = configured(tmp_path)
    calls = []

    def request(route, key, payload=None):
        calls.append((route, payload))
        return {"data": [{"id": "model", "execution_status": "ready"}]}, ""

    monkeypatch.setattr(helper, "request_json", request)
    result = helper.check(path=path)
    assert calls == [("/v1/models?available=true", None)]
    assert result["inference_requested"] is False
    assert result["production_ready"] is False


def test_smoke_is_one_bounded_request_and_does_not_print_secret(tmp_path, monkeypatch):
    path = configured(tmp_path)
    calls = []

    def request(route, key, payload=None):
        calls.append((route, payload))
        if payload is None:
            return {"data": [{"id": "model", "execution_status": "ready"}]}, ""
        return {"model": "actual-model", "choices": [{"message": {"content": "TANKAI_OK"}}]}, "provider/actual-model"

    monkeypatch.setattr(helper, "request_json", request)
    result = helper.check(smoke=True, path=path)
    assert len(calls) == 2
    assert calls[1][1]["max_tokens"] == 32
    assert result["expected_reply"] is True
    assert result["routed_via"] == "provider/actual-model"
    assert result["production_ready"] is False
    assert "freellmapi-test-unified-key" not in json.dumps(result)


def test_smoke_rejects_empty_model_response(tmp_path, monkeypatch):
    path = configured(tmp_path)

    def request(route, key, payload=None):
        if payload is None:
            return {"data": [{"id": "model", "execution_status": "ready"}]}, ""
        return {"choices": []}, ""

    monkeypatch.setattr(helper, "request_json", request)
    with pytest.raises(ValueError, match="no textual model"):
        helper.check(smoke=True, path=path)
