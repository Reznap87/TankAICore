"""Rebuild an isolated complete fixture installation, without live users/providers."""
import json
import gc
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from tankai.web.auth import AuthStore
from tankai.web.data_snapshot import create_snapshot, restore_snapshot, verify_snapshot
from tankai.web.runtime import WorkspaceRuntimeManager


def seed_data(tmp_path, monkeypatch):
    monkeypatch.setenv("TANKAI_LLM", "mock")
    monkeypatch.setenv("TANKAI_CRITIC_LLM", "")
    monkeypatch.setenv("TANKAI_EMBEDDER", "hashing")
    monkeypatch.setenv("TANKAI_REQUIRE_INDEPENDENT_CRITIC", "0")
    root = tmp_path / "source"
    root.mkdir()
    auth = AuthStore(root / "auth.db")
    _, tenant, workspace = auth.create_user_with_tenant(email="snapshot@example.com",
        password="Fixture-password-123!", display_name="Fixture", tenant_name="Fixture")
    manager = WorkspaceRuntimeManager(root)
    runtime = manager.get(tenant_id=tenant, workspace_id=workspace)
    run = runtime.tank.run("Local snapshot fixture")
    runtime.history.append({"goal_id": run.goal_id, "goal": "Fixture task", "final_answer": "Fixture answer"})
    runtime.tank.ltm.add_semantic("Snapshot restores trusted project knowledge", source="fixture")
    runtime.tank.memory.add("Short memory fixture", source="fixture", validity="valid")
    before = runtime.usage_ledger.summary(run.goal_id)
    manager.close()
    # AuthStore opens per-operation connections. Collect released handles before
    # the offline boundary, just as stopping its process closes every handle.
    gc.collect()
    return root, tenant, workspace, run.goal_id, before


def test_isolated_restore_preserves_login_history_memory_index_and_usage(tmp_path, monkeypatch):
    source, tenant, workspace, run_id, usage = seed_data(tmp_path, monkeypatch)
    snapshot, restored = tmp_path / "snapshot", tmp_path / "restored"
    report = create_snapshot(source, snapshot, offline=True)
    assert report["verified"] and report["database_files"] == 4
    assert restore_snapshot(snapshot, restored)["existing_installation_replaced"] is False
    auth = AuthStore(restored / "auth.db")
    login = auth.authenticate(email="snapshot@example.com", password="Fixture-password-123!")
    assert login and login.context.workspace_id == workspace
    manager = WorkspaceRuntimeManager(restored)
    runtime = manager.get(tenant_id=tenant, workspace_id=workspace)
    try:
        assert runtime.history.list_recent()[0]["final_answer"] == "Fixture answer"
        assert runtime.usage_ledger.summary(run_id) == usage
        assert runtime.tank.memory.search("Short memory fixture")
        assert runtime.tank.ltm.retrieve("trusted project knowledge")[0]["content"] == "Snapshot restores trusted project knowledge"
    finally:
        manager.close()


def test_snapshot_requires_quiescence_and_never_overwrites_existing_targets(tmp_path, monkeypatch):
    source, *_ = seed_data(tmp_path, monkeypatch)
    target = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="offline"):
        create_snapshot(source, target)
    assert not target.exists()
    create_snapshot(source, target, offline=True)
    with pytest.raises(FileExistsError):
        create_snapshot(source, target, offline=True)
    with pytest.raises(FileExistsError):
        restore_snapshot(target, source)
    with pytest.raises(ValueError, match="separate"):
        create_snapshot(source, source / "nested", offline=True)


@pytest.mark.parametrize("tamper", ["checksum", "traversal", "extra", "sidecar", "duplicate"])
def test_restore_rejects_tampering_before_creating_target(tmp_path, monkeypatch, tamper):
    source, *_ = seed_data(tmp_path, monkeypatch)
    snapshot, destination = tmp_path / "snapshot", tmp_path / "restored"
    create_snapshot(source, snapshot, offline=True)
    manifest = json.loads((snapshot / "snapshot.json").read_text())
    if tamper == "checksum":
        with (snapshot / "auth.db").open("ab") as handle:
            handle.write(b"tampered")
    elif tamper == "traversal":
        manifest["files"][0]["path"] = "../auth.db"
    elif tamper == "extra":
        (snapshot / "unexpected.env").write_text("should not enter snapshot")
    elif tamper == "sidecar":
        (snapshot / "auth.db-wal").write_bytes(b"injected")
    else:
        manifest["files"].append(dict(manifest["files"][0]))
    (snapshot / "snapshot.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        restore_snapshot(snapshot, destination)
    assert not destination.exists()


def test_unknown_data_file_blocks_incomplete_snapshot(tmp_path, monkeypatch):
    source, *_ = seed_data(tmp_path, monkeypatch)
    (source / "new-feature.db").write_bytes(b"unknown")
    with pytest.raises(ValueError, match="incomplete"):
        create_snapshot(source, tmp_path / "snapshot", offline=True)
    assert not (tmp_path / "snapshot").exists()
