from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tankai.dev_orchestrator import (
    CommandSpec,
    DevelopmentOrchestrator,
    DevelopmentRole,
    GateJob,
    GitWorkspaceError,
    GitWorkspaceManager,
    IntegrationExecutionError,
    IntegrationJob,
    ProjectStateStore,
    TaskSpec,
    TaskState,
    TransitionError,
    WorkerExecutionError,
    WorkerIntegrationRunner,
    WorkerExecutionBackend,
    WorkerIsolationSpec,
    WorkerJob,
    WorkerPipelineJob,
    WorkerPipelineRunner,
    WorkerRunRecord,
    WorkerRunState,
    render_command,
)


def init_repo(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "Worker Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "worker@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "gc.auto", "0"], cwd=path, check=True)
    subprocess.run(["git", "config", "maintenance.auto", "false"], cwd=path, check=True)
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    (path / "backend" / "src" / "auth").mkdir(parents=True)
    (path / "backend" / "src" / "auth" / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "--all"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=path, check=True, stdout=subprocess.PIPE)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def support_agent(
    orchestrator: DevelopmentOrchestrator,
    commit: str,
    task_id: str,
    role: DevelopmentRole,
) -> str:
    orchestrator.create_task(TaskSpec(
        task_id=task_id,
        goal=f"Support pool {role.value}",
        base_commit=commit,
        allowed_paths=[],
        acceptance_criteria=["Unabhängige Prüfung ausgeführt"],
    ))
    return orchestrator.start_agent(task_id, role).agent_id


def make_pipeline(tmp_path: Path, *, security: bool = False):
    repository = tmp_path / "repo"
    commit = init_repo(repository)
    orchestrator = DevelopmentOrchestrator.initialize(
        str(tmp_path / "state.json"),
        current_version="0.9.0-test",
        current_branch="main",
        current_commit=commit,
    )
    test_command = CommandSpec(
        argv=[
            sys.executable,
            "-S",
            "-c",
            "from backend.src.auth.service import authenticate; assert authenticate('ok') is True",
        ]
    )
    orchestrator.create_task(TaskSpec(
        task_id="AUTH-001",
        goal="Implement authentication",
        base_commit=commit,
        allowed_paths=["backend/src/auth/**"],
        acceptance_criteria=["authenticate returns a boolean"],
        required_tests=[render_command(test_command)],
        requires_security_review=security,
    ))
    author = orchestrator.start_agent("AUTH-001", DevelopmentRole.BACKEND)
    reviewer = support_agent(orchestrator, commit, "REVIEW-POOL", DevelopmentRole.REVIEWER)
    qa = support_agent(orchestrator, commit, "QA-POOL", DevelopmentRole.QA)
    security_agent = (
        support_agent(orchestrator, commit, "SECURITY-POOL", DevelopmentRole.SECURITY)
        if security
        else None
    )
    manager = GitWorkspaceManager(repository, tmp_path / "worktrees")
    job = WorkerPipelineJob(
        worker=WorkerJob(
            agent_id=author.agent_id,
            implementation_summary="Authentication service implemented.",
            commit_message="Implement authentication service",
            implementation_commands=[CommandSpec(
                argv=[
                    sys.executable,
                    "-S",
                    "-c",
                    (
                        "from pathlib import Path; "
                        "Path('backend/src/auth/service.py').write_text("
                        "\"def authenticate(token):\\n    return token == 'ok'\\n\", encoding='utf-8')"
                    ),
                ]
            )],
            test_commands=[test_command],
        ),
        gates=GateJob(
            reviewer_agent_id=reviewer,
            review_commands=[CommandSpec(argv=[sys.executable, "-S", "-m", "compileall", "-q", "backend/src/auth"])],
            qa_agent_id=qa,
            qa_commands=[test_command],
            security_agent_id=security_agent,
            security_commands=(
                [CommandSpec(
                    argv=[
                        sys.executable,
                        "-S",
                        "-c",
                        (
                            "from pathlib import Path; "
                            "s=Path('backend/src/auth/service.py').read_text(); "
                            "assert 'eval(' not in s and 'exec(' not in s"
                        ),
                    ]
                )]
                if security
                else []
            ),
        ),
    )
    return repository, orchestrator, manager, job


