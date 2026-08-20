from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from tankai.dev_orchestrator import (
    CommandSpec,
    ContainerRuntimeError,
    DockerCommandExecutor,
    WorkerIsolationSpec,
    Workspace,
    writable_roots_for_scopes,
)


DIGEST_IMAGE = "tankai-worker@sha256:" + "a" * 64


def make_workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "workspace"
    (root / "backend" / "src" / "auth").mkdir(parents=True)
    (root / "backend" / "src" / "auth" / "secrets").mkdir(parents=True)
    (root / ".git").write_text("gitdir: /host/repository/.git/worktrees/test\n", encoding="utf-8")
    return Workspace(
        agent_id="AGENT_BACKEND_01",
        branch="tankai/test",
        path=root,
        base_commit="a" * 40,
    )


def test_isolation_requires_digest_by_default() -> None:
    with pytest.raises(PydanticValidationError):
        WorkerIsolationSpec(image="tankai-worker:latest")
    with pytest.raises(PydanticValidationError):
        WorkerIsolationSpec(
            image="--privileged",
            require_image_digest=False,
            user="10001:10001",
        )

    local_id = WorkerIsolationSpec(image="sha256:" + "d" * 64, user="10001:10001")
    assert local_id.image.startswith("sha256:")

    spec = WorkerIsolationSpec(
        image="tankai-worker:latest",
        require_image_digest=False,
        user="10001:10001",
    )
    assert spec.image == "tankai-worker:latest"


