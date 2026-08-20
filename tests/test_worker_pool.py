from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from tankai.dev_orchestrator import (
    CommandSpec,
    DevelopmentOrchestrator,
    DevelopmentRole,
    GateJob,
    GitWorkspaceManager,
    TaskSpec,
    TestExecution,
    WorkerIsolationSpec,
    WorkerJob,
    WorkerPipelineJob,
    WorkerPoolJob,
    WorkerPoolRunner,
    WorkerRunState,
    render_command,
)


def init_repo(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "Pool Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "pool@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "gc.auto", "0"], cwd=path, check=True)
    for module in ("auth", "notifications"):
        target = path / "backend" / "src" / module
        target.mkdir(parents=True)
        (target / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "--all"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=path, check=True, stdout=subprocess.PIPE)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def start_support_agent(
    orchestrator: DevelopmentOrchestrator,
    commit: str,
    task_id: str,
    role: DevelopmentRole,
) -> str:
    orchestrator.create_task(TaskSpec(
        task_id=task_id,
        goal=f"Shared {role.value} gate",
        base_commit=commit,
        acceptance_criteria=["Unabhängige Prüfung ausgeführt"],
    ))
    return orchestrator.start_agent(task_id, role).agent_id


def make_pool(tmp_path: Path):
    repository = tmp_path / "repo"
    commit = init_repo(repository)
    orchestrator = DevelopmentOrchestrator.initialize(
        str(tmp_path / "state.json"),
        current_version="1.2.0-test",
        current_branch="main",
        current_commit=commit,
    )
    reviewer = start_support_agent(
        orchestrator, commit, "REVIEW-POOL", DevelopmentRole.REVIEWER
    )
    qa = start_support_agent(orchestrator, commit, "QA-POOL", DevelopmentRole.QA)
    signal_dir = tmp_path / "signals"
    signal_dir.mkdir()
    pipelines: list[WorkerPipelineJob] = []

    definitions = (
        (
            "AUTH-001",
            DevelopmentRole.BACKEND,
            "backend/src/auth/**",
            "backend/src/auth/service.py",
            "auth",
        ),
        (
            "NOTIFY-001",
            DevelopmentRole.BACKEND,
            "backend/src/notifications/**",
            "backend/src/notifications/service.py",
            "notifications",
        ),
    )
    for task_id, role, scope, relative_file, marker in definitions:
        test = CommandSpec(argv=[
            sys.executable,
            "-S",
            "-c",
            f"from pathlib import Path; assert Path('{relative_file}').read_text() == '{marker}\\n'",
        ])
        orchestrator.create_task(TaskSpec(
            task_id=task_id,
            goal=f"Implement {marker}",
            base_commit=commit,
            allowed_paths=[scope],
            acceptance_criteria=[f"{marker} file exists"],
            required_tests=[render_command(test)],
        ))
        agent = orchestrator.start_agent(task_id, role)
        implementation = CommandSpec(
            argv=[
                sys.executable,
                "-S",
                "-c",
                (
                    "from pathlib import Path; import os,time; "
                    "signals=Path(os.environ['POOL_SIGNAL_DIR']); "
                    f"(signals/'{marker}').write_text('ready'); "
                    "deadline=time.monotonic()+8; "
                    "exec(\"while len(list(signals.iterdir())) < 2:\\n"
                    "    assert time.monotonic() < deadline, 'pool did not run concurrently'\\n"
                    "    time.sleep(0.02)\"); "
                    f"Path('{relative_file}').write_text('{marker}\\n', encoding='utf-8')"
                ),
            ],
            timeout_seconds=12,
            env={"POOL_SIGNAL_DIR": str(signal_dir)},
        )
        pipelines.append(WorkerPipelineJob(
            worker=WorkerJob(
                agent_id=agent.agent_id,
                implementation_summary=f"Implemented {marker}",
                commit_message=f"Implement {marker}",
                implementation_commands=[implementation],
                test_commands=[test],
            ),
            gates=GateJob(
                reviewer_agent_id=reviewer,
                review_commands=[test],
                qa_agent_id=qa,
                qa_commands=[test],
            ),
        ))

    return (
        repository,
        orchestrator,
        GitWorkspaceManager(repository, tmp_path / "worktrees"),
        WorkerPoolJob(pipelines=pipelines, max_parallel=2),
    )


