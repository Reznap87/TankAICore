"""Real Linux child/locks/socket tests; no Cloudflare API or model requests."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

import pytest

from tankai.web import pilot_supervisor as pilot
from tankai.web.application_root import ApplicationRootError, RootLease, initialize_root


POSIX = pytest.mark.skipif(os.name != "posix", reason="Linux pilot uses flock and Unix sockets")


def test_child_environment_does_not_forward_provider_auth_or_proxy_settings(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "HTTP_PROXY",
                 "TANKAI_AUTH_STATE_URL", "TANKAI_AUTH_STATE_TOKEN", "TANKAI_BOOTSTRAP_OWNER",
                 "TANKAI_LLM", "TANKAI_CRITIC_LLM", "PYTHONPATH"):
        monkeypatch.setenv(name, "synthetic-value-that-must-not-escape")
    env = pilot.child_environment(tmp_path)
    assert "synthetic-value-that-must-not-escape" not in env.values()
    assert env["TANKAI_LLM"] == env["TANKAI_CRITIC_LLM"] == "mock"
    assert env["TANKAI_ALLOW_REGISTRATION"] == env["TANKAI_DEV_QUEUE_ENABLED"] == "0"
    assert not any("KEY" in key or "PROXY" in key for key in env)


@pytest.fixture
def synthetic_child(tmp_path, monkeypatch):
    """Child commits SQLite after SIGTERM, then closes its managed lease."""
    original_popen = subprocess.Popen
    script = tmp_path / "child.py"
    script.write_text('''
import os, signal, sqlite3, time
from pathlib import Path
from tankai.web.application_root import RootLease
root = Path(os.environ['TANKAI_DATA_ROOT'])
lease = RootLease(root)
db = sqlite3.connect(root/'synthetic.sqlite')
db.execute('PRAGMA journal_mode=WAL')
db.execute('CREATE TABLE facts(value TEXT)')
db.commit()
stopping = False
def stop(*args):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop)
(root/'ready').write_text('ready')
while not stopping:
    time.sleep(.01)
time.sleep(.15)
db.execute("INSERT INTO facts VALUES('committed-after-signal')")
db.commit()
db.close()
lease.close()
''', encoding="utf-8")

    def spawn(command, **kwargs):
        assert command == [sys.executable, "-m", "tankai.web.server"]
        assert kwargs["close_fds"] is True and kwargs["cwd"] == Path(kwargs["env"]["TANKAI_DATA_ROOT"])
        return original_popen([sys.executable, str(script)], **kwargs)

    def ready(self):
        deadline = time.monotonic() + 10
        while not (self.root / "ready").exists():
            assert self.child.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.01)

    monkeypatch.setattr(pilot.subprocess, "Popen", spawn)
    monkeypatch.setattr(pilot.Supervisor, "_wait_ready", ready)


@POSIX
def test_quiesce_waits_for_child_commit_and_holds_root_lease(tmp_path, synthetic_child):
    supervisor = pilot.Supervisor(tmp_path / "root", "fresh")
    try:
        supervisor.start()
        assert supervisor.status()["snapshot_ready"] is False
        with pytest.raises(ApplicationRootError):
            RootLease(supervisor.root)
        with ThreadPoolExecutor(1) as pool:
            result = pool.submit(supervisor.quiesce)
            time.sleep(.04)
            assert not result.done()
            assert supervisor.phase == "quiescing"
            receipt = result.result(timeout=10)
        assert receipt["snapshot_ready"] and not receipt["child_running"]
        assert receipt["phase"] == "quiesced" and len(receipt["root_sha256"]) == 64
        # A normal read/write open could create WAL/SHM and must invalidate the seal.
        uri = (supervisor.root / "synthetic.sqlite").as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True)) as db:
            assert db.execute("SELECT value FROM facts").fetchall() == [("committed-after-signal",)]
        with pytest.raises(ApplicationRootError):
            RootLease(supervisor.root)
        assert supervisor.quiesce() == receipt  # Read-only retry after a lost response.
    finally:
        supervisor.close()
    with RootLease(supervisor.root):
        pass


@POSIX
def test_copied_snapshot_is_inspect_only_and_fresh_never_adopts_it(tmp_path, synthetic_child):
    source = pilot.Supervisor(tmp_path / "root", "fresh")
    source.start()
    try:
        before = source.quiesce()
        restored = tmp_path / "copy"
        shutil.copytree(source.root, restored)
        for _ in range(2):
            fresh = pilot.Supervisor(restored, "fresh")
            with pytest.raises(pilot.PilotError, match="startup blocked"):
                fresh.start()
            assert fresh.child is None
        inspect = pilot.Supervisor(restored, "inspect")
        try:
            inspect.start()
            after = inspect.status()
            assert after["phase"] == "inspect" and not after["child_running"]
            assert not after["snapshot_ready"]  # Inspection cannot attest how the copy was captured.
            assert after["root_sha256"] == before["root_sha256"]
            assert after["root_id"] == before["root_id"]
            with pytest.raises(ApplicationRootError):
                RootLease(restored)
        finally:
            inspect.close()
    finally:
        source.close()


@POSIX
@pytest.mark.parametrize("failure", ["timeout", "crash", "foreign-lock"])
def test_failed_quiescence_never_yields_snapshot_ready(tmp_path, synthetic_child, monkeypatch, failure):
    supervisor = pilot.Supervisor(tmp_path / "root", "fresh")
    supervisor.start()
    try:
        if failure == "timeout":
            timeout = .001
        elif failure == "crash":
            supervisor.child.kill()
            supervisor.child.wait(timeout=5)
            timeout = 5
        else:
            timeout = 5
            def busy(*args):
                raise ApplicationRootError("synthetic busy root")
            monkeypatch.setattr(pilot, "RootLease", busy)
        with pytest.raises(pilot.PilotError):
            supervisor.quiesce(timeout=timeout)
        assert supervisor.phase == "blocked"
        with pytest.raises(pilot.PilotError):
            supervisor.quiesce()
        assert supervisor.status()["snapshot_ready"] is False
    finally:
        supervisor.close()


@POSIX
def test_unexpected_child_death_blocks_without_restart(tmp_path, synthetic_child):
    supervisor = pilot.Supervisor(tmp_path / "root", "fresh")
    supervisor.start()
    try:
        pid = supervisor.child.pid
        supervisor.child.terminate()
        supervisor.child.wait(timeout=5)
        assert supervisor.status()["phase"] == "blocked"
        assert supervisor.child.pid == pid
        with pytest.raises(pilot.PilotError):
            supervisor.quiesce()
    finally:
        supervisor.close()


@POSIX
@pytest.mark.parametrize("mutation", ["bytes", "symlink", "hardlink", "oversized", "many-files"])
def test_changed_or_unsafe_sealed_root_blocks_receipt(tmp_path, mutation):
    root = tmp_path / "root"
    initialize_root(root)
    inspector = pilot.Supervisor(root, "inspect")
    inspector.start()
    try:
        if mutation == "bytes":
            (root / "unexpected").write_bytes(b"new")
        elif mutation == "symlink":
            (root / "unexpected").symlink_to(tmp_path)
        elif mutation == "hardlink":
            os.link(root / ".tankai-root.json", root / "unexpected")
        elif mutation == "oversized":
            with (root / "unexpected").open("wb") as output:
                output.truncate(pilot.MAX_BYTES + 1)
        else:
            for i in range(pilot.MAX_FILES):
                (root / str(i)).touch()
        with pytest.raises(pilot.PilotError, match="status blocked"):
            inspector.status()
        assert inspector.phase == "blocked"
        if mutation == "hardlink":
            with pytest.raises(pilot.PilotError):
                inspector.status()
        else:
            assert inspector.status()["snapshot_ready"] is False
    finally:
        inspector.close()


@POSIX
def test_control_socket_is_private_exclusive_and_recovers_stale_socket(tmp_path):
    directory = tmp_path / "control"
    first = pilot.ControlSocket(directory)
    try:
        assert (directory.stat().st_mode & 0o777) == 0o700
        assert ((directory / "control.sock").stat().st_mode & 0o777) == 0o600
        with pytest.raises(BlockingIOError):
            pilot.ControlSocket(directory)
        assert (directory / "control.sock").exists()
    finally:
        first.close()
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(directory / "control.sock"))
    stale.close()
    second = pilot.ControlSocket(directory)
    second.close()


@POSIX
@pytest.mark.parametrize("wire", [b"activate\n", b"status extra\n", b"x" * 40, b"status\nquiesce\n"])
def test_control_protocol_rejects_other_commands_without_quiescing(tmp_path, wire):
    root = tmp_path / "root"
    initialize_root(root)
    supervisor = pilot.Supervisor(root, "inspect")
    supervisor.start()
    endpoint = pilot.ControlSocket(tmp_path / "control")
    try:
        with ThreadPoolExecutor(1) as pool:
            serving = pool.submit(endpoint.serve_once, supervisor)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(endpoint.directory / "control.sock"))
                client.sendall(wire)
                assert json.loads(client.recv(2048)) == {"error": "pilot command blocked"}
            serving.result(timeout=2)
        assert supervisor.status()["phase"] == "inspect"
    finally:
        supervisor.close()
        endpoint.close()


@POSIX
def test_real_web_child_readiness_and_shutdown(tmp_path):
    supervisor = pilot.Supervisor(tmp_path / "root", "fresh")
    try:
        supervisor.start()
        assert supervisor.status()["phase"] == "running"
        assert (supervisor.root / "auth.db").is_file()
        receipt = supervisor.quiesce()
        assert receipt["snapshot_ready"] and receipt["phase"] == "quiesced"
        assert supervisor.child.returncode == 0
    finally:
        supervisor.close()


@POSIX
def test_control_client_round_trip(tmp_path):
    root = tmp_path / "root"
    initialize_root(root)
    supervisor = pilot.Supervisor(root, "inspect")
    supervisor.start()
    endpoint = pilot.ControlSocket(tmp_path / "control")
    try:
        with ThreadPoolExecutor(1) as pool:
            serving = pool.submit(endpoint.serve_once, supervisor)
            assert pilot.control("status", endpoint.directory) == supervisor.status()
            serving.result(timeout=2)
    finally:
        supervisor.close()
        endpoint.close()