def test_worker_pipeline_executes_commits_and_passes_independent_gates(tmp_path: Path) -> None:
    repository, orchestrator, manager, job = make_pipeline(tmp_path, security=True)
    result = WorkerPipelineRunner(orchestrator, manager).run(job)

    assert result.run.state == WorkerRunState.READY_TO_INTEGRATE
    assert result.run.implementation_commit
    assert result.run.changed_files == ["backend/src/auth/service.py"]
    assert result.run.implementation_executions[0].passed is True
    assert result.run.test_executions[0].passed is True
    assert all(item.passed for item in result.run.review_executions)
    assert all(item.passed for item in result.run.qa_executions)
    assert all(item.passed for item in result.run.security_executions)
    assert result.workspace.path.exists()

    state = orchestrator.state()
    task = state.tasks["AUTH-001"]
    assert task.state == TaskState.READY_TO_INTEGRATE
    assert task.implementation_commit == result.run.implementation_commit
    assert task.worker_run_id == result.run.run_id
    assert len(result.run.status_messages) >= 9

    # MAIN is deliberately untouched until the orchestrator performs integration.
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip() == state.current_commit




def test_execution_guard_blocks_commit_after_fence_loss(tmp_path: Path) -> None:
    repository, orchestrator, manager, job = make_pipeline(tmp_path)
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()
    lost = {"value": False}
    stages: list[str] = []

    def guard(stage: str) -> None:
        stages.append(stage)
        if stage == "before_implementation_commit":
            lost["value"] = True
        if lost["value"]:
            raise RuntimeError("external fence lost")

    with pytest.raises(WorkerExecutionError, match="external fence lost") as captured:
        WorkerPipelineRunner(
            orchestrator,
            manager,
            execution_guard=guard,
        ).run(job)

    run = orchestrator.state().worker_runs[captured.value.run_id]
    workspace = Path(run.workspace_path)
    assert "before_implementation_commit" in stages
    assert manager.head_commit(manager.workspace_from_binding(
        agent_id=run.agent_id,
        branch=run.branch,
        workspace_path=workspace,
        base_commit=run.base_commit,
    )) == baseline
    assert (workspace / "backend/src/auth/service.py").exists()
    assert run.implementation_commit is None
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip() == baseline

def test_worker_pipeline_blocks_out_of_scope_change_and_persists_error(tmp_path: Path) -> None:
    _, orchestrator, manager, job = make_pipeline(tmp_path)
    bad_worker = job.worker.model_copy(update={
        "implementation_commands": [CommandSpec(
            argv=[
                sys.executable,
                "-S",
                "-c",
                "from pathlib import Path; Path('README.md').write_text('changed\\n')",
            ]
        )],
        "cleanup_workspace_on_failure": True,
    })
    bad_job = job.model_copy(update={"worker": bad_worker})

    with pytest.raises(WorkerExecutionError) as captured:
        WorkerPipelineRunner(orchestrator, manager).run(bad_job)

    state = orchestrator.state()
    run = state.worker_runs[captured.value.run_id]
    assert run.state == WorkerRunState.BLOCKED
    assert run.phase.value == "failed"
    assert "außerhalb" in run.error
    assert state.tasks["AUTH-001"].state == TaskState.BLOCKED
    assert not Path(run.workspace_path).exists()
    assert state.agents[run.agent_id].workspace_path is None
    assert state.agents[run.agent_id].branch is None


def test_worker_pipeline_rejected_review_never_reaches_qa(tmp_path: Path) -> None:
    _, orchestrator, manager, job = make_pipeline(tmp_path)
    rejected = job.model_copy(update={
        "gates": job.gates.model_copy(update={
            "review_commands": [CommandSpec(argv=[sys.executable, "-S", "-c", "raise SystemExit(7)"])]
        })
    })

    with pytest.raises(WorkerExecutionError) as captured:
        WorkerPipelineRunner(orchestrator, manager).run(rejected)

    state = orchestrator.state()
    run = state.worker_runs[captured.value.run_id]
    assert run.state == WorkerRunState.BLOCKED
    assert run.review_executions[-1].exit_code == 7
    assert run.qa_executions == []
    assert state.tasks["AUTH-001"].state == TaskState.BLOCKED


