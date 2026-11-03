"""Exercise the actual CLI against synthetic loopback HTTP, never a provider."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import threading
from uuid import uuid4

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pilot_rehearsal.py"
ACCOUNT = "1" * 32
IMAGE = f"registry.cloudflare.com/{ACCOUNT}/synthetic@sha256:" + "2" * 64
COMMIT = "3" * 40
TOKEN = "synthetic_key_" + "x" * 40


def state(phase="empty", revision=0, operation=None, capsule=None):
    return dict(phase=phase, revision=revision, operation_id=operation,
                automatic_resume=False, recovery_required=False, capsule=capsule)


@contextmanager
def service(fault=None):
    calls = []
    source, inspection = state(), state()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_call()

        def do_POST(self):
            self.handle_call()

        def handle_call(self):
            nonlocal source, inspection
            action = self.path.removeprefix("/_pilot/")
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else None
            operation = self.headers.get("Idempotency-Key")
            calls.append((action, self.command, self.headers.get("If-Match"), operation, body))
            assert self.headers.get("Authorization") == "Bearer " + TOKEN
            if action == "status":
                value = source.copy()
            elif action == "inspection":
                value = inspection.copy()
            elif action == "start":
                source = value = state("running", 2, operation)
            elif action == "capture":
                saved = dict(pin="4" * 64, generation=operation, image=IMAGE,
                             root_id=str(uuid4()), root_sha256="5" * 64,
                             created_at=1000, expires_at=86401000)
                source = value = state("captured", 4, operation, saved)
            elif action == "restore":
                assert body == {"pin": source["capsule"]["pin"]}
                source = value = state("staged", 6, operation, source["capsule"])
                inspection = state("staged", 2, operation, source["capsule"].copy())
            else:
                raise AssertionError(action)
            value = json.loads(json.dumps(value))
            status, content_type = 200, "application/json"
            if fault == "used" and len(calls) == 1:
                value = state("running", 2, str(uuid4()))
            if fault == "uncertain" and action == "capture":
                status = 502
                value = {"diagnostic": TOKEN}
            if fault == "redirect" and action == "start":
                status = 302
            if fault == "wrong_image" and action == "capture":
                value["capsule"]["image"] = IMAGE + "0"
            if fault == "wrong_root" and action == "inspection" and len(calls) == 6:
                value["capsule"]["root_sha256"] = "6" * 64
            if fault == "recovery" and action == "start":
                value["recovery_required"] = True
            if fault == "wrong_revision" and action == "start":
                value["revision"] = 99
            if fault == "wrong_operation" and action == "start":
                value["operation_id"] = str(uuid4())
            if fault == "oversized" and action == "status":
                value = {"data": "x" * 8193}
            if fault == "html":
                content_type = "text/html"
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if status == 302:
                self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, calls
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def invoke(port, *extra, token=TOKEN):
    result = subprocess.run([sys.executable, str(SCRIPT), "--execute", "--account-id", ACCOUNT,
                             "--image", IMAGE, "--source-commit", COMMIT, "--port", str(port), *extra],
                            input=token + "\n", capture_output=True, text=True, timeout=15)
    assert token not in result.stdout + result.stderr
    assert result.stderr == ""
    return result.returncode, json.loads(result.stdout)


def test_default_is_offline_plan():
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout)["network_requests"] == 0


def test_full_protocol_and_receipt(tmp_path, monkeypatch):
    # Ambient proxies cannot receive the bearer, even when localhost bypass is disabled.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    receipt = tmp_path / "receipt.json"
    with service() as (port, calls):
        code, report = invoke(port, "--output", str(receipt))
    assert code == 0 and report["result"] == "PASS"
    assert report["requests_attempted"] == 7
    assert report["mutating_requests_attempted"] == 3 and report["cleanup_required"]
    assert report["native_execution_independently_verified"] is False
    assert json.loads(receipt.read_text()) == report
    assert [c[0] for c in calls] == ["status", "inspection", "start", "capture", "restore", "inspection", "status"]
    mutations = [c for c in calls if c[1] == "POST"]
    assert [c[2] for c in mutations] == ['"0"', '"2"', '"4"']
    assert len({c[3] for c in mutations}) == 3


@pytest.mark.parametrize("fault,count,mutations", [
    ("used", 2, 0), ("uncertain", 4, 2), ("redirect", 3, 1),
    ("wrong_image", 4, 2), ("wrong_root", 7, 3), ("recovery", 3, 1),
    ("wrong_revision", 3, 1), ("wrong_operation", 3, 1), ("oversized", 1, 0), ("html", 1, 0),
])
def test_stops_without_retries_or_diagnostic_leaks(fault, count, mutations):
    with service(fault) as (port, calls):
        code, report = invoke(port)
    assert code == 1 and report["result"] == "BLOCKED"
    assert len(calls) == count and report["requests_attempted"] == count
    assert report["mutating_requests_attempted"] == mutations
    assert report["cleanup_required"] is bool(mutations)


def test_existing_receipt_prevents_network(tmp_path):
    receipt = tmp_path / "existing.json"
    receipt.write_text("preserved")
    with service() as (port, calls):
        code, report = invoke(port, "--output", str(receipt))
    assert code == 1 and report["result"] == "BLOCKED" and calls == []
    assert receipt.read_text() == "preserved"


def test_invalid_token_prevents_network():
    with service() as (port, calls):
        code, report = invoke(port, token="bad_unique_test_key")
    assert code == 1 and report["result"] == "BLOCKED" and calls == []
