"""Bounded immutable text snapshots selected by trusted operator configuration.

No URL is resolved or fetched. Digests pin text identity, not truth, freshness or
an authenticated publisher. Core consumes explicit bundles as untrusted data;
owner-pilot admission separately checks current session, workspace and lifetime.
These values alone do not authorize browser uploads, model calls or research.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from urllib.parse import urlsplit

from .web_research import ResearchEvidence, SourceRecord, WebResearchError, _normalize_public_url


MAX_SOURCES = 5
MAX_SOURCE_BYTES = 8_000
MAX_TOTAL_BYTES = 24_000


class SuppliedSourceError(ValueError):
    """Fixed diagnostics exclude source contents, URLs and private paths."""


def _text(value: str, limit: int, *, multiline: bool = False) -> None:
    if (type(value) is not str or len(value) > limit or not value.strip()
            or any((ord(c) < 32 and not (multiline and c in "\n\r\t")) or ord(c) == 127 for c in value)):
        raise SuppliedSourceError("invalid supplied text")
    try:
        if len(value.encode("utf-8")) > limit:
            raise SuppliedSourceError("supplied text exceeds byte limit")
    except UnicodeError:
        raise SuppliedSourceError("invalid supplied encoding") from None


@dataclass(frozen=True, slots=True)
class SuppliedSource:
    title: str
    url: str
    text: str = field(repr=False)
    sha256: str

    def __post_init__(self) -> None:
        _text(self.title, 200)
        if any(c in self.title for c in "[]()<>`*_!\\"):
            raise SuppliedSourceError("invalid supplied title")
        _text(self.text, MAX_SOURCE_BYTES, multiline=True)
        _text(self.url, 2_048)
        if any(c in self.url for c in "[]()<>`\\\"*"):
            raise SuppliedSourceError("invalid supplied URL")
        try:
            normalized = _normalize_public_url(self.url)  # Lexical only: no DNS.
            parsed = urlsplit(normalized)
            if (parsed.scheme != "https" or parsed.port not in {None, 443}
                    or normalized != self.url or parsed.query):
                raise ValueError
        except (ValueError, WebResearchError):
            raise SuppliedSourceError("invalid supplied URL") from None
        if (type(self.sha256) is not str or re.fullmatch(r"[a-f0-9]{64}", self.sha256) is None
                or hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.sha256):
            raise SuppliedSourceError("supplied digest mismatch")

    @property
    def source_id(self) -> str:
        identity = json.dumps([self.title, self.url, self.sha256], ensure_ascii=True,
                              separators=(",", ":")).encode("ascii")
        return "SRC-" + hashlib.sha256(identity).hexdigest()[:8].upper()


@dataclass(frozen=True, slots=True)
class ApprovedSourceBundle:
    approval_id: str
    sources: tuple[SuppliedSource, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.approval_id) is not str or re.fullmatch(r"[A-Za-z0-9-]{1,64}", self.approval_id) is None:
            raise SuppliedSourceError("invalid approval identifier")
        if (type(self.sources) is not tuple or not 1 <= len(self.sources) <= MAX_SOURCES
                or any(type(source) is not SuppliedSource for source in self.sources)):
            raise SuppliedSourceError("invalid supplied source collection")
        if sum(len(source.text.encode("utf-8")) for source in self.sources) > MAX_TOTAL_BYTES:
            raise SuppliedSourceError("supplied collection exceeds byte limit")
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise SuppliedSourceError("duplicate or colliding source identifiers")
        if len({source.url for source in self.sources}) != len(self.sources):
            raise SuppliedSourceError("ambiguous duplicate source URL")

    def evidence(self, query: str) -> ResearchEvidence:
        # Fresh mutable records cannot modify the immutable approved snapshot.
        return ResearchEvidence(query=query[:800], provider="approved-supplied",
            evidence_mode="supplied", approval_id=self.approval_id,
            sources=[SourceRecord(source_id=source.source_id, title=source.title,
                url=source.url, excerpt=source.text, fetched=False,
                content_sha256=source.sha256) for source in self.sources])