def test_state_store_migrates_v1_worker_fields(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    orchestrator = DevelopmentOrchestrator.initialize(
        str(state_path),
        current_version="0.8.0-old",
        current_branch="main",
        current_commit="abc123",
    )
    orchestrator.create_task(TaskSpec(
        task_id="OLD",
        goal="old task",
        base_commit="abc123",
        acceptance_criteria=["exists"],
    ))
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    payload.pop("worker_runs", None)
    payload["tasks"]["OLD"].pop("worker_run_id", None)
    payload["tasks"]["OLD"].pop("implementation_commit", None)
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    migrated = ProjectStateStore(state_path).load()
    assert migrated.schema_version == 6
    assert migrated.worker_runs == {}
    assert migrated.tasks["OLD"].worker_run_id is None
    assert migrated.tasks["OLD"].implementation_commit is None


def test_worker_cannot_hide_out_of_scope_change_in_own_commit(tmp_path: Path) -> None:
    _, orchestrator, manager, job = make_pipeline(tmp_path)
    malicious_worker = job.worker.model_copy(update={
        "implementation_commands": [CommandSpec(
            argv=[
                sys.executable,
                "-S",
                "-c",
                "from pathlib import Path; Path('README.md').write_text('hidden\\n')",
            ]
        ), CommandSpec(argv=["git", "add", "--all"]), CommandSpec(
            argv=["git", "commit", "-m", "worker bypass"]
        )],
    })
    malicious = job.model_copy(update={"worker": malicious_worker})

    with pytest.raises(WorkerExecutionError) as captured:
        WorkerPipelineRunner(orchestrator, manager).run(malicious)

    run = orchestrator.state().worker_runs[captured.value.run_id]
    assert run.state == WorkerRunState.BLOCKED
    assert "HEAD" in run.error


def test_gate_that_modifies_tracked_source_is_rejected(tmp_path: Path) -> None:
    _, orchestrator, manager, job = make_pipeline(tmp_path)
    mutating_gate = job.model_copy(update={
        "gates": job.gates.model_copy(update={
            "review_commands": [CommandSpec(
                argv=[
                    sys.executable,
                    "-S",
                    "-c",
                    "from pathlib import Path; Path('backend/src/auth/service.py').write_text('tampered\\n')",
                ]
            )]
        })
    })

    with pytest.raises(WorkerExecutionError) as captured:
        WorkerPipelineRunner(orchestrator, manager).run(mutating_gate)

    run = orchestrator.state().worker_runs[captured.value.run_id]
    assert run.state == WorkerRunState.BLOCKED
    assert run.review_executions[-1].command == "[gate-integrity]"
    assert "versionierte Dateien" in run.review_executions[-1].summary


def test_reopened_worker_reuses_isolated_workspace_and_creates_new_commit(tmp_path: Path) -> None:
    _, orchestrator, manager, job = make_pipeline(tmp_path)
    rejected = job.model_copy(update={
        "gates": job.gates.model_copy(update={
            "review_commands": [CommandSpec(argv=[sys.executable, "-S", "-c", "raise SystemExit(9)"])]
        })
    })
    with pytest.raises(WorkerExecutionError) as first_error:
        WorkerPipelineRunner(orchestrator, manager).run(rejected)
    first_run = orchestrator.state().worker_runs[first_error.value.run_id]
    first_workspace = first_run.workspace_path
    first_commit = first_run.implementation_commit
    assert first_commit

    orchestrator.reopen_task("AUTH-001", reason="Review findings fixed")
    rework_worker = job.worker.model_copy(update={
        "implementation_commands": [CommandSpec(
            argv=[
                sys.executable,
                "-S",
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('backend/src/auth/service.py').write_text("
                    "\"def authenticate(token):\\n    return token in {'ok', 'safe'}\\n\", encoding='utf-8')"
                ),
            ]
        )],
        "implementation_summary": "Authentication service corrected after review.",
        "commit_message": "Fix authentication after review",
    })
    rework = job.model_copy(update={"worker": rework_worker})
    result = WorkerPipelineRunner(orchestrator, manager).run(rework)

    assert result.run.state == WorkerRunState.READY_TO_INTEGRATE
    assert result.run.workspace_path == first_workspace
    assert result.run.implementation_commit != first_commit
    state = orchestrator.state()
    assert len(state.worker_runs) == 2
    assert state.tasks["AUTH-001"].worker_run_id == result.run.run_id
    assert state.tasks["AUTH-001"].implementation_commit == result.run.implementation_commit


def test_worker_task_cannot_be_integrated_by_state_only(tmp_path: Path) -> None:
    _, orchestrator, manager, job = make_pipeline(tmp_path)
    result = WorkerPipelineRunner(orchestrator, manager).run(job)
    with pytest.raises(TransitionError, match="reale Git-Integration"):
        orchestrator.integrate_task("AUTH-001", new_commit=result.run.implementation_commit or "")


def test_cli_executes_validated_worker_pipeline_job(tmp_path: Path) -> None:
    repository, orchestrator, _, job = make_pipeline(tmp_path)
    job_path = tmp_path / "worker-job.json"
    job_path.write_text(job.model_dump_json(indent=2), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tankai.dev_orchestrator.cli",
            "--state",
            str(tmp_path / "state.json"),
            "run-pipeline",
            "--repository",
            str(repository),
            "--workspace-root",
            str(tmp_path / "cli-worktrees"),
            "--job",
            str(job_path),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["run"]["state"] == "ready_to_integrate"
    assert payload["project"]["tasks"]["AUTH-001"] == "ready_to_integrate"
    assert orchestrator.state().schema_version == 6


def advance_main_and_state(
    repository: Path,
    orchestrator: DevelopmentOrchestrator,
    *,
    relative_path: str,
    content: str,
    message: str,
) -> str:
    target = repository / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "--all"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()

    def mutate(state):
        state.current_commit = commit
        state.release_status = "stable"

    orchestrator.store.transaction(mutate)
    return commit


def integration_job_for(result, job: WorkerPipelineJob, *, command: CommandSpec | None = None):
    return IntegrationJob(
        run_id=result.run.run_id,
        test_commands=[command or job.worker.test_commands[0]],
    )


def test_worker_integration_fast_forwards_main_and_finalizes_state(tmp_path: Path) -> None:
    repository, orchestrator, manager, job = make_pipeline(tmp_path)
    pipeline = WorkerPipelineRunner(orchestrator, manager).run(job)
    previous = orchestrator.state().current_commit

    result = WorkerIntegrationRunner(orchestrator, manager).run(
        integration_job_for(pipeline, job)
    )

    state = orchestrator.state()
    assert result.run.state == WorkerRunState.INTEGRATED
    assert result.run.integration_commit == result.integration_commit
    assert result.integration_commit != previous
    assert state.current_commit == result.integration_commit
    assert state.tasks["AUTH-001"].state == TaskState.INTEGRATED
    assert state.tasks["AUTH-001"].integration_commit == result.integration_commit
    assert state.release_status == "stable"
    assert all(item.passed for item in result.run.integration_executions)
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip() == result.integration_commit
    assert not result.workspace.path.exists()
    assert subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{result.workspace.branch}"],
        cwd=repository,
        check=False,
    ).returncode != 0
    assert state.agents[result.run.agent_id].workspace_path is None
    assert state.agents[result.run.agent_id].branch is None


def test_worker_integration_rebases_onto_new_stable_commit(tmp_path: Path) -> None:
    repository, orchestrator, manager, job = make_pipeline(tmp_path)
    pipeline = WorkerPipelineRunner(orchestrator, manager).run(job)
    original_implementation = pipeline.run.implementation_commit
    advanced = advance_main_and_state(
        repository,
        orchestrator,
        relative_path="README.md",
        content="baseline\nmain advanced\n",
        message="Advance main independently",
    )

    result = WorkerIntegrationRunner(orchestrator, manager).run(
        integration_job_for(pipeline, job)
    )

    assert result.rebased is True
    assert result.previous_commit == advanced
    assert result.run.rebased_from_commit == original_implementation
    assert result.run.rebased_commit == result.integration_commit
    assert result.integration_commit != original_implementation
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", advanced, result.integration_commit],
        cwd=repository,
        check=False,
    ).returncode == 0
    assert (repository / "README.md").read_text(encoding="utf-8") == "baseline\nmain advanced\n"


