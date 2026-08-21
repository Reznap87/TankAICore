"""Command line interface for the TankAI release publication ledger."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .release_publication import (
    PublicationError,
    PublicationTarget,
    create_publication_ledger,
    record_artifact_receipt,
    record_source_receipt,
    verify_publication_ledger,
)


def _target(value: str, kind: str) -> PublicationTarget:
    try:
        target_id, locator = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Ziel muss TARGET_ID=LOCATOR verwenden") from exc
    return PublicationTarget(target_id=target_id, kind=kind, locator=locator)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TankAI Release-Publikationsledger")
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="Publikationsplan erzeugen")
    plan.add_argument("--release-directory", type=Path, required=True)
    plan.add_argument("--ledger", type=Path, required=True)
    plan.add_argument("--version", required=True)
    plan.add_argument("--commit", required=True)
    plan.add_argument("--branch", default="main")
    plan.add_argument(
        "--drive-target",
        action="append",
        default=[],
        metavar="TARGET_ID=FOLDER_ID",
    )
    plan.add_argument(
        "--github-target",
        action="append",
        default=[],
        metavar="TARGET_ID=OWNER/REPO",
    )

    artifact = sub.add_parser("record-artifact", help="Drive-Artefakt-Receipt eintragen")
    artifact.add_argument("--ledger", type=Path, required=True)
    artifact.add_argument("--release-directory", type=Path, required=True)
    artifact.add_argument("--target-id", required=True)
    artifact.add_argument("--artifact", required=True)
    artifact.add_argument("--remote-id", required=True)
    artifact.add_argument("--remote-url", required=True)
    artifact.add_argument("--remote-size", required=True, type=int)
    artifact.add_argument("--remote-digest-algorithm", required=True, choices=["sha256", "sha1", "md5"])
    artifact.add_argument("--remote-digest", required=True)

    source = sub.add_parser("record-source", help="GitHub-Commit-Receipt eintragen")
    source.add_argument("--ledger", type=Path, required=True)
    source.add_argument("--target-id", required=True)
    source.add_argument("--commit", required=True)
    source.add_argument("--branch", default="main")
    source.add_argument("--remote-url", required=True)

    status = sub.add_parser("status", help="Ledger und lokale Artefakte prüfen")
    status.add_argument("--ledger", type=Path, required=True)
    status.add_argument("--release-directory", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            targets = [
                *(_target(value, "google_drive") for value in args.drive_target),
                *(_target(value, "github") for value in args.github_target),
            ]
            payload = create_publication_ledger(
                args.release_directory,
                args.ledger,
                version=args.version,
                commit=args.commit,
                branch=args.branch,
                targets=targets,
            )
            output = {
                "ok": True,
                "ledger": str(args.ledger.resolve()),
                "release_id": payload["release_id"],
                "artifact_count": len(payload["artifacts"]),
                "target_count": len(payload["targets"]),
            }
        elif args.command == "record-artifact":
            payload = record_artifact_receipt(
                args.ledger,
                args.release_directory,
                target_id=args.target_id,
                artifact_path=args.artifact,
                remote_id=args.remote_id,
                remote_url=args.remote_url,
                remote_size=args.remote_size,
                remote_digest_algorithm=args.remote_digest_algorithm,
                remote_digest=args.remote_digest,
            )
            output = {"ok": True, "event_count": len(payload["events"])}
        elif args.command == "record-source":
            payload = record_source_receipt(
                args.ledger,
                target_id=args.target_id,
                commit=args.commit,
                branch=args.branch,
                remote_url=args.remote_url,
            )
            output = {"ok": True, "event_count": len(payload["events"])}
        else:
            status = verify_publication_ledger(
                args.ledger,
                release_directory=args.release_directory,
            )
            output = {
                "ok": status.valid,
                "complete": status.complete,
                "release_id": status.release_id,
                "artifact_count": status.artifact_count,
                "targets": list(status.target_status),
                "errors": list(status.errors),
            }
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0 if status.valid else 2
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (OSError, PublicationError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
