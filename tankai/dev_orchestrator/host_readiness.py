"""Read-only readiness receipt for a dedicated single-host TankAI runner."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .container_runtime import (
    ContainerRuntimeError,
    DockerCommandExecutor,
    RuntimeSecurityProfile,
)


MIN_CPU_COUNT = 2
MIN_MEMORY_MB = 8192
MIN_FREE_DISK_MB = 20480
REQUIRED_STORAGE_NAMES = ("queue", "fences", "repositories", "worktrees", "states")
LOCAL_PERSISTENT_FILESYSTEM_TYPES = frozenset(
    {
        "bcachefs",
        "btrfs",
        "ext2",
        "ext3",
        "ext4",
        "f2fs",
        "xfs",
        "zfs",
    }
)
_MOUNT_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class StorageProbe:
    name: str
    path: Path
    exists: bool
    is_directory: bool
    is_symlink: bool
    writable: bool
    world_writable: bool
    filesystem_type: str


@dataclass(frozen=True)
class HostProbe:
    system: str
    kernel_release: str
    host_mode: str
    effective_uid: int | None
    cpu_count: int
    memory_mb: int
    free_disk_mb: int
    storage: tuple[StorageProbe, ...]


def _decode_mount_path(value: str) -> str:
    return _MOUNT_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _read_mounts(path: Path = Path("/proc/self/mountinfo")) -> tuple[tuple[Path, str], ...]:
    mounts: list[tuple[Path, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    for line in lines:
        left, separator, right = line.partition(" - ")
        left_fields = left.split()
        right_fields = right.split()
        if not separator or len(left_fields) < 5 or not right_fields:
            continue
        mounts.append((Path(_decode_mount_path(left_fields[4])), right_fields[0].casefold()))
    return tuple(mounts)


def _nearest_existing(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def _filesystem_type(path: Path, mounts: tuple[tuple[Path, str], ...]) -> str:
    resolved = path.resolve(strict=False)
    matches: list[tuple[int, str]] = []
    for mount_point, filesystem_type in mounts:
        try:
            resolved.relative_to(mount_point.resolve())
        except ValueError:
            continue
        matches.append((len(mount_point.parts), filesystem_type))
    return max(matches, default=(0, "unknown"))[1]


def _memory_mb(path: Path = Path("/proc/meminfo")) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    match = re.search(r"(?m)^MemTotal:\s+(\d+)\s+kB$", text)
    return int(match.group(1)) // 1024 if match else 0


def _host_mode(system: str, kernel_release: str) -> str:
    if system.casefold() != "linux":
        return system.casefold() or "unknown"
    release = kernel_release.casefold()
    if "microsoft" not in release:
        return "linux"
    return "wsl2" if "wsl2" in release else "wsl1"


def collect_host_probe(data_root: Path) -> HostProbe:
    """Inspect the host without creating or modifying any path."""
    if not data_root.is_absolute():
        raise ValueError("data-root muss ein absoluter Pfad sein")
    data_root = Path(os.path.abspath(data_root))
    mounts = _read_mounts()
    effective_uid = os.geteuid() if hasattr(os, "geteuid") else None
    paths = (("data_root", data_root),) + tuple(
        (name, data_root / name) for name in REQUIRED_STORAGE_NAMES
    )
    storage: list[StorageProbe] = []
    for name, path in paths:
        exists = path.exists()
        is_directory = path.is_dir() if exists else False
        mode = path.stat().st_mode if exists else 0
        storage.append(
            StorageProbe(
                name=name,
                path=path,
                exists=exists,
                is_directory=is_directory,
                is_symlink=path.is_symlink(),
                writable=(
                    exists
                    and is_directory
                    and os.access(path, os.R_OK | os.W_OK | os.X_OK)
                ),
                world_writable=bool(mode & stat.S_IWOTH),
                filesystem_type=_filesystem_type(path, mounts),
            )
        )
    disk_root = _nearest_existing(data_root)
    free_disk_mb = shutil.disk_usage(disk_root).free // (1024 * 1024)
    system = platform.system()
    kernel_release = platform.release()
    return HostProbe(
        system=system,
        kernel_release=kernel_release,
        host_mode=_host_mode(system, kernel_release),
        effective_uid=effective_uid,
        cpu_count=os.cpu_count() or 0,
        memory_mb=_memory_mb(),
        free_disk_mb=free_disk_mb,
        storage=tuple(storage),
    )


def inspect_runtime(
    runtime_binary: str,
) -> tuple[RuntimeSecurityProfile | None, str | None]:
    executor = DockerCommandExecutor(runtime_binary)
    try:
        version = executor.ensure_available()
        return executor.inspect_security_profile(server_version=version), None
    except ContainerRuntimeError as exc:
        return None, str(exc)


def evaluate_host_readiness(
    probe: HostProbe,
    *,
    data_root: Path,
    runtime_binary: str,
    runtime_profile: RuntimeSecurityProfile | None,
    runtime_error: str | None = None,
) -> dict[str, object]:
    checks: list[dict[str, str]] = []

    def add(check_id: str, passed: bool | None, summary: str) -> None:
        checks.append(
            {
                "id": check_id,
                "status": "UNKNOWN" if passed is None else "PASS" if passed else "FAIL",
                "summary": summary,
            }
        )

    supported_host = probe.host_mode in {"linux", "wsl2"}
    add("host.linux_or_wsl2", supported_host, f"host_mode={probe.host_mode}")
    add(
        "host.dedicated_non_root_user",
        probe.effective_uid is not None and probe.effective_uid != 0,
        f"effective_uid={probe.effective_uid if probe.effective_uid is not None else 'unknown'}",
    )
    add(
        "host.cpu_capacity",
        probe.cpu_count >= MIN_CPU_COUNT,
        f"logical_cpus={probe.cpu_count}; minimum={MIN_CPU_COUNT}",
    )
    add(
        "host.memory_capacity",
        probe.memory_mb >= MIN_MEMORY_MB,
        f"memory_mb={probe.memory_mb}; minimum={MIN_MEMORY_MB}",
    )

    layout_ready = all(
        item.exists and item.is_directory and not item.is_symlink
        for item in probe.storage
    )
    missing = [
        item.name
        for item in probe.storage
        if not item.exists or not item.is_directory or item.is_symlink
    ]
    add(
        "storage.layout",
        layout_ready,
        "vollständig" if layout_ready else "fehlt/ungültig: " + ", ".join(missing),
    )
    permissions_ready = layout_ready and all(
        item.writable and not item.world_writable for item in probe.storage
    )
    add(
        "storage.runner_permissions",
        permissions_ready,
        "Runner-Zugriff vorhanden, nicht world-writable"
        if permissions_ready
        else "Pfadzugriff fehlt oder Pfad ist world-writable",
    )
    filesystem_types = sorted({item.filesystem_type for item in probe.storage})
    local_storage = all(
        item.filesystem_type in LOCAL_PERSISTENT_FILESYSTEM_TYPES
        for item in probe.storage
    )
    add(
        "storage.local_filesystem",
        local_storage,
        "filesystem_types=" + ",".join(filesystem_types),
    )
    add(
        "storage.free_capacity",
        probe.free_disk_mb >= MIN_FREE_DISK_MB,
        f"free_disk_mb={probe.free_disk_mb}; minimum={MIN_FREE_DISK_MB}",
    )

    runtime_available = runtime_profile is not None and runtime_error is None
    add(
        "runtime.available",
        runtime_available,
        (
            f"runtime={runtime_profile.runtime}; version={runtime_profile.server_version}"
            if runtime_profile is not None
            else runtime_error or f"Runtime nicht verfügbar: {runtime_binary}"
        ),
    )
    if runtime_profile is None:
        add("runtime.linux", None, "Sicherheitsprofil nicht verfügbar")
        add("runtime.rootless", None, "Sicherheitsprofil nicht verfügbar")
        add("runtime.cgroup_v2", None, "Sicherheitsprofil nicht verfügbar")
    else:
        add(
            "runtime.linux",
            runtime_profile.os_type.casefold() == "linux",
            f"os_type={runtime_profile.os_type or 'unknown'}",
        )
        add(
            "runtime.rootless",
            runtime_profile.rootless,
            f"rootless={str(runtime_profile.rootless).lower()}",
        )
        cgroup = runtime_profile.cgroup_version.casefold()
        add(
            "runtime.cgroup_v2",
            cgroup in {"2", "v2"},
            f"cgroup_version={runtime_profile.cgroup_version or 'unknown'}",
        )

    failed = {item["id"] for item in checks if item["status"] != "PASS"}
    next_actions: list[str] = []
    if "host.linux_or_wsl2" in failed:
        next_actions.append("Runner in Linux oder WSL2 ausführen; Windows-PowerShell ist kein Runner-Host.")
    if "host.dedicated_non_root_user" in failed:
        next_actions.append("Dediziertes nicht-root Runner-Konto verwenden.")
    if {"host.cpu_capacity", "host.memory_capacity"} & failed:
        next_actions.append("Mindestens 2 logische CPUs und 8192 MiB RAM bereitstellen.")
    if "storage.layout" in failed:
        next_actions.append(
            "data_root sowie queue, fences, repositories, worktrees und states manuell anlegen."
        )
    if "storage.runner_permissions" in failed:
        next_actions.append(
            "Speicherpfade dem Runner gezielt les-/schreibbar geben; world-writable vermeiden."
        )
    if "storage.local_filesystem" in failed:
        next_actions.append("Lokales Linux-Dateisystem verwenden; keine NFS/SMB- oder Windows-Mounts.")
    if "storage.free_capacity" in failed:
        next_actions.append("Mindestens 20480 MiB freien lokalen Speicher bereitstellen.")
    if {"runtime.available", "runtime.linux", "runtime.rootless"} & failed:
        next_actions.append("Rootless Docker oder Podman als Linux-Runtime installieren und starten.")
    if "runtime.cgroup_v2" in failed:
        next_actions.append("Cgroup v2 für die Worker-Ressourcenlimits aktivieren.")

    return {
        "gate": "ops.development.single_host_runner_bootstrap",
        "ready": not failed,
        "data_root": str(data_root),
        "host": {
            "mode": probe.host_mode,
            "system": probe.system,
            "kernel_release": probe.kernel_release,
            "logical_cpus": probe.cpu_count,
            "memory_mb": probe.memory_mb,
            "free_disk_mb": probe.free_disk_mb,
        },
        "storage": [
            {
                "name": item.name,
                "path": str(item.path),
                "filesystem_type": item.filesystem_type,
            }
            for item in probe.storage
        ],
        "checks": checks,
        "next_actions": next_actions,
        "safety": {
            "mutations_performed": False,
            "secret_values_read": False,
            "network_calls_performed": False,
            "web_runtime_socket_access_granted": False,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tankai-single-host-doctor")
    parser.add_argument("--container-runtime", default="docker")
    parser.add_argument("--data-root", default="/srv/tankai")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_root = Path(args.data_root).expanduser()
    try:
        probe = collect_host_probe(data_root)
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "gate": "ops.development.single_host_runner_bootstrap",
                    "ready": False,
                    "error": str(exc),
                    "safety": {"mutations_performed": False},
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    runtime_profile, runtime_error = inspect_runtime(args.container_runtime)
    receipt = evaluate_host_readiness(
        probe,
        data_root=data_root,
        runtime_binary=args.container_runtime,
        runtime_profile=runtime_profile,
        runtime_error=runtime_error,
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
