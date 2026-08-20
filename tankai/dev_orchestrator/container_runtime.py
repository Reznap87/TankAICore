"""Hardened OCI/Docker command execution for development workers.

The public web process must not receive a Docker socket. This executor is meant
for a dedicated runner process on a Linux host using a rootless Docker/Podman
service. It never invokes a shell, never inherits the host environment and
mounts only the assigned Git worktree.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Sequence

from .git_workspace import Workspace
from .models import CommandSpec, TestExecution, WorkerIsolationSpec
from .orchestrator import normalize_repo_path
from .process_control import run_bounded_process


class ContainerRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContainerCommandPlan:
    container_name: str
    argv: list[str]
    writable_roots: tuple[str, ...]
    read_only_workspace: bool


@dataclass(frozen=True)
class ManagedContainerRecord:
    container_id: str
    name: str
    state: str
    created_at: datetime
    labels: dict[str, str]


@dataclass(frozen=True)
class RuntimeSecurityProfile:
    runtime: str
    server_version: str
    rootless: bool
    os_type: str
    cgroup_version: str
    security_options: tuple[str, ...]


_LABEL_KEY_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,62}$")
_CONTAINER_ID_RE = re.compile(r"^[a-fA-F0-9]{12,64}$")
_METADATA_LABEL_KEYS = {
    "job_id", "repository_id", "workspace_id", "tenant_id",
    "fence_epoch", "worker_id",
}


def _safe_label_value(value: object, label: str) -> str:
    text = str(value).strip()
    if not text or len(text) > 240 or any(ord(ch) < 32 for ch in text):
        raise ContainerRuntimeError(f"Ungültiges Container-Label {label}")
    return text


def _parse_runtime_time(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ContainerRuntimeError("Container-Erstellungszeit fehlt")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContainerRuntimeError("Container-Erstellungszeit ist ungültig") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_mount_source(path: Path) -> str:
    value = str(path.resolve())
    if any(ch in value for ch in ("\x00", "\n", "\r", ",")):
        raise ContainerRuntimeError(f"Unsicherer Bind-Mount-Pfad: {value!r}")
    return value


def _ensure_within_workspace(root: Path, candidate: Path) -> Path:
    root_resolved = root.resolve()
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        parent = candidate.parent
        while parent != root_resolved and not parent.exists():
            parent = parent.parent
        resolved_parent = parent.resolve(strict=True)
        if resolved_parent != root_resolved and root_resolved not in resolved_parent.parents:
            raise ContainerRuntimeError(
                f"Schreib-Mount verlässt den Worker-Workspace: {candidate}"
            )
        candidate.mkdir(parents=True, exist_ok=True)
        resolved = candidate.resolve(strict=True)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ContainerRuntimeError(
            f"Schreib-Mount verlässt den Worker-Workspace: {candidate} -> {resolved}"
        )
    return resolved


def _scope_root(scope: str, workspace: Path) -> str:
    """Return a concrete mount root for one glob-like repository scope."""
    normalized = normalize_repo_path(scope)
    wildcard_at = min(
        (index for index, ch in enumerate(normalized) if ch in "*?["),
        default=-1,
    )
    if wildcard_at >= 0:
        prefix = normalized[:wildcard_at]
        if prefix and not prefix.endswith("/"):
            prefix = prefix.rsplit("/", 1)[0] if "/" in prefix else ""
        root = prefix.rstrip("/")
    else:
        candidate = workspace / normalized
        if candidate.exists():
            # Existing exact files can be mounted directly instead of making
            # their whole parent directory writable.
            root = normalized
        else:
            root = str(PurePosixPath(normalized).parent)
            if root == ".":
                root = ""
    return normalize_repo_path(root) if root else ""


def writable_roots_for_scopes(scopes: Sequence[str], workspace: Path) -> tuple[str, ...]:
    roots: set[str] = set()
    for scope in scopes:
        root = _scope_root(scope, workspace)
        roots.add(root)
    # Keep only the shallowest roots; a parent mount already covers children.
    ordered = sorted(roots, key=lambda item: (item.count("/"), item))
    minimal: list[str] = []
    for root in ordered:
        if root == "":
            return ("",)
        if any(root == parent or root.startswith(parent + "/") for parent in minimal):
            continue
        minimal.append(root)
    return tuple(minimal)


def _concrete_denied_roots(scopes: Sequence[str], workspace: Path) -> tuple[str, ...]:
    roots: set[str] = set()
    for scope in scopes:
        root = _scope_root(scope, workspace)
        if root:
            source = (workspace / root).resolve()
            if source.exists():
                roots.add(root)
    return tuple(sorted(roots, key=lambda item: (item.count("/"), item)))


class DockerCommandExecutor:
    """Build and execute hardened `docker run` invocations."""

    def __init__(
        self,
        runtime_binary: str = "docker",
        *,
        require_rootless: bool = False,
    ) -> None:
        if not runtime_binary or any(ch in runtime_binary for ch in ("\x00", "\n", "\r")):
            raise ContainerRuntimeError("Ungültige Container-Runtime")
        self.runtime_binary = runtime_binary
        self.require_rootless = require_rootless

    def ensure_available(self) -> str:
        executable = shutil.which(self.runtime_binary)
        if executable is None:
            raise ContainerRuntimeError(
                f"Container-Runtime nicht gefunden: {self.runtime_binary}"
            )
        try:
            completed = subprocess.run(
                [executable, "version", "--format", "{{.Server.Version}}"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=15,
                check=False,
            )
        except OSError as exc:
            raise ContainerRuntimeError(
                f"Container-Runtime kann nicht gestartet werden: {exc}"
            ) from exc
        if completed.returncode != 0:
            raise ContainerRuntimeError(
                "Container-Runtime ist nicht betriebsbereit: " + completed.stdout[-4000:]
            )
        version = completed.stdout.strip()
        if self.require_rootless:
            profile = self.inspect_security_profile(server_version=version)
            if profile.os_type.casefold() != "linux":
                raise ContainerRuntimeError(
                    f"Worker-Runtime muss Linux verwenden, erkannt: {profile.os_type or 'unbekannt'}"
                )
            if not profile.rootless:
                raise ContainerRuntimeError(
                    "Worker-Runtime läuft nicht rootless; öffentliche Code-Ausführung wird blockiert"
                )
        return version

    def inspect_security_profile(
        self,
        *,
        server_version: str | None = None,
    ) -> RuntimeSecurityProfile:
        executable = shutil.which(self.runtime_binary)
        if executable is None:
            raise ContainerRuntimeError(
                f"Container-Runtime nicht gefunden: {self.runtime_binary}"
            )
        runtime_name = Path(executable).name.casefold()
        is_podman = "podman" in runtime_name
        argv = (
            [executable, "info", "--format", "json"]
            if is_podman
            else [executable, "info", "--format", "{{json .}}"]
        )
        try:
            completed = subprocess.run(
                argv,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=15,
                check=False,
            )
        except OSError as exc:
            raise ContainerRuntimeError(
                f"Runtime-Sicherheitsprofil kann nicht gelesen werden: {exc}"
            ) from exc
        if completed.returncode != 0:
            raise ContainerRuntimeError(
                "Runtime-Sicherheitsprofil ist nicht verfügbar: "
                + completed.stdout[-4000:]
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ContainerRuntimeError("Runtime lieferte kein gültiges JSON-Sicherheitsprofil") from exc
        return self._parse_security_profile(
            payload,
            runtime="podman" if is_podman else "docker",
            server_version=server_version or "",
        )

    @staticmethod
    def _parse_security_profile(
        payload: dict,
        *,
        runtime: str,
        server_version: str,
    ) -> RuntimeSecurityProfile:
        if runtime == "podman":
            host = payload.get("host") or {}
            security = host.get("security") or {}
            rootless = bool(security.get("rootless", host.get("rootless", False)))
            os_type = str(host.get("os") or payload.get("os") or "")
            cgroup_version = str(
                host.get("cgroupVersion")
                or host.get("cgroup_version")
                or ""
            )
            options_raw = security.get("apparmorEnabled", False), security.get("seccompEnabled", False)
            options = tuple(
                name
                for name, enabled in zip(("apparmor", "seccomp"), options_raw)
                if enabled
            )
        else:
            raw_options = payload.get("SecurityOptions") or []
            options = tuple(str(item) for item in raw_options)
            rootless = any(
                option.casefold() == "name=rootless"
                or option.casefold().startswith("name=rootless,")
                for option in options
            )
            os_type = str(payload.get("OSType") or "")
            cgroup_version = str(payload.get("CgroupVersion") or "")
            if not server_version:
                server_version = str(payload.get("ServerVersion") or "")
        return RuntimeSecurityProfile(
            runtime=runtime,
            server_version=server_version,
            rootless=rootless,
            os_type=os_type,
            cgroup_version=cgroup_version,
            security_options=options,
        )

    def build_plan(
        self,
        workspace: Workspace,
        command: CommandSpec,
        isolation: WorkerIsolationSpec,
        *,
        allowed_paths: Sequence[str],
        denied_paths: Sequence[str] = (),
        read_only_workspace: bool,
        run_id: str,
        phase: str,
        metadata_labels: Mapping[str, str] | None = None,
    ) -> ContainerCommandPlan:
        workspace_path = workspace.path.resolve()
        if not workspace_path.is_dir():
            raise ContainerRuntimeError(f"Worker-Workspace fehlt: {workspace_path}")
        source_root = _safe_mount_source(workspace_path)
        name_token = re.sub(r"[^a-z0-9_.-]+", "-", run_id.lower()).strip("-.")[:40]
        container_name = f"tankai-{name_token or 'run'}-{uuid.uuid4().hex[:10]}"
        user = isolation.user
        if user is None:
            uid = os.getuid() if hasattr(os, "getuid") else 0
            gid = os.getgid() if hasattr(os, "getgid") else 0
            if uid == 0 or gid == 0:
                raise ContainerRuntimeError(
                    "Worker-Runner darf Container nicht als Host-root starten; "
                    "dedizierten nicht-root Runner oder isolation.user verwenden"
                )
            user = f"{uid}:{gid}"

        args = [
            self.runtime_binary,
            "run",
            "--rm",
            "--init",
            "--pull=never",
            "--name",
            container_name,
            "--hostname",
            "tankai-worker",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            str(isolation.pids_limit),
            "--memory",
            f"{isolation.memory_mb}m",
            "--memory-swap",
            f"{isolation.memory_mb}m",
            "--cpus",
            str(isolation.cpus),
            "--ulimit",
            f"nofile={isolation.nofile_limit}:{isolation.nofile_limit}",
            "--ipc",
            "none",
            "--user",
            user,
            "--workdir",
            "/workspace",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,noexec,size={isolation.tmpfs_mb}m,mode=1777",
            "--tmpfs",
            f"/build:rw,nosuid,nodev,exec,size={isolation.build_tmpfs_mb}m,mode=1777",
            "--label",
            "tankai.managed=true",
            "--label",
            f"tankai.run_id={_safe_label_value(run_id, 'run_id')}",
            "--label",
            f"tankai.phase={_safe_label_value(phase, 'phase')}",
        ]
        for key, value in sorted((metadata_labels or {}).items()):
            normalized_key = str(key).strip().lower()
            if not _LABEL_KEY_RE.fullmatch(normalized_key):
                raise ContainerRuntimeError(f"Ungültiger Container-Label-Schlüssel: {key!r}")
            if normalized_key not in _METADATA_LABEL_KEYS:
                raise ContainerRuntimeError(
                    f"Nicht freigegebener Container-Metadaten-Schlüssel: {normalized_key}"
                )
            args.extend([
                "--label",
                f"tankai.{normalized_key}={_safe_label_value(value, normalized_key)}",
            ])
        args.extend([
            "--mount",
            f"type=bind,src={source_root},dst=/workspace,readonly",
        ])

        writable_roots: tuple[str, ...] = ()
        if not read_only_workspace:
            writable_roots = writable_roots_for_scopes(allowed_paths, workspace_path)
            if not writable_roots:
                raise ContainerRuntimeError("Worker besitzt keinen beschreibbaren Pfad")
            for root in writable_roots:
                source = (
                    workspace_path
                    if root == ""
                    else _ensure_within_workspace(workspace_path, workspace_path / root)
                )
                source_value = _safe_mount_source(source)
                target = "/workspace" if root == "" else f"/workspace/{root}"
                args.extend([
                    "--mount",
                    f"type=bind,src={source_value},dst={target}",
                ])
            # A concrete denied path is over-mounted read-only even if its parent is writable.
            for root in _concrete_denied_roots(denied_paths, workspace_path):
                source_value = _safe_mount_source(
                    _ensure_within_workspace(workspace_path, workspace_path / root)
                )
                args.extend([
                    "--mount",
                    f"type=bind,src={source_value},dst=/workspace/{root},readonly",
                ])

        # This mount must be last so even a broad writable scope cannot expose or
        # modify the host repository's Git metadata. Linked worktrees use a .git
        # file; the main repository uses a .git directory.
        git_path = workspace_path / ".git"
        if git_path.is_dir():
            args.extend([
                "--tmpfs",
                "/workspace/.git:ro,nosuid,nodev,noexec,size=64k,mode=0555",
            ])
        else:
            args.extend([
                "--mount",
                "type=bind,src=/dev/null,dst=/workspace/.git,readonly",
            ])

        safe_env = {
            "HOME": "/tmp",
            "TMPDIR": "/build",
            "TANKAI_BUILD_DIR": "/build",
            "XDG_CACHE_HOME": "/tmp/cache",
            "PYTHONPYCACHEPREFIX": "/tmp/pycache",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "NO_COLOR": "1",
            **command.env,
        }
        for key in sorted(safe_env):
            args.extend(["--env", f"{key}={safe_env[key]}"])
        args.append(isolation.image)
        args.extend(command.argv)
        return ContainerCommandPlan(
            container_name=container_name,
            argv=args,
            writable_roots=writable_roots,
            read_only_workspace=read_only_workspace,
        )

    def execute(
        self,
        workspace: Workspace,
        command: CommandSpec,
        isolation: WorkerIsolationSpec,
        *,
        allowed_paths: Sequence[str],
        denied_paths: Sequence[str] = (),
        read_only_workspace: bool,
        run_id: str,
        phase: str,
        cancellation_check: Callable[[], None] | None = None,
        metadata_labels: Mapping[str, str] | None = None,
    ) -> TestExecution:
        plan = self.build_plan(
            workspace,
            command,
            isolation,
            allowed_paths=allowed_paths,
            denied_paths=denied_paths,
            read_only_workspace=read_only_workspace,
            run_id=run_id,
            phase=phase,
            metadata_labels=metadata_labels,
        )
        try:
            completed = run_bounded_process(
                plan.argv,
                timeout_seconds=command.timeout_seconds,
                output_limit=10_000,
                cancellation_check=cancellation_check,
            )
        except FileNotFoundError as exc:
            raise ContainerRuntimeError(
                f"Container-Runtime nicht gefunden: {self.runtime_binary}"
            ) from exc
        except BaseException:
            # Killing only the local docker/podman client is insufficient: the
            # daemon may already have created the named container. Remove it
            # before propagating the lease/fence cancellation.
            self._force_remove(plan.container_name)
            raise
        if completed.timed_out:
            self._force_remove(plan.container_name)
            return TestExecution(
                command=" ".join(command.argv),
                passed=False,
                exit_code=None,
                summary=(completed.output + f"\nTIMEOUT nach {command.timeout_seconds}s")[-10_000:],
            )
        return TestExecution(
            command=" ".join(command.argv),
            passed=completed.returncode == 0,
            exit_code=completed.returncode,
            summary=completed.output,
        )

    def list_managed_containers(self, *, repository_id: str) -> list[ManagedContainerRecord]:
        repository = _safe_label_value(repository_id, "repository_id")
        executable = shutil.which(self.runtime_binary)
        if executable is None:
            raise ContainerRuntimeError(
                f"Container-Runtime nicht gefunden: {self.runtime_binary}"
            )
        try:
            listed = subprocess.run(
                [
                    executable, "ps", "-a",
                    "--filter", "label=tankai.managed=true",
                    "--filter", f"label=tankai.repository_id={repository}",
                    "--format", "{{.ID}}",
                ],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=30, check=False,
            )
        except OSError as exc:
            raise ContainerRuntimeError(f"Containerliste kann nicht gelesen werden: {exc}") from exc
        if listed.returncode != 0:
            raise ContainerRuntimeError(
                "Containerliste ist nicht verfügbar: " + listed.stdout[-4000:]
            )
        ids: list[str] = []
        for raw in listed.stdout.splitlines():
            container_id = raw.strip()
            if not container_id:
                continue
            if not _CONTAINER_ID_RE.fullmatch(container_id):
                raise ContainerRuntimeError("Runtime lieferte eine ungültige Container-ID")
            if container_id not in ids:
                ids.append(container_id)

        records: list[ManagedContainerRecord] = []
        for container_id in ids:
            try:
                inspected = subprocess.run(
                    [executable, "inspect", container_id],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=30, check=False,
                )
            except OSError as exc:
                raise ContainerRuntimeError(
                    f"Container {container_id} kann nicht geprüft werden: {exc}"
                ) from exc
            if inspected.returncode != 0:
                raise ContainerRuntimeError(
                    f"Container {container_id} kann nicht geprüft werden: "
                    + inspected.stdout[-4000:]
                )
            try:
                payload = json.loads(inspected.stdout)
                item = payload[0] if isinstance(payload, list) else payload
                config = item.get("Config") or item.get("config") or {}
                raw_labels = config.get("Labels") or config.get("labels") or {}
                state_payload = item.get("State") or item.get("state") or {}
                labels = {str(k): str(v) for k, v in dict(raw_labels).items()}
                created = item.get("Created") or item.get("created")
                name = str(item.get("Name") or item.get("name") or container_id).lstrip("/")
                state = str(
                    state_payload.get("Status")
                    or state_payload.get("status")
                    or item.get("Status")
                    or "unknown"
                )
            except (AttributeError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ContainerRuntimeError(
                    f"Container {container_id} lieferte ungültige Inspect-Daten"
                ) from exc
            records.append(ManagedContainerRecord(
                container_id=container_id,
                name=name,
                state=state,
                created_at=_parse_runtime_time(created),
                labels=labels,
            ))
        return records

    def remove_container(self, container_id: str) -> None:
        if not _CONTAINER_ID_RE.fullmatch(container_id):
            raise ContainerRuntimeError("Ungültige Container-ID für Bereinigung")
        executable = shutil.which(self.runtime_binary)
        if executable is None:
            raise ContainerRuntimeError(
                f"Container-Runtime nicht gefunden: {self.runtime_binary}"
            )
        try:
            completed = subprocess.run(
                [executable, "rm", "-f", container_id],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=30, check=False,
            )
        except OSError as exc:
            raise ContainerRuntimeError(f"Container konnte nicht entfernt werden: {exc}") from exc
        if completed.returncode != 0:
            raise ContainerRuntimeError(
                f"Container {container_id} konnte nicht entfernt werden: "
                + completed.stdout[-4000:]
            )

    def _force_remove(self, container_name: str) -> None:
        try:
            subprocess.run(
                [self.runtime_binary, "rm", "-f", container_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
