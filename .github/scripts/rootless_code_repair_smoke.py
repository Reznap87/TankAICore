#!/usr/bin/env python3
"""Real rootless OCI correction/restart smoke on a private synthetic repository."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE_ROOT))

from tankai.dev_orchestrator import (
    CommandSpec, DevelopmentOrchestrator, DevelopmentRole, DockerCommandExecutor, GateJob,
    GitWorkspaceManager, TaskSpec, WorkerIsolationSpec, WorkerPipelineRunner, render_command,
)
from tankai.dev_orchestrator.code_proposal import CodeProposal, ProposedFile, ProposalChecks
from tankai.dev_orchestrator.code_request import CodeResponse, CodeResponseSubmission, build_code_request, prepare_code_response
from tankai.dev_orchestrator.code_repair import CodeRepairSubmission, build_code_repair_request
from tankai.dev_orchestrator.job_queue import DevelopmentJobQueue, JobState, QueuedWorkerDispatcher, WorkspaceQueuePolicy
from tankai.web.auth import AuthStore

STUB = "def stable_unique(values):\n    raise NotImplementedError\n"
WRONG = "def stable_unique(values):\n    return sorted(set(values))\n"
FIXED = '''def stable_unique(values):
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
'''
TESTS = '''import unittest
from src.order import stable_unique
class OrderTests(unittest.TestCase):
    def test_empty(self): self.assertEqual(stable_unique([]), [])
    def test_duplicates(self): self.assertEqual(stable_unique([3,1,3,2,1]), [3,1,2])
    def test_negative(self): self.assertEqual(stable_unique([-2,0,-2,7,0]), [-2,0,7])
    def test_input(self):
        values = [8,2,7]
        self.assertEqual(stable_unique(values), [8,2,7])
        self.assertEqual(values, [8,2,7])
'''
PROBE = '''import json, os, pathlib, resource
p = pathlib.Path
status = dict(line.split(':', 1) for line in p('/proc/self/status').read_text().splitlines() if ':' in line)
assert os.geteuid() == 10001 and os.getegid() == 10001
assert int(status['CapEff'].strip(), 16) == 0 and status['NoNewPrivs'].strip() == '1'
assert os.statvfs('/').f_flag & os.ST_RDONLY
assert p('/workspace/.git').read_bytes() == b''
assert 'TANKAI_SMOKE_HOST_ONLY' not in os.environ
assert {item.name for item in p('/sys/class/net').iterdir()} == {'lo'}
assert p('/sys/fs/cgroup/memory.max').read_text().strip() == '268435456'
assert p('/sys/fs/cgroup/pids.max').read_text().strip() == '64'
quota, period = map(int, p('/sys/fs/cgroup/cpu.max').read_text().split())
assert quota == period and resource.getrlimit(resource.RLIMIT_NOFILE) == (1024, 1024)
for filename in ('/workspace/README.md', '/workspace/tests/test_order.py'):
    try:
        with open(filename, 'r+b'): pass
    except OSError: pass
    else: raise AssertionError('protected path was writable')
print(json.dumps({'uid': os.geteuid(), 'gid': os.getegid(), 'capabilities_empty': True,
 'no_new_privileges': True, 'root_read_only': True, 'git_masked': True,
 'host_environment_absent': True, 'network_none': True, 'memory_bytes': 268435456,
 'pids': 64, 'cpu_quota_equals_period': True, 'nofile': 1024, 'protected_files_read_only': True}))
'''


def queue_at(root: Path) -> DevelopmentJobQueue:
    return DevelopmentJobQueue(root / "queue/jobs.db", auth_store=AuthStore(root / "auth/auth.db"),
        repository_base=root / "repositories", workspace_base=root / "worktrees", state_base=root / "states")


def git(repository: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=repository, text=True).strip()


def write_new(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")


def read_configuration(root: Path) -> dict:
    raw = (root / "smoke-scope.json").read_bytes()
    if len(raw) > 4096:
        raise ValueError("Invalid smoke scope")
    value = json.loads(raw)
    if value.get("version") != 1 or not re.fullmatch(r"[0-9a-f-]{36}", value.get("repository_id", "")):
        raise ValueError("Invalid smoke repository identity")
    return value


def worker(root: Path, runtime: str, crash: bool) -> None:
    queue = queue_at(root)
    config = read_configuration(root)
    if crash:
        original_guard = WorkerPipelineRunner._guard
        def stop(self, stage):
            original_guard(self, stage)
            if self.container_metadata.get("job_id") == config["job_id"] and stage == "submit_for_review":
                os._exit(91)
        WorkerPipelineRunner._guard = stop
    result = QueuedWorkerDispatcher(queue, worker_id="rootless-smoke-worker", container_runtime=runtime).run_once()
    if result is None or result.state != JobState.SUCCEEDED:
        raise RuntimeError("Rootless correction worker did not succeed: " + (result.error if result else "no job"))


def cleanup(root: Path, runtime: str) -> None:
    if not (root / "smoke-scope.json").exists():
        print("rootless code correction cleanup: no scope")
        return
    config = read_configuration(root)
    executor = DockerCommandExecutor(runtime, require_rootless=True)
    executor.ensure_available()
    for container in executor.list_managed_containers(repository_id=config["repository_id"]):
        if (container.labels.get("tankai.repository_id") != config["repository_id"]
                or container.labels.get("tankai.managed") != "true"):
            raise RuntimeError("Refusing cleanup outside this smoke scope")
        executor.remove_container(container.container_id)
    print("rootless code correction cleanup: PASS")


def smoke(root: Path, runtime: str, image_file: Path, output: Path, base_file: Path | None) -> None:
    if sys.platform != "linux" or os.geteuid() == 0:
        raise RuntimeError("Smoke requires an unprivileged Linux user")
    executor = DockerCommandExecutor(runtime, require_rootless=True)
    runtime_version = executor.ensure_available()
    profile = executor.inspect_security_profile(server_version=runtime_version)
    if profile.runtime != "podman" or profile.cgroup_version not in {"2", "v2"}:
        raise RuntimeError("This live proof requires rootless Podman with cgroup v2")
    image = image_file.read_text(encoding="utf-8").strip()
    if re.fullmatch(r"[0-9a-f]{64}", image):
        image = "sha256:" + image
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Expected the immutable built image ID")
    inspected = json.loads(subprocess.check_output([runtime, "image", "inspect", image], text=True))[0]
    if inspected["Id"].removeprefix("sha256:") != image.removeprefix("sha256:"):
        raise RuntimeError("Image identity mismatch")
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    repository = root / "repositories/project"
    (repository / "src").mkdir(parents=True)
    (repository / "tests").mkdir()
    for name, content in [("README.md", "rootless smoke baseline\n"), ("src/order.py", STUB), ("tests/test_order.py", TESTS)]:
        (repository / name).write_bytes(content.encode("utf-8"))
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "TankAI rootless smoke")
    git(repository, "config", "user.email", "smoke@example.invalid")
    git(repository, "config", "core.autocrlf", "false")
    git(repository, "add", "--all")
    git(repository, "commit", "-m", "Fixed source and tests")
    base = git(repository, "rev-parse", "HEAD")
    orchestrator = DevelopmentOrchestrator.initialize(str(root / "states/project.json"),
        current_version="rootless-smoke", current_branch="main", current_commit=base)
    test = CommandSpec(argv=["python", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"], timeout_seconds=30)
    review = CommandSpec(argv=["python", "-c", PROBE], timeout_seconds=30)
    orchestrator.create_task(TaskSpec(task_id="ORDER-1", goal="Stable integer deduplication without modifying input",
        base_commit=base, allowed_paths=["src/**"], denied_paths=["tests/**"], required_tests=[render_command(test)],
        acceptance_criteria=["Four unchanged tests pass"]))
    author = orchestrator.start_agent("ORDER-1", DevelopmentRole.BACKEND)
    def support(task_id, role):
        orchestrator.create_task(TaskSpec(task_id=task_id, goal="Fixed technical gate", base_commit=base,
            allowed_paths=[], acceptance_criteria=["Fixed gate passes"]))
        return orchestrator.start_agent(task_id, role).agent_id
    checks = ProposalChecks(test_commands=[test], gates=GateJob(
        reviewer_agent_id=support("REVIEW", DevelopmentRole.REVIEWER), review_commands=[review],
        qa_agent_id=support("QA", DevelopmentRole.QA), qa_commands=[test]),
        isolation=WorkerIsolationSpec(image=image, memory_mb=256, cpus=1.0, pids_limit=64, user="10001:10001"))
    package = build_code_request(state=orchestrator.state(), agent_id=author.agent_id, repository=repository,
        context_paths=["src/order.py", "tests/test_order.py"], checks=checks, data_class="public")
    def response(request_package, content):
        source = next(item for item in request_package.request.context_files if item.path == "src/order.py")
        return CodeResponse(request_sha256=request_package.request_sha256, proposal=CodeProposal(
            task_id="ORDER-1", base_commit=base, files=(ProposedFile(path="src/order.py", content=content,
            expected_sha256=source.sha256),)))
    previous = prepare_code_response(package, response(package, WRONG), state=orchestrator.state(),
        agent_id=author.agent_id, repository=repository, checks=checks, python_executable="python")
    queue = queue_at(root)
    owner, tenant, workspace_id = queue.auth.create_user_with_tenant(email="smoke@example.invalid",
        password="Synthetic-smoke-password-123!", display_name="Smoke", tenant_name="Synthetic smoke")
    queue.set_policy(actor_user_id=owner, workspace_id=workspace_id, policy=WorkspaceQueuePolicy(
        tenant_id=tenant, workspace_id=workspace_id, max_queued=4, max_running=1, max_memory_mb=256,
        max_cpus=1.0, max_pids=64, max_runtime_seconds=180, max_attempts=2,
        max_jobs_per_user_hour=10, allowed_images=[image]))
    binding = queue.register_repository(actor_user_id=owner, workspace_id=workspace_id, name="Smoke",
        repository_path=repository, workspace_root=root / "worktrees/project", state_path=root / "states/project.json")
    write_new(root / "smoke-scope.json", {"version": 1, "repository_id": binding.repository_id})
    initial_submission = CodeResponseSubmission(package=package, response=response(package, WRONG),
        checks=checks, python_executable="python")
    initial_enqueued = queue.enqueue_code_response_with_outcome(actor_user_id=owner, workspace_id=workspace_id,
        repository_id=binding.repository_id, submission=initial_submission)
    assert initial_enqueued.job.pipeline == previous
    os.environ["TANKAI_SMOKE_HOST_ONLY"] = "host-only-synthetic-sentinel"
    initial = QueuedWorkerDispatcher(queue, worker_id="initial-rootless-smoke", container_runtime=runtime).run_once()
    if initial is None or initial.state != JobState.FAILED:
        raise RuntimeError("Incorrect initial code did not fail")
    replay_initial = queue.enqueue_code_response_with_outcome(actor_user_id=owner, workspace_id=workspace_id,
        repository_id=binding.repository_id, submission=initial_submission)
    assert replay_initial.idempotent_replay and replay_initial.job.job_id == initial_enqueued.job.job_id
    parent = orchestrator.state().worker_runs[orchestrator.state().tasks["ORDER-1"].worker_run_id]
    if not parent.implementation_commit or not parent.test_executions:
        details = parent.implementation_executions[-1].summary if parent.implementation_executions else parent.error
        raise RuntimeError("Initial worker did not reach the expected test failure: " + details[-3000:])
    parent_before = parent.model_dump_json()
    request = build_code_repair_request(state=orchestrator.state(), previous_job=previous, run_id=parent.run_id,
        repository=repository, editable_paths=["src/order.py"], context_paths=["src/order.py", "tests/test_order.py"], data_class="public")
    submission = CodeRepairSubmission(package=request, response=response(request, FIXED), previous_job=previous,
        python_executable="python")
    before = orchestrator.store.path.read_bytes()
    enqueued = queue.enqueue_code_repair_with_outcome(actor_user_id=owner, workspace_id=workspace_id,
        repository_id=binding.repository_id, submission=submission)
    assert orchestrator.store.path.read_bytes() == before
    write_new(root / "worker.json", {"job_id": enqueued.job.job_id})
    # Keep the cleanup sentinel small; worker reads only this private fixture.
    (root / "smoke-scope.json").write_text(json.dumps({"version": 1, "repository_id": binding.repository_id,
        "job_id": enqueued.job.job_id}), encoding="utf-8")
    command = [sys.executable, str(Path(__file__).resolve()), "--root", str(root), "--runtime", runtime, "--worker"]
    crashed = subprocess.run([*command, "crash-before-review"], capture_output=True, timeout=180)
    if crashed.returncode != 91:
        raise RuntimeError("Expected worker exit 91: " + crashed.stderr.decode(errors="replace")[-3000:])
    task = orchestrator.state().tasks["ORDER-1"]
    saved = orchestrator.state().worker_runs[task.worker_run_id]
    assert saved.implementation_commit and task.code_repair_attempts == 1
    assert not executor.list_managed_containers(repository_id=binding.repository_id)
    old = queue.get_job(actor_user_id=owner, workspace_id=workspace_id, job_id=enqueued.job.job_id)
    queue.fence_store.force_expire_for_recovery(binding.repository_id, expected_epoch=old.fence_epoch)
    with sqlite3.connect(queue.path) as connection:
        connection.execute("UPDATE development_jobs SET lease_expires_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), enqueued.job.job_id))
    restarted = subprocess.run([*command, "run"], capture_output=True, timeout=180)
    if restarted.returncode:
        raise RuntimeError("Restarted worker failed: " + restarted.stderr.decode(errors="replace")[-3000:])
    finished = queue.get_job(actor_user_id=owner, workspace_id=workspace_id, job_id=enqueued.job.job_id)
    run = orchestrator.state().worker_runs[task.worker_run_id]
    assert finished.state == JobState.SUCCEEDED and run.state.value == "ready_to_integrate"
    assert run.run_id == saved.run_id and run.implementation_commit == saved.implementation_commit
    assert run.implementation_executions == saved.implementation_executions
    assert orchestrator.state().tasks["ORDER-1"].code_repair_attempts == 1
    assert orchestrator.state().worker_runs[parent.run_id].model_dump_json() == parent_before
    assert git(repository, "rev-parse", "HEAD") == base and (repository / "src/order.py").read_text() == STUB
    actual_tests = Path(run.workspace_path) / "tests/test_order.py"
    assert actual_tests.read_bytes() == TESTS.encode()
    assert (Path(run.workspace_path) / "src/order.py").read_bytes() == FIXED.encode()
    assert (Path(run.workspace_path) / "src/order.py").stat().st_uid == os.geteuid()
    audit = next(item for item in run.review_executions if item.command == render_command(review))
    assert audit.passed
    controls = json.loads(audit.summary.splitlines()[-1])
    final = orchestrator.store.path.read_bytes()
    replay = queue.enqueue_code_repair_with_outcome(actor_user_id=owner, workspace_id=workspace_id,
        repository_id=binding.repository_id, submission=submission)
    assert replay.idempotent_replay and replay.job.job_id == finished.job_id
    assert orchestrator.store.path.read_bytes() == final
    assert not executor.list_managed_containers(repository_id=binding.repository_id)
    receipt = {"version": 1, "outcome": "PASS", "real_container_execution": True,
        "runtime": asdict(profile), "host_uid": os.geteuid(), "image_id": image,
        "base_image": base_file.read_text().strip() if base_file else None,
        "source_commit": git(SOURCE_ROOT, "rev-parse", "HEAD"), "pr_head": os.environ.get("TANKAI_SOURCE_HEAD"),
        "queue_job_id": finished.job_id, "run_id": run.run_id, "implementation_commit": run.implementation_commit,
        "initial_test_failed": True, "unchanged_unit_tests": 4, "tests_passed": True,
        "initial_code_response_queued": True, "initial_duplicate_same_job": True,
        "review_passed": True, "qa_passed": True, "isolation_controls": controls,
        "actual_worker_exit": crashed.returncode, "same_run_and_commit_after_restart": True,
        "lease_recovery": "test-only expiry after observed worker exit and empty container inventory",
        "queue_attempts": finished.attempts, "old_fence_epoch": old.fence_epoch, "new_fence_epoch": finished.fence_epoch,
        "code_repair_attempts": 1, "duplicate_submission_same_job": True, "failed_run_preserved": True,
        "tests_sha256": hashlib.sha256(TESTS.encode()).hexdigest(), "source_main_unchanged": True,
        "host_file_owner_preserved": True, "containers_remaining": 0,
        "new_provider_api_calls": 0, "independent_model_review": False,
        "merged": False, "production_deployed": False}
    write_new(output, receipt)
    print("rootless code correction receipt: " + json.dumps(receipt, sort_keys=True))
    print("rootless code correction smoke: PASS")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime", default="podman")
    parser.add_argument("--image-id-file", type=Path)
    parser.add_argument("--base-image-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=("run", "crash-before-review"))
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.cleanup:
        cleanup(root, args.runtime)
    elif args.worker:
        worker(root, args.runtime, args.worker == "crash-before-review")
    else:
        if args.image_id_file is None or args.output is None:
            parser.error("--image-id-file and --output are required")
        smoke(root, args.runtime, args.image_id_file, args.output, args.base_image_file)


if __name__ == "__main__":
    main()