def test_worker_integration_blocks_rebase_conflict_without_touching_main(tmp_path: Path) -> None:
    repository, orchestrator, manager, job = make_pipeline(tmp_path)
    pipeline = WorkerPipelineRunner(orchestrator, manager).run(job)
    advanced = advance_main_and_state(
        repository,
        orchestrator,
        relative_path="backend/src/auth/service.py",
        content="def authenticate(token):\n    return False\n",
        message="Conflicting main implementation",
    )

    with pytest.raises(IntegrationExecutionError, match="Rebase"):
        WorkerIntegrationRunner(orchestrator, manager).run(
            integration_job_for(pipeline, job)
        )

    state = orchestrator.state()
    assert state.current_commit == advanced
    assert state.tasks["AUTH-001"].state == TaskState.BLOCKED
    assert state.worker_runs[pipeline.run.run_id].state == WorkerRunState.BLOCKED
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip() == advanced
    assert not (manager.common_git_dir / "tankai-integration-journal.json").exists()


def test_worker_integration_rolls_back_failed_post_merge_tests(tmp_path: Path) -> None:
    repository, orchestrator, manager, job = make_pipeline(tmp_path)
    pipeline = WorkerPipelineRunner(orchestrator, manager).run(job)
    previous = orchestrator.state().current_commit
    failing = CommandSpec(argv=[sys.executable, "-S", "-c", "raise SystemExit(5)"])
    integration_job = IntegrationJob(
        run_id=pipeline.run.run_id,
        test_commands=[job.worker.test_commands[0], failing],
    )

    with pytest.raises(IntegrationExecutionError, match="Post-Merge"):
        WorkerIntegrationRunner(orchestrator, manager).run(integration_job)

    state = orchestrator.state()
    assert state.current_commit == previous
    assert state.tasks["AUTH-001"].state == TaskState.BLOCKED
    assert state.worker_runs[pipeline.run.run_id].state == WorkerRunState.BLOCKED
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip() == previous
    assert not (repository / "backend/src/auth/service.py").exists()


