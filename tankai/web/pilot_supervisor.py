"""Linux-only, offline pilot bridge for a future native container snapshot.

The supervisor stays alive after the web child has drained and relinquished its
managed root. No HTTP control endpoint, automatic restart, adoption or activation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from urllib.request import ProxyHandler, build_opener

from tankai.web.application_root import (
    RootLease, identity, initialize_root, read_regular, regular,
    root_metadata, safe_path, write_new,
)

ROOT = Path("/app/data/pilot")
CONTROL = Path("/tmp/tankai-pilot-control")
MAX_FILES = 256
MAX_BYTES = 32 * 1024 * 1024
DRAIN_SECONDS = 150


class PilotError(RuntimeError):
    """Fixed diagnostics, with no paths, credentials or child output."""


def child_environment(root: Path) -> dict[str, str]:
    # Never pass through ambient settings, .env files, credentials or proxies.
    return {
        "PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "TANKAI_HOST": "0.0.0.0", "TANKAI_PORT": "8765",
        "TANKAI_AUTH_MODE": "session", "TANKAI_DATA_ROOT": str(root),
        "TANKAI_AUTH_DB": str(root / "auth.db"), "TANKAI_COOKIE_SECURE": "1",
        "TANKAI_ALLOW_REGISTRATION": "0", "TANKAI_DEV_QUEUE_ENABLED": "0",
        "TANKAI_LLM": "mock", "TANKAI_CRITIC_LLM": "mock",
        "TANKAI_EMBEDDER": "hashing",
    }


def fingerprint(root: Path, lease: RootLease) -> str:
    """Bounded, link-rejecting digest of this cooperative managed root only."""
    lease.check()
    digest = hashlib.sha256()
    count = total = 0
    def walk_error(_error):
        raise PilotError("pilot inventory unavailable")
    for parent, directories, names in os.walk(root, followlinks=False, onerror=walk_error):
        directories.sort()
        names.sort()
        for name in directories + names:
            count += 1
            if count > MAX_FILES:
                raise PilotError("pilot root capacity exceeded")
            path = safe_path(Path(parent) / name)
            info = path.lstat()
            relative = path.relative_to(root).as_posix().encode()
            digest.update(len(relative).to_bytes(4, "big") + relative)
            if stat.S_ISDIR(info.st_mode):
                digest.update(b"directory")
            else:
                total += regular(path).st_size
                if total > MAX_BYTES:
                    raise PilotError("pilot root capacity exceeded")
                data = read_regular(path, MAX_BYTES - total + info.st_size)
                digest.update(len(data).to_bytes(8, "big") + data)
    lease.check()
    return digest.hexdigest()


class Supervisor:
    def __init__(self, root: Path, mode: str):
        if os.name != "posix" or mode not in {"fresh", "inspect"}:
            raise PilotError("unsupported pilot mode or platform")
        self.root = safe_path(root)
        self.mode = mode
        self.phase = "starting"
        self.child: subprocess.Popen | None = None
        self.lease: RootLease | None = None
        self.sealed_digest: str | None = None
        self.root_id: str | None = None
        self.root_identity: tuple | None = None

    def start(self):
        try:
            if self.mode == "fresh":
                metadata = initialize_root(self.root)  # Never adopt an old root.
                self.root_id = metadata["root_id"]
                self.root_identity = identity(self.root.lstat())
                self.child = subprocess.Popen(
                    [sys.executable, "-m", "tankai.web.server"],
                    env=child_environment(self.root), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=True, cwd=self.root,
                )
                self._wait_ready()
                self.phase = "running"
            else:
                self._seal("inspect")
        except Exception:
            self.phase = "blocked"
            raise PilotError("pilot startup blocked") from None

    def _wait_ready(self):
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.child is None or self.child.poll() is not None:
                raise PilotError("pilot child stopped before readiness")
            try:
                with opener.open("http://127.0.0.1:8765/api/health", timeout=0.5) as response:
                    raw = response.read(4097)
                    health = json.loads(raw) if len(raw) <= 4096 else {}
                if (health.get("ok") is True and health.get("auth_required") is True
                        and health.get("registration_enabled") is False
                        and health.get("development_queue_enabled") is False
                        and health.get("production_ready") is False):
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        raise PilotError("pilot readiness timed out")

    def _seal(self, phase: str):
        self.lease = RootLease(self.root)
        metadata = root_metadata(self.root)
        if ((self.root_id is not None and metadata["root_id"] != self.root_id)
                or (self.root_identity is not None and self.lease.root_identity != self.root_identity)):
            raise PilotError("pilot root replaced")
        self.root_id = metadata["root_id"]
        self.root_identity = self.lease.root_identity
        self.sealed_digest = fingerprint(self.root, self.lease)
        self.phase = phase

    def quiesce(self, *, timeout: float = DRAIN_SECONDS):
        if self.phase in {"quiesced", "inspect"}:
            return self.status()
        if self.phase != "running" or self.child is None or self.child.poll() is not None:
            self.phase = "blocked"
            raise PilotError("pilot is not running")
        self.phase = "quiescing"
        try:
            self.child.terminate()
            if self.child.wait(timeout=timeout) != 0:
                raise PilotError("pilot child did not close cleanly")
            self._seal("quiesced")
            return self.status()
        except Exception:
            self.phase = "blocked"
            raise PilotError("pilot quiescence blocked") from None

    def status(self) -> dict:
        child_running = self.child is not None and self.child.poll() is None
        if self.phase == "running" and not child_running:
            self.phase = "blocked"  # Never restart a crashed child.
        ready = self.phase in {"quiesced", "inspect"}
        try:
            if ready and (self.lease is None or child_running
                          or fingerprint(self.root, self.lease) != self.sealed_digest):
                raise PilotError("sealed pilot root changed")
            root_id = root_metadata(self.root)["root_id"]
            if root_id != self.root_id or identity(self.root.lstat()) != self.root_identity:
                raise PilotError("pilot root replaced")
        except Exception:
            self.phase = "blocked"
            raise PilotError("pilot status blocked") from None
        return {"format": "tankai-pilot-supervisor-v1", "phase": self.phase,
                "snapshot_ready": self.phase == "quiesced", "child_running": child_running,
                "root_id": root_id, "root_sha256": self.sealed_digest if ready else None}

    def close(self):
        # Cleanup is not a successful snapshot receipt. Even a killed child must
        # never be promoted to quiesced, and its existing root prevents fresh boot.
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=DRAIN_SECONDS)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait(timeout=5)
        if self.lease is not None:
            self.lease.close()
            self.lease = None


class ControlSocket:
    """Private Unix socket; a retained lock makes stale-socket cleanup safe."""
    def __init__(self, directory: Path):
        import fcntl
        self.directory = safe_path(directory)
        self.guard = None
        self.server = None
        self.owned_socket = False
        try:
            self.directory.mkdir(mode=0o700, exist_ok=True)
            info = self.directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise PilotError("private pilot control directory required")
            guard = self.directory / "guard"
            if not os.path.lexists(guard):
                write_new(guard, b"0")
            before = regular(guard)
            self.guard = os.open(guard, os.O_RDWR | os.O_NOFOLLOW)
            if os.fstat(self.guard).st_ino != before.st_ino:
                raise PilotError("pilot control lock changed")
            fcntl.flock(self.guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = self.directory / "control.sock"
            if os.path.lexists(path):
                info = path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                    raise PilotError("unexpected pilot control path")
                path.unlink()
            self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server.bind(str(path))
            self.owned_socket = True
            path.chmod(0o600)
            self.server.listen(4)
            self.server.settimeout(0.2)
        except BaseException:
            self.close()
            raise

    def serve_once(self, supervisor: Supervisor):
        try:
            connection, _ = self.server.accept()
        except socket.timeout:
            return
        with connection:
            connection.settimeout(1)
            try:
                request = bytearray()
                while len(request) <= 32 and not request.endswith(b"\n"):
                    part = connection.recv(33 - len(request))
                    if not part:
                        break
                    request.extend(part)
                if request == b"status\n":
                    result = supervisor.status()
                elif request == b"quiesce\n":
                    result = supervisor.quiesce()
                else:
                    raise PilotError("invalid pilot command")
            except Exception:
                result = {"error": "pilot command blocked"}
            try:
                connection.sendall(json.dumps(result, sort_keys=True).encode() + b"\n")
            except OSError:
                pass  # A lost client does not resume or repeat the operation.

    def close(self):
        if self.server is not None:
            self.server.close()
        if self.owned_socket:
            (self.directory / "control.sock").unlink(missing_ok=True)
            self.owned_socket = False
        if self.guard is not None:
            os.close(self.guard)
            self.guard = None


def control(command: str, directory: Path = CONTROL) -> dict:
    if command not in {"status", "quiesce"}:
        raise PilotError("invalid pilot command")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(DRAIN_SECONDS + 10)
        client.connect(str(directory / "control.sock"))
        client.sendall(command.encode() + b"\n")
        raw = bytearray()
        while len(raw) <= 2048 and not raw.endswith(b"\n"):
            part = client.recv(2049 - len(raw))
            if not part:
                break
            raw.extend(part)
    if len(raw) > 2048 or not raw.endswith(b"\n"):
        raise PilotError("invalid pilot response")
    result = json.loads(raw)
    if not isinstance(result, dict) or "error" in result:
        raise PilotError("pilot command blocked")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fresh", "inspect", "status", "quiesce"))
    args = parser.parse_args()
    supervisor = endpoint = None
    try:
        if os.name != "posix":
            raise PilotError("Linux pilot required")
        if args.command in {"status", "quiesce"}:
            print(json.dumps(control(args.command), sort_keys=True))
            return
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
        endpoint = ControlSocket(CONTROL)
        supervisor = Supervisor(ROOT, args.command)
        supervisor.start()
        while not stop.is_set():
            endpoint.serve_once(supervisor)
    except Exception:
        print("pilot operation blocked", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if supervisor is not None:
            supervisor.close()
        if endpoint is not None:
            endpoint.close()


if __name__ == "__main__":
    main()
