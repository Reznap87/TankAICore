from __future__ import annotations

import json
from pathlib import Path

import pytest

from tankai.dev_orchestrator.container_runtime import RuntimeSecurityProfile
from tankai.dev_orchestrator.host_readiness import (
    HostProbe,
    StorageProbe,
    _filesystem_type,
    _read_mounts,
    collect_host_probe,
    evaluate_host_readiness,
    main,
)


DATA_ROOT = Path("/srv/tankai")


def _host_probe(
    *,
    host_mode: str = "wsl2",
    effective_uid: int = 1000,
    filesystem_type: str = "ext4",
    cpu_count: int = 4,
    memory_mb: int = 16384,
    free_disk_mb: int = 81920,
) -> HostProbe:
    names = ("data_root", "queue", "fences", "repositories", "worktrees", "states")
    storage = tuple(
        StorageProbe(
            name=name,
            path=DATA_ROOT if name == "data_root" else DATA_ROOT / name,
            exists=True,
            is_directory=True,
            is_symlink=False,
            writable=True,
            world_writable=False,
            filesystem_type=filesystem_type,
        )
        for name in names
    )
    return HostProbe(
        system="Linux",
        kernel_release="6.6.87.2-microsoft-standard-WSL2",
        host_mode=host_mode,
        effective_uid=effective_uid,
        cpu_count=cpu_count,
        memory_mb=memory_mb,
        free_disk_mb=free_disk_mb,
        storage=storage,
    )


def _runtime_profile(
    *, rootless: bool = True, os_type: str = "linux", cgroup_version: str = "2"
) -> RuntimeSecurityProfile:
    return RuntimeSecurityProfile(
        runtime="docker",
        server_version="27.1.0",
        rootless=rootless,
        os_type=os_type,
        cgroup_version=cgroup_version,
        security_options=("name=rootless", "name=seccomp,profile=builtin"),
    )


def _checks(receipt: dict[str, object]) -> dict[str, str]:
    return {
        item["id"]: item["status"]
        for item in receipt["checks"]  # type: ignore[index,union-attr]
    }


def test_ready_receipt_requires_complete_safe_single_host_contract() -> None:
    receipt = evaluate_host_readiness(
        _host_probe(),
        data_root=DATA_ROOT,
        runtime_binary="docker",
        runtime_profile=_runtime_profile(),
    )

    assert receipt["ready"] is True
    assert set(_checks(receipt).values()) == {"PASS"}
    assert receipt["next_actions"] == []
    assert receipt["safety"] == {
        "mutations_performed": False,
        "secret_values_read": False,
        "network_calls_performed": False,
        "web_runtime_socket_access_granted": False,
    }


def test_wsl1_windows_backed_root_runner_and_rootful_runtime_fail_closed() -> None:
    receipt = evaluate_host_readiness(
        _host_probe(
            host_mode="wsl1",
            effective_uid=0,
            filesystem_type="9p",
            memory_mb=4096,
        ),
        data_root=DATA_ROOT,
        runtime_binary="docker",
        runtime_profile=_runtime_profile(rootless=False, cgroup_version="1"),
    )
    checks = _checks(receipt)

    assert receipt["ready"] is False
    assert checks["host.linux_or_wsl2"] == "FAIL"
    assert checks["host.dedicated_non_root_user"] == "FAIL"
    assert checks["host.memory_capacity"] == "FAIL"
    assert checks["storage.local_filesystem"] == "FAIL"
    assert checks["runtime.rootless"] == "FAIL"
    assert checks["runtime.cgroup_v2"] == "FAIL"


def test_missing_runtime_makes_security_profile_unknown() -> None:
    receipt = evaluate_host_readiness(
        _host_probe(),
        data_root=DATA_ROOT,
        runtime_binary="docker",
        runtime_profile=None,
        runtime_error="Container-Runtime nicht gefunden: docker",
    )
    checks = _checks(receipt)

    assert receipt["ready"] is False
    assert checks["runtime.available"] == "FAIL"
    assert checks["runtime.linux"] == "UNKNOWN"
    assert checks["runtime.rootless"] == "UNKNOWN"
    assert checks["runtime.cgroup_v2"] == "UNKNOWN"


def test_host_probe_does_not_create_missing_storage_layout(tmp_path: Path) -> None:
    data_root = tmp_path / "missing" / "tankai"
    probe = collect_host_probe(data_root)

    assert not data_root.exists()
    assert all(not item.exists for item in probe.storage)

    with pytest.raises(ValueError, match="absoluter Pfad"):
        collect_host_probe(Path("relative/tankai"))

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-root"
    link.symlink_to(target, target_is_directory=True)
    linked_probe = collect_host_probe(link)
    assert linked_probe.storage[0].is_symlink is True


def test_mountinfo_parser_detects_windows_and_local_mounts(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "36 25 0:32 / / rw,relatime - ext4 /dev/root rw\n"
        "44 36 0:43 / /mnt/c rw,relatime - 9p C: rw\n"
        "45 36 0:44 / /srv/tankai\\040data rw,relatime - xfs /dev/sdb rw\n",
        encoding="utf-8",
    )
    mounts = _read_mounts(mountinfo)

    assert _filesystem_type(Path("/mnt/c/project"), mounts) == "9p"
    assert _filesystem_type(Path("/srv/tankai data/states"), mounts) == "xfs"
    assert _filesystem_type(Path("/var/lib/tankai"), mounts) == "ext4"


def test_cli_prints_machine_readable_receipt(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "tankai.dev_orchestrator.host_readiness.collect_host_probe",
        lambda _path: _host_probe(host_mode="linux"),
    )
    monkeypatch.setattr(
        "tankai.dev_orchestrator.host_readiness.inspect_runtime",
        lambda _runtime: (_runtime_profile(), None),
    )

    assert main(["--data-root", str(DATA_ROOT), "--container-runtime", "podman"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["gate"] == "ops.development.single_host_runner_bootstrap"
    assert receipt["ready"] is True