def test_worker_integration_rejects_dirty_main_before_state_transition(tmp_path: Path) -> None:
    repository, orchestrator, manager, job = make_pipeline(tmp_path)
    pipeline = WorkerPipelineRunner(orchestrator, manager).run(job)
    (repository / "local-note.txt").write_text("do not delete\n", encoding="utf-8")

    with pytest.raises(IntegrationExecutionError, match="nicht sauber"):
        WorkerIntegrationRunner(orchestrator, manager).run(
            integration_job_for(pipeline, job)
        )

    state = orchestrator.state()
    assert state.tasks["AUTH-001"].state == TaskState.READY_TO_INTEGRATE
    assert state.worker_runs[pipeline.run.run_id].state == WorkerRunState.READY_TO_INTEGRATE
    assert (repository / "local-note.txt").exists()


def test_integration_journal_recovers_merge_before_state_commit(tmp_path: Path) -> None:
    repository, orchestrator, manager, job = make_pipeline(tmp_path)
    pipeline = WorkerPipelineRunner(orchestrator, manager).run(job)
    runner = WorkerIntegrationRunner(orchestrator, manager)
    previous = orchestrator.state().current_commit
    orchestrator.begin_worker_integration(
        pipeline.run.run_id,
        expected_project_commit=previous,
    )
    merged = manager.fast_forward_repository(
        branch="main",
        source_branch=pipeline.workspace.branch,
        expected_base_commit=previous,
    )
    runner.journal.write({
        "stage": "merged",
        "run_id": pipeline.run.run_id,
        "task_id": pipeline.run.task_id,
        "branch": pipeline.workspace.branch,
        "workspace_path": str(pipeline.workspace.path),
        "previous_commit": previous,
        "original_base_commit": pipeline.run.base_commit,
        "original_implementation_commit": pipeline.run.implementation_commit,
        "integration_commit": merged,
    })

    with manager.integration_lock():
        runner.recover_interrupted_integration()

    state = orchestrator.state()
    assert manager.repository_head() == previous
    assert state.current_commit == previous
    assert state.worker_runs[pipeline.run.run_id].state == WorkerRunState.READY_TO_INTEGRATE
    assert state.tasks["AUTH-001"].state == TaskState.READY_TO_INTEGRATE
    assert not runner.journal.path.exists()