def test_writable_roots_are_minimized_and_safe(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    roots = writable_roots_for_scopes(
        [
            "backend/src/auth/**",
            "backend/src/auth/tests/**",
            "frontend/src/*.ts",
        ],
        workspace.path,
    )
    assert set(roots) == {"backend/src/auth", "frontend/src"}



def test_existing_exact_file_scope_is_not_widened_to_parent(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    exact_file = workspace.path / "backend" / "src" / "auth" / "config.py"
    exact_file.write_text("ENABLED = True\n", encoding="utf-8")
    roots = writable_roots_for_scopes(["backend/src/auth/config.py"], workspace.path)
    assert roots == ("backend/src/auth/config.py",)

    spec = WorkerIsolationSpec(image=DIGEST_IMAGE, user="10001:10001")
    plan = DockerCommandExecutor().build_plan(
        workspace,
        CommandSpec(argv=["python", "-V"]),
        spec,
        allowed_paths=["backend/src/auth/config.py"],
        read_only_workspace=False,
        run_id="RUN-EXACT-FILE",
        phase="implement",
    )
    assert any(
        item.endswith("dst=/workspace/backend/src/auth/config.py")
        for item in plan.argv
    )
    assert not any(
        item.endswith("dst=/workspace/backend/src/auth")
        for item in plan.argv
    )

def test_docker_plan_has_security_limits_and_no_host_environment(tmp_path: Path, monkeypatch) -> None:
    workspace = make_workspace(tmp_path)
    monkeypatch.setenv("SHOULD_NOT_LEAK_TO_WORKER", "secret")
    spec = WorkerIsolationSpec(
        image=DIGEST_IMAGE,
        user="10001:10001",
        memory_mb=384,
        cpus=0.75,
        pids_limit=64,
        tmpfs_mb=96,
        build_tmpfs_mb=256,
        nofile_limit=512,
    )
    command = CommandSpec(
        argv=["python", "-c", "print('ok')"],
        env={"EXPLICIT_VALUE": "allowed"},
    )
    plan = DockerCommandExecutor("docker").build_plan(
        workspace,
        command,
        spec,
        allowed_paths=["backend/src/auth/**"],
        denied_paths=["backend/src/auth/secrets/**"],
        read_only_workspace=False,
        run_id="RUN-AUTH-001",
        phase="implement",
    )
    joined = "\n".join(plan.argv)
    assert plan.read_only_workspace is False
    assert plan.writable_roots == ("backend/src/auth",)
    assert "--network\nnone" in joined
    assert "--read-only" in plan.argv
    assert "--cap-drop\nALL" in joined
    assert "no-new-privileges:true" in plan.argv
    assert "--pids-limit\n64" in joined
    assert "--memory\n384m" in joined
    assert "--memory-swap\n384m" in joined
    assert "--cpus\n0.75" in joined
    assert "nofile=512:512" in plan.argv
    assert "/build:rw,nosuid,nodev,exec,size=256m,mode=1777" in plan.argv
    assert "TMPDIR=/build" in plan.argv
    assert f"type=bind,src={workspace.path.resolve()},dst=/workspace,readonly" in plan.argv
    assert any(item.endswith("dst=/workspace/backend/src/auth") for item in plan.argv)
    assert any(item.endswith("dst=/workspace/backend/src/auth/secrets,readonly") for item in plan.argv)
    image_index = plan.argv.index(DIGEST_IMAGE)
    assert plan.argv[image_index + 1 :] == ["python", "-c", "print('ok')"]
    assert "EXPLICIT_VALUE=allowed" in plan.argv
    assert all("SHOULD_NOT_LEAK_TO_WORKER" not in item for item in plan.argv)
    git_hide = "type=bind,src=/dev/null,dst=/workspace/.git,readonly"
    assert git_hide in plan.argv
    assert plan.argv.index(git_hide) > max(
        index for index, item in enumerate(plan.argv) if item.endswith("dst=/workspace/backend/src/auth")
    )


def test_read_only_plan_does_not_add_writable_mounts(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    spec = WorkerIsolationSpec(image=DIGEST_IMAGE, user="10001:10001")
    plan = DockerCommandExecutor().build_plan(
        workspace,
        CommandSpec(argv=["python", "-V"]),
        spec,
        allowed_paths=["backend/src/auth/**"],
        read_only_workspace=True,
        run_id="RUN-READONLY",
        phase="qa",
    )
    assert plan.writable_roots == ()
    assert not any(
        item.endswith("dst=/workspace/backend/src/auth")
        for item in plan.argv
    )


def test_root_runner_without_explicit_nonroot_user_is_rejected(tmp_path: Path, monkeypatch) -> None:
    workspace = make_workspace(tmp_path)
    spec = WorkerIsolationSpec(image=DIGEST_IMAGE)
    executor = DockerCommandExecutor()
    monkeypatch.setattr(os, "getuid", lambda: 0)
    monkeypatch.setattr(os, "getgid", lambda: 0)
    with pytest.raises(ContainerRuntimeError, match="Host-root"):
        executor.build_plan(
            workspace,
            CommandSpec(argv=["python", "-V"]),
            spec,
            allowed_paths=["backend/src/auth/**"],
            read_only_workspace=False,
            run_id="RUN-ROOT",
            phase="implement",
        )


def test_main_repository_git_directory_is_hidden_with_tmpfs(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    (root / ".git").mkdir(parents=True)
    workspace = Workspace(
        agent_id="TECH_AI_ORCHESTRATOR",
        branch="main",
        path=root,
        base_commit="d" * 40,
    )
    spec = WorkerIsolationSpec(image=DIGEST_IMAGE, user="10001:10001")
    plan = DockerCommandExecutor().build_plan(
        workspace,
        CommandSpec(argv=["python", "-V"]),
        spec,
        allowed_paths=(),
        read_only_workspace=True,
        run_id="RUN-INTEGRATION",
        phase="integration_test",
    )
    assert "/workspace/.git:ro,nosuid,nodev,noexec,size=64k,mode=0555" in plan.argv
    assert "type=bind,src=/dev/null,dst=/workspace/.git,readonly" not in plan.argv


def test_writable_mount_symlink_escape_is_blocked(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace.path / "backend" / "src" / "linked"
    link.symlink_to(outside, target_is_directory=True)
    spec = WorkerIsolationSpec(image=DIGEST_IMAGE, user="10001:10001")
    with pytest.raises(ContainerRuntimeError, match="verlässt"):
        DockerCommandExecutor().build_plan(
            workspace,
            CommandSpec(argv=["python", "-V"]),
            spec,
            allowed_paths=["backend/src/linked/**"],
            read_only_workspace=False,
            run_id="RUN-SYMLINK",
            phase="implement",
        )


def test_missing_container_runtime_fails_closed() -> None:
    with pytest.raises(ContainerRuntimeError, match="nicht gefunden"):
        DockerCommandExecutor("tankai-runtime-does-not-exist").ensure_available()


def test_container_output_is_bounded(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    runtime = tmp_path / "fake-docker"
    runtime.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write('A' * 25000 + 'OUTPUT-END\\n')\n",
        encoding="utf-8",
    )
    runtime.chmod(0o755)
    spec = WorkerIsolationSpec(image=DIGEST_IMAGE, user="10001:10001")
    result = DockerCommandExecutor(str(runtime)).execute(
        workspace,
        CommandSpec(argv=["python", "-V"]),
        spec,
        allowed_paths=["backend/src/auth/**"],
        read_only_workspace=True,
        run_id="RUN-OUTPUT",
        phase="qa",
    )
    assert result.passed is True
    assert result.exit_code == 0
    assert len(result.summary.encode("utf-8")) <= 10_000
    assert result.summary.endswith("OUTPUT-END")


def test_container_timeout_forces_named_cleanup(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    marker = tmp_path / "removed.txt"
    runtime = tmp_path / "fake-docker-timeout"
    runtime.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys, time\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'rm':\n"
        "    marker.write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "print('STARTED', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    runtime.chmod(0o755)
    spec = WorkerIsolationSpec(image=DIGEST_IMAGE, user="10001:10001")
    result = DockerCommandExecutor(str(runtime)).execute(
        workspace,
        CommandSpec(argv=["python", "-V"], timeout_seconds=0.1),
        spec,
        allowed_paths=["backend/src/auth/**"],
        read_only_workspace=True,
        run_id="RUN-TIMEOUT",
        phase="qa",
    )
    assert result.passed is False
    assert result.exit_code is None
    assert "TIMEOUT nach 0.1s" in result.summary
    assert marker.exists()
    assert marker.read_text(encoding="utf-8").startswith("rm -f tankai-run-timeout-")


def _write_fake_runtime(path: Path, *, rootless: bool, podman: bool = False) -> None:
    if podman:
        info = {
            "host": {
                "os": "linux",
                "cgroupVersion": "v2",
                "security": {
                    "rootless": rootless,
                    "apparmorEnabled": True,
                    "seccompEnabled": True,
                },
            }
        }
    else:
        info = {
            "ServerVersion": "27.1.0",
            "OSType": "linux",
            "CgroupVersion": "2",
            "SecurityOptions": ["name=seccomp,profile=builtin"]
            + (["name=rootless"] if rootless else []),
        }
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"INFO = {info!r}\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'version':\n"
        "    print('27.1.0')\n"
        "elif len(sys.argv) > 1 and sys.argv[1] == 'info':\n"
        "    print(json.dumps(INFO))\n"
        "else:\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_rootless_runtime_profile_is_mechanically_required(tmp_path: Path) -> None:
    runtime = tmp_path / "fake-docker"
    _write_fake_runtime(runtime, rootless=True)
    executor = DockerCommandExecutor(str(runtime), require_rootless=True)
    assert executor.ensure_available() == "27.1.0"
    profile = executor.inspect_security_profile(server_version="27.1.0")
    assert profile.runtime == "docker"
    assert profile.rootless is True
    assert profile.os_type == "linux"
    assert profile.cgroup_version == "2"


def test_rootful_runtime_is_rejected_for_online_workers(tmp_path: Path) -> None:
    runtime = tmp_path / "fake-docker-rootful"
    _write_fake_runtime(runtime, rootless=False)
    with pytest.raises(ContainerRuntimeError, match="nicht rootless"):
        DockerCommandExecutor(str(runtime), require_rootless=True).ensure_available()


def test_podman_rootless_profile_is_parsed(tmp_path: Path) -> None:
    runtime = tmp_path / "fake-podman"
    _write_fake_runtime(runtime, rootless=True, podman=True)
    executor = DockerCommandExecutor(str(runtime), require_rootless=True)
    assert executor.ensure_available() == "27.1.0"
    profile = executor.inspect_security_profile(server_version="5.0.0")
    assert profile.runtime == "podman"
    assert profile.rootless is True
    assert set(profile.security_options) == {"apparmor", "seccomp"}


def test_runtime_doctor_cli_reports_rootless_profile(tmp_path: Path, capsys) -> None:
    from tankai.dev_orchestrator.runtime_cli import main as runtime_cli_main

    runtime = tmp_path / "fake-docker-doctor"
    _write_fake_runtime(runtime, rootless=True)
    assert runtime_cli_main(["--container-runtime", str(runtime)]) == 0
    output = capsys.readouterr().out
    assert '"ok": true' in output
    assert '"rootless": true' in output


def test_runtime_doctor_cli_fails_closed_for_rootful_runtime(tmp_path: Path, capsys) -> None:
    from tankai.dev_orchestrator.runtime_cli import main as runtime_cli_main

    runtime = tmp_path / "fake-docker-doctor-rootful"
    _write_fake_runtime(runtime, rootless=False)
    assert runtime_cli_main(["--container-runtime", str(runtime)]) == 2
    output = capsys.readouterr().out
    assert '"ok": false' in output
    assert "nicht rootless" in output


def test_container_cancellation_removes_named_container_immediately(tmp_path: Path) -> None:
    import time

    workspace = make_workspace(tmp_path)
    marker = tmp_path / "cancelled-container.txt"
    runtime = tmp_path / "fake-docker-cancel"
    runtime.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys, time\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "if len(sys.argv) > 1 and sys.argv[1] == 'rm':\n"
        "    marker.write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "print('RUNNING', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    runtime.chmod(0o755)
    checks = {"count": 0}

    def cancel() -> None:
        checks["count"] += 1
        if checks["count"] >= 3:
            raise RuntimeError("external lease lost")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="external lease lost"):
        DockerCommandExecutor(str(runtime)).execute(
            workspace,
            CommandSpec(argv=["python", "-V"], timeout_seconds=30),
            WorkerIsolationSpec(image=DIGEST_IMAGE, user="10001:10001"),
            allowed_paths=["backend/src/auth/**"],
            read_only_workspace=True,
            run_id="RUN-CANCEL",
            phase="qa",
            cancellation_check=cancel,
        )
    assert time.monotonic() - started < 5
    assert marker.exists()
    assert marker.read_text(encoding="utf-8").startswith("rm -f tankai-run-cancel-")


def test_container_plan_includes_reaper_identity_labels(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    spec = WorkerIsolationSpec(image=DIGEST_IMAGE, user="10001:10001")
    plan = DockerCommandExecutor().build_plan(
        workspace,
        CommandSpec(argv=["python", "-V"]),
        spec,
        allowed_paths=["backend/src/auth/**"],
        read_only_workspace=True,
        run_id="RUN-LABELS-001",
        phase="qa",
        metadata_labels={
            "job_id": "JOB-001",
            "repository_id": "REPO-001",
            "workspace_id": "WORKSPACE-001",
            "tenant_id": "TENANT-001",
            "fence_epoch": "7",
            "worker_id": "runner-01",
        },
    )
    labels = {
        plan.argv[index + 1]
        for index, value in enumerate(plan.argv[:-1])
        if value == "--label"
    }
    assert "tankai.managed=true" in labels
    assert "tankai.run_id=RUN-LABELS-001" in labels
    assert "tankai.phase=qa" in labels
    assert "tankai.job_id=JOB-001" in labels
    assert "tankai.repository_id=REPO-001" in labels
    assert "tankai.workspace_id=WORKSPACE-001" in labels
    assert "tankai.tenant_id=TENANT-001" in labels
    assert "tankai.fence_epoch=7" in labels
    assert "tankai.worker_id=runner-01" in labels


def test_runtime_lists_and_removes_managed_containers(tmp_path: Path) -> None:
    import json

    container_id = "a" * 64
    marker = tmp_path / "removed.txt"
    runtime = tmp_path / "fake-runtime-reaper"
    inspect_payload = [{
        "Id": container_id,
        "Name": "/tankai-run",
        "Created": "2026-07-28T10:00:00Z",
        "Config": {"Labels": {
            "tankai.managed": "true",
            "tankai.repository_id": "REPO-001",
            "tankai.job_id": "JOB-001",
        }},
        "State": {"Status": "exited"},
    }]
    runtime.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"container_id = {container_id!r}\n"
        f"payload = {json.dumps(inspect_payload)!r}\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "if cmd == 'ps': print(container_id)\n"
        "elif cmd == 'inspect': print(payload)\n"
        "elif cmd == 'rm': marker.write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n"
        "elif cmd == 'version': print('1.0')\n"
        "else: raise SystemExit(2)\n",
        encoding="utf-8",
    )
    runtime.chmod(0o755)
    executor = DockerCommandExecutor(str(runtime))
    records = executor.list_managed_containers(repository_id="REPO-001")
    assert len(records) == 1
    assert records[0].container_id == container_id
    assert records[0].name == "tankai-run"
    assert records[0].state == "exited"
    assert records[0].labels["tankai.job_id"] == "JOB-001"
    executor.remove_container(container_id)
    assert marker.read_text(encoding="utf-8") == f"rm -f {container_id}"


def test_container_metadata_cannot_override_reserved_labels(tmp_path: Path) -> None:
    workspace = make_workspace(tmp_path)
    spec = WorkerIsolationSpec(image=DIGEST_IMAGE, user="10001:10001")
    with pytest.raises(ContainerRuntimeError, match="Nicht freigegebener"):
        DockerCommandExecutor().build_plan(
            workspace,
            CommandSpec(argv=["python", "-V"]),
            spec,
            allowed_paths=["backend/src/auth/**"],
            read_only_workspace=True,
            run_id="RUN-RESERVED",
            phase="qa",
            metadata_labels={"managed": "false"},
        )
