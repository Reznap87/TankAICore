"""Deterministic, secret-aware release snapshots for TankAI Core.

The backup builder packages only source-controlled project material. Runtime
state, secrets, databases, caches and VCS metadata are excluded by default.
Every archive contains an internal SHA-256 manifest and metadata document and
can be verified without extracting files to disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Sequence


class ReleaseBackupError(RuntimeError):
    """Raised when a release snapshot cannot be built or verified safely."""


@dataclass(frozen=True)
class BackupPolicy:
    excluded_directories: tuple[str, ...] = (
        ".git",
        ".github-cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tankai",
        ".tox",
        ".venv",
        "__pycache__",
        "data",
        "legacy-global-state",
        "node_modules",
        "tenants",
        "venv",
    )
    excluded_suffixes: tuple[str, ...] = (
        ".db",
        ".db-shm",
        ".db-wal",
        ".npz",
        ".pyc",
        ".pyo",
        ".sqlite",
        ".sqlite3",
    )
    excluded_names: tuple[str, ...] = (
        ".coverage",
        ".env",
        "coverage.xml",
        "tankai_runs.jsonl",
    )
    max_file_bytes: int = 32 * 1024 * 1024
    scan_secrets: bool = True
    allow_env_example: bool = True


@dataclass(frozen=True)
class BackupFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class BackupArtifacts:
    archive_path: Path
    metadata_path: Path
    manifest_path: Path
    checksums_path: Path
    archive_sha256: str
    file_count: int
    source_bytes: int


@dataclass(frozen=True)
class BackupVerification:
    valid: bool
    archive_sha256: str
    file_count: int
    source_bytes: int
    errors: tuple[str, ...] = field(default_factory=tuple)


_TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".css",
    ".dockerfile",
    ".env",
    ".example",
    ".gitignore",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

# Split credential markers so this scanner does not flag its own source file.
_SECRET_PATTERNS = (
    ("OpenAI-style API key", re.compile(r"\b" + "sk" + r"-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub classic token", re.compile(r"\b" + "gh" + r"p_[A-Za-z0-9]{20,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub" + r"_pat_[A-Za-z0-9_]{20,}\b")),
    ("Google API key", re.compile(r"\bAI" + r"za[0-9A-Za-z_-]{20,}\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----")),
    ("AWS secret", re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{20,}")),
    ("generic client secret", re.compile(r"(?i)client_secret\s*[:=]\s*['\"]?[^\s'\"]{16,}")),
)

_INTERNAL_MANIFEST = "BACKUP_MANIFEST.sha256"
_INTERNAL_METADATA = "BACKUP_METADATA.json"
_ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)
_DEFAULT_CREATED_AT = datetime(2020, 1, 1, tzinfo=timezone.utc)
_MAX_ARCHIVE_ENTRIES = 10_000
_MAX_ARCHIVE_FILE_BYTES = 32 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 512 * 1024 * 1024


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ReleaseBackupError(f"Pfad verlässt das Projekt: {path}") from exc
    posix = relative.as_posix()
    pure = PurePosixPath(posix)
    if pure.is_absolute() or ".." in pure.parts or not posix:
        raise ReleaseBackupError(f"Unsicherer Archivpfad: {posix!r}")
    return posix


def _is_excluded(relative: PurePosixPath, policy: BackupPolicy) -> bool:
    parts = set(relative.parts)
    if any(name in parts for name in policy.excluded_directories):
        return True
    name = relative.name
    if name in policy.excluded_names:
        return True
    if name.startswith(".env.") and not (
        policy.allow_env_example and name == ".env.example"
    ):
        return True
    return any(name.endswith(suffix) for suffix in policy.excluded_suffixes)


def _looks_textual(path: Path, data: bytes) -> bool:
    if path.suffix.casefold() in _TEXT_SUFFIXES:
        return True
    if b"\x00" in data[:4096]:
        return False
    try:
        data[:8192].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _scan_secret_markers(relative: str, data: bytes) -> None:
    path = Path(relative)
    if not _looks_textual(path, data):
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return
    findings = [label for label, pattern in _SECRET_PATTERNS if pattern.search(text)]
    if findings:
        raise ReleaseBackupError(
            f"Mögliche Zugangsdaten in {relative}: {', '.join(findings)}"
        )


def collect_backup_files(
    project_root: Path,
    *,
    policy: BackupPolicy | None = None,
) -> list[BackupFile]:
    """Return a sorted, validated source file inventory."""
    policy = policy or BackupPolicy()
    root = project_root.resolve(strict=True)
    if not root.is_dir():
        raise ReleaseBackupError(f"Projektwurzel ist kein Verzeichnis: {root}")

    files: list[BackupFile] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        safe_dirs: list[str] = []
        for directory_name in sorted(directory_names):
            candidate = current_path / directory_name
            relative = PurePosixPath(_safe_relative_path(candidate, root))
            if _is_excluded(relative, policy):
                continue
            if candidate.is_symlink():
                raise ReleaseBackupError(f"Symlink-Verzeichnis ist nicht zulässig: {relative}")
            safe_dirs.append(directory_name)
        directory_names[:] = safe_dirs

        for file_name in sorted(file_names):
            candidate = current_path / file_name
            relative_text = _safe_relative_path(candidate, root)
            relative = PurePosixPath(relative_text)
            if _is_excluded(relative, policy):
                continue
            if candidate.is_symlink():
                raise ReleaseBackupError(f"Symlink-Datei ist nicht zulässig: {relative_text}")
            resolved_candidate = candidate.resolve(strict=True)
            if resolved_candidate != root and root not in resolved_candidate.parents:
                raise ReleaseBackupError(
                    f"Datei verlässt die Projektwurzel: {relative_text}"
                )
            file_stat = candidate.stat()
            if not stat.S_ISREG(file_stat.st_mode):
                raise ReleaseBackupError(f"Spezialdatei ist nicht zulässig: {relative_text}")
            if file_stat.st_size > policy.max_file_bytes:
                raise ReleaseBackupError(
                    f"Datei überschreitet das Backup-Limit ({policy.max_file_bytes} Byte): "
                    f"{relative_text}"
                )
            data = candidate.read_bytes()
            if policy.scan_secrets:
                _scan_secret_markers(relative_text, data)
            files.append(
                BackupFile(
                    path=relative_text,
                    size=len(data),
                    sha256=_sha256_bytes(data),
                )
            )
    files.sort(key=lambda item: item.path)
    if not files:
        raise ReleaseBackupError("Keine sicherungsfähigen Projektdateien gefunden")
    return files


def _manifest_text(files: Sequence[BackupFile]) -> str:
    return "".join(f"{item.sha256}  {item.path}\n" for item in files)


def _zip_info(name: str, *, executable: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=_ZIP_TIMESTAMP)
    info.create_system = 3
    mode = 0o755 if executable else 0o644
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _is_executable(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_IXUSR)


def create_release_backup(
    project_root: Path,
    output_directory: Path,
    *,
    version: str,
    commit: str,
    branch: str = "main",
    policy: BackupPolicy | None = None,
    created_at: datetime | None = None,
) -> BackupArtifacts:
    """Build a deterministic ZIP plus external metadata and checksums."""
    if not version.strip():
        raise ReleaseBackupError("Version darf nicht leer sein")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", version):
        raise ReleaseBackupError(f"Ungültige Version: {version!r}")
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}|uncommitted", commit):
        raise ReleaseBackupError(f"Ungültige Commit-Referenz: {commit!r}")
    if not branch.strip() or any(ord(ch) < 32 for ch in branch):
        raise ReleaseBackupError("Ungültiger Branch")

    root = project_root.resolve(strict=True)
    output = output_directory.resolve()
    if output == root or root in output.parents:
        raise ReleaseBackupError(
            "Backup-Ausgabeverzeichnis darf nicht innerhalb des Projekts liegen"
        )
    files = collect_backup_files(root, policy=policy)
    output.mkdir(parents=True, exist_ok=True)
    source_bytes = sum(item.size for item in files)
    source_timestamp = (created_at or _DEFAULT_CREATED_AT).astimezone(timezone.utc)
    generated = datetime.now(timezone.utc)
    root_prefix = f"tankai-core-{version}"
    base_name = f"tankai-core-{version}"
    archive_path = output / f"{base_name}.zip"
    metadata_path = output / f"{base_name}.backup.json"
    manifest_path = output / f"{base_name}.manifest.sha256"
    checksums_path = output / f"{base_name}.SHA256SUMS"

    internal_metadata = {
        "schema_version": 1,
        "project": "TankAI Core",
        "version": version,
        "commit": commit,
        "branch": branch,
        "source_timestamp_utc": source_timestamp.isoformat().replace("+00:00", "Z"),
        "file_count": len(files),
        "source_bytes": source_bytes,
        "root_prefix": root_prefix,
        "excluded_runtime_state": True,
        "files": [asdict(item) for item in files],
    }
    manifest_bytes = _manifest_text(files).encode("utf-8")
    metadata_bytes = (
        json.dumps(internal_metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix="tankai-backup-", suffix=".zip", dir=output
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            for item in files:
                source = root / item.path
                archive_name = f"{root_prefix}/{item.path}"
                archive.writestr(
                    _zip_info(archive_name, executable=_is_executable(source)),
                    source.read_bytes(),
                )
            archive.writestr(
                _zip_info(f"{root_prefix}/{_INTERNAL_MANIFEST}"),
                manifest_bytes,
            )
            archive.writestr(
                _zip_info(f"{root_prefix}/{_INTERNAL_METADATA}"),
                metadata_bytes,
            )
        os.replace(temporary, archive_path)
        os.chmod(archive_path, 0o644)
    finally:
        temporary.unlink(missing_ok=True)

    archive_sha256 = _sha256_file(archive_path)
    external_metadata = {
        **internal_metadata,
        "generated_at_utc": generated.isoformat().replace("+00:00", "Z"),
        "archive": archive_path.name,
        "archive_sha256": archive_sha256,
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "metadata_sha256": _sha256_bytes(metadata_bytes),
    }
    metadata_path.write_text(
        json.dumps(external_metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest_path.write_bytes(manifest_bytes)
    checksum_entries = [
        (archive_sha256, archive_path.name),
        (_sha256_file(metadata_path), metadata_path.name),
        (_sha256_file(manifest_path), manifest_path.name),
    ]
    checksums_path.write_text(
        "".join(f"{digest}  {name}\n" for digest, name in checksum_entries),
        encoding="utf-8",
    )
    return BackupArtifacts(
        archive_path=archive_path,
        metadata_path=metadata_path,
        manifest_path=manifest_path,
        checksums_path=checksums_path,
        archive_sha256=archive_sha256,
        file_count=len(files),
        source_bytes=source_bytes,
    )


def _parse_manifest(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line:
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\x00\r\n]+)", raw_line)
        if match is None:
            raise ReleaseBackupError(f"Ungültige Manifestzeile {line_number}")
        digest, path = match.groups()
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or not path:
            raise ReleaseBackupError(f"Unsicherer Manifestpfad: {path!r}")
        if path in entries:
            raise ReleaseBackupError(f"Doppelter Manifestpfad: {path}")
        entries[path] = digest
    if not entries:
        raise ReleaseBackupError("Manifest ist leer")
    return entries


def verify_release_backup(archive_path: Path) -> BackupVerification:
    """Verify archive structure, metadata and every source file digest."""
    archive = archive_path.resolve(strict=True)
    errors: list[str] = []
    source_bytes = 0
    verified_files = 0
    try:
        with zipfile.ZipFile(archive, "r") as handle:
            infos = handle.infolist()
            if len(infos) > _MAX_ARCHIVE_ENTRIES:
                raise ReleaseBackupError("Archiv enthält zu viele Einträge")
            total_declared = 0
            for info in infos:
                if info.flag_bits & 0x1:
                    raise ReleaseBackupError(f"Verschlüsselter ZIP-Eintrag ist nicht zulässig: {info.filename}")
                if info.is_dir():
                    raise ReleaseBackupError(f"ZIP-Verzeichniseintrag ist nicht zulässig: {info.filename}")
                if info.file_size > _MAX_ARCHIVE_FILE_BYTES:
                    raise ReleaseBackupError(f"ZIP-Eintrag überschreitet das Dateilimit: {info.filename}")
                total_declared += info.file_size
                if total_declared > _MAX_ARCHIVE_TOTAL_BYTES:
                    raise ReleaseBackupError("Archiv überschreitet das Gesamtgrößenlimit")
                if info.compress_size == 0 and info.file_size > 0:
                    raise ReleaseBackupError(f"Ungültige ZIP-Kompressionsdaten: {info.filename}")
                if info.file_size > 1024 * 1024 and info.compress_size > 0:
                    ratio = info.file_size / info.compress_size
                    if ratio > 1000:
                        raise ReleaseBackupError(f"Verdächtiges ZIP-Kompressionsverhältnis: {info.filename}")
            names = [item.filename for item in infos]
            if len(names) != len(set(names)):
                raise ReleaseBackupError("Archiv enthält doppelte Pfade")
            for info in infos:
                pure = PurePosixPath(info.filename)
                if pure.is_absolute() or ".." in pure.parts or not info.filename:
                    raise ReleaseBackupError(f"Unsicherer ZIP-Pfad: {info.filename!r}")
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ReleaseBackupError(f"ZIP enthält einen Symlink: {info.filename}")

            metadata_names = [name for name in names if name.endswith("/" + _INTERNAL_METADATA)]
            manifest_names = [name for name in names if name.endswith("/" + _INTERNAL_MANIFEST)]
            if len(metadata_names) != 1 or len(manifest_names) != 1:
                raise ReleaseBackupError("Interne Backup-Metadaten sind unvollständig")
            metadata = json.loads(handle.read(metadata_names[0]).decode("utf-8"))
            root_prefix = str(metadata.get("root_prefix") or "")
            if not root_prefix or "/" in root_prefix or ".." in root_prefix:
                raise ReleaseBackupError("Ungültiger Root-Prefix in Backup-Metadaten")
            manifest = _parse_manifest(handle.read(manifest_names[0]).decode("utf-8"))
            expected_paths = {f"{root_prefix}/{path}" for path in manifest}
            actual_source_paths = {
                name
                for name in names
                if name not in {metadata_names[0], manifest_names[0]}
            }
            if actual_source_paths != expected_paths:
                missing = sorted(expected_paths - actual_source_paths)
                extra = sorted(actual_source_paths - expected_paths)
                raise ReleaseBackupError(
                    f"Archivinhalt stimmt nicht mit Manifest überein; "
                    f"fehlend={missing[:5]}, zusätzlich={extra[:5]}"
                )
            for relative, expected_digest in sorted(manifest.items()):
                data = handle.read(f"{root_prefix}/{relative}")
                actual_digest = _sha256_bytes(data)
                if actual_digest != expected_digest:
                    raise ReleaseBackupError(f"Prüfsummenfehler: {relative}")
                source_bytes += len(data)
                verified_files += 1
            if int(metadata.get("file_count", -1)) != verified_files:
                raise ReleaseBackupError("Dateianzahl in Metadaten stimmt nicht")
            if int(metadata.get("source_bytes", -1)) != source_bytes:
                raise ReleaseBackupError("Byteanzahl in Metadaten stimmt nicht")
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError, ReleaseBackupError) as exc:
        errors.append(str(exc))

    return BackupVerification(
        valid=not errors,
        archive_sha256=_sha256_file(archive),
        file_count=verified_files,
        source_bytes=source_bytes,
        errors=tuple(errors),
    )


def verify_checksum_file(checksums_path: Path, directory: Path | None = None) -> tuple[str, ...]:
    """Validate a generated SHA256SUMS file and return verified file names."""
    checksums = checksums_path.resolve(strict=True)
    base = (directory or checksums.parent).resolve(strict=True)
    verified: list[str] = []
    for line_number, raw_line in enumerate(checksums.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line:
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\x00\r\n/]+)", raw_line)
        if match is None:
            raise ReleaseBackupError(f"Ungültige SHA256SUMS-Zeile {line_number}")
        digest, name = match.groups()
        candidate = (base / name).resolve(strict=True)
        if candidate.parent != base:
            raise ReleaseBackupError(f"SHA256SUMS-Pfad verlässt das Verzeichnis: {name}")
        if _sha256_file(candidate) != digest:
            raise ReleaseBackupError(f"SHA-256 stimmt nicht: {name}")
        verified.append(name)
    if not verified:
        raise ReleaseBackupError("SHA256SUMS ist leer")
    return tuple(verified)
