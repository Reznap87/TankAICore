from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tankai.dev_orchestrator.publication_cli import main as publication_cli_main
from tankai.dev_orchestrator.release_publication import (
    PublicationError,
    PublicationTarget,
    create_publication_ledger,
    record_artifact_receipt,
    record_source_receipt,
    verify_publication_ledger,
)


COMMIT = "a" * 40


def make_release(tmp_path: Path) -> Path:
    root = tmp_path / "release"
    root.mkdir()
    (root / "project.zip").write_bytes(b"zip-content")
    (root / "project.patch").write_text("patch-content\n", encoding="utf-8")
    return root


def targets() -> list[PublicationTarget]:
    return [
        PublicationTarget("drive-main", "google_drive", "1AbCdEfGhIjKlMnOp"),
        PublicationTarget("github-main", "github", "Reznap87/tankai-core"),
    ]


def md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()


def record_drive_file(ledger: Path, release: Path, artifact: str, remote_id: str) -> None:
    path = release / artifact
    record_artifact_receipt(
        ledger,
        release,
        target_id="drive-main",
        artifact_path=artifact,
        remote_id=remote_id,
        remote_url=f"https://drive.google.com/file/d/{remote_id}/view",
        remote_size=path.stat().st_size,
        remote_digest_algorithm="md5",
        remote_digest=md5(path),
        recorded_at_utc="2026-07-29T10:01:00Z",
    )


def test_plan_is_valid_and_initially_incomplete(tmp_path: Path) -> None:
    release = make_release(tmp_path)
    ledger = release / "publication.json"
    payload = create_publication_ledger(
        release,
        ledger,
        version="1.8.0-test",
        commit=COMMIT,
        branch="main",
        targets=targets(),
        created_at_utc="2026-07-29T10:00:00Z",
    )
    assert ledger.stat().st_mode & 0o777 == 0o644
    assert len(payload["artifacts"]) == 2
    status = verify_publication_ledger(ledger, release_directory=release)
    assert status.valid is True
    assert status.complete is False
    assert status.artifact_count == 2


def test_tampered_event_chain_is_rejected(tmp_path: Path) -> None:
    release = make_release(tmp_path)
    ledger = tmp_path / "publication.json"
    create_publication_ledger(
        release,
        ledger,
        version="1.8.0-test",
        commit=COMMIT,
        branch="main",
        targets=targets(),
    )
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    payload["events"][0]["payload"]["artifact_count"] = 999
    ledger.write_text(json.dumps(payload), encoding="utf-8")
    status = verify_publication_ledger(ledger, release_directory=release)
    assert status.valid is False
    assert any("Ereignishash" in error for error in status.errors)


def test_drive_receipts_and_github_source_complete_release(tmp_path: Path) -> None:
    release = make_release(tmp_path)
    ledger = tmp_path / "publication.json"
    create_publication_ledger(
        release,
        ledger,
        version="1.8.0-test",
        commit=COMMIT,
        branch="main",
        targets=targets(),
    )
    record_drive_file(ledger, release, "project.zip", "1RemoteZipId")
    record_drive_file(ledger, release, "project.patch", "1RemotePatchId")
    record_source_receipt(
        ledger,
        target_id="github-main",
        commit=COMMIT,
        branch="main",
        remote_url=f"https://github.com/Reznap87/tankai-core/commit/{COMMIT}",
        recorded_at_utc="2026-07-29T10:02:00Z",
    )
    status = verify_publication_ledger(ledger, release_directory=release)
    assert status.valid is True
    assert status.complete is True
    assert all(item["complete"] for item in status.target_status)


def test_wrong_remote_checksum_is_rejected(tmp_path: Path) -> None:
    release = make_release(tmp_path)
    ledger = tmp_path / "publication.json"
    create_publication_ledger(
        release,
        ledger,
        version="1.8.0-test",
        commit=COMMIT,
        branch="main",
        targets=targets(),
    )
    with pytest.raises(PublicationError, match="Prüfsumme"):
        record_artifact_receipt(
            ledger,
            release,
            target_id="drive-main",
            artifact_path="project.zip",
            remote_id="1RemoteZipId",
            remote_url="https://drive.google.com/file/d/1RemoteZipId/view",
            remote_size=(release / "project.zip").stat().st_size,
            remote_digest_algorithm="md5",
            remote_digest="0" * 32,
        )


