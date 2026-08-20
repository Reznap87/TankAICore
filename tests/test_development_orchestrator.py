from __future__ import annotations

import json

import pytest
from pydantic import ValidationError as PydanticValidationError

from tankai.dev_orchestrator import (
    AgentSpec,
    CapabilityAction,
    CapabilitySpec,
    CapabilityStatus,
    ConflictError,
    DevelopmentOrchestrator,
    DevelopmentRole,
    ProjectState,
    ProjectStateStore,
    QAStatus,
    SpawnRequest,
    StateConflictError,
    TaskSpec,
    TaskState,
    TestExecution,
    TransitionError,
    ValidationError,
    path_matches_scope,
    scopes_overlap,
    task_graph_order,
)


BASE = "abc123"


def make_orchestrator(tmp_path, **limits) -> DevelopmentOrchestrator:
    return DevelopmentOrchestrator.initialize(
        str(tmp_path / "project-state.json"),
        current_version="0.8.0-test",
        current_branch="main",
        current_commit=BASE,
        **limits,
    )


def create_task(
    orchestrator: DevelopmentOrchestrator,
    task_id: str,
    paths: list[str],
    *,
    dependencies: list[str] | None = None,
    required_tests: list[str] | None = None,
    requires_security_review: bool = False,
) -> TaskSpec:
    return orchestrator.create_task(
        TaskSpec(
            task_id=task_id,
            goal=f"Implementiere {task_id}",
            base_commit=BASE,
            allowed_paths=paths,
            dependencies=dependencies or [],
            acceptance_criteria=["Funktion ist implementiert und geprüft"],
            required_tests=required_tests or [],
            requires_security_review=requires_security_review,
        )
    )


def create_support_agent(
    orchestrator: DevelopmentOrchestrator,
    task_id: str,
    role: DevelopmentRole,
) -> str:
    create_task(orchestrator, task_id, [])
    return orchestrator.start_agent(task_id, role).agent_id


def test_path_scope_matching_and_overlap() -> None:
    assert path_matches_scope("backend/src/auth/token.py", "backend/src/auth/**")
    assert not path_matches_scope("backend/src/users/user.py", "backend/src/auth/**")
    assert scopes_overlap("backend/src/auth/**", "backend/src/auth/token.py")
    assert scopes_overlap("backend/src/**", "backend/src/auth/**")
    assert not scopes_overlap("backend/src/auth/**", "backend/src/notifications/**")
    with pytest.raises(ValidationError):
        path_matches_scope("../secret", "backend/**")
    with pytest.raises(ValidationError):
        path_matches_scope("C:\\secret", "backend/**")
    with pytest.raises(ValidationError):
        path_matches_scope("backend/src/\x00secret", "backend/**")


