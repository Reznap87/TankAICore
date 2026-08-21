"""Atomic, revisioned persistence for the development orchestrator state."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from .models import AgentGovernancePolicy, ProjectState, utcnow


class StateStoreError(RuntimeError):
    pass


class StateConflictError(StateStoreError):
    pass


T = TypeVar("T")


class ProjectStateStore:
    """Stores one authoritative project state as atomically replaced JSON.

    A small lock-file protocol protects separate local processes. Revision checks
    prevent stale writers from silently replacing a newer state.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        lock_timeout_seconds: float = 10.0,
        stale_lock_seconds: float = 120.0,
    ) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.lock_timeout_seconds = lock_timeout_seconds
        self.stale_lock_seconds = stale_lock_seconds
        self._thread_lock = threading.RLock()

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> ProjectState:
        if not self.path.exists():
            raise StateStoreError(f"Projektzustand fehlt: {self.path}")
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            schema_version = int(payload.get("schema_version", 1))
            if schema_version < 2:
                payload.setdefault("worker_runs", {})
                for task in payload.get("tasks", {}).values():
                    task.setdefault("worker_run_id", None)
                    task.setdefault("implementation_commit", None)
                schema_version = 2
            if schema_version < 3:
                payload.setdefault("worker_runs", {})
                for run in payload.get("worker_runs", {}).values():
                    run.setdefault("integration_executions", [])
                    run.setdefault("rebased_from_commit", None)
                    run.setdefault("rebased_commit", None)
                    run.setdefault("integration_commit", None)
                schema_version = 3
            if schema_version < 4:
                payload.setdefault("worker_runs", {})
                for run in payload.get("worker_runs", {}).values():
                    run.setdefault("execution_backend", "host")
                    run.setdefault("isolation", None)
                    run.setdefault("integration_isolation", None)
                schema_version = 4
            if schema_version < 5:
                payload.setdefault(
                    "governance",
                    AgentGovernancePolicy().model_dump(mode="json"),
                )
                payload.setdefault("cycle_sequence", 1)
                payload.setdefault("cycle_id", "cycle-000001")
                cycle_agents = payload.setdefault("cycle_agent_ids", [])
                for agent_id, agent in payload.get("agents", {}).items():
                    agent.setdefault("cycle_id", payload["cycle_id"])
                    agent.setdefault("contract_version", 1)
                    task = payload.get("tasks", {}).get(agent.get("task_id"), {})
                    agent.setdefault("acceptance_criteria", task.get("acceptance_criteria", []))
                    agent.setdefault("required_tests", task.get("required_tests", []))
                    agent.setdefault("reviewer_agent_id", task.get("reviewer_agent_id"))
                    agent.setdefault("priority", task.get("priority", 50))
                    agent.setdefault("deadlock_rules", task.get("deadlock_rules", []))
                    if agent_id not in cycle_agents:
                        cycle_agents.append(agent_id)
                for task in payload.get("tasks", {}).values():
                    task.setdefault("priority", 50)
                    task.setdefault("deadlock_rules", [])
                payload["schema_version"] = 5
                schema_version = 5
            if schema_version < 6:
                payload.setdefault("capabilities", {})
                for task in payload.get("tasks", {}).values():
                    task.setdefault("capability_id", None)
                    task.setdefault("capability_action", None)
                payload["schema_version"] = 6
            return ProjectState.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise StateStoreError(f"Projektzustand ist nicht lesbar: {self.path}: {exc}") from exc

    def create(self, state: ProjectState) -> ProjectState:
        with self._thread_lock, self._process_lock():
            if self.path.exists():
                raise StateConflictError(f"Projektzustand existiert bereits: {self.path}")
            state.revision = 1
            state.updated_at = utcnow()
            self._write_unlocked(state)
            return state.model_copy(deep=True)

    def save(self, state: ProjectState, *, expected_revision: int | None = None) -> ProjectState:
        with self._thread_lock, self._process_lock():
            current_revision = 0
            if self.path.exists():
                current_revision = self.load().revision
            if expected_revision is not None and current_revision != expected_revision:
                raise StateConflictError(
                    f"Veralteter Projektzustand: erwartet Revision {expected_revision}, "
                    f"aktuell {current_revision}"
                )
            state.revision = current_revision + 1
            state.updated_at = utcnow()
            self._write_unlocked(state)
            return state.model_copy(deep=True)

    def transaction(self, mutate: Callable[[ProjectState], T]) -> tuple[ProjectState, T]:
        with self._thread_lock, self._process_lock():
            state = self.load()
            result = mutate(state)
            state.revision += 1
            state.updated_at = utcnow()
            self._write_unlocked(state)
            return state.model_copy(deep=True), result

    def _write_unlocked(self, state: ProjectState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(
            state.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=self.path.parent,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            self._fsync_directory()
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def _fsync_directory(self) -> None:
        try:
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            os.close(directory_fd)

    @contextmanager
    def _process_lock(self) -> Iterator[None]:
        deadline = time.monotonic() + self.lock_timeout_seconds
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(f"pid={os.getpid()} created={time.time()}\n")
                break
            except FileExistsError:
                try:
                    age = time.time() - self.lock_path.stat().st_mtime
                    if age > self.stale_lock_seconds:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise StateStoreError(f"Timeout beim Sperren von {self.path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
