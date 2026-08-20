"""Tamper-evident publication ledger for TankAI Core releases.

The ledger separates local release verification from external connector calls.
Google Drive artifact mirrors and GitHub source publications are recorded only
when their identifiers, URLs and checksums/commit references match the local
release plan. Every receipt is appended to a SHA-256 hash chain and the JSON
file is replaced atomically under a local process lock.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence
from urllib.parse import urlparse


class PublicationError(RuntimeError):
    """Raised when a publication plan or receipt is invalid."""


@dataclass(frozen=True)
class PublicationTarget:
    target_id: str
    kind: str
    locator: str
    required: bool = True


@dataclass(frozen=True)
class PublicationStatus:
    valid: bool
    complete: bool
    release_id: str
    artifact_count: int
    target_status: tuple[dict[str, Any], ...]
    errors: tuple[str, ...] = ()


_SCHEMA_VERSION = 1
_PROJECT = "TankAI Core"
_ZERO_HASH = "0" * 64
_TARGET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_GITHUB_REPOSITORY = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}"
)
_DRIVE_FOLDER_ID = re.compile(r"[A-Za-z0-9_-]{10,200}|root")
_REMOTE_ID = re.compile(r"[A-Za-z0-9._:@/-]{1,512}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_MAX_ARTIFACTS = 2_000
_MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024
_MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
_ALLOWED_DIGESTS = {"sha256", "sha1", "md5"}
_DIGEST_LENGTHS = {"sha256": 64, "sha1": 40, "md5": 32}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest_bytes(data: bytes, algorithm: str = "sha256") -> str:
    try:
        digest = hashlib.new(algorithm)
    except ValueError as exc:
        raise PublicationError(f"Nicht unterstützter Digest: {algorithm}") from exc
    digest.update(data)
    return digest.hexdigest()


def _digest_file(path: Path, algorithm: str = "sha256") -> str:
    try:
        digest = hashlib.new(algorithm)
    except ValueError as exc:
        raise PublicationError(f"Nicht unterstützter Digest: {algorithm}") from exc
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise PublicationError(f"Artefakt verlässt das Release-Verzeichnis: {path}") from exc
    text = relative.as_posix()
    pure = PurePosixPath(text)
    if not text or pure.is_absolute() or ".." in pure.parts:
        raise PublicationError(f"Unsicherer Artefaktpfad: {text!r}")
    return text


def _validate_target(target: PublicationTarget) -> dict[str, Any]:
    if _TARGET_ID.fullmatch(target.target_id) is None:
        raise PublicationError(f"Ungültige Ziel-ID: {target.target_id!r}")
    kind = target.kind.strip().lower()
    locator = target.locator.strip()
    if kind == "google_drive":
        if _DRIVE_FOLDER_ID.fullmatch(locator) is None:
            raise PublicationError(f"Ungültige Google-Drive-Ordner-ID: {locator!r}")
        mode = "artifact_mirror"
    elif kind == "github":
        if _GITHUB_REPOSITORY.fullmatch(locator) is None:
            raise PublicationError(f"Ungültiges GitHub-Repository: {locator!r}")
        mode = "source_repository"
    else:
        raise PublicationError(f"Nicht unterstütztes Publikationsziel: {kind!r}")
    return {
        "target_id": target.target_id,
        "kind": kind,
        "mode": mode,
        "locator": locator,
        "required": bool(target.required),
    }


def _artifact_inventory(
    release_directory: Path,
    *,
    excluded_paths: Sequence[Path] = (),
) -> list[dict[str, Any]]:
    root = release_directory.resolve(strict=True)
    if not root.is_dir():
        raise PublicationError(f"Release-Verzeichnis ist kein Verzeichnis: {root}")
    excluded = {path.resolve() for path in excluded_paths if path.exists()}
    artifacts: list[dict[str, Any]] = []
    total_bytes = 0
    for candidate in sorted(root.rglob("*")):
        relative = _safe_relative(candidate, root)
        if candidate.is_symlink():
            raise PublicationError(f"Symlink im Release-Verzeichnis ist nicht zulässig: {relative}")
        if candidate.resolve() in excluded:
            continue
        if candidate.is_dir():
            continue
        info = candidate.stat()
        if not stat.S_ISREG(info.st_mode):
            raise PublicationError(f"Spezialdatei ist nicht zulässig: {relative}")
        if info.st_size > _MAX_ARTIFACT_BYTES:
            raise PublicationError(f"Release-Artefakt überschreitet das Dateilimit: {relative}")
        total_bytes += info.st_size
        if total_bytes > _MAX_TOTAL_BYTES:
            raise PublicationError("Release-Artefakte überschreiten das Gesamtgrößenlimit")
        media_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        artifacts.append(
            {
                "path": relative,
                "size": info.st_size,
                "sha256": _digest_file(candidate, "sha256"),
                "media_type": media_type,
            }
        )
        if len(artifacts) > _MAX_ARTIFACTS:
            raise PublicationError("Release-Verzeichnis enthält zu viele Artefakte")
    if not artifacts:
        raise PublicationError("Keine publizierbaren Release-Artefakte gefunden")
    return artifacts


def _release_identity(
    *,
    version: str,
    commit: str,
    branch: str,
    artifacts: Sequence[Mapping[str, Any]],
) -> str:
    identity_artifacts = [
        {
            "path": str(artifact["path"]),
            "size": int(artifact["size"]),
            "sha256": str(artifact["sha256"]),
        }
        for artifact in artifacts
    ]
    identity = {
        "project": _PROJECT,
        "version": version,
        "commit": commit,
        "branch": branch,
        "artifacts": identity_artifacts,
    }
    return _digest_bytes(_canonical_bytes(identity), "sha256")


def _event_hash(event_without_hash: Mapping[str, Any]) -> str:
    return _digest_bytes(_canonical_bytes(event_without_hash), "sha256")


def _new_event(
    *,
    sequence: int,
    event_type: str,
    payload: Mapping[str, Any],
    previous_hash: str,
    recorded_at_utc: str | None = None,
) -> dict[str, Any]:
    event = {
        "sequence": sequence,
        "type": event_type,
        "recorded_at_utc": recorded_at_utc or utcnow(),
        "previous_hash": previous_hash,
        "payload": dict(payload),
    }
    event["event_hash"] = _event_hash(event)
    return event


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class PublicationLedgerStore:
    """Atomic local store with a stale-aware inter-process lock."""

    def __init__(
        self,
        path: str | Path,
        *,
        lock_timeout_seconds: float = 10.0,
        stale_lock_seconds: float = 120.0,
    ) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.lock_timeout_seconds = lock_timeout_seconds
        self.stale_lock_seconds = stale_lock_seconds
        self._thread_lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PublicationError(f"Publikationsledger fehlt: {self.path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise PublicationError(f"Publikationsledger ist nicht lesbar: {self.path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise PublicationError("Publikationsledger muss ein JSON-Objekt sein")
        return payload

    def create(self, payload: Mapping[str, Any]) -> None:
        with self._thread_lock, self._process_lock():
            if self.path.exists():
                raise PublicationError(f"Publikationsledger existiert bereits: {self.path}")
            _atomic_write_json(self.path, payload)

    def update(self, mutate) -> dict[str, Any]:
        with self._thread_lock, self._process_lock():
            payload = self.load()
            updated = mutate(payload)
            if not isinstance(updated, dict):
                raise PublicationError("Ledger-Mutation lieferte kein JSON-Objekt")
            _atomic_write_json(self.path, updated)
            return updated

    @contextmanager
    def _process_lock(self) -> Iterator[None]:
        deadline = time.monotonic() + self.lock_timeout_seconds
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(f"pid={os.getpid()} created={time.time()}\n")
                break
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > self.stale_lock_seconds:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise PublicationError(f"Timeout beim Sperren von {self.path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)


def create_publication_ledger(
    release_directory: Path,
    ledger_path: Path,
    *,
    version: str,
    commit: str,
    branch: str,
    targets: Sequence[PublicationTarget],
    created_at_utc: str | None = None,
) -> dict[str, Any]:
    """Create a deterministic release plan and an initial hash-chain event."""
    version = version.strip()
    branch = branch.strip()
    commit = commit.strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", version):
        raise PublicationError(f"Ungültige Version: {version!r}")
    if _COMMIT.fullmatch(commit) is None:
        raise PublicationError(f"Ungültiger Commit: {commit!r}")
    if not branch or any(ord(character) < 32 for character in branch):
        raise PublicationError("Ungültiger Branch")
    if not targets:
        raise PublicationError("Mindestens ein Publikationsziel ist erforderlich")

    normalized_targets = [_validate_target(target) for target in targets]
    target_ids = [target["target_id"] for target in normalized_targets]
    if len(target_ids) != len(set(target_ids)):
        raise PublicationError("Publikationsziel-IDs müssen eindeutig sein")

    ledger = ledger_path.resolve()
    root = release_directory.resolve(strict=True)
    if root == ledger or root in ledger.parents:
        excluded = [ledger, ledger.with_suffix(ledger.suffix + ".lock")]
    else:
        excluded = []
    artifacts = _artifact_inventory(root, excluded_paths=excluded)
    release_id = _release_identity(
        version=version,
        commit=commit,
        branch=branch,
        artifacts=artifacts,
    )
    created = created_at_utc or utcnow()
    event = _new_event(
        sequence=1,
        event_type="ledger_created",
        payload={
            "release_id": release_id,
            "artifact_count": len(artifacts),
            "target_count": len(normalized_targets),
        },
        previous_hash=_ZERO_HASH,
        recorded_at_utc=created,
    )
    payload: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "project": _PROJECT,
        "release_id": release_id,
        "version": version,
        "commit": commit,
        "branch": branch,
        "created_at_utc": created,
        "artifact_root": ".",
        "artifacts": artifacts,
        "targets": normalized_targets,
        "events": [event],
    }
    PublicationLedgerStore(ledger).create(payload)
    return payload


def _validate_remote_url(target: Mapping[str, Any], remote_id: str, remote_url: str) -> None:
    if _REMOTE_ID.fullmatch(remote_id) is None:
        raise PublicationError(f"Ungültige Remote-ID: {remote_id!r}")
    parsed = urlparse(remote_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise PublicationError("Remote-URL muss eine HTTPS-URL ohne Zugangsdaten sein")
    host = parsed.hostname.casefold()
    kind = target["kind"]
    if kind == "google_drive":
        if host not in {"drive.google.com", "docs.google.com"}:
            raise PublicationError("Google-Drive-Receipt verwendet eine fremde Domain")
        if remote_id not in parsed.path:
            raise PublicationError("Google-Drive-Remote-ID fehlt in der Receipt-URL")
    elif kind == "github":
        if host != "github.com":
            raise PublicationError("GitHub-Receipt verwendet eine fremde Domain")
        expected_prefix = "/" + target["locator"].casefold()
        if not parsed.path.casefold().startswith(expected_prefix):
            raise PublicationError("GitHub-Receipt verweist auf ein anderes Repository")
    else:
        raise PublicationError(f"Unbekannter Zieltyp im Ledger: {kind!r}")


def _validate_ledger_payload(
    payload: Mapping[str, Any],
    *,
    release_directory: Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    normalized = dict(payload)
    try:
        if int(payload.get("schema_version", 0)) != _SCHEMA_VERSION:
            raise PublicationError("Nicht unterstützte Ledger-Schemaversion")
        if payload.get("project") != _PROJECT:
            raise PublicationError("Ledger gehört nicht zu TankAI Core")
        version = str(payload["version"])
        commit = str(payload["commit"])
        branch = str(payload["branch"])
        if _COMMIT.fullmatch(commit) is None:
            raise PublicationError("Ungültige Commit-Referenz im Ledger")

        artifacts_raw = payload.get("artifacts")
        targets_raw = payload.get("targets")
        events_raw = payload.get("events")
        if not isinstance(artifacts_raw, list) or not artifacts_raw:
            raise PublicationError("Ledger enthält keine Artefakte")
        if not isinstance(targets_raw, list) or not targets_raw:
            raise PublicationError("Ledger enthält keine Ziele")
        if not isinstance(events_raw, list) or not events_raw:
            raise PublicationError("Ledger enthält keine Ereignisse")

        artifacts: list[dict[str, Any]] = []
        artifact_names: set[str] = set()
        for raw in artifacts_raw:
            if not isinstance(raw, dict):
                raise PublicationError("Ungültiger Artefakteintrag")
            path = str(raw.get("path", ""))
            pure = PurePosixPath(path)
            if not path or pure.is_absolute() or ".." in pure.parts or path in artifact_names:
                raise PublicationError(f"Ungültiger oder doppelter Artefaktpfad: {path!r}")
            size = int(raw.get("size", -1))
            sha256 = str(raw.get("sha256", ""))
            if size < 0 or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
                raise PublicationError(f"Ungültige Artefaktmetadaten: {path}")
            artifact_names.add(path)
            artifacts.append(
                {
                    "path": path,
                    "size": size,
                    "sha256": sha256,
                    "media_type": str(raw.get("media_type") or "application/octet-stream"),
                }
            )

        targets: list[dict[str, Any]] = []
        target_ids: set[str] = set()
        for raw in targets_raw:
            if not isinstance(raw, dict):
                raise PublicationError("Ungültiger Zieleintrag")
            validated = _validate_target(
                PublicationTarget(
                    target_id=str(raw.get("target_id", "")),
                    kind=str(raw.get("kind", "")),
                    locator=str(raw.get("locator", "")),
                    required=bool(raw.get("required", True)),
                )
            )
            if raw.get("mode") != validated["mode"]:
                raise PublicationError(f"Ungültiger Zielmodus: {raw.get('target_id')}")
            if validated["target_id"] in target_ids:
                raise PublicationError("Doppelte Ziel-ID im Ledger")
            target_ids.add(validated["target_id"])
            targets.append(validated)

        expected_release_id = _release_identity(
            version=version,
            commit=commit,
            branch=branch,
            artifacts=artifacts,
        )
        if payload.get("release_id") != expected_release_id:
            raise PublicationError("Release-ID stimmt nicht mit den Artefakten überein")

        previous_hash = _ZERO_HASH
        artifact_receipts: set[tuple[str, str]] = set()
        source_receipts: set[str] = set()
        for expected_sequence, raw_event in enumerate(events_raw, start=1):
            if not isinstance(raw_event, dict):
                raise PublicationError("Ungültiges Ledger-Ereignis")
            event = dict(raw_event)
            recorded_hash = str(event.pop("event_hash", ""))
            if event.get("sequence") != expected_sequence:
                raise PublicationError("Ledger-Ereignisse sind nicht fortlaufend nummeriert")
            if event.get("previous_hash") != previous_hash:
                raise PublicationError("Ledger-Hashkette ist unterbrochen")
            if recorded_hash != _event_hash(event):
                raise PublicationError("Ledger-Ereignishash ist ungültig")
            event_type = event.get("type")
            event_payload = event.get("payload")
            if not isinstance(event_payload, dict):
                raise PublicationError("Ledger-Ereignis enthält keine gültige Nutzlast")
            if expected_sequence == 1:
                if event_type != "ledger_created" or previous_hash != _ZERO_HASH:
                    raise PublicationError("Erstes Ledger-Ereignis ist ungültig")
                if event_payload.get("release_id") != expected_release_id:
                    raise PublicationError("Genesis-Ereignis enthält eine falsche Release-ID")
            elif event_type == "artifact_published":
                target_id = str(event_payload.get("target_id", ""))
                artifact_path = str(event_payload.get("artifact_path", ""))
                pair = (target_id, artifact_path)
                target = next((item for item in targets if item["target_id"] == target_id), None)
                artifact = next((item for item in artifacts if item["path"] == artifact_path), None)
                if target is None or target["kind"] != "google_drive" or artifact is None:
                    raise PublicationError("Artefakt-Receipt referenziert ein ungültiges Ziel oder Artefakt")
                if pair in artifact_receipts:
                    raise PublicationError("Doppeltes Artefakt-Receipt im Ledger")
                algorithm = str(event_payload.get("remote_digest_algorithm", ""))
                digest = str(event_payload.get("remote_digest", ""))
                if (
                    algorithm not in _ALLOWED_DIGESTS
                    or re.fullmatch(r"[0-9a-f]+", digest) is None
                    or len(digest) != _DIGEST_LENGTHS[algorithm]
                ):
                    raise PublicationError("Ungültiger Remote-Digest im Receipt")
                if int(event_payload.get("remote_size", -1)) != artifact["size"]:
                    raise PublicationError("Remote-Größe stimmt nicht mit dem Artefakt überein")
                _validate_remote_url(
                    target,
                    str(event_payload.get("remote_id", "")),
                    str(event_payload.get("remote_url", "")),
                )
                artifact_receipts.add(pair)
            elif event_type == "source_published":
                target_id = str(event_payload.get("target_id", ""))
                target = next((item for item in targets if item["target_id"] == target_id), None)
                if target is None or target["kind"] != "github":
                    raise PublicationError("Source-Receipt referenziert kein GitHub-Ziel")
                if target_id in source_receipts:
                    raise PublicationError("Doppeltes Source-Receipt im Ledger")
                if event_payload.get("commit") != commit or event_payload.get("branch") != branch:
                    raise PublicationError("GitHub-Receipt referenziert einen anderen Stand")
                _validate_remote_url(
                    target,
                    str(event_payload.get("remote_id", "")),
                    str(event_payload.get("remote_url", "")),
                )
                source_receipts.add(target_id)
            else:
                raise PublicationError(f"Unbekannter Ledger-Ereignistyp: {event_type!r}")
            previous_hash = recorded_hash

        if release_directory is not None:
            root = release_directory.resolve(strict=True)
            for artifact in artifacts:
                candidate = root / artifact["path"]
                if candidate.is_symlink() or not candidate.is_file():
                    raise PublicationError(f"Lokales Artefakt fehlt oder ist unsicher: {artifact['path']}")
                if candidate.stat().st_size != artifact["size"]:
                    raise PublicationError(f"Lokale Artefaktgröße stimmt nicht: {artifact['path']}")
                if _digest_file(candidate, "sha256") != artifact["sha256"]:
                    raise PublicationError(f"Lokale Artefaktprüfsumme stimmt nicht: {artifact['path']}")
            for raw_event in events_raw[1:]:
                if raw_event.get("type") != "artifact_published":
                    continue
                event_payload = raw_event["payload"]
                artifact = next(
                    item for item in artifacts
                    if item["path"] == event_payload["artifact_path"]
                )
                algorithm = event_payload["remote_digest_algorithm"]
                local_remote_digest = _digest_file(root / artifact["path"], algorithm)
                if local_remote_digest != event_payload["remote_digest"]:
                    raise PublicationError(
                        f"Remote-Receipt-Prüfsumme stimmt nicht mit dem lokalen Artefakt überein: "
                        f"{artifact['path']}"
                    )

        normalized.update(
            {
                "artifacts": artifacts,
                "targets": targets,
                "_artifact_receipts": artifact_receipts,
                "_source_receipts": source_receipts,
            }
        )
    except (KeyError, TypeError, ValueError, OSError, PublicationError) as exc:
        errors.append(str(exc))
    return normalized, errors


def verify_publication_ledger(
    ledger_path: Path,
    *,
    release_directory: Path | None = None,
) -> PublicationStatus:
    try:
        payload = PublicationLedgerStore(ledger_path).load()
    except PublicationError as exc:
        return PublicationStatus(False, False, "", 0, (), (str(exc),))
    normalized, errors = _validate_ledger_payload(
        payload,
        release_directory=release_directory,
    )
    if errors:
        return PublicationStatus(
            False,
            False,
            str(payload.get("release_id", "")),
            len(payload.get("artifacts", [])) if isinstance(payload.get("artifacts"), list) else 0,
            (),
            tuple(errors),
        )

    artifacts = normalized["artifacts"]
    artifact_receipts = normalized["_artifact_receipts"]
    source_receipts = normalized["_source_receipts"]
    statuses: list[dict[str, Any]] = []
    complete = True
    for target in normalized["targets"]:
        if target["kind"] == "google_drive":
            missing = [
                artifact["path"]
                for artifact in artifacts
                if (target["target_id"], artifact["path"]) not in artifact_receipts
            ]
            target_complete = not missing
            status = {
                "target_id": target["target_id"],
                "kind": target["kind"],
                "locator": target["locator"],
                "required": target["required"],
                "complete": target_complete,
                "missing_artifacts": missing,
            }
        else:
            target_complete = target["target_id"] in source_receipts
            status = {
                "target_id": target["target_id"],
                "kind": target["kind"],
                "locator": target["locator"],
                "required": target["required"],
                "complete": target_complete,
                "source_commit_published": target_complete,
            }
        if target["required"] and not target_complete:
            complete = False
        statuses.append(status)
    return PublicationStatus(
        True,
        complete,
        normalized["release_id"],
        len(artifacts),
        tuple(statuses),
        (),
    )


def record_artifact_receipt(
    ledger_path: Path,
    release_directory: Path,
    *,
    target_id: str,
    artifact_path: str,
    remote_id: str,
    remote_url: str,
    remote_size: int,
    remote_digest_algorithm: str,
    remote_digest: str,
    recorded_at_utc: str | None = None,
) -> dict[str, Any]:
    """Append a verified Google Drive artifact receipt."""
    algorithm = remote_digest_algorithm.strip().lower()
    digest = remote_digest.strip().lower()
    if algorithm not in _ALLOWED_DIGESTS:
        raise PublicationError(f"Nicht unterstützter Remote-Digest: {algorithm!r}")
    if re.fullmatch(r"[0-9a-f]+", digest) is None or len(digest) != _DIGEST_LENGTHS[algorithm]:
        raise PublicationError("Remote-Digest hat ein ungültiges Format")

    root = release_directory.resolve(strict=True)

    def mutate(payload: dict[str, Any]) -> dict[str, Any]:
        normalized, errors = _validate_ledger_payload(payload, release_directory=root)
        if errors:
            raise PublicationError(errors[0])
        target = next(
            (item for item in normalized["targets"] if item["target_id"] == target_id),
            None,
        )
        artifact = next(
            (item for item in normalized["artifacts"] if item["path"] == artifact_path),
            None,
        )
        if target is None or target["kind"] != "google_drive":
            raise PublicationError("Artefakt-Receipts sind nur für bekannte Google-Drive-Ziele erlaubt")
        if artifact is None:
            raise PublicationError(f"Unbekanntes Release-Artefakt: {artifact_path}")
        if (target_id, artifact_path) in normalized["_artifact_receipts"]:
            raise PublicationError("Für dieses Ziel und Artefakt existiert bereits ein Receipt")
        if int(remote_size) != artifact["size"]:
            raise PublicationError("Remote-Dateigröße stimmt nicht mit dem lokalen Artefakt überein")
        local_digest = _digest_file(root / artifact_path, algorithm)
        if digest != local_digest:
            raise PublicationError("Remote-Prüfsumme stimmt nicht mit dem lokalen Artefakt überein")
        _validate_remote_url(target, remote_id, remote_url)
        events = list(payload["events"])
        events.append(
            _new_event(
                sequence=len(events) + 1,
                event_type="artifact_published",
                payload={
                    "target_id": target_id,
                    "artifact_path": artifact_path,
                    "remote_id": remote_id,
                    "remote_url": remote_url,
                    "remote_size": artifact["size"],
                    "remote_digest_algorithm": algorithm,
                    "remote_digest": digest,
                },
                previous_hash=events[-1]["event_hash"],
                recorded_at_utc=recorded_at_utc,
            )
        )
        updated = dict(payload)
        updated["events"] = events
        return updated

    return PublicationLedgerStore(ledger_path).update(mutate)


def record_source_receipt(
    ledger_path: Path,
    *,
    target_id: str,
    commit: str,
    branch: str,
    remote_url: str,
    recorded_at_utc: str | None = None,
) -> dict[str, Any]:
    """Append a verified GitHub source commit receipt."""
    normalized_commit = commit.strip().lower()

    def mutate(payload: dict[str, Any]) -> dict[str, Any]:
        normalized, errors = _validate_ledger_payload(payload)
        if errors:
            raise PublicationError(errors[0])
        target = next(
            (item for item in normalized["targets"] if item["target_id"] == target_id),
            None,
        )
        if target is None or target["kind"] != "github":
            raise PublicationError("Source-Receipts sind nur für bekannte GitHub-Ziele erlaubt")
        if target_id in normalized["_source_receipts"]:
            raise PublicationError("Für dieses GitHub-Ziel existiert bereits ein Source-Receipt")
        if normalized_commit != normalized["commit"] or branch != normalized["branch"]:
            raise PublicationError("GitHub-Receipt referenziert nicht den geplanten Commit und Branch")
        _validate_remote_url(target, normalized_commit, remote_url)
        parsed = urlparse(remote_url)
        expected_suffix = f"/commit/{normalized_commit}"
        if not parsed.path.casefold().endswith(expected_suffix.casefold()):
            raise PublicationError("GitHub-Receipt muss auf die exakte Commit-URL verweisen")
        events = list(payload["events"])
        events.append(
            _new_event(
                sequence=len(events) + 1,
                event_type="source_published",
                payload={
                    "target_id": target_id,
                    "commit": normalized_commit,
                    "branch": branch,
                    "remote_id": normalized_commit,
                    "remote_url": remote_url,
                },
                previous_hash=events[-1]["event_hash"],
                recorded_at_utc=recorded_at_utc,
            )
        )
        updated = dict(payload)
        updated["events"] = events
        return updated

    return PublicationLedgerStore(ledger_path).update(mutate)
