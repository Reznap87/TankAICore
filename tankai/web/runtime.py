"""Mandantengetrennte TankAI-Laufzeitverwaltung."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from tankai import TankAI, get_llm
from tankai.core.llm import AnthropicLLM, OpenAILLM, get_critic_llm
from tankai.core.request_dispatch import DispatchDenied
from tankai.core.long_term_memory import LongTermMemory


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} muss eine ganze Zahl sein") from exc
    if value < minimum or value > maximum:
        raise RuntimeError(f"{name} muss zwischen {minimum} und {maximum} liegen")
    return value


class WorkspaceHistory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def append(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())

    def list_recent(self, n: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
        output: list[dict[str, Any]] = []
        for line in lines[-max(1, min(n, 100)):]:
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    output.append(item)
            except json.JSONDecodeError:
                continue
        return list(reversed(output))


@dataclass
class WorkspaceRuntime:
    workspace_id: str
    tenant_id: str
    root: Path
    tank: TankAI
    history: WorkspaceHistory
    lock: threading.RLock
    _closed: bool = field(default=False, init=False, repr=False)
    _closing: bool = field(default=False, init=False, repr=False)
    _closed_resource_ids: set[int] = field(default_factory=set, init=False, repr=False)

    def close(self) -> None:
        with self.lock:
            if self._closed:
                return
            self._closing = True
            errors = []
            for resource in (self.tank.ltm, self.tank.memory):
                if resource is not None and id(resource) not in self._closed_resource_ids:
                    try:
                        resource.close()
                        self._closed_resource_ids.add(id(resource))
                    except Exception as error:
                        errors.append(error)
            self._closed = not errors
            if errors:
                raise RuntimeError("Workspace-Ressourcen konnten nicht vollständig geschlossen werden") from errors[0]


class WorkspaceRuntimeUnavailable(RuntimeError):
    """No runtime capacity, or the manager is shutting down; safe to retry later."""


class WorkspaceRuntimeManager:
    """Cache ohne Autorität: IDs stammen ausschließlich aus dem AuthStore."""

    def __init__(self, data_root: str | Path, *, max_runtimes: int | None = None,
                 idle_ttl_seconds: int | None = None, clock: Callable[[], float] | None = None,
                 require_request_admission: bool = False) -> None:
        if type(require_request_admission) is not bool:
            raise ValueError("require_request_admission must be a boolean")
        self.require_request_admission = require_request_admission
        self.max_runtimes = (_env_int("TANKAI_RUNTIME_CACHE_MAX", 32, minimum=1, maximum=1024)
                             if max_runtimes is None else max_runtimes)
        self.idle_ttl_seconds = (_env_int("TANKAI_RUNTIME_IDLE_TTL_SECONDS", 900, minimum=1, maximum=86400)
                                 if idle_ttl_seconds is None else idle_ttl_seconds)
        if type(self.max_runtimes) is not int or not 1 <= self.max_runtimes <= 1024:
            raise ValueError("max_runtimes muss zwischen 1 und 1024 liegen")
        if type(self.idle_ttl_seconds) is not int or not 1 <= self.idle_ttl_seconds <= 86400:
            raise ValueError("idle_ttl_seconds muss zwischen 1 und 86400 liegen")
        self._clock = clock or time.monotonic
        self.data_root = Path(data_root).resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_root, 0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._runtimes: OrderedDict[str, WorkspaceRuntime] = OrderedDict()
        self._last_used: dict[str, float] = {}
        self._leases: dict[str, int] = {}
        self._cleanup_failures: dict[str, str] = {}
        self._closed = False

    def _workspace_root(self, tenant_id: str, workspace_id: str) -> Path:
        # UUIDs kommen aus dem AuthStore; zusätzliche Prüfung verhindert Pfadmanipulation.
        for value in (tenant_id, workspace_id):
            if len(value) != 36 or any(ch not in "0123456789abcdef-" for ch in value.lower()):
                raise ValueError("Ungültige interne Workspace-ID")
        root = (self.data_root / "tenants" / tenant_id / "workspaces" / workspace_id).resolve()
        if self.data_root not in root.parents:
            raise ValueError("Workspace-Pfad verlässt Datenwurzel")
        return root

    def get(self, *, tenant_id: str, workspace_id: str) -> WorkspaceRuntime:
        """Sequential compatibility lookup. Concurrent callers must use acquire()."""
        key = f"{tenant_id}:{workspace_id}"
        with self._lock:
            if self._closed:
                raise WorkspaceRuntimeUnavailable("Workspace-Verwaltung wird beendet")
            now = self._clock()
            self._evict_expired(now)
            existing = self._runtimes.get(key)
            if existing is not None and getattr(existing, '_closing', False):
                try:
                    evicted = self._evict(key)
                except Exception as error:
                    self._cleanup_failures[key] = type(error).__name__
                    raise WorkspaceRuntimeUnavailable("Workspace-Ressourcen werden noch geschlossen") from error
                if not evicted:
                    raise WorkspaceRuntimeUnavailable("Workspace-Laufzeit wird geschlossen")
                existing = None
            if existing is not None:
                self._touch(key, now)
                return existing
            # Validate identities even when capacity is exhausted; no path is
            # created and no resource is opened before admission.
            self._workspace_root(tenant_id, workspace_id)
            if len(self._runtimes) >= self.max_runtimes:
                capacity_freed = False
                for victim in list(self._runtimes):
                    try:
                        if self._evict(victim):
                            capacity_freed = True
                            break
                    except Exception as error:
                        self._cleanup_failures[victim] = type(error).__name__
                if not capacity_freed:
                    raise WorkspaceRuntimeUnavailable("Alle Workspace-Laufzeiten sind belegt. Bitte später erneut versuchen.")
            runtime = self._create_runtime(tenant_id, workspace_id)
            self._runtimes[key] = runtime
            self._leases[key] = 0
            self._touch(key, self._clock())
            return runtime

    def _touch(self, key: str, now: float) -> None:
        self._last_used[key] = now
        self._runtimes.move_to_end(key)

    def _evict(self, key: str) -> bool:
        runtime = self._runtimes[key]
        is_owned = getattr(runtime.lock, '_is_owned', None)
        if (self._leases[key] or (is_owned is not None and is_owned())
                or not runtime.lock.acquire(blocking=False)):
            return False
        try:
            runtime.close()
            del self._runtimes[key], self._last_used[key], self._leases[key]
            self._cleanup_failures.pop(key, None)
            return True
        finally:
            runtime.lock.release()

    def _evict_expired(self, now: float) -> None:
        for key in list(self._runtimes):
            if now - self._last_used[key] >= self.idle_ttl_seconds:
                try:
                    self._evict(key)
                except Exception as error:
                    # Keep this entry and its capacity reservation. One broken
                    # workspace must not block healthy tenants during a sweep.
                    self._cleanup_failures[key] = type(error).__name__

    @contextmanager
    def acquire(self, *, tenant_id: str, workspace_id: str) -> Iterator[WorkspaceRuntime]:
        """Pin before waiting on the workspace lock, including queued requests."""
        key = f"{tenant_id}:{workspace_id}"
        with self._lock:
            runtime = self.get(tenant_id=tenant_id, workspace_id=workspace_id)
            self._leases[key] += 1
        try:
            with runtime.lock:
                yield runtime
        finally:
            # Release workspace lock before taking manager lock: eviction and
            # shutdown never wait on an active request while holding this lock.
            with self._lock:
                self._leases[key] -= 1
                self._touch(key, self._clock())
                if self._closed and not self._leases[key]:
                    self._evict(key)

    def _create_runtime(self, tenant_id: str, workspace_id: str) -> WorkspaceRuntime:
        root = self._workspace_root(tenant_id, workspace_id)
        root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        ltm = tank = None
        try:
            if self.require_request_admission and _env("TANKAI_EMBEDDER", "hashing") != "hashing":
                raise DispatchDenied("pilot requires deterministic local hashing embeddings")
            ltm = LongTermMemory(
                db_path=root / "ltm.db",
                vector_path=root / "vectors.npz",
                cold_dir=root / "cold",
                in_memory=False,
                embedder=_env("TANKAI_EMBEDDER", "hashing"),
            )
            llm = get_llm()
            critic_llm = get_critic_llm(default=llm)
            if self.require_request_admission:
                for adapter in (llm, critic_llm):
                    if type(adapter) not in (OpenAILLM, AnthropicLLM):
                        raise DispatchDenied("pilot requires reviewed guarded provider adapters")
                    adapter.requires_request_admission = True
            tank = TankAI(
                llm=llm,
                critic_llm=critic_llm,
                require_independent_critic=(self.require_request_admission
                                          or _env_bool("TANKAI_REQUIRE_INDEPENDENT_CRITIC", False)),
                require_research_evidence=_env_bool("TANKAI_REQUIRE_RESEARCH_EVIDENCE", True),
                max_llm_calls_per_run=5 if self.require_request_admission else _env_int(
                    "TANKAI_LLM_MAX_CALLS_PER_RUN", 40, minimum=7, maximum=40
                ),
                verbose=False,
                memory_db=str(root / "memory.db"),
                use_ltm=False,
                enable_tools=False,
                parallel=False,
                run_store_path=str(root / "runs.jsonl"),
                max_retries=0 if self.require_request_admission else 2,
                verified_single_step=self.require_request_admission,
            )
            tank.ltm = ltm
            tank.tools.register_defaults(
                ltm=ltm,
                enable_web_research=not self.require_request_admission,
                strict_web_research=_env_bool("TANKAI_STRICT_WEB_RESEARCH", False),
            )
            runtime = WorkspaceRuntime(
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                root=root,
                tank=tank,
                history=WorkspaceHistory(root / "web_history.jsonl"),
                lock=threading.RLock(),
            )
            return runtime
        except BaseException:
            # Partial initialization must not retain SQLite/vector resources.
            for resource in (tank.memory if tank is not None else None, ltm):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
            raise

    def close(self) -> None:
        with self._lock:
            self._closed = True
            errors = []
            for key in list(self._runtimes):
                try:
                    self._evict(key)
                except Exception as error:
                    errors.append(error)
            if errors:
                raise RuntimeError("Workspace-Cache konnte nicht vollständig geschlossen werden") from errors[0]
