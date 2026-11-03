"""Cold whole-root rehearsal and failure-before-write tests, synthetic data only."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cold_recovery_drill.py"
spec = importlib.util.spec_from_file_location("cold_recovery_drill", SCRIPT)
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


@pytest.fixture
def image():
    files = {"auth.db":b"synthetic accounts", "monetary.sqlite":b"synthetic pending hold",
             "approved-source.json":b"synthetic reviewed text"}
    key = b"s" * 32
    manifest = {name:drill.digest(data) for name, data in files.items()}
    encrypted = drill.seal(files, key)
    return files, key, manifest, encrypted


def test_full_cold_rehearsal_uses_lost_source_and_two_fresh_verifier_processes():
    receipt = drill.run_drill()
    assert receipt["ok"] and receipt["synthetic_only"]
    assert receipt["writer_exited_before_capture"] and receipt["source_removed_before_restore"]
    assert receipt["file_count"] == 15 and receipt["fresh_verifier_processes"] == 2
    assert receipt["checks"] == {"login_and_roles":True, "tenant_isolation":True,
        "history_and_runs":True, "hot_ltm_vector_cold_memory":True, "source_grant_loaded":True,
        "old_sessions_and_agent_tokens_revoked":True, "budget_and_replay_preserved":True,
        "blocked_owner_http_status":429, "provider_calls":0}
    assert receipt["seed_llm_mode"] == "simulation"
    assert not receipt["production_host_verified"] and not receipt["live_writer_fencing_verified"]


def test_valid_synthetic_capsule_restores_exact_component_bytes(tmp_path, image):
    files, key, manifest, encrypted = image
    destination = tmp_path / "new-root"
    drill.restore(encrypted, key, drill.digest(encrypted), manifest, destination)
    assert drill.inventory(destination) == files


@pytest.mark.parametrize("damage", ["pin", "key", "ciphertext", "nonce", "tag", "truncated", "appended", "format"])
def test_invalid_capsule_creates_no_destination(tmp_path, image, damage):
    _, key, manifest, encrypted = image
    pin = drill.digest(encrypted)
    if damage == "key":
        key = b"x" * 32
    elif damage == "pin":
        pin = "0" * 64
    else:
        data = bytearray(encrypted)
        if damage == "ciphertext": data[len(drill.MAGIC)+15] ^= 1
        if damage == "nonce": data[len(drill.MAGIC)] ^= 1
        if damage == "tag": data[-1] ^= 1
        if damage == "format": data[0] ^= 1
        if damage == "truncated": data = data[:-3]
        if damage == "appended": data += b"extra"
        encrypted = bytes(data)
        pin = drill.digest(encrypted)  # A recomputed hash cannot repair GCM authentication.
    destination = tmp_path / "must-not-exist"
    with pytest.raises(drill.DrillError):
        drill.restore(encrypted, key, pin, manifest, destination)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("damage", ["missing-account", "extra-file", "old-budget", "changed-source"])
def test_missing_or_mixed_components_fail_even_in_authentic_envelope(tmp_path, image, damage):
    files, key, manifest, _ = image
    files = dict(files)
    if damage == "missing-account": del files["auth.db"]
    if damage == "extra-file": files["unexpected"] = b"foreign"
    if damage == "old-budget": files["monetary.sqlite"] = b"stale ledger without hold"
    if damage == "changed-source": files["approved-source.json"] = b"unreviewed source"
    encrypted = drill.seal(files, key)
    destination = tmp_path / "must-not-exist"
    with pytest.raises(drill.DrillError):
        drill.restore(encrypted, key, drill.digest(encrypted), manifest, destination)
    assert not destination.exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/windows-path", "a\\b", "a/../escape"])
def test_unsafe_capsule_paths_fail_before_writes(tmp_path, name):
    key, value = b"s" * 32, b"synthetic"
    manifest = {name:drill.digest(value)}
    encrypted = drill.seal({name:value}, key)
    destination = tmp_path / "must-not-exist"
    with pytest.raises(drill.DrillError):
        drill.restore(encrypted, key, drill.digest(encrypted), manifest, destination)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_existing_destination_is_never_overwritten(tmp_path, image):
    _, key, manifest, encrypted = image
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "preserve"
    sentinel.write_bytes(b"unchanged")
    with pytest.raises(FileExistsError):
        drill.restore(encrypted, key, drill.digest(encrypted), manifest, destination)
    assert {p.name:p.read_bytes() for p in destination.iterdir()} == {"preserve":b"unchanged"}


def test_inventory_rejects_uncheckpointed_sidecar(tmp_path):
    (tmp_path / "auth.db-wal").write_bytes(b"uncheckpointed")
    with pytest.raises(drill.DrillError, match="sidecar"):
        drill.inventory(tmp_path)


def test_inventory_rejects_file_alias(tmp_path):
    original, alias = tmp_path / "original", tmp_path / "alias"
    original.write_bytes(b"synthetic")
    try:
        os.link(original, alias)
    except (OSError, NotImplementedError):
        pytest.skip("hard links unavailable")
    with pytest.raises(drill.DrillError, match="invalid"):
        drill.inventory(tmp_path)


def test_child_socket_guard_blocks_public_dns_and_connect_before_network():
    code = f'''
import sys, socket
sys.path.insert(0, {str(SCRIPT.parent)!r})
from cold_recovery_drill import network_guard, DrillError
network_guard()
blocked = 0
for action in [lambda:socket.getaddrinfo("example.test",443),
               lambda:socket.socket().connect(("203.0.113.1",443))]:
    try:
        action()
    except DrillError:
        blocked += 1
assert blocked == 2
print("NETWORK_BLOCKED")
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "NETWORK_BLOCKED"
