from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

source = Path(__file__).resolve().parents[1] / ".github/scripts/rootless_code_repair_smoke.py"
spec = importlib.util.spec_from_file_location("rootless_code_repair_smoke", source)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
REPOSITORY = "11111111-1111-4111-8111-111111111111"


def scope(root):
    (root / "smoke-scope.json").write_text(json.dumps({"version": 1, "repository_id": REPOSITORY}), encoding="utf-8")


def test_cleanup_without_created_scope_does_not_contact_runtime(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        pytest.fail("An uncreated scope cannot start runtime cleanup")
    monkeypatch.setattr(smoke, "DockerCommandExecutor", unavailable)
    smoke.cleanup(tmp_path / "absent", "podman")


def test_cleanup_refuses_container_with_other_repository_label(tmp_path, monkeypatch):
    scope(tmp_path)
    removed = []
    fake = SimpleNamespace(ensure_available=lambda: "4.9.3",
        list_managed_containers=lambda **kwargs: [SimpleNamespace(container_id="a" * 64,
            labels={"tankai.repository_id": "other-repository", "tankai.managed": "true"})],
        remove_container=removed.append)
    monkeypatch.setattr(smoke, "DockerCommandExecutor", lambda *args, **kwargs: fake)
    with pytest.raises(RuntimeError, match="outside"):
        smoke.cleanup(tmp_path, "podman")
    assert removed == []


def test_cleanup_removes_only_validated_scope_container(tmp_path, monkeypatch):
    scope(tmp_path)
    removed = []
    fake = SimpleNamespace(ensure_available=lambda: "4.9.3",
        list_managed_containers=lambda **kwargs: [SimpleNamespace(container_id="b" * 64,
            labels={"tankai.repository_id": REPOSITORY, "tankai.managed": "true"})],
        remove_container=removed.append)
    monkeypatch.setattr(smoke, "DockerCommandExecutor", lambda *args, **kwargs: fake)
    smoke.cleanup(tmp_path, "podman")
    assert removed == ["b" * 64]


def test_real_smoke_refuses_unsupported_host_before_creating_files(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke.sys, "platform", "win32")
    root = tmp_path / "not-created"
    with pytest.raises(RuntimeError, match="unprivileged Linux"):
        smoke.smoke(root, "podman", tmp_path / "image", tmp_path / "receipt", None)
    assert not root.exists()
