"""CLI for deterministic TankAI Core backup artifacts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .release_backup import (
    ReleaseBackupError,
    create_release_backup,
    verify_checksum_file,
    verify_release_backup,
)


def _git_value(root: Path, args: list[str], fallback: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return fallback
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else fallback



def _source_timestamp(root: Path) -> datetime:
    raw_epoch = os.getenv("SOURCE_DATE_EPOCH", "").strip()
    if raw_epoch:
        try:
            return datetime.fromtimestamp(int(raw_epoch), tz=timezone.utc)
        except (ValueError, OSError, OverflowError) as exc:
            raise ReleaseBackupError("SOURCE_DATE_EPOCH ist ungültig") from exc
    raw_commit_epoch = _git_value(root, ["show", "-s", "--format=%ct", "HEAD"], "")
    if raw_commit_epoch.isdigit():
        return datetime.fromtimestamp(int(raw_commit_epoch), tz=timezone.utc)
    return datetime(2020, 1, 1, tzinfo=timezone.utc)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tankai-release-backup")
    subcommands = parser.add_subparsers(dest="command", required=True)

    build = subcommands.add_parser("build", help="Erstellt ein geprüftes Release-Backup")
    build.add_argument("--project-root", type=Path, default=Path("."))
    build.add_argument("--output-directory", type=Path, required=True)
    build.add_argument("--version", required=True)
    build.add_argument("--commit")
    build.add_argument("--branch")

    verify = subcommands.add_parser("verify", help="Prüft ZIP und optionale SHA256SUMS")
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--checksums", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "build":
            root = args.project_root.resolve(strict=True)
            commit = args.commit or _git_value(root, ["rev-parse", "HEAD"], "uncommitted")
            branch = args.branch or _git_value(
                root, ["rev-parse", "--abbrev-ref", "HEAD"], "main"
            )
            artifacts = create_release_backup(
                root,
                args.output_directory,
                version=args.version,
                commit=commit,
                branch=branch,
                created_at=_source_timestamp(root),
            )
            print(json.dumps({
                "ok": True,
                "archive": str(artifacts.archive_path),
                "metadata": str(artifacts.metadata_path),
                "manifest": str(artifacts.manifest_path),
                "checksums": str(artifacts.checksums_path),
                "archive_sha256": artifacts.archive_sha256,
                "file_count": artifacts.file_count,
                "source_bytes": artifacts.source_bytes,
            }, ensure_ascii=False, indent=2))
            return 0

        verification = verify_release_backup(args.archive)
        checksums_verified: tuple[str, ...] = ()
        if args.checksums is not None:
            checksums_verified = verify_checksum_file(args.checksums)
        print(json.dumps({
            "ok": verification.valid,
            "archive_sha256": verification.archive_sha256,
            "file_count": verification.file_count,
            "source_bytes": verification.source_bytes,
            "errors": list(verification.errors),
            "checksums_verified": list(checksums_verified),
        }, ensure_ascii=False, indent=2))
        return 0 if verification.valid else 2
    except (OSError, ReleaseBackupError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