def test_foreign_drive_domain_is_rejected(tmp_path: Path) -> None:
    release = make_release(tmp_path)
    ledger = tmp_path / "publication.json"
    create_publication_ledger(
        release,
        ledger,
        version="1.8.0-test",
        commit=COMMIT,
        branch="main",
        targets=targets(),
    )
    path = release / "project.zip"
    with pytest.raises(PublicationError, match="fremde Domain"):
        record_artifact_receipt(
            ledger,
            release,
            target_id="drive-main",
            artifact_path="project.zip",
            remote_id="1RemoteZipId",
            remote_url="https://example.com/file/d/1RemoteZipId/view",
            remote_size=path.stat().st_size,
            remote_digest_algorithm="md5",
            remote_digest=md5(path),
        )


def test_duplicate_receipt_is_rejected(tmp_path: Path) -> None:
    release = make_release(tmp_path)
    ledger = tmp_path / "publication.json"
    create_publication_ledger(
        release,
        ledger,
        version="1.8.0-test",
        commit=COMMIT,
        branch="main",
        targets=targets(),
    )
    record_drive_file(ledger, release, "project.zip", "1RemoteZipId")
    with pytest.raises(PublicationError, match="bereits"):
        record_drive_file(ledger, release, "project.zip", "1RemoteZipId")


def test_wrong_github_repository_or_commit_is_rejected(tmp_path: Path) -> None:
    release = make_release(tmp_path)
    ledger = tmp_path / "publication.json"
    create_publication_ledger(
        release,
        ledger,
        version="1.8.0-test",
        commit=COMMIT,
        branch="main",
        targets=targets(),
    )
    with pytest.raises(PublicationError, match="geplanten Commit"):
        record_source_receipt(
            ledger,
            target_id="github-main",
            commit="b" * 40,
            branch="main",
            remote_url=f"https://github.com/Reznap87/tankai-core/commit/{'b' * 40}",
        )
    with pytest.raises(PublicationError, match="anderes Repository"):
        record_source_receipt(
            ledger,
            target_id="github-main",
            commit=COMMIT,
            branch="main",
            remote_url=f"https://github.com/other/repository/commit/{COMMIT}",
        )


def test_local_artifact_tampering_is_detected(tmp_path: Path) -> None:
    release = make_release(tmp_path)
    ledger = tmp_path / "publication.json"
    create_publication_ledger(
        release,
        ledger,
        version="1.8.0-test",
        commit=COMMIT,
        branch="main",
        targets=targets(),
    )
    (release / "project.zip").write_bytes(b"changed")
    status = verify_publication_ledger(ledger, release_directory=release)
    assert status.valid is False
    assert any("Artefaktgröße" in error or "Artefaktprüfsumme" in error for error in status.errors)


def test_symlink_artifact_is_rejected(tmp_path: Path) -> None:
    release = make_release(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (release / "linked.txt").symlink_to(outside)
    with pytest.raises(PublicationError, match="Symlink"):
        create_publication_ledger(
            release,
            tmp_path / "publication.json",
            version="1.8.0-test",
            commit=COMMIT,
            branch="main",
            targets=targets(),
        )


def test_cli_plan_record_and_status(tmp_path: Path, capsys) -> None:
    release = make_release(tmp_path)
    ledger = tmp_path / "publication.json"
    assert publication_cli_main([
        "plan",
        "--release-directory", str(release),
        "--ledger", str(ledger),
        "--version", "1.8.0-test",
        "--commit", COMMIT,
        "--drive-target", "drive-main=1AbCdEfGhIjKlMnOp",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    artifact = release / "project.zip"
    assert publication_cli_main([
        "record-artifact",
        "--ledger", str(ledger),
        "--release-directory", str(release),
        "--target-id", "drive-main",
        "--artifact", "project.zip",
        "--remote-id", "1RemoteZipId",
        "--remote-url", "https://drive.google.com/file/d/1RemoteZipId/view",
        "--remote-size", str(artifact.stat().st_size),
        "--remote-digest-algorithm", "md5",
        "--remote-digest", md5(artifact),
    ]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert publication_cli_main([
        "status",
        "--ledger", str(ledger),
        "--release-directory", str(release),
    ]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["ok"] is True
    assert status["complete"] is False
