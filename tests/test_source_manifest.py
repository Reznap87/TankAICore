"""Offline bounded operator grants; no model, search, DNS or account changes."""
from dataclasses import FrozenInstanceError
import hashlib
import json
import os
import socket
import subprocess
import sys
from types import SimpleNamespace
from uuid import UUID

import pytest

from tankai.core.supplied_sources import ApprovedSourceBundle, SuppliedSource, SuppliedSourceError
from tankai.web import source_manifest as module
from tankai.web.source_manifest import (
    SourceManifestDenied, SourceManifestError, WorkspaceSourceManifest, load_source_manifest,
)


NOW = 1_790_000_000
OWNER, TENANT, WORKSPACE = [str(UUID(int=i)) for i in (1, 2, 3)]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("source grant validation must never resolve or connect")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def source(text="Reviewed fixture text.", **changes):
    data = dict(title="Reviewed fixture", url="https://example.org/facts", text=text,
                sha256=hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest())
    data.update(changes)
    return SuppliedSource(**data)


def payload():
    item = source()
    return dict(schema_version=1, owner_id=OWNER, tenant_id=TENANT, workspace_id=WORKSPACE,
                valid_from=NOW, valid_until=NOW+3600, approval_id="review-001",
                sources=[dict(title=item.title, url=item.url, text=item.text, sha256=item.sha256)])


def write(tmp_path, data=None, raw=None):
    raw = raw if raw is not None else json.dumps(payload() if data is None else data).encode()
    path = tmp_path / "sources.json"
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def test_pinned_manifest_selects_exact_identity_and_keeps_immutable_content(tmp_path):
    path, digest = write(tmp_path)
    grant = load_source_manifest(path, expected_sha256=digest)
    selected = grant.select(owner_id=OWNER, tenant_id=TENANT, workspace_id=WORKSPACE, now=NOW)
    assert selected.approval_id == "review-001" and selected.sources[0].text == source().text
    assert selected.sources[0].source_id == source().source_id
    assert selected.sources[0].text not in repr(grant)
    assert selected.sources[0].text not in repr(selected.sources[0])
    with pytest.raises(FrozenInstanceError):
        selected.sources[0].text = "changed"
    with pytest.raises(FrozenInstanceError):
        grant.workspace_id = str(UUID(int=10))


@pytest.mark.parametrize("changes", [
    {"owner_id":str(UUID(int=4))}, {"tenant_id":str(UUID(int=4))},
    {"workspace_id":str(UUID(int=4))}, {"now":NOW-1}, {"now":NOW+3600},
    {"now":None}, {"now":True}, {"now":float(NOW)},
])
def test_other_identity_or_expired_approval_never_selects_source_content(tmp_path, changes):
    path, digest = write(tmp_path)
    grant = load_source_manifest(path, expected_sha256=digest)
    args = dict(owner_id=OWNER, tenant_id=TENANT, workspace_id=WORKSPACE, now=NOW)
    args.update(changes)
    with pytest.raises(SourceManifestDenied):
        grant.select(**args)


@pytest.mark.parametrize("changes", [
    {"sha256":"0"*64}, {"sha256":True}, {"title":"[spoof](https://evil.test/)"},
    {"title":"line\nline"}, {"text":""}, {"text":"\x00private"},
    {"text":"a"*8001}, {"text":"ä"*4001}, {"text":"\ud800"},
    {"url":"file:///private-secret"}, {"url":"https://user:private@example.org/facts"},
    {"url":"https://127.0.0.1/facts"}, {"url":"http://example.org/facts"},
    {"url":"https://example.org/facts?key=private"}, {"url":"https://example.org/facts#fragment"},
    {"url":"https://example.org:80/facts"}, {"url":"https://example.org/[spoof](x)"},
])
def test_invalid_source_is_bounded_and_rejected_without_echoing_content(changes):
    with pytest.raises(SuppliedSourceError) as failure:
        source(**changes)
    assert "private" not in str(failure.value) and "secret" not in str(failure.value)


def test_text_identity_changes_id_and_duplicate_or_ambiguous_sources_are_rejected(monkeypatch):
    first, second = source(), source("changed", url="https://example.org/other")
    assert first.source_id != second.source_id
    with pytest.raises(SuppliedSourceError, match="duplicate"):
        ApprovedSourceBundle("review", (first, first))
    with pytest.raises(SuppliedSourceError, match="ambiguous"):
        ApprovedSourceBundle("review", (first, source("changed")))
    monkeypatch.setattr(SuppliedSource, "source_id", property(lambda self:"SRC-00000000"))
    with pytest.raises(SuppliedSourceError, match="colliding"):
        ApprovedSourceBundle("review", (first, second))


@pytest.mark.parametrize("items", [(), [], ("not a source",), (source(),)*6])
def test_collection_shape_and_count_are_bounded(items):
    with pytest.raises(SuppliedSourceError):
        ApprovedSourceBundle("review", items)