def test_spawn_uses_same_role_commit_and_disjoint_paths(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    create_task(orchestrator, "AUTH-001", ["backend/src/auth/**"])
    parent = orchestrator.start_agent("AUTH-001", DevelopmentRole.BACKEND)

    child = orchestrator.approve_spawn(
        SpawnRequest(
            parent_agent_id=parent.agent_id,
            requested_role=DevelopmentRole.BACKEND,
            reason="Unabhängiges Notifications-Modul",
            task_id="NOTIFY-001",
            assigned_subtask="Implementiere Notifications",
            allowed_paths=["backend/src/notifications/**"],
            base_commit=BASE,
            acceptance_criteria=["Notifications sind getestet"],
        )
    )

    state = orchestrator.state()
    assert child.parent_agent_id == parent.agent_id
    assert child.generation == 1
    assert state.tasks["NOTIFY-001"].assigned_agent_id == child.agent_id
    assert {lock.scope for lock in state.file_locks} == {
        "backend/src/auth/**",
        "backend/src/notifications/**",
    }
    assert state.audit_log[-1].event_type == "spawn_approved"


def test_spawn_rejects_overlap_role_mismatch_and_stale_commit(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    create_task(orchestrator, "AUTH-001", ["backend/src/auth/**"])
    parent = orchestrator.start_agent("AUTH-001", DevelopmentRole.BACKEND)

    base_request = dict(
        parent_agent_id=parent.agent_id,
        requested_role=DevelopmentRole.BACKEND,
        reason="Teilaufgabe",
        assigned_subtask="Implementiere Teilaufgabe",
        base_commit=BASE,
        acceptance_criteria=["Getestet"],
    )
    with pytest.raises(ConflictError):
        orchestrator.approve_spawn(
            SpawnRequest(
                **base_request,
                task_id="AUTH-OVERLAP",
                allowed_paths=["backend/src/auth/tokens/**"],
            )
        )
    with pytest.raises(ValidationError):
        orchestrator.approve_spawn(
            SpawnRequest(
                **{**base_request, "requested_role": DevelopmentRole.FRONTEND},
                task_id="WRONG-ROLE",
                allowed_paths=["frontend/src/**"],
            )
        )
    with pytest.raises(ValidationError):
        orchestrator.approve_spawn(
            SpawnRequest(
                **{**base_request, "base_commit": "stale"},
                task_id="STALE",
                allowed_paths=["backend/src/stale/**"],
            )
        )
    with pytest.raises(ValidationError, match="Abnahmekriterium"):
        orchestrator.approve_spawn(
            SpawnRequest(
                **{**base_request, "acceptance_criteria": [" "]},
                task_id="NO-CRITERIA",
                allowed_paths=["backend/src/criteria/**"],
            )
        )


def test_spawn_limits_are_enforced(tmp_path) -> None:
    orchestrator = make_orchestrator(
        tmp_path, max_active_agents=4, max_clone_depth=1, max_children_per_agent=1
    )
    create_task(orchestrator, "ROOT", ["src/root/**"])
    parent = orchestrator.start_agent("ROOT", DevelopmentRole.BACKEND)
    child = orchestrator.approve_spawn(
        SpawnRequest(
            parent_agent_id=parent.agent_id,
            requested_role=DevelopmentRole.BACKEND,
            reason="Independent child",
            task_id="CHILD",
            assigned_subtask="Child task",
            allowed_paths=["src/child/**"],
            base_commit=BASE,
            acceptance_criteria=["Done"],
        )
    )
    with pytest.raises(ValidationError, match="MAX_CHILDREN_PER_AGENT"):
        orchestrator.approve_spawn(
            SpawnRequest(
                parent_agent_id=parent.agent_id,
                requested_role=DevelopmentRole.BACKEND,
                reason="Second child",
                task_id="CHILD-2",
                assigned_subtask="Second child task",
                allowed_paths=["src/child2/**"],
                base_commit=BASE,
                acceptance_criteria=["Done"],
            )
        )
    with pytest.raises(ValidationError, match="MAX_CLONE_DEPTH"):
        orchestrator.approve_spawn(
            SpawnRequest(
                parent_agent_id=child.agent_id,
                requested_role=DevelopmentRole.BACKEND,
                reason="Grandchild",
                task_id="GRANDCHILD",
                assigned_subtask="Grandchild task",
                allowed_paths=["src/grandchild/**"],
                base_commit=BASE,
                acceptance_criteria=["Done"],
            )
        )


def test_task_graph_rejects_unknown_and_cycles() -> None:
    first = TaskSpec(task_id="A", goal="A", base_commit=BASE)
    second = TaskSpec(task_id="B", goal="B", base_commit=BASE, dependencies=["A"])
    assert task_graph_order({"A": first, "B": second}) == ["A", "B"]
    with pytest.raises(ValidationError, match="unbekannte Abhängigkeit"):
        task_graph_order({"B": second})
    first.dependencies = ["B"]
    with pytest.raises(ValidationError, match="Zyklischer"):
        task_graph_order({"A": first, "B": second})


def test_completion_requires_scope_and_real_required_tests(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    create_task(
        orchestrator,
        "AUTH-001",
        ["backend/src/auth/**", "backend/tests/auth/**"],
        required_tests=["pytest tests/auth"],
    )
    agent = orchestrator.start_agent("AUTH-001", DevelopmentRole.BACKEND)

    with pytest.raises(ValidationError, match="außerhalb"):
        orchestrator.submit_task_completion(
            agent.agent_id,
            implementation_summary="Done",
            changed_files=["backend/src/users/user.py"],
            tests=[TestExecution(command="pytest tests/auth", passed=True, exit_code=0)],
        )
    with pytest.raises(TransitionError, match="Erforderliche Tests fehlen"):
        orchestrator.submit_task_completion(
            agent.agent_id,
            implementation_summary="Done",
            changed_files=["backend/src/auth/token.py"],
            tests=[TestExecution(command="pytest tests/other", passed=True, exit_code=0)],
        )

    task = orchestrator.submit_task_completion(
        agent.agent_id,
        implementation_summary="Token rotation implemented",
        changed_files=["backend/src/auth/token.py", "backend/tests/auth/test_token.py"],
        tests=[TestExecution(command="pytest tests/auth", passed=True, exit_code=0)],
    )
    assert task.state == TaskState.REVIEW_PENDING
    state = orchestrator.state()
    assert state.agents[agent.agent_id].status.value == "waiting_for_review"
    assert "AUTH-001" in state.pending_reviews


def test_independent_review_qa_security_and_integration_gate(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    create_task(
        orchestrator,
        "AUTH-001",
        ["backend/src/auth/**"],
        required_tests=["pytest tests/auth"],
        requires_security_review=True,
    )
    author = orchestrator.start_agent("AUTH-001", DevelopmentRole.BACKEND)
    reviewer_id = create_support_agent(orchestrator, "REVIEW-POOL", DevelopmentRole.REVIEWER)
    qa_id = create_support_agent(orchestrator, "QA-POOL", DevelopmentRole.QA)
    security_id = create_support_agent(orchestrator, "SECURITY-POOL", DevelopmentRole.SECURITY)

    orchestrator.submit_task_completion(
        author.agent_id,
        implementation_summary="Secure auth",
        changed_files=["backend/src/auth/service.py"],
        tests=[TestExecution(command="pytest tests/auth", passed=True, exit_code=0)],
    )
    with pytest.raises(ValidationError):
        orchestrator.assign_reviewer("AUTH-001", author.agent_id)

    orchestrator.assign_reviewer("AUTH-001", reviewer_id)
    task = orchestrator.record_review(
        "AUTH-001", reviewer_id, approved=True, notes="Architecture and code approved"
    )
    assert task.state == TaskState.QA_PENDING

    orchestrator.assign_qa("AUTH-001", qa_id)
    task = orchestrator.record_qa(
        "AUTH-001",
        qa_id,
        executions=[TestExecution(command="pytest -q", passed=True, exit_code=0)],
    )
    assert task.qa_status == QAStatus.PASSED
    assert task.state == TaskState.QA_PENDING
    with pytest.raises(TransitionError):
        orchestrator.integrate_task("AUTH-001", new_commit="def456")

    task = orchestrator.record_security_review(
        "AUTH-001", security_id, approved=True, notes="No cross-tenant access"
    )
    assert task.state == TaskState.READY_TO_INTEGRATE
    integrated = orchestrator.integrate_task("AUTH-001", new_commit="def456")
    assert integrated.state == TaskState.INTEGRATED
    state = orchestrator.state()
    assert state.current_commit == "def456"
    assert "AUTH-001" in state.completed_tasks
    assert all(lock.task_id != "AUTH-001" for lock in state.file_locks)


def test_parallel_task_requires_rebase_after_first_integration(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    create_task(orchestrator, "A", ["src/a/**"])
    create_task(orchestrator, "B", ["src/b/**"])
    author_a = orchestrator.start_agent("A", DevelopmentRole.BACKEND)
    author_b = orchestrator.start_agent("B", DevelopmentRole.BACKEND)
    reviewer_id = create_support_agent(orchestrator, "REVIEW-POOL", DevelopmentRole.REVIEWER)
    qa_id = create_support_agent(orchestrator, "QA-POOL", DevelopmentRole.QA)

    for task_id, author, changed in (("A", author_a, "src/a/a.py"), ("B", author_b, "src/b/b.py")):
        orchestrator.submit_task_completion(
            author.agent_id,
            implementation_summary=task_id,
            changed_files=[changed],
            tests=[],
        )
        orchestrator.assign_reviewer(task_id, reviewer_id)
        orchestrator.record_review(task_id, reviewer_id, approved=True, notes="ok")
        orchestrator.assign_qa(task_id, qa_id)
        orchestrator.record_qa(
            task_id,
            qa_id,
            executions=[TestExecution(command="pytest", passed=True, exit_code=0)],
        )

    orchestrator.integrate_task("A", new_commit="commit-a")
    with pytest.raises(TransitionError, match="Rebase erforderlich"):
        orchestrator.integrate_task("B", new_commit="commit-b")
    orchestrator.rebase_task("B", new_base_commit="commit-a")
    orchestrator.integrate_task("B", new_commit="commit-b")
    assert orchestrator.state().current_commit == "commit-b"


def test_atomic_store_revision_conflict(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    store = ProjectStateStore(tmp_path / "project-state.json")
    stale = store.load()
    create_task(orchestrator, "A", ["src/a/**"])
    with pytest.raises(StateConflictError):
        store.save(stale, expected_revision=stale.revision)
    parsed = json.loads((tmp_path / "project-state.json").read_text(encoding="utf-8"))
    assert parsed["revision"] >= 2


def test_dependent_task_is_rebased_to_current_stable_before_start(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    create_task(orchestrator, "A", ["src/a/**"])
    create_task(orchestrator, "B", ["src/b/**"], dependencies=["A"])
    author = orchestrator.start_agent("A", DevelopmentRole.BACKEND)
    reviewer = create_support_agent(orchestrator, "REVIEW-POOL", DevelopmentRole.REVIEWER)
    qa = create_support_agent(orchestrator, "QA-POOL", DevelopmentRole.QA)
    orchestrator.submit_task_completion(
        author.agent_id,
        implementation_summary="A",
        changed_files=["src/a/a.py"],
        tests=[],
    )
    orchestrator.assign_reviewer("A", reviewer)
    orchestrator.record_review("A", reviewer, approved=True, notes="ok")
    orchestrator.assign_qa("A", qa)
    orchestrator.record_qa(
        "A", qa, executions=[TestExecution(command="pytest", passed=True, exit_code=0)]
    )
    orchestrator.integrate_task("A", new_commit="commit-a")

    agent_b = orchestrator.start_agent("B", DevelopmentRole.BACKEND)
    state = orchestrator.state()
    assert state.tasks["B"].base_commit == "commit-a"
    assert agent_b.base_commit == "commit-a"
    assert any(event.event_type == "task_rebased_before_start" for event in state.audit_log)


def test_rejected_task_can_be_reopened_and_cancel_releases_lock(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    create_task(orchestrator, "A", ["src/a/**"])
    author = orchestrator.start_agent("A", DevelopmentRole.BACKEND)
    reviewer = create_support_agent(orchestrator, "REVIEW-POOL", DevelopmentRole.REVIEWER)
    orchestrator.submit_task_completion(
        author.agent_id,
        implementation_summary="A",
        changed_files=["src/a/a.py"],
        tests=[],
    )
    orchestrator.assign_reviewer("A", reviewer)
    blocked = orchestrator.record_review("A", reviewer, approved=False, notes="missing validation")
    assert blocked.state == TaskState.BLOCKED

    reopened = orchestrator.reopen_task("A", reason="Validierung ergänzen")
    assert reopened.state == TaskState.ACTIVE
    assert orchestrator.state().agents[author.agent_id].status.value == "active"

    cancelled = orchestrator.cancel_task("A", reason="Anforderung entfällt")
    assert cancelled.state == TaskState.CANCELLED
    state = orchestrator.state()
    assert all(lock.task_id != "A" for lock in state.file_locks)
    assert state.agents[author.agent_id].status.value == "terminated"


def _init_git_repo(path):
    import subprocess

    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    (path / "backend" / "src" / "auth").mkdir(parents=True)
    (path / "backend" / "src" / "auth" / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "--all"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=path, check=True, stdout=subprocess.PIPE)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, text=True, stdout=subprocess.PIPE
    ).stdout.strip()


def test_git_workspace_isolated_branch_scope_validation_and_commit(tmp_path) -> None:
    from tankai.dev_orchestrator import GitWorkspaceManager

    repository = tmp_path / "repo"
    commit = _init_git_repo(repository)
    orchestrator = DevelopmentOrchestrator.initialize(
        str(tmp_path / "state.json"),
        current_version="0.8.0-test",
        current_branch="main",
        current_commit=commit,
    )
    orchestrator.create_task(
        TaskSpec(
            task_id="AUTH-001",
            goal="Implement auth",
            base_commit=commit,
            allowed_paths=["backend/src/auth/**"],
            acceptance_criteria=["Auth works"],
        )
    )
    agent = orchestrator.start_agent("AUTH-001", DevelopmentRole.BACKEND)
    manager = GitWorkspaceManager(repository, tmp_path / "worktrees")
    workspace = manager.create_workspace(agent)
    bound = orchestrator.bind_workspace(
        agent.agent_id, branch=workspace.branch, workspace_path=str(workspace.path)
    )
    assert bound.branch == workspace.branch
    assert workspace.path.exists()

    target = workspace.path / "backend" / "src" / "auth" / "service.py"
    target.write_text("def authenticate():\n    return True\n", encoding="utf-8")
    changed = manager.validate_changes(agent, workspace)
    assert changed == ["backend/src/auth/service.py"]
    test_result = manager.run_command(
        workspace, ["python", "-c", "from backend.src.auth.service import authenticate; assert authenticate()"]
    )
    assert test_result.passed is True
    committed = manager.commit_changes(agent, workspace, message="Implement auth")
    assert len(committed) == 40
    assert committed != commit
    manager.remove_workspace(workspace, delete_branch=True)
    assert not workspace.path.exists()


def test_git_workspace_rejects_out_of_scope_changes(tmp_path) -> None:
    from tankai.dev_orchestrator import GitWorkspaceManager

    repository = tmp_path / "repo"
    commit = _init_git_repo(repository)
    orchestrator = DevelopmentOrchestrator.initialize(
        str(tmp_path / "state.json"),
        current_version="0.8.0-test",
        current_branch="main",
        current_commit=commit,
    )
    orchestrator.create_task(
        TaskSpec(
            task_id="AUTH-001",
            goal="Implement auth",
            base_commit=commit,
            allowed_paths=["backend/src/auth/**"],
            acceptance_criteria=["Auth works"],
        )
    )
    agent = orchestrator.start_agent("AUTH-001", DevelopmentRole.BACKEND)
    manager = GitWorkspaceManager(repository, tmp_path / "worktrees")
    workspace = manager.create_workspace(agent)
    (workspace.path / "README.md").write_text("unauthorized\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="außerhalb"):
        manager.validate_changes(agent, workspace)
    manager.remove_workspace(workspace, delete_branch=True)


def test_v2_governance_defaults_and_expanded_role_catalog(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    state = orchestrator.state()
    assert state.schema_version == 6
    assert state.governance.model_dump() == {
        "max_active_agents": 40,
        "max_total_agents_per_cycle": 80,
        "max_clone_depth": 5,
        "max_children_per_agent": 3,
        "max_agents_per_file": 1,
        "max_agents_per_module": 4,
    }
    assert DevelopmentRole.REALTIME_AUDIO.value == "realtime_audio"
    assert DevelopmentRole.AI_SAFETY.value == "ai_safety"
    assert DevelopmentRole.PROJECT_PERSISTENCE.value == "project_persistence"


def test_agent_contract_copies_task_governance_fields(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    orchestrator.create_task(TaskSpec(
        task_id="AUDIO-001",
        goal="Echtzeit-Audiopuffer absichern",
        base_commit=BASE,
        affected_components=["audio/engine"],
        allowed_paths=["audio/engine/**"],
        acceptance_criteria=["Keine Allokation im Audio-Thread"],
        required_tests=["pytest tests/audio"],
        priority=90,
        deadlock_rules=["Bei Audio-Lock-Konflikt sofort blockieren"],
    ))
    agent = orchestrator.start_agent("AUDIO-001", DevelopmentRole.REALTIME_AUDIO)
    assert agent.contract_version == 2
    assert agent.cycle_id == "cycle-000001"
    assert agent.acceptance_criteria == ["Keine Allokation im Audio-Thread"]
    assert agent.required_tests == ["pytest tests/audio"]
    assert agent.priority == 90
    assert agent.deadlock_rules == ["Bei Audio-Lock-Konflikt sofort blockieren"]


def test_module_capacity_blocks_excess_parallel_agents(tmp_path) -> None:
    orchestrator = make_orchestrator(
        tmp_path,
        max_active_agents=4,
        max_total_agents_per_cycle=8,
        max_agents_per_module=1,
    )
    for task_id, path in (("AUTH-A", "backend/auth/a/**"), ("AUTH-B", "backend/auth/b/**")):
        orchestrator.create_task(TaskSpec(
            task_id=task_id,
            goal=task_id,
            base_commit=BASE,
            affected_components=["backend/auth"],
            allowed_paths=[path],
            acceptance_criteria=["Getestet"],
        ))
    orchestrator.start_agent("AUTH-A", DevelopmentRole.AUTHENTICATION)
    with pytest.raises(ValidationError, match="MAX_AGENTS_PER_MODULE"):
        orchestrator.start_agent("AUTH-B", DevelopmentRole.AUTHENTICATION)


def test_total_agents_per_cycle_and_explicit_cycle_reset(tmp_path) -> None:
    orchestrator = make_orchestrator(
        tmp_path,
        max_active_agents=2,
        max_total_agents_per_cycle=2,
        max_agents_per_module=2,
    )
    for index in range(3):
        orchestrator.create_task(TaskSpec(
            task_id=f"TASK-{index}",
            goal=f"Task {index}",
            base_commit=BASE,
            allowed_paths=[f"module/{index}/**"],
            acceptance_criteria=["Getestet"],
        ))
    first = orchestrator.start_agent("TASK-0", DevelopmentRole.BACKEND_CORE)
    orchestrator.cancel_task("TASK-0", reason="Cycle capacity test")
    second = orchestrator.start_agent("TASK-1", DevelopmentRole.BACKEND_CORE)
    orchestrator.cancel_task("TASK-1", reason="Cycle capacity test")
    with pytest.raises(ValidationError, match="MAX_TOTAL_AGENTS_PER_CYCLE"):
        orchestrator.start_agent("TASK-2", DevelopmentRole.BACKEND_CORE)

    state = orchestrator.begin_cycle(reason="Vorheriger Zyklus abgeschlossen")
    assert state.cycle_id == "cycle-000002"
    assert state.cycle_agent_ids == []
    third = orchestrator.start_agent("TASK-2", DevelopmentRole.BACKEND_CORE)
    assert third.cycle_id == "cycle-000002"


def test_cycle_reset_is_blocked_with_non_terminal_agent(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    create_task(orchestrator, "ACTIVE", ["src/active/**"])
    orchestrator.start_agent("ACTIVE", DevelopmentRole.BACKEND)
    with pytest.raises(TransitionError, match="nicht-terminalen Agenten"):
        orchestrator.begin_cycle(reason="Zu früh")


def test_specialized_quality_and_security_roles_can_gate(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    create_task(
        orchestrator,
        "SECURE-001",
        ["backend/secure/**"],
        requires_security_review=True,
    )
    author = orchestrator.start_agent("SECURE-001", DevelopmentRole.BACKEND_CORE)
    reviewer = create_support_agent(orchestrator, "ARCH-REVIEW", DevelopmentRole.CHIEF_ARCHITECT)
    qa = create_support_agent(orchestrator, "QUALITY", DevelopmentRole.QUALITY_LEAD)
    security = create_support_agent(orchestrator, "APPSEC", DevelopmentRole.APPSEC)
    orchestrator.submit_task_completion(
        author.agent_id,
        implementation_summary="secure",
        changed_files=["backend/secure/service.py"],
        tests=[],
    )
    orchestrator.assign_reviewer("SECURE-001", reviewer)
    orchestrator.record_review("SECURE-001", reviewer, approved=True, notes="ok")
    orchestrator.assign_qa("SECURE-001", qa)
    orchestrator.record_qa(
        "SECURE-001",
        qa,
        executions=[TestExecution(command="pytest", passed=True, exit_code=0)],
    )
    task = orchestrator.record_security_review(
        "SECURE-001", security, approved=True, notes="ok"
    )
    assert task.state == TaskState.READY_TO_INTEGRATE


def test_schema_four_state_migrates_to_v2_governance(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    create_task(orchestrator, "LEGACY", ["legacy/**"])
    agent = orchestrator.start_agent("LEGACY", DevelopmentRole.BACKEND)
    path = tmp_path / "project-state.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 4
    for key in ("governance", "cycle_sequence", "cycle_id", "cycle_agent_ids"):
        payload.pop(key, None)
    legacy_agent = payload["agents"][agent.agent_id]
    for key in (
        "cycle_id", "contract_version", "acceptance_criteria", "required_tests",
        "reviewer_agent_id", "priority", "deadlock_rules",
    ):
        legacy_agent.pop(key, None)
    legacy_task = payload["tasks"]["LEGACY"]
    legacy_task.pop("priority", None)
    legacy_task.pop("deadlock_rules", None)
    path.write_text(json.dumps(payload), encoding="utf-8")

    migrated = ProjectStateStore(path).load()
    assert migrated.schema_version == 6
    assert migrated.governance.max_active_agents == 40
    assert migrated.cycle_agent_ids == [agent.agent_id]
    assert migrated.agents[agent.agent_id].contract_version == 1
    assert migrated.agents[agent.agent_id].acceptance_criteria == [
        "Funktion ist implementiert und geprüft"
    ]


def test_replicated_agent_contract_requires_parent() -> None:
    with pytest.raises(PydanticValidationError, match="Eltern-Agenten"):
        AgentSpec(
            agent_id="AGENT_BACKEND_CORE_02",
            role=DevelopmentRole.BACKEND_CORE,
            generation=1,
            base_commit=BASE,
            task_id="CHILD",
            allowed_paths=["backend/child/**"],
            acceptance_criteria=["Getestet"],
        )


def test_project_state_rejects_missing_current_cycle_registration() -> None:
    agent = AgentSpec(
        agent_id="AGENT_BACKEND_CORE_01",
        role=DevelopmentRole.BACKEND_CORE,
        generation=0,
        cycle_id="cycle-000001",
        base_commit=BASE,
        task_id="ROOT",
        allowed_paths=["backend/root/**"],
        acceptance_criteria=["Getestet"],
    )
    with pytest.raises(PydanticValidationError, match="Zyklusregister"):
        ProjectState(
            current_version="test",
            current_branch="main",
            current_commit=BASE,
            agents={agent.agent_id: agent},
            cycle_agent_ids=[],
        )


def test_schema_five_migrates_to_capability_registry(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    path = tmp_path / "project-state.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 5
    payload.pop("capabilities", None)
    path.write_text(json.dumps(payload), encoding="utf-8")

    migrated = ProjectStateStore(path).load()
    assert migrated.schema_version == 6
    assert migrated.capabilities == {}


def test_capability_registry_persists_required_contract(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    capability = orchestrator.register_capability(CapabilitySpec(
        capability_id="core.orchestrator.module_ownership",
        module_id="core/orchestrator",
        status=CapabilityStatus.NOT_STARTED,
        source_ref="TankAI-Core-1.9.0@6853ffc",
        dependencies=[],
        interface="ProjectState Schema 6 persistent capability registry",
        acceptance_tests=["pytest -q tests/test_development_orchestrator.py"],
    ))
    reloaded = orchestrator.state().capabilities[capability.capability_id]
    assert reloaded.model_dump(mode="json") == capability.model_dump(mode="json")


def test_capability_create_rules_and_active_task_exclusion(tmp_path) -> None:
    orchestrator = make_orchestrator(tmp_path)
    orchestrator.register_capability(CapabilitySpec(
        capability_id="runtime.memory.existing",
        module_id="runtime/memory",
        status=CapabilityStatus.IMPLEMENTED,
        source_ref="existing-commit",
        interface="memory",
        acceptance_tests=["memory regression"],
    ))
    with pytest.raises(ConflictError, match="CREATE ist nur"):
        orchestrator.create_task(TaskSpec(
            task_id="DUPLICATE-CREATE",
            goal="duplicate",
            capability_id="runtime.memory.existing",
            capability_action=CapabilityAction.CREATE,
            base_commit=BASE,
            allowed_paths=["runtime/memory/**"],
            acceptance_criteria=["never"],
        ))

    orchestrator.register_capability(CapabilitySpec(
        capability_id="core.orchestrator.new-capability",
        module_id="core/orchestrator",
        status=CapabilityStatus.NOT_STARTED,
        source_ref="",
        interface="registry",
        acceptance_tests=["registry test"],
    ))
    orchestrator.create_task(TaskSpec(
        task_id="CAP-001",
        goal="implement registry",
        capability_id="core.orchestrator.new-capability",
        capability_action=CapabilityAction.CREATE,
        base_commit=BASE,
        affected_components=["core/orchestrator"],
        allowed_paths=["tankai/dev_orchestrator/**"],
        acceptance_criteria=["registry works"],
    ))
    owner = orchestrator.start_agent("CAP-001", DevelopmentRole.BACKEND_CORE)
    orchestrator.set_capability_status(
        "core.orchestrator.new-capability",
        CapabilityStatus.IN_PROGRESS,
        owner_agent_id=owner.agent_id,
        source_ref=BASE,
    )
    with pytest.raises(ConflictError, match="bereits aktiv"):
        orchestrator.create_task(TaskSpec(
            task_id="CAP-002",
            goal="competing implementation",
            capability_id="core.orchestrator.new-capability",
            capability_action=CapabilityAction.EXTEND,
            base_commit=BASE,
            allowed_paths=["docs/**"],
            acceptance_criteria=["must be blocked"],
        ))
