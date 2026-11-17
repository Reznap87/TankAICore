"""Hash-pinned, owner/tenant/workspace/time-bound operator source grants.

Loading and selection are offline and do not call AuthStore, providers, search
or the Core. The caller must separately authenticate current owner membership.
OwnerPilot performs that check before consuming a grant. No HTTP input is a
trusted approval, and default/public startup does not activate this source mode.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID

from tankai.core.monetary_budget import MAX_TIMESTAMP
from tankai.core.supplied_sources import ApprovedSourceBundle, SuppliedSource


MAX_MANIFEST_BYTES = 128_000


class SourceManifestError(ValueError):
    """Invalid or unavailable reviewed configuration; contents are not echoed."""


class SourceManifestDenied(PermissionError):
    """The supplied identity or current time has no authority for this grant."""


def _uuid(value: str) -> None:
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise SourceManifestError("invalid source manifest identity") from None


@dataclass(frozen=True, slots=True)
class WorkspaceSourceManifest:
    owner_id: str
    tenant_id: str
    workspace_id: str
    valid_from: int
    valid_until: int
    bundle: ApprovedSourceBundle = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.tenant_id, self.workspace_id):
            _uuid(value)
        if (type(self.valid_from) is not int or type(self.valid_until) is not int
                or not 0 <= self.valid_from < self.valid_until <= MAX_TIMESTAMP
                or self.valid_until - self.valid_from > 7 * 86_400
                or type(self.bundle) is not ApprovedSourceBundle):
            raise SourceManifestError("invalid source manifest bounds")

    def select(self, *, owner_id: str, tenant_id: str, workspace_id: str, now: int) -> ApprovedSourceBundle:
        if ((owner_id, tenant_id, workspace_id) != (self.owner_id, self.tenant_id, self.workspace_id)
                or type(now) is not int or not self.valid_from <= now < self.valid_until):
            raise SourceManifestDenied("source grant does not cover current identity or time")
        return self.bundle


def _object(pairs: list[tuple[str, object]]) -> dict:
    output = {}
    for name, value in pairs:
        if name in output:
            raise SourceManifestError("duplicate source manifest field")
        output[name] = value
    return output


def _constant(value: str) -> None:
    raise SourceManifestError("non-finite source manifest value")


def load_source_manifest(path: str | Path, *, expected_sha256: str) -> WorkspaceSourceManifest:
    """Read one bounded regular UTF-8 JSON file pinned by trusted startup code.

    The digest covers ALL metadata and text. No discovery, directory scan,
    refresh, network access or write occurs. Changes during reading fail closed.
    """
    if type(expected_sha256) is not str or re.fullmatch(r"[a-f0-9]{64}", expected_sha256) is None:
        raise SourceManifestError("explicit manifest digest required")
    try:
        selected = Path(path)
        if selected.is_symlink():
            raise SourceManifestError("regular source manifest required")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(selected, flags), "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_MANIFEST_BYTES:
                raise SourceManifestError("invalid source manifest size or type")
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
            after = os.fstat(handle.fileno())
            current = selected.stat(follow_symlinks=False)
            signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if (signature(before) != signature(after) or signature(after) != signature(current)
                    or not stat.S_ISREG(current.st_mode) or len(raw) != before.st_size):
                raise SourceManifestError("source manifest changed during read")
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise SourceManifestError("source manifest digest mismatch")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
        expected = {"schema_version", "owner_id", "tenant_id", "workspace_id",
                    "valid_from", "valid_until", "approval_id", "sources"}
        if (type(data) is not dict or set(data) != expected or type(data["schema_version"]) is not int
                or data["schema_version"] != 1 or type(data["sources"]) is not list
                or not 1 <= len(data["sources"]) <= 5):
            raise SourceManifestError("invalid source manifest contract")
        sources = []
        for entry in data["sources"]:
            if type(entry) is not dict or set(entry) != {"title", "url", "text", "sha256"}:
                raise SourceManifestError("invalid source manifest entry")
            sources.append(SuppliedSource(**entry))
        bundle = ApprovedSourceBundle(data["approval_id"], tuple(sources))
        return WorkspaceSourceManifest(data["owner_id"], data["tenant_id"], data["workspace_id"],
                                       data["valid_from"], data["valid_until"], bundle)
    except SourceManifestError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise SourceManifestError("source manifest unavailable or invalid") from None