def test_aggregate_utf8_bytes_are_bounded():
    items = tuple(source(str(i)+"a"*7999, url=f"https://example.org/{i}") for i in range(4))
    with pytest.raises(SuppliedSourceError, match="byte limit"):
        ApprovedSourceBundle("review", items)


@pytest.mark.parametrize("changes", [
    {"schema_version":True}, {"schema_version":2}, {"extra":"private"},
    {"owner_id":"not-uuid"}, {"tenant_id":None}, {"workspace_id":True},
    {"valid_from":True}, {"valid_until":NOW}, {"valid_until":NOW+7*86400+1},
    {"valid_until":10**50}, {"valid_until":float(NOW+3600)},
    {"approval_id":"[SRC-DEADBEEF]"}, {"sources":[]}, {"sources":None},
])
def test_invalid_manifest_metadata_is_rejected_even_with_matching_file_digest(tmp_path, changes):
    data = payload()
    data.update(changes)
    path, digest = write(tmp_path, data)
    with pytest.raises(SourceManifestError):
        load_source_manifest(path, expected_sha256=digest)


@pytest.mark.parametrize("raw", [
    b'{"schema_version":1,"schema_version":1}',
    b'{"sources":[{"text":"one","text":"two"}]}',
    b'{"valid_until":NaN}', b'{"valid_until":Infinity}',
    b'{"valid_until":-Infinity}', b'[]', b'null', b'not JSON', b'\xff', b'',
])
def test_malformed_ambiguous_or_nonfinite_json_is_rejected(tmp_path, raw):
    path, digest = write(tmp_path, raw=raw)
    with pytest.raises(SourceManifestError):
        load_source_manifest(path, expected_sha256=digest)


def test_source_fields_and_source_digest_are_strict(tmp_path):
    for mutation in ({"text":"tampered private content"}, {"extra":"private"}):
        data = payload()
        data["sources"][0].update(mutation)
        path, digest = write(tmp_path, data)
        with pytest.raises(SourceManifestError) as failure:
            load_source_manifest(path, expected_sha256=digest)
        assert "private" not in str(failure.value)


def test_file_tampering_and_wrong_operator_pin_are_rejected(tmp_path):
    path, digest = write(tmp_path)
    with pytest.raises(SourceManifestError, match="digest"):
        load_source_manifest(path, expected_sha256="0"*64)
    data = payload()
    data["owner_id"] = str(UUID(int=99))
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SourceManifestError, match="digest"):
        load_source_manifest(path, expected_sha256=digest)


@pytest.mark.parametrize("pin", [None, True, "", "A"*64, "0"*65])
def test_missing_or_noncanonical_pin_is_rejected_before_any_file_open(tmp_path, monkeypatch, pin):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid pin must not read files")
    monkeypatch.setattr(module.os, "open", forbidden)
    with pytest.raises(SourceManifestError):
        load_source_manifest(tmp_path / "private-secret", expected_sha256=pin)


def test_oversized_missing_and_directory_inputs_fail_closed(tmp_path):
    path, digest = write(tmp_path, raw=b"a"*(module.MAX_MANIFEST_BYTES+1))
    for candidate in (path, tmp_path, tmp_path/"missing"):
        with pytest.raises(SourceManifestError):
            load_source_manifest(candidate, expected_sha256=digest)


def test_file_changed_during_read_is_rejected(tmp_path, monkeypatch):
    path, digest = write(tmp_path)
    original = module.os.fstat
    calls = []
    def changed(fd):
        value = original(fd)
        calls.append(fd)
        if len(calls) == 2:
            return SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino,
                st_size=value.st_size, st_mtime_ns=value.st_mtime_ns+1, st_mode=value.st_mode)
        return value
    monkeypatch.setattr(module.os, "fstat", changed)
    with pytest.raises(SourceManifestError, match="changed"):
        load_source_manifest(path, expected_sha256=digest)


def test_symlink_is_not_accepted_as_reviewed_manifest(tmp_path):
    path, digest = write(tmp_path)
    link = tmp_path / "link.json"
    try:
        link.symlink_to(path)
    except OSError:
        pytest.skip("symlink creation is unavailable for this account")
    with pytest.raises(SourceManifestError):
        load_source_manifest(link, expected_sha256=digest)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation requires POSIX")
def test_fifo_input_is_rejected_without_waiting_for_a_writer(tmp_path):
    path = tmp_path / "sources.pipe"
    os.mkfifo(path)
    code = '''
import sys
from tankai.web.source_manifest import SourceManifestError, load_source_manifest
try:
    load_source_manifest(sys.argv[1], expected_sha256="0"*64)
except SourceManifestError:
    sys.exit(0)
sys.exit(1)
'''
    result = subprocess.run([sys.executable, "-c", code, str(path)],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