def test_worker_pool_runs_two_programming_agents_concurrently(tmp_path: Path) -> None:
    _, orchestrator, manager, job = make_pool(tmp_path)

    result = WorkerPoolRunner(orchestrator, manager).run(job)

    assert result.passed is True
    assert set(result.completed) == {
        job.pipelines[0].worker.agent_id,
        job.pipelines[1].worker.agent_id,
    }
    assert result.failures == {}
    assert result.cancelled == ()
    assert all(
        item.run.state == WorkerRunState.READY_TO_INTEGRATE
        for item in result.completed.values()
    )


def test_worker_pool_reports_one_failure_without_erasing_success(tmp_path: Path) -> None:
    _, orchestrator, manager, job = make_pool(tmp_path)
    broken = job.pipelines[0].model_copy(deep=True)
    signal_dir = tmp_path / "signals"
    broken.worker.implementation_commands = [
        CommandSpec(
            argv=[
                sys.executable,
                "-S",
                "-c",
                (
                    "from pathlib import Path; import os; "
                    "Path(os.environ['POOL_SIGNAL_DIR'], 'auth').write_text('ready'); "
                    "raise SystemExit(9)"
                ),
            ],
            env={"POOL_SIGNAL_DIR": str(signal_dir)},
        )
    ]
    mixed = job.model_copy(update={"pipelines": [broken, job.pipelines[1]]})

    result = WorkerPoolRunner(orchestrator, manager).run(mixed)

    assert result.passed is False
    assert broken.worker.agent_id in result.failures
    assert job.pipelines[1].worker.agent_id in result.completed


class RecordingPoolContainerExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool, str]] = []
        self.probes = 0
        self._lock = threading.Lock()

    def ensure_available(self) -> str:
        with self._lock:
            self.probes += 1
        return "test-rootless-runtime"

    def execute(
        self,
        workspace,
        command,
        isolation,
        *,
        allowed_paths,
        denied_paths=(),
        read_only_workspace,
        run_id,
        phase,
    ) -> TestExecution:
        completed = subprocess.run(
            command.argv,
            cwd=workspace.path,
            env={**os.environ, **command.env},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=command.timeout_seconds,
            check=False,
        )
        with self._lock:
            self.calls.append((phase, read_only_workspace, run_id))
        return TestExecution(
            command=" ".join(command.argv),
            passed=completed.returncode == 0,
            exit_code=completed.returncode,
            summary=completed.stdout[-10_000:],
        )


def test_worker_pool_shares_controlled_container_executor(tmp_path: Path) -> None:
    _, orchestrator, manager, job = make_pool(tmp_path)
    isolation = WorkerIsolationSpec(
        image="tankai-worker@sha256:" + "c" * 64,
        user="10001:10001",
    )
    isolated = job.model_copy(update={
        "pipelines": [
            pipeline.model_copy(update={"isolation": isolation})
            for pipeline in job.pipelines
        ]
    })
    executor = RecordingPoolContainerExecutor()

    result = WorkerPoolRunner(
        orchestrator,
        manager,
        container_executor=executor,
        require_container_isolation=True,
    ).run(isolated)

    assert result.passed is True
    assert executor.probes == 2
    phases = [phase for phase, _, _ in executor.calls]
    assert phases.count("implement") == 2
    assert phases.count("test") == 2
    assert phases.count("review") == 2
    assert phases.count("qa") == 2
    assert all(read_only for phase, read_only, _ in executor.calls if phase != "implement")


def test_cli_executes_parallel_worker_pool(tmp_path: Path) -> None:
    repository, _, _, job = make_pool(tmp_path)
    job_path = tmp_path / "pool-job.json"
    job_path.write_text(job.model_dump_json(indent=2), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tankai.dev_orchestrator.cli",
            "--state",
            str(tmp_path / "state.json"),
            "run-pool",
            "--repository",
            str(repository),
            "--workspace-root",
            str(tmp_path / "worktrees"),
            "--job",
            str(job_path),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["passed"] is True
    assert len(payload["completed"]) == 2
    assert payload["failures"] == {}
    assert payload["cancelled"] == []
