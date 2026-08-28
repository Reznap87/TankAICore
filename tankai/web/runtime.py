"""Mandantengetrennte TankAI-Laufzeitverwaltung."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tankai import TankAI, get_llm
from tankai.core.llm import get_critic_llm
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

    def close(self) -> None:
        if self.tank.ltm:
            self.tank.ltm.close()
        self.tank.memory.close()


class WorkspaceRuntimeManager:
    """Cache ohne Autorität: IDs stammen ausschließlich aus dem AuthStore."""

    def __init__(self, data_root: str | Path) -> None:
        self.data_root = Path(data_root).resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.data_root, 0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._runtimes: dict[str, WorkspaceRuntime] = {}

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
        key = f"{tenant_id}:{workspace_id}"
        with self._lock:
            existing = self._runtimes.get(key)
            if existing is not None:
                return existing
            root = self._workspace_root(tenant_id, workspace_id)
            root.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(root, 0o700)
            except OSError:
                pass
            ltm = LongTermMemory(
                db_path=root / "ltm.db",
                vector_path=root / "vectors.npz",
                cold_dir=root / "cold",
                in_memory=False,
                embedder=_env("TANKAI_EMBEDDER", "hashing"),
            )
            llm = get_llm()
            critic_llm = get_critic_llm(default=llm)
            tank = TankAI(
                llm=llm,
                critic_llm=critic_llm,
                require_independent_critic=_env_bool("TANKAI_REQUIRE_INDEPENDENT_CRITIC", False),
                require_research_evidence=_env_bool("TANKAI_REQUIRE_RESEARCH_EVIDENCE", True),
                max_llm_calls_per_run=_env_int(
                    "TANKAI_LLM_MAX_CALLS_PER_RUN", 40, minimum=7, maximum=40
                ),
                verbose=False,
                memory_db=str(root / "memory.db"),
                use_ltm=False,
                enable_tools=False,
                parallel=False,
                run_store_path=str(root / "runs.jsonl"),
            )
            tank.ltm = ltm
            tank.tools.register_defaults(
                ltm=ltm,
                enable_web_research=True,
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
            self._runtimes[key] = runtime
            return runtime

    def close(self) -> None:
        with self._lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        for runtime in runtimes:
            runtime.close()