def test_cli_integrates_ready_worker_run(tmp_path: Path) -> None:
    repository, orchestrator, manager, job = make_pipeline(tmp_path)
    pipeline = WorkerPipelineRunner(orchestrator, manager).run(job)
    integration_job = integration_job_for(pipeline, job)
    path = tmp_path / "integration-job.json"
    path.write_text(integration_job.model_dump_json(indent=2), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tankai.dev_orchestrator.cli",
            "--state",
            str(tmp_path / "state.json"),
            "integrate",
            "--repository",
            str(repository),
            "--workspace-root",
            str(tmp_path / "worktrees"),
            "--job",
            str(path),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["run"]["state"] == "integrated"
    assert payload["project"]["tasks"]["AUTH-001"] == "integrated"
    assert payload["integration"]["integration_commit"] == payload["project"]["commit"]


def test_integration_requires_all_task_required_tests(tmp_path: Path) -> None:
    repository, orchestrator, manager, job = make_pipeline(tmp_path)
    pipeline = WorkerPipelineRunner(orchestrator, manager).run(job)
    previous = orchestrator.state().current_commit
    incomplete = IntegrationJob(
        run_id=pipeline.run.run_id,
        test_commands=[CommandSpec(argv=[sys.executable, "-S", "-c", "assert True"])],
    )

    with pytest.raises(IntegrationExecutionError, match="verpflichtenden Tests"):
        WorkerIntegrationRunner(orchestrator, manager).run(incomplete)

    state = orchestrator.state()
    assert state.current_commit == previous
    assert state.tasks["AUTH-001"].state == TaskState.READY_TO_INTEGRATE
    assert state.worker_runs[pipeline.run.run_id].state == WorkerRunState.READY_TO_INTEGRATE
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip() == previous


def test_integration_records_automatic_diff_check(tmp_path: Path) -> None:
    _, orchestrator, manager, job = make_pipeline(tmp_path)
    pipeline = WorkerPipelineRunner(orchestrator, manager).run(job)

    result = WorkerIntegrationRunner(orchestrator, manager).run(
        integration_job_for(pipeline, job)
    )

    assert result.run.integration_executions[0].passed is True
    assert result.run.integration_executions[0].command.startswith("git diff --check ")


def test_recovery_restores_branch_if_crash_occurs_after_rebase_before_state_update(
    tmp_path: Path,
) -> None:
    repository, orchestrator, manager, job = make_pipeline(tmp_path)
    pipeline = WorkerPipelineRunner(orchestrator, manager).run(job)
    original_implementation = pipeline.run.implementation_commit
    assert original_implementation
    advanced = advance_main_and_state(
        repository,
        orchestrator,
        relative_path="README.md",
        content="baseline\nadvanced\n",
        message="Advance main for rebase recovery",
    )
    runner = WorkerIntegrationRunner(orchestrator, manager)
    orchestrator.begin_worker_integration(
        pipeline.run.run_id,
        expected_project_commit=advanced,
    )
    runner.journal.write({
        "stage": "prepared",
        "run_id": pipeline.run.run_id,
        "task_id": pipeline.run.task_id,
        "branch": pipeline.workspace.branch,
        "workspace_path": str(pipeline.workspace.path),
        "previous_commit": advanced,
        "original_base_commit": pipeline.run.base_commit,
        "original_implementation_commit": original_implementation,
        "integration_commit": None,
    })
    rebased_workspace, rebased_commit = manager.rebase_workspace(
        pipeline.workspace,
        old_base_commit=pipeline.run.base_commit,
        new_base_commit=advanced,
        expected_head=original_implementation,
    )
    assert rebased_commit != original_implementation

    with manager.integration_lock():
        runner.recover_interrupted_integration()

    state = orchestrator.state()
    assert manager.head_commit(rebased_workspace) == original_implementation
    assert state.worker_runs[pipeline.run.run_id].state == WorkerRunState.READY_TO_INTEGRATE
    assert state.worker_runs[pipeline.run.run_id].implementation_commit == original_implementation
    assert state.tasks["AUTH-001"].state == TaskState.READY_TO_INTEGRATE
    assert manager.repository_head() == advanced
    assert not runner.journal.path.exists()


def test_state_store_migrates_v2_integration_fields(tmp_path: Path) -> None:
    state_path = tmp_path / "state-v2.json"
    DevelopmentOrchestrator.initialize(
        str(state_path),
        current_version="0.9.0-old",
        current_branch="main",
        current_commit="abc123",
    )
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    payload["worker_runs"] = {
        "RUN-OLD": {
            "run_id": "RUN-OLD",
            "agent_id": "AGENT-OLD",
            "task_id": "TASK-OLD",
            "base_commit": "abc123",
            "branch": "tankai/old/task",
            "workspace_path": "/tmp/old-workspace",
        }
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    migrated = ProjectStateStore(state_path).load()
    run = migrated.worker_runs["RUN-OLD"]
    assert migrated.schema_version == 6
    assert run.integration_executions == []
    assert run.rebased_from_commit is None
    assert run.rebased_commit is None
    assert run.integration_commit is None
    assert run.execution_backend == WorkerExecutionBackend.HOST
    assert run.isolation is None
    assert run.integration_isolation is None


def test_worker_integration_rolls_back_when_state_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, orchestrator, manager, job = make_pipeline(tmp_path)
    pipeline = WorkerPipelineRunner(orchestrator, manager).run(job)
    previous = orchestrator.state().current_commit

    def fail_state_commit(*args, **kwargs):
        raise RuntimeError("simulated durable state failure")

    monkeypatch.setattr(orchestrator, "finalize_worker_integration", fail_state_commit)
    with pytest.raises(IntegrationExecutionError, match="state failure"):
        WorkerIntegrationRunner(orchestrator, manager).run(
            integration_job_for(pipeline, job)
        )

    state = orchestrator.state()
    assert state.current_commit == previous
    assert manager.repository_head() == previous
    assert state.tasks["AUTH-001"].state == TaskState.BLOCKED
    assert state.worker_runs[pipeline.run.run_id].state == WorkerRunState.BLOCKED
    assert not (manager.common_git_dir / "tankai-integration-journal.json").exists()


def test_integration_journal_rejects_workspace_outside_managed_root(tmp_path: Path) -> None:
    repository, orchestrator, manager, job = make_pipeline(tmp_path)
    pipeline = WorkerPipelineRunner(orchestrator, manager).run(job)
    current = orchestrator.state().current_commit
    runner = WorkerIntegrationRunner(orchestrator, manager)
    orchestrator.begin_worker_integration(
        pipeline.run.run_id,
        expected_project_commit=current,
    )
    runner.journal.write({
        "stage": "prepared",
        "run_id": pipeline.run.run_id,
        "task_id": pipeline.run.task_id,
        "branch": pipeline.workspace.branch,
        "workspace_path": str(repository),
        "previous_commit": current,
        "original_base_commit": pipeline.run.base_commit,
        "original_implementation_commit": pipeline.run.implementation_commit,
        "integration_commit": None,
    })

    with manager.integration_lock():
        with pytest.raises(GitWorkspaceError, match="außerhalb des Workspace-Roots"):
            runner.recover_interrupted_integration()

    assert manager.repository_head() == current
    assert runner.journal.path.exists()


class LocalRecordingContainerExecutor:
    """Test double: records container policy while executing commands locally."""

    def __init__(self, manager: GitWorkspaceManager) -> None:
        self.manager = manager
        self.calls: list[dict] = []
        self.probes = 0

    def ensure_available(self) -> str:
        self.probes += 1
        return "test-runtime"

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
        metadata_labels=None,
    ):
        self.calls.append({
            "phase": phase,
            "read_only": read_only_workspace,
            "image": isolation.image,
            "allowed_paths": tuple(allowed_paths),
            "denied_paths": tuple(denied_paths),
            "run_id": run_id,
            "metadata_labels": dict(metadata_labels or {}),
        })
        return self.manager.run_command(
            workspace,
            command.argv,
            timeout_seconds=command.timeout_seconds,
            env=command.env,
        )


def test_container_policy_covers_worker_gates_and_post_merge_tests(tmp_path: Path) -> None:
    repository, orchestrator, manager, job = make_pipeline(tmp_path, security=True)
    image = "tankai-worker@sha256:" + "b" * 64
    isolation = WorkerIsolationSpec(image=image, user="10001:10001")
    fake = LocalRecordingContainerExecutor(manager)
    isolated_job = job.model_copy(update={"isolation": isolation})

    result = WorkerPipelineRunner(
        orchestrator,
        manager,
        container_executor=fake,
        require_container_isolation=True,
        container_metadata={
            "job_id": "JOB-AUTH-001",
            "repository_id": "REPO-AUTH",
            "workspace_id": "WORKSPACE-AUTH",
            "tenant_id": "TENANT-AUTH",
            "fence_epoch": "4",
            "worker_id": "runner-01",
        },
    ).run(isolated_job)

    assert result.run.execution_backend == WorkerExecutionBackend.DOCKER
    assert result.run.isolation is not None
    assert result.run.isolation.image == image
    assert fake.probes == 1
    assert [call["phase"] for call in fake.calls] == [
        "implement",
        "test",
        "review",
        "security",
        "qa",
    ]
    assert fake.calls[0]["read_only"] is False
    assert all(call["read_only"] is True for call in fake.calls[1:])
    assert all(
        call["metadata_labels"]["job_id"] == "JOB-AUTH-001"
        for call in fake.calls
    )
    assert all(
        call["metadata_labels"]["fence_epoch"] == "4"
        for call in fake.calls
    )

    integration = WorkerIntegrationRunner(
        orchestrator,
        manager,
        container_executor=fake,
        require_container_isolation=True,
        container_metadata={
            "job_id": "JOB-AUTH-001",
            "repository_id": "REPO-AUTH",
            "workspace_id": "WORKSPACE-AUTH",
            "tenant_id": "TENANT-AUTH",
            "fence_epoch": "4",
            "worker_id": "integrator-01",
        },
    ).run(IntegrationJob(
        run_id=result.run.run_id,
        test_commands=job.worker.test_commands,
        isolation=isolation,
    ))
    assert integration.run.state == WorkerRunState.INTEGRATED
    assert integration.run.integration_isolation is not None
    assert integration.run.integration_isolation.image == image
    assert fake.calls[-1]["phase"] == "integration_test"
    assert fake.calls[-1]["read_only"] is True
    assert fake.calls[-1]["metadata_labels"]["job_id"] == "JOB-AUTH-001"
    assert fake.calls[-1]["metadata_labels"]["worker_id"] == "integrator-01"
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip() == integration.integration_commit


def test_container_checked_run_cannot_downgrade_integration_to_host() -> None:
    isolation = WorkerIsolationSpec(
        image="tankai-worker@sha256:" + "c" * 64,
        user="10001:10001",
    )
    run = WorkerRunRecord(
        run_id="RUN-CONTAINER",
        agent_id="AGENT-CONTAINER",
        task_id="TASK-CONTAINER",
        base_commit="a" * 40,
        branch="tankai/container/task",
        workspace_path="/tmp/container-task",
        execution_backend=WorkerExecutionBackend.DOCKER,
        isolation=isolation,
    )
    runner = object.__new__(WorkerIntegrationRunner)
    runner.require_container_isolation = False
    with pytest.raises(Exception, match="herabgestuft"):
        runner._validate_isolation(
            run,
            IntegrationJob(
                run_id=run.run_id,
                test_commands=[CommandSpec(argv=["python", "-V"])],
            ),
        )


def test_running_host_command_is_terminated_when_guard_loses_lease(tmp_path: Path) -> None:
    import time

    _, orchestrator, manager, job = make_pipeline(tmp_path)
    started_marker = tmp_path / "command-started.txt"
    finished_marker = tmp_path / "command-finished.txt"
    long_command = CommandSpec(
        argv=[
            sys.executable,
            "-S",
            "-c",
            (
                "from pathlib import Path; import time; "
                f"Path({str(started_marker)!r}).write_text('started', encoding='utf-8'); "
                "time.sleep(30); "
                f"Path({str(finished_marker)!r}).write_text('finished', encoding='utf-8')"
            ),
        ],
        timeout_seconds=60,
    )
    guarded_job = job.model_copy(update={
        "worker": job.worker.model_copy(update={"implementation_commands": [long_command]})
    })

    def guard(stage: str) -> None:
        if stage.endswith(":running") and started_marker.exists():
            raise RuntimeError("lease revoked during command")

    began = time.monotonic()
    with pytest.raises(WorkerExecutionError, match="lease revoked during command"):
        WorkerPipelineRunner(
            orchestrator,
            manager,
            execution_guard=guard,
        ).run(guarded_job)
    assert time.monotonic() - began < 5
    time.sleep(0.2)
    assert started_marker.exists()
    assert not finished_marker.exists()


def test_reaper_removes_only_clean_unprotected_worktree_and_keeps_branch(tmp_path: Path) -> None:
    _, orchestrator, manager, job = make_pipeline(tmp_path)
    state = orchestrator.state()
    agent = state.agents[job.worker.agent_id]
    workspace = manager.create_workspace(agent)
    branch = workspace.branch

    records = manager.reap_managed_worktrees(
        protected_paths=(), min_age_seconds=0, dry_run=False
    )
    removed = [item for item in records if item.action == "removed"]
    assert [item.workspace_path for item in removed] == [str(workspace.path)]
    assert not workspace.path.exists()
    assert manager.branch_head(branch)

    restored = manager.create_workspace(agent)
    assert restored.path.exists()
    assert restored.branch == branch


def test_reaper_quarantines_dirty_worktree(tmp_path: Path) -> None:
    _, orchestrator, manager, job = make_pipeline(tmp_path)
    agent = orchestrator.state().agents[job.worker.agent_id]
    workspace = manager.create_workspace(agent)
    (workspace.path / "backend/src/auth/dirty.py").write_text("DIRTY = True\n", encoding="utf-8")

    records = manager.reap_managed_worktrees(
        protected_paths=(), min_age_seconds=0, dry_run=False
    )
    quarantined = [item for item in records if item.action == "quarantined"]
    assert quarantined
    assert "dirty_workspace" in quarantined[0].reason
    assert workspace.path.exists()
