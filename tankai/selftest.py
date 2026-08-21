#!/usr/bin/env python3
"""TankAI Self-Test für Konfiguration, Migration, Persistenz und Pipeline."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _ok(name: str) -> None:
    print(f"  ✓ {name}")


def _fail(name: str, err: Exception) -> None:
    print(f"  ✗ {name}: {err}")


def run_selftest() -> int:
    failed = 0
    print("TankAI Self-Test\n")

    try:
        from tankai import TankAI, get_llm
        from tankai.core.llm import BaseLLM
        from tankai.core.embeddings import get_embedder
        from tankai.core.long_term_memory import LongTermMemory
        from tankai.core.tools import ToolRegistry
        from tankai.core.vector_store import VectorStore
        from tankai.core.models import TaskStatus
        from tankai.core.web_research import (
            SearchResult, WebResearchTool, assert_public_url, WebResearchError
        )
        _ok("Imports")
    except Exception as exc:
        _fail("Imports", exc)
        return 1

    # Kein stiller Mock-Fallback.
    try:
        previous = os.environ.pop("TANKAI_LLM", None)
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                get_llm()
            except RuntimeError:
                pass
            else:
                raise AssertionError("get_llm() akzeptierte fehlende Provider-Konfiguration")
            finally:
                os.chdir(cwd)
        if previous is not None:
            os.environ["TANKAI_LLM"] = previous
        _ok("Explizite LLM-Konfiguration")
    except Exception as exc:
        _fail("Explizite LLM-Konfiguration", exc)
        failed += 1

    try:
        emb = get_embedder("hashing", dim=128)
        vector = emb.embed("Multi-Agenten Test")
        assert vector.shape == (128,)
        _ok("Embeddings")
    except Exception as exc:
        _fail("Embeddings", exc)
        failed += 1

    try:
        registry = ToolRegistry()
        registry.register_defaults()
        assert "22" in registry.run("calculator", expression="3*7+1")
        assert "sha256" in registry.run("hash", text="tankai")
        _ok(f"Tools ({len(registry.list_tools())} registriert)")
    except Exception as exc:
        _fail("Tools", exc)
        failed += 1

    try:
        class StaticBackend:
            provider_name = "static-test"

            def search(self, query: str, *, count: int = 5):
                return [
                    SearchResult(
                        title="Offizielle Testquelle",
                        url="https://example.org/fakten",
                        snippet="Verifizierbarer Suchauszug.",
                        content="Der Seitentext enthält eine überprüfbare Testaussage.",
                    ),
                    SearchResult(
                        title="Zweite Quelle",
                        url="https://example.com/zweite",
                        snippet="Zweiter Suchauszug.",
                    ),
                ][:count]

        web_tool = WebResearchTool(StaticBackend(), fetcher=None, url_validator=lambda url: url)
        evidence = web_tool.research("Testrecherche")
        assert len(evidence.sources) == 2
        assert evidence.source_ids[0].startswith("SRC-")
        rendered = evidence.render()
        assert evidence.source_ids[0] in rendered
        assert "https://example.org/fakten" in rendered
        _ok("Webrecherche-Evidence mit stabilen Quellen-IDs")
    except Exception as exc:
        _fail("Webrecherche-Evidence", exc)
        failed += 1

    try:
        try:
            assert_public_url("http://127.0.0.1/private")
        except WebResearchError:
            pass
        else:
            raise AssertionError("Loopback-Ziel wurde nicht blockiert")
        try:
            assert_public_url("http://169.254.169.254/latest/meta-data")
        except WebResearchError:
            pass
        else:
            raise AssertionError("Link-Local-Metadatenziel wurde nicht blockiert")
        _ok("SSRF-Schutz blockiert Loopback und Link-Local")
    except Exception as exc:
        _fail("SSRF-Schutz", exc)
        failed += 1

    try:
        class ResearchMainLLM(BaseLLM):
            provider_name = "test-main"
            model_name = "research-v1"

            def __init__(self, cite: bool = True):
                self.cite = cite

            def complete(self, prompt: str, *, system: str = "", **kwargs):
                low = (system + "\n" + prompt).lower()
                if "planner" in system.lower():
                    return json.dumps({
                        "rationale": "Aktuelle Fakten benötigen Webrecherche.",
                        "steps": [{
                            "description": "Aktuelle Fakten zum Testziel recherchieren",
                            "specialist_type": "research",
                            "expected_output": "Belegte Fakten mit Quellen-IDs",
                        }],
                    })
                if "synthesizer" in system.lower():
                    match = __import__("re").search(r"\[(SRC-[A-F0-9]{8})\]", prompt)
                    citation = f"[{match.group(1)}]" if (match and self.cite) else ""
                    return f"Finale belegte Aussage {citation}".strip()
                if "recherchierst" in system.lower():
                    match = __import__("re").search(r"\[(SRC-[A-F0-9]{8})\]", prompt)
                    citation = f"[{match.group(1)}]" if (match and self.cite) else ""
                    return f"Belegte Testaussage {citation}\n\nQuellenliste: {citation}".strip()
                return "Analyse abgeschlossen."

        class PassingCriticLLM(BaseLLM):
            provider_name = "test-critic"
            model_name = "critic-v2"

            def complete(self, prompt: str, *, system: str = "", **kwargs):
                return json.dumps({
                    "passed": True,
                    "score": 0.91,
                    "issues": [],
                    "suggestions": [],
                })

        tank_live = TankAI(
            llm=ResearchMainLLM(cite=True),
            critic_llm=PassingCriticLLM(),
            require_independent_critic=True,
            verbose=False,
            use_ltm=False,
            enable_tools=False,
            run_store_path=None,
        )
        tank_live.tools.register_defaults(enable_web_research=False)
        tank_live.tools.register(web_tool)
        live_result = tank_live.run(
            "Prüfe eine aktuelle Testaussage",
            definition_of_done="Aussage mit echter Quellen-ID und unabhängiger Prüfung",
        )
        assert live_result.status == TaskStatus.COMPLETED
        assert live_result.execution_mode == "live"
        assert live_result.critic_independent is True
        assert live_result.source_ids
        assert live_result.source_ids[0] in live_result.final_answer
        assert any(r.details.get("source_ids") for r in live_result.receipts)
        _ok("Live-Pipeline mit Webquellen und getrenntem Critic")
    except Exception as exc:
        _fail("Live-Webpipeline / separater Critic", exc)
        failed += 1

    try:
        tank_no_cite = TankAI(
            llm=ResearchMainLLM(cite=False),
            critic_llm=PassingCriticLLM(),
            require_independent_critic=True,
            verbose=False,
            use_ltm=False,
            enable_tools=False,
            max_retries=0,
            run_store_path=None,
        )
        tank_no_cite.tools.register_defaults(enable_web_research=False)
        tank_no_cite.tools.register(web_tool)
        no_cite_result = tank_no_cite.run("Test ohne Zitat")
        assert no_cite_result.status == TaskStatus.FAILED
        assert any(
            "Quellen-ID" in issue
            for critique in no_cite_result.critiques
            for issue in critique.issues
        )
        _ok("Deterministische Quellenprüfung blockiert unbelegte Synthese")
    except Exception as exc:
        _fail("Deterministische Quellenprüfung", exc)
        failed += 1

    try:
        same = ResearchMainLLM()
        try:
            TankAI(
                llm=same,
                critic_llm=same,
                require_independent_critic=True,
                verbose=False,
                enable_tools=False,
                run_store_path=None,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("Identischer Critic wurde trotz Pflicht akzeptiert")
        _ok("Pflicht für unabhängigen Critic wird erzwungen")
    except Exception as exc:
        _fail("Critic-Unabhängigkeit", exc)
        failed += 1

    try:
        from tankai.core.memory import Memory
        memory = Memory()
        memory.add("Ungeprüfter Altbestand", "legacy", validity="unknown", confidence=0.9)
        memory.add("Validierter Wissenseintrag", "test", validity="valid", confidence=0.8)
        assert memory.search("Altbestand") == []
        assert memory.search("Altbestand", include_unverified=True)
        assert memory.search("Wissenseintrag")
        _ok("Unverifiziertes Short-Term-Memory wird nicht automatisch abgerufen")
    except Exception as exc:
        _fail("Memory-Gültigkeitsfilter", exc)
        failed += 1

    # Reproduziert die alte DB ohne retention_policy und prüft die Migration.
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "old_ltm.db"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE runs (
                    id TEXT PRIMARY KEY, goal_description TEXT NOT NULL,
                    definition_of_done TEXT, status TEXT, final_answer TEXT,
                    duration_seconds REAL, created_at TEXT NOT NULL, metadata TEXT
                );
                CREATE TABLE receipts (
                    id TEXT PRIMARY KEY, run_id TEXT, action TEXT, actor TEXT,
                    input_summary TEXT, output_summary TEXT, success INTEGER,
                    details TEXT, timestamp TEXT
                );
                CREATE TABLE memory_entries (
                    id TEXT PRIMARY KEY, content TEXT NOT NULL, source TEXT NOT NULL,
                    memory_type TEXT NOT NULL, validity TEXT NOT NULL,
                    confidence REAL NOT NULL, related_goal_id TEXT, related_run_id TEXT,
                    conflicts_with TEXT, provenance TEXT, created_at TEXT NOT NULL,
                    last_accessed TEXT, access_count INTEGER DEFAULT 0, metadata TEXT
                );
                INSERT INTO memory_entries
                (id,content,source,memory_type,validity,confidence,created_at)
                VALUES ('legacy','Altbestand','test','semantic','valid',0.8,'2026-01-01T00:00:00+00:00');
            """)
            conn.commit()
            conn.close()

            ltm = LongTermMemory(
                db_path=db,
                vector_path=Path(tmp) / "vectors.npz",
                cold_dir=Path(tmp) / "cold",
                embedder="hashing",
            )
            columns = ltm._table_columns("memory_entries")
            assert "retention_policy" in columns
            row = ltm._conn.execute(
                "SELECT retention_policy FROM memory_entries WHERE id='legacy'"
            ).fetchone()
            assert row[0] == "hot"
            indexes = {
                item[1]
                for item in ltm._conn.execute("PRAGMA index_list(memory_entries)")
            }
            assert "idx_memory_retention" in indexes
            assert ltm._conn.execute("PRAGMA user_version").fetchone()[0] == ltm.SCHEMA_VERSION
            ltm.close()
        _ok("SQLite-Migration alter LTM-Datenbank")
    except Exception as exc:
        _fail("SQLite-Migration", exc)
        failed += 1

    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vectors.npz"
            store = VectorStore(dim=64, persist_path=str(path), embedder=get_embedder("hashing", dim=64))
            store.add("a", "Spezialisierte Agenten", {"kind": "semantic"})
            loaded = VectorStore(dim=64, persist_path=str(path), embedder=get_embedder("hashing", dim=64))
            assert loaded.ids == ["a"]
            assert loaded.metadatas == [{"kind": "semantic"}]
            assert loaded.search("Agenten", k=1)
        _ok("Sichere Vector-Persistenz ohne Pickle")
    except Exception as exc:
        _fail("Vector-Persistenz", exc)
        failed += 1


    try:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ltm_cold = LongTermMemory(
                db_path=base / "cold_test.db",
                vector_path=base / "cold_vectors.npz",
                cold_dir=base / "cold",
                embedder="hashing",
            )
            entry = ltm_cold.add_semantic("Volltext darf bei Archivfehler nicht verloren gehen.")
            bad_target = base / "not_a_directory"
            bad_target.write_text("block", encoding="utf-8")
            ltm_cold.cold_dir = bad_target
            assert ltm_cold._move_to_cold(entry.id) is False
            row = ltm_cold._conn.execute(
                "SELECT content, retention_policy FROM memory_entries WHERE id=?", (entry.id,)
            ).fetchone()
            assert row[0] == "Volltext darf bei Archivfehler nicht verloren gehen."
            assert (row[1] or "hot") == "hot"
            assert entry.id in ltm_cold.vectors.ids
            ltm_cold.close()
        _ok("Cold-Storage ohne Datenverlust bei Schreibfehler")
    except Exception as exc:
        _fail("Cold-Storage", exc)
        failed += 1

    result = None
    ltm = None
    try:
        ltm = LongTermMemory(in_memory=True, embedder="hashing")
        tank = TankAI(
            llm=get_llm("mock"),
            verbose=False,
            use_ltm=False,
            parallel=True,
            enable_tools=False,
            run_store_path=None,
        )
        tank.ltm = ltm
        tank.tools.register_defaults(ltm=ltm)
        result = tank.run(
            goal_description="Nenne drei Vorteile und drei Risiken von Multi-Agenten-Systemen.",
            definition_of_done="Jeweils mindestens drei Punkte, klar strukturiert.",
        )
        assert result.final_answer
        assert result.plan is not None
        assert result.status == TaskStatus.SIMULATED
        assert result.execution_mode == "simulation"
        assert len(result.receipts) >= 5
        _ok(f"Pipeline ({result.status.value}, {len(result.receipts)} Receipts)")
    except Exception as exc:
        _fail("Pipeline", exc)
        failed += 1

    try:
        from tankai.dev_orchestrator import (
            ConflictError,
            DevelopmentOrchestrator,
            DevelopmentRole,
            SpawnRequest,
            TaskSpec,
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "project-state.json"
            dev = DevelopmentOrchestrator.initialize(
                str(state_path),
                current_version="selftest",
                current_branch="main",
                current_commit="selftest-commit",
            )
            dev.create_task(TaskSpec(
                task_id="AUTH-SELFTEST",
                goal="Auth-Modul implementieren",
                base_commit="selftest-commit",
                allowed_paths=["backend/src/auth/**"],
                acceptance_criteria=["Auth ist getestet"],
            ))
            parent = dev.start_agent("AUTH-SELFTEST", DevelopmentRole.BACKEND)
            child = dev.approve_spawn(SpawnRequest(
                parent_agent_id=parent.agent_id,
                requested_role=DevelopmentRole.BACKEND,
                reason="Unabhängiges Notification-Modul",
                task_id="NOTIFY-SELFTEST",
                assigned_subtask="Notifications implementieren",
                allowed_paths=["backend/src/notifications/**"],
                base_commit="selftest-commit",
                acceptance_criteria=["Notifications sind getestet"],
            ))
            assert child.generation == 1
            assert len(dev.state().file_locks) == 2
            try:
                dev.approve_spawn(SpawnRequest(
                    parent_agent_id=parent.agent_id,
                    requested_role=DevelopmentRole.BACKEND,
                    reason="Kollidierende Teilaufgabe",
                    task_id="AUTH-CONFLICT",
                    assigned_subtask="Auth-Untermodul",
                    allowed_paths=["backend/src/auth/tokens/**"],
                    base_commit="selftest-commit",
                    acceptance_criteria=["Getestet"],
                ))
            except ConflictError:
                pass
            else:
                raise AssertionError("Kollidierender Spawn wurde akzeptiert")
        _ok("Kontrollierte Agenten-Replikation und Datei-Sperren")
    except Exception as exc:
        _fail("Development-Orchestrator", exc)
        failed += 1

    try:
        from tankai.dev_orchestrator import (
            CommandSpec,
            GateJob,
            GitWorkspaceManager,
            IntegrationJob,
            WorkerJob,
            WorkerPipelineJob,
            WorkerPipelineRunner,
            WorkerIntegrationRunner,
            WorkerRunState,
            render_command,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repository = root / "repo"
            repository.mkdir()
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repository,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            subprocess.run(["git", "config", "user.name", "TankAI Selftest"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "selftest@example.invalid"], cwd=repository, check=True)
            subprocess.run(["git", "config", "gc.auto", "0"], cwd=repository, check=True)
            subprocess.run(["git", "config", "maintenance.auto", "false"], cwd=repository, check=True)
            (repository / "src").mkdir()
            (repository / "src" / ".gitkeep").write_text("", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-m", "baseline"],
                cwd=repository,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            state_path = root / "worker-state.json"
            worker_dev = DevelopmentOrchestrator.initialize(
                str(state_path),
                current_version="selftest-worker",
                current_branch="main",
                current_commit=commit,
            )
            test_command = CommandSpec(
                argv=[
                    sys.executable,
                    "-S",
                    "-c",
                    "from src.worker_feature import value; assert value == 42",
                ]
            )
            worker_dev.create_task(TaskSpec(
                task_id="WORKER-SELFTEST",
                goal="Create a verified worker feature",
                base_commit=commit,
                allowed_paths=["src/**"],
                acceptance_criteria=["Feature returns 42"],
                required_tests=[render_command(test_command)],
            ))
            author = worker_dev.start_agent("WORKER-SELFTEST", DevelopmentRole.BACKEND)
            support = {}
            for task_id, role in (("REVIEW-SELFTEST", DevelopmentRole.REVIEWER), ("QA-SELFTEST", DevelopmentRole.QA)):
                worker_dev.create_task(TaskSpec(
                    task_id=task_id,
                    goal=f"{role.value} pool",
                    base_commit=commit,
                    acceptance_criteria=["Gate executed"],
                ))
                support[role] = worker_dev.start_agent(task_id, role).agent_id
            workspace_manager = GitWorkspaceManager(repository, root / "worktrees")
            result_worker = WorkerPipelineRunner(
                worker_dev,
                workspace_manager,
            ).run(WorkerPipelineJob(
                worker=WorkerJob(
                    agent_id=author.agent_id,
                    implementation_summary="Created worker feature.",
                    commit_message="Add worker selftest feature",
                    implementation_commands=[CommandSpec(
                        argv=[
                            sys.executable,
                            "-S",
                            "-c",
                            "from pathlib import Path; Path('src/worker_feature.py').write_text('value = 42\\n', encoding='utf-8')",
                        ]
                    )],
                    test_commands=[test_command],
                ),
                gates=GateJob(
                    reviewer_agent_id=support[DevelopmentRole.REVIEWER],
                    review_commands=[CommandSpec(
                        argv=[sys.executable, "-S", "-m", "compileall", "-q", "src"]
                    )],
                    qa_agent_id=support[DevelopmentRole.QA],
                    qa_commands=[test_command],
                ),
            ))
            assert result_worker.run.state == WorkerRunState.READY_TO_INTEGRATE
            assert worker_dev.state().tasks["WORKER-SELFTEST"].state.value == "ready_to_integrate"
            integrated = WorkerIntegrationRunner(
                worker_dev,
                workspace_manager,
            ).run(IntegrationJob(
                run_id=result_worker.run.run_id,
                test_commands=[test_command],
            ))
            assert integrated.run.state == WorkerRunState.INTEGRATED
            assert worker_dev.state().tasks["WORKER-SELFTEST"].state.value == "integrated"
            assert workspace_manager.repository_head() == integrated.integration_commit
        _ok("Worker-Runner mit realem Rebase/Merge und Post-Merge-Gate")
    except Exception as exc:
        _fail("Worker-Runner", exc)
        failed += 1

    try:
        from tankai.dev_orchestrator import (
            CommandSpec,
            DockerCommandExecutor,
            WorkerIsolationSpec,
            Workspace,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "container-workspace"
            (root / "src").mkdir(parents=True)
            (root / ".git").write_text("gitdir: /host/hidden\n", encoding="utf-8")
            plan = DockerCommandExecutor().build_plan(
                Workspace(
                    agent_id="SELFTEST-CONTAINER",
                    branch="tankai/selftest",
                    path=root,
                    base_commit="a" * 40,
                ),
                CommandSpec(argv=["python", "-V"]),
                WorkerIsolationSpec(
                    image="tankai-worker@sha256:" + "a" * 64,
                    user="10001:10001",
                ),
                allowed_paths=["src/**"],
                read_only_workspace=False,
                run_id="RUN-CONTAINER-SELFTEST",
                phase="implement",
            )
            assert "--network" in plan.argv and "none" in plan.argv
            assert "--read-only" in plan.argv
            assert "type=bind,src=/dev/null,dst=/workspace/.git,readonly" in plan.argv
            assert plan.writable_roots == ("src",)
        _ok("Worker-Container-Richtlinie und Mount-Plan")
    except Exception as exc:
        _fail("Worker-Container-Richtlinie", exc)
        failed += 1

    try:
        from tankai.dev_orchestrator import (
            DevelopmentJobQueue,
            JobState,
            ManagedContainerRecord,
            WorkspaceQueuePolicy,
        )
        from tankai.web.auth import AuthStore
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = AuthStore(root / "auth.db")
            owner, tenant_id, workspace_id = auth.create_user_with_tenant(
                email="queue-selftest@example.com",
                password="Queue-selftest-password-123",
                display_name="Queue Selftest",
                tenant_name="Queue Tenant",
            )
            repository_base = root / "repositories"
            repository = repository_base / "main"
            repository.mkdir(parents=True)
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=repository, check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            subprocess.run(["git", "config", "user.name", "Queue Selftest"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "queue-selftest@example.invalid"], cwd=repository, check=True)
            (repository / "README.md").write_text("queue selftest\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-m", "baseline"], cwd=repository, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
            queue = DevelopmentJobQueue(
                root / "development-jobs.db",
                auth_store=auth,
                repository_base=repository_base,
                workspace_base=root / "worktrees",
                state_base=root / "states",
            )
            image = "tankai-worker@sha256:" + "d" * 64
            queue.set_policy(
                actor_user_id=owner, workspace_id=workspace_id,
                policy=WorkspaceQueuePolicy(
                    tenant_id=tenant_id, workspace_id=workspace_id,
                    max_runtime_seconds=120, allowed_images=[image],
                ),
            )
            binding = queue.register_repository(
                actor_user_id=owner, workspace_id=workspace_id, name="Main",
                repository_path=repository, workspace_root=root / "worktrees" / "main",
                state_path=root / "states" / "main.json",
            )
            command = CommandSpec(argv=[sys.executable, "-S", "-c", "pass"], timeout_seconds=10)
            queued = queue.enqueue(
                actor_user_id=owner, workspace_id=workspace_id,
                repository_id=binding.repository_id, idempotency_key="selftest-job",
                pipeline=WorkerPipelineJob(
                    worker=WorkerJob(
                        agent_id="AGENT_BACKEND_SELFTEST",
                        implementation_summary="Queue selftest",
                        commit_message="Queue selftest",
                        implementation_commands=[command], test_commands=[command],
                    ),
                    gates=GateJob(
                        reviewer_agent_id="AGENT_REVIEW_SELFTEST", review_commands=[command],
                        qa_agent_id="AGENT_QA_SELFTEST", qa_commands=[command],
                    ),
                    isolation=WorkerIsolationSpec(
                        image=image, memory_mb=256, cpus=1, pids_limit=64,
                        user="10001:10001",
                    ),
                ),
            )
            lease = queue.claim_next(worker_id="selftest-runner", lease_seconds=60)
            assert lease is not None and lease.job.job_id == queued.job_id
            queue.start_job(job_id=queued.job_id, lease_token=lease.lease_token)
            queue.complete_job(
                job_id=queued.job_id, lease_token=lease.lease_token,
                result={"status": "verified"},
            )
            stored = queue.get_job(
                actor_user_id=owner, workspace_id=workspace_id, job_id=queued.job_id
            )
            assert stored.state == JobState.SUCCEEDED
            assert stored.result == {"status": "verified"}

            class _ReaperProbe:
                def __init__(self) -> None:
                    self.removed: list[str] = []

                def ensure_available(self) -> str:
                    return "selftest-rootless-runtime"

                def list_managed_containers(self, *, repository_id: str):
                    return [ManagedContainerRecord(
                        container_id="e" * 64,
                        name="tankai-selftest",
                        state="exited",
                        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
                        labels={
                            "tankai.managed": "true",
                            "tankai.run_id": "RUN-QUEUE-SELFTEST",
                            "tankai.phase": "qa",
                            "tankai.job_id": queued.job_id,
                            "tankai.repository_id": repository_id,
                            "tankai.workspace_id": workspace_id,
                            "tankai.tenant_id": tenant_id,
                            "tankai.fence_epoch": str(lease.fence_epoch),
                            "tankai.worker_id": "selftest-runner",
                        },
                    )]

                def remove_container(self, container_id: str) -> None:
                    self.removed.append(container_id)

            reaper = _ReaperProbe()
            cleanup = queue.reap_containers(
                actor_user_id=owner,
                workspace_id=workspace_id,
                repository_id=binding.repository_id,
                min_age_seconds=0,
                dry_run=False,
                container_executor=reaper,
            )
            assert cleanup[0]["action"] == "removed"
            assert reaper.removed == ["e" * 64]
        _ok("Mandantengebundene Development-Queue, Lease und Container-Reaper")
    except Exception as exc:
        _fail("Development-Queue", exc)
        failed += 1

    try:
        from tankai.dev_orchestrator.fencing import FenceBusy, FenceLost, LeaseFenceStore

        with tempfile.TemporaryDirectory() as tmp:
            store = LeaseFenceStore(Path(tmp) / "external-fences.db")
            first = store.acquire(
                scope_key="repository-selftest",
                job_id="job-1",
                owner_id="worker-1",
                lease_token="token-1",
                lease_seconds=60,
            )
            try:
                store.acquire(
                    scope_key="repository-selftest",
                    job_id="job-2",
                    owner_id="worker-2",
                    lease_token="token-2",
                    lease_seconds=60,
                )
                raise AssertionError("Aktiver Fence wurde unerlaubt ersetzt")
            except FenceBusy:
                pass
            store.force_expire_for_recovery(
                "repository-selftest", expected_epoch=first.epoch
            )
            second = store.acquire(
                scope_key="repository-selftest",
                job_id="job-2",
                owner_id="worker-2",
                lease_token="token-2",
                lease_seconds=60,
            )
            assert second.epoch == first.epoch + 1
            try:
                store.assert_active(
                    scope_key="repository-selftest",
                    job_id="job-1",
                    epoch=first.epoch,
                    lease_token="token-1",
                )
                raise AssertionError("Veraltete Fence-Epoche blieb gültig")
            except FenceLost:
                pass
        _ok("Externe monotone Lease-Fences blockieren veraltete Worker")
    except Exception as exc:
        _fail("Lease-Fencing", exc)
        failed += 1

    try:
        from tankai.web.server import HTML
        assert ".innerHTML" not in HTML
        assert "catch{}" not in HTML
        assert "textContent" in HTML
        assert "__CSP_NONCE__" not in HTML
        _ok("Web-Rendering ohne dynamisches innerHTML")
    except Exception as exc:
        _fail("Web-Sicherheit", exc)
        failed += 1

    try:
        if result:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "selftest.json"
                path.write_text(
                    json.dumps(
                        {
                            "status": result.status.value,
                            "execution_mode": result.execution_mode,
                            "receipts": len(result.receipts),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                assert json.loads(path.read_text(encoding="utf-8"))["status"] == "simulated"
            _ok("JSON-Export")
    except Exception as exc:
        _fail("JSON-Export", exc)
        failed += 1

    try:
        from tankai.dev_orchestrator.release_backup import (
            create_release_backup,
            verify_checksum_file,
            verify_release_backup,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            output = Path(tmp) / "release"
            (root / "tankai").mkdir(parents=True)
            (root / "tankai" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / "README.md").write_text("# Backup-Test\n", encoding="utf-8")
            artifacts = create_release_backup(
                root,
                output,
                version="selftest",
                commit="e" * 40,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
            verification = verify_release_backup(artifacts.archive_path)
            assert verification.valid
            assert verification.file_count == 2
            assert len(verify_checksum_file(artifacts.checksums_path)) == 3
        _ok("Deterministisches Release-Backup und SHA-256-Verifikation")
    except Exception as exc:
        _fail("Release-Backup", exc)
        failed += 1

    try:
        from tankai.dev_orchestrator.release_publication import (
            PublicationTarget,
            create_publication_ledger,
            record_artifact_receipt,
            verify_publication_ledger,
        )
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp) / "release"
            release.mkdir()
            artifact = release / "tankai.zip"
            artifact.write_bytes(b"tankai-release-selftest")
            ledger = Path(tmp) / "publication.json"
            create_publication_ledger(
                release,
                ledger,
                version="selftest",
                commit="f" * 40,
                branch="main",
                targets=[PublicationTarget(
                    "drive-selftest",
                    "google_drive",
                    "1AbCdEfGhIjKlMnOp",
                )],
                created_at_utc="2026-01-01T00:00:00Z",
            )
            record_artifact_receipt(
                ledger,
                release,
                target_id="drive-selftest",
                artifact_path="tankai.zip",
                remote_id="1SelfTestRemoteId",
                remote_url="https://drive.google.com/file/d/1SelfTestRemoteId/view",
                remote_size=artifact.stat().st_size,
                remote_digest_algorithm="sha256",
                remote_digest=hashlib.sha256(artifact.read_bytes()).hexdigest(),
                recorded_at_utc="2026-01-01T00:01:00Z",
            )
            publication = verify_publication_ledger(
                ledger,
                release_directory=release,
            )
            assert publication.valid and publication.complete
        _ok("Manipulationssicheres Release-Publikationsledger")
    except Exception as exc:
        _fail("Release-Publikationsledger", exc)
        failed += 1

    try:
        from tankai.dev_orchestrator import (
            DevelopmentOrchestrator,
            DevelopmentRole,
            TaskSpec,
        )
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = DevelopmentOrchestrator.initialize(
                str(Path(tmp) / "project-state.json"),
                current_version="selftest",
                current_branch="main",
                current_commit="a" * 40,
            )
            state = orchestrator.state()
            assert state.schema_version == 6
            assert state.governance.max_active_agents == 40
            assert state.governance.max_total_agents_per_cycle == 80
            assert state.governance.max_clone_depth == 5
            assert state.governance.max_children_per_agent == 3
            orchestrator.create_task(TaskSpec(
                task_id="SELFTEST-AUDIO",
                goal="Prüfe den vollständigen V2-Agentenvertrag",
                base_commit="a" * 40,
                affected_components=["audio/engine"],
                allowed_paths=["audio/engine/**"],
                acceptance_criteria=["Agentenvertrag ist vollständig"],
                required_tests=["python -m tankai --selftest"],
                priority=100,
            ))
            agent = orchestrator.start_agent(
                "SELFTEST-AUDIO", DevelopmentRole.REALTIME_AUDIO
            )
            assert agent.contract_version == 2
            assert agent.cycle_id == state.cycle_id
            assert agent.acceptance_criteria == ["Agentenvertrag ist vollständig"]
            orchestrator.cancel_task("SELFTEST-AUDIO", reason="Self-Test abgeschlossen")
            next_state = orchestrator.begin_cycle(reason="Self-Test-Folgezyklus")
            assert next_state.cycle_id == "cycle-000002"
        _ok("TECH AI V2 Agenten-Governance und Zyklusgrenzen")
    except Exception as exc:
        _fail("Agenten-Governance", exc)
        failed += 1

    if ltm:
        ltm.close()

    print()
    if failed:
        print(f"FEHLGESCHLAGEN: {failed} Check(s)")
        return 1
    print("ALLE CHECKS BESTANDEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_selftest())
