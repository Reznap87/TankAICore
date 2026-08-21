from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tankai.dev_orchestrator.release_backup import (
    BackupPolicy,
    ReleaseBackupError,
    collect_backup_files,
    create_release_backup,
    verify_checksum_file,
    verify_release_backup,
)
from tankai.dev_orchestrator.release_cli import main as release_cli_main


def make_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "tankai").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "tankai" / "core.py").write_text("print('tankai')\n", encoding="utf-8")
    (root / "tests" / "test_core.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    (root / "README.md").write_text("# TankAI\n", encoding="utf-8")
    (root / ".env.example").write_text("TANKAI_LLM=openai\n", encoding="utf-8")
    return root


def test_backup_is_deterministic_and_verifiable(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    fixed = datetime(2026, 7, 28, 20, 0, tzinfo=timezone.utc)
    first = create_release_backup(
        root,
        tmp_path / "first",
        version="1.7.0-test",
        commit="a" * 40,
        created_at=fixed,
    )
    second = create_release_backup(
        root,
        tmp_path / "second",
        version="1.7.0-test",
        commit="a" * 40,
        created_at=fixed,
    )
    assert first.archive_sha256 == second.archive_sha256
    assert first.archive_path.read_bytes() == second.archive_path.read_bytes()
    assert first.archive_path.stat().st_mode & 0o777 == 0o644
    verification = verify_release_backup(first.archive_path)
    assert verification.valid is True
    assert verification.file_count == 4
    assert verify_checksum_file(first.checksums_path) == (
        first.archive_path.name,
        first.metadata_path.name,
        first.manifest_path.name,
    )


def test_runtime_state_and_secrets_are_excluded(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("secret", encoding="utf-8")
    (root / ".env").write_text("TOKEN=not-backed-up", encoding="utf-8")
    (root / "legacy-global-state").mkdir()
    (root / "legacy-global-state" / "memory.db").write_bytes(b"db")
    (root / "tankai" / "cache.pyc").write_bytes(b"cache")
    paths = [item.path for item in collect_backup_files(root)]
    assert ".env.example" in paths
    assert ".env" not in paths
    assert not any(path.startswith(".git/") for path in paths)
    assert not any(path.startswith("legacy-global-state/") for path in paths)
    assert not any(path.endswith(".pyc") for path in paths)


def test_probable_credential_blocks_backup(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    marker = "sk" + "-" + "A" * 32
    (root / "accidental-secret.txt").write_text(marker, encoding="utf-8")
    with pytest.raises(ReleaseBackupError, match="Zugangsdaten"):
        create_release_backup(
            root,
            tmp_path / "out",
            version="1.7.0-test",
            commit="b" * 40,
        )


def test_secret_scan_can_only_be_disabled_explicitly(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    marker = "gh" + "p_" + "B" * 30
    (root / "fixture.txt").write_text(marker, encoding="utf-8")
    files = collect_backup_files(root, policy=BackupPolicy(scan_secrets=False))
    assert any(item.path == "fixture.txt" for item in files)


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (root / "linked.txt").symlink_to(outside)
    with pytest.raises(ReleaseBackupError, match="Symlink"):
        collect_backup_files(root)


def test_tampered_archive_fails_verification(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    artifacts = create_release_backup(
        root,
        tmp_path / "out",
        version="1.7.0-test",
        commit="c" * 40,
    )
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(artifacts.archive_path, "r") as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename.endswith("README.md"):
                data += b"tampered"
            target.writestr(info, data)
    verification = verify_release_backup(tampered)
    assert verification.valid is False
    assert any("Prüfsummenfehler" in error for error in verification.errors)


def test_cli_build_and_verify(tmp_path: Path, capsys) -> None:
    root = make_project(tmp_path)
    output = tmp_path / "release"
    assert release_cli_main([
        "build",
        "--project-root", str(root),
        "--output-directory", str(output),
        "--version", "1.7.0-test",
        "--commit", "d" * 40,
        "--branch", "main",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert release_cli_main([
        "verify",
        "--archive", payload["archive"],
        "--checksums", payload["checksums"],
    ]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["ok"] is True
    assert len(verified["checksums_verified"]) == 3



def test_output_directory_inside_project_is_rejected(tmp_path: Path) -> None:
    root = make_project(tmp_path)
    with pytest.raises(ReleaseBackupError, match="nicht innerhalb"):
        create_release_backup(
            root,
            root / "release",
            version="1.7.0-test",
            commit="f" * 40,
        )

def test_archive_has_no_path_traversal_or_symlinks(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../escape.txt", b"bad")
    verification = verify_release_backup(archive)
    assert verification.valid is False
    assert any("Unsicherer ZIP-Pfad" in error for error in verification.errors)
