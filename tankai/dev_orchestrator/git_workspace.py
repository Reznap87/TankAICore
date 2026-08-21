"""Isolated Git worktrees for development agents.

This module creates real branches/worktrees and verifies that an agent changed
only files inside its assigned write scopes. Commands are executed without a
shell and with explicit timeouts.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

from .models import AgentSpec, TestExecution
from .orchestrator import ValidationError, normalize_repo_path, path_matches_scope
from .process_control import run_bounded_process


class GitWorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Workspace:
    agent_id: str
    branch: str
    path: Path
    base_commit: str


@dataclass(frozen=True)
class WorktreeReapRecord:
    workspace_path: str
    branch: str | None
    action: str
    reason: str


class GitWorkspaceManager:
    def __init__(
        self,
        repository: str | Path,
        workspace_root: str | Path,
        *,
        command_timeout_seconds: float = 120.0,
    ) -> None:
        self.repository = Path(repository).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.command_timeout_seconds = command_timeout_seconds
        if self.workspace_root == self.repository or self.repository in self.workspace_root.parents:
            raise GitWorkspaceError("Workspace-Root muss außerhalb des Quell-Repositorys liegen")
        if not (self.repository / ".git").exists():
            raise GitWorkspaceError(f"Kein Git-Repository: {self.repository}")
        self._git(["rev-parse", "--is-inside-work-tree"], cwd=self.repository)

    @property
    def common_git_dir(self) -> Path:
        raw = self._git(["rev-parse", "--git-common-dir"], cwd=self.repository).strip()
        path = Path(raw)
        if not path.is_absolute():
            path = (self.repository / path).resolve()
        return path

    @contextmanager
    def integration_lock(
        self,
        *,
        timeout_seconds: float = 30.0,
        stale_seconds: float = 14_400.0,
    ) -> Iterator[None]:
        """Serialize all mutations of the authoritative branch across processes."""
        with self._git_metadata_lock(
            "tankai-integration.lock",
            timeout_seconds=timeout_seconds,
            stale_seconds=stale_seconds,
            label="Git-Integration",
        ):
            yield

    @contextmanager
    def workspace_lock(
        self,
        *,
        timeout_seconds: float = 30.0,
        stale_seconds: float = 14_400.0,
    ) -> Iterator[None]:
        """Serialize Git worktree/branch metadata changes between worker processes."""
        with self._git_metadata_lock(
            "tankai-workspace.lock",
            timeout_seconds=timeout_seconds,
            stale_seconds=stale_seconds,
            label="Git-Worktree-Verwaltung",
        ):
            yield

    @contextmanager
    def _git_metadata_lock(
        self,
        filename: str,
        *,
        timeout_seconds: float,
        stale_seconds: float,
        label: str,
    ) -> Iterator[None]:
        lock_path = self.common_git_dir / filename
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(f"pid={os.getpid()} created={time.time()}\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                break
            except FileExistsError:
                try:
                    owner_alive = False
                    content = lock_path.read_text(encoding="utf-8", errors="replace")
                    match = re.search(r"\bpid=(\d+)\b", content)
                    if match:
                        try:
                            os.kill(int(match.group(1)), 0)
                            owner_alive = True
                        except ProcessLookupError:
                            owner_alive = False
                        except PermissionError:
                            owner_alive = True
                    if not owner_alive and time.time() - lock_path.stat().st_mtime > 1.0:
                        lock_path.unlink(missing_ok=True)
                        continue
                    if not match and time.time() - lock_path.stat().st_mtime > stale_seconds:
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise GitWorkspaceError(f"Timeout beim Sperren der {label}")
                time.sleep(0.05)
        try:
            yield
        finally:
            lock_path.unlink(missing_ok=True)

    def get_or_create_workspace(self, agent: AgentSpec) -> Workspace:
        if agent.workspace_path or agent.branch:
            if not agent.workspace_path or not agent.branch:
                raise GitWorkspaceError("Unvollständige bestehende Workspace-Bindung")
            path = Path(agent.workspace_path).resolve()
            if not path.exists():
                raise GitWorkspaceError(f"Gebundener Workspace fehlt: {path}")
            if self.workspace_root != path and self.workspace_root not in path.parents:
                raise GitWorkspaceError("Gebundener Workspace liegt außerhalb des Workspace-Roots")
            branch = self._git(["branch", "--show-current"], cwd=path).strip()
            if branch != agent.branch:
                raise GitWorkspaceError(
                    f"Gebundener Workspace verwendet Branch {branch!r} statt {agent.branch!r}"
                )
            completed = subprocess.run(
                ["git", "merge-base", "--is-ancestor", agent.base_commit, "HEAD"],
                cwd=path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.command_timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                raise GitWorkspaceError(
                    "Workspace-Branch basiert nicht mehr auf dem bestätigten Basis-Commit"
                )
            self.assert_clean(Workspace(
                agent_id=agent.agent_id,
                branch=agent.branch,
                path=path,
                base_commit=agent.base_commit,
            ))
            return Workspace(
                agent_id=agent.agent_id,
                branch=agent.branch,
                path=path,
                base_commit=agent.base_commit,
            )
        return self.create_workspace(agent)

    def create_workspace(self, agent: AgentSpec) -> Workspace:
        if agent.status.value != "active":
            raise GitWorkspaceError(f"Agent ist nicht aktiv: {agent.agent_id}")
        self._git(["cat-file", "-e", f"{agent.base_commit}^{{commit}}"], cwd=self.repository)
        branch = self._branch_name(agent)
        self._git(["check-ref-format", "--branch", branch], cwd=self.repository)
        directory_name = re.sub(r"[^A-Za-z0-9._-]+", "-", agent.agent_id).strip(".-")
        if not directory_name:
            raise GitWorkspaceError("Agent-ID ergibt keinen sicheren Workspace-Namen")
        path = self.workspace_root / directory_name
        if path.exists():
            raise GitWorkspaceError(f"Workspace existiert bereits: {path}")
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        with self.workspace_lock():
            if path.exists():
                raise GitWorkspaceError(f"Workspace existiert bereits: {path}")
            if self._branch_exists(branch):
                completed = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", agent.base_commit, branch],
                    cwd=self.repository,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=self.command_timeout_seconds,
                    check=False,
                )
                if completed.returncode != 0:
                    raise GitWorkspaceError(
                        "Erhaltener Agent-Branch basiert nicht auf dem bestätigten Basis-Commit"
                    )
                self._git(["worktree", "add", str(path), branch], cwd=self.repository)
            else:
                self._git(
                    ["worktree", "add", "--detach", str(path), agent.base_commit],
                    cwd=self.repository,
                )
                try:
                    self._git(["switch", "-c", branch], cwd=path)
                except Exception:
                    self._git(["worktree", "remove", "--force", str(path)], cwd=self.repository)
                    raise
        return Workspace(
            agent_id=agent.agent_id,
            branch=branch,
            path=path,
            base_commit=agent.base_commit,
        )

    def head_commit(self, workspace: Workspace) -> str:
        return self._git(["rev-parse", "HEAD"], cwd=workspace.path).strip()

    def repository_head(self) -> str:
        return self._git(["rev-parse", "HEAD"], cwd=self.repository).strip()

    def repository_branch(self) -> str:
        return self._git(["branch", "--show-current"], cwd=self.repository).strip()

    def branch_head(self, branch: str) -> str:
        self._git(["check-ref-format", "--branch", branch], cwd=self.repository)
        return self._git(["rev-parse", f"refs/heads/{branch}"], cwd=self.repository).strip()

    def workspace_from_binding(
        self,
        *,
        agent_id: str,
        branch: str,
        workspace_path: str | Path,
        base_commit: str,
    ) -> Workspace:
        """Open a journaled worktree without trusting the current ProjectState HEAD."""
        path = Path(workspace_path).resolve()
        if not path.exists():
            raise GitWorkspaceError(f"Journal-Workspace fehlt: {path}")
        if self.workspace_root != path and self.workspace_root not in path.parents:
            raise GitWorkspaceError("Journal-Workspace liegt außerhalb des Workspace-Roots")
        current_branch = self._git(["branch", "--show-current"], cwd=path).strip()
        if current_branch != branch:
            raise GitWorkspaceError(
                f"Journal-Workspace verwendet Branch {current_branch!r} statt {branch!r}"
            )
        self._git(["cat-file", "-e", f"{base_commit}^{{commit}}"], cwd=path)
        return Workspace(
            agent_id=agent_id,
            branch=branch,
            path=path,
            base_commit=base_commit,
        )

    def assert_repository_ready(self, *, branch: str, expected_commit: str) -> None:
        current_branch = self.repository_branch()
        if current_branch != branch:
            raise GitWorkspaceError(
                f"Haupt-Repository steht auf Branch {current_branch!r} statt {branch!r}"
            )
        current_commit = self.repository_head()
        if current_commit != expected_commit:
            raise GitWorkspaceError(
                "Repository und ProjectState sind nicht synchron: "
                f"Repository={current_commit}, State={expected_commit}"
            )
        changed = self.repository_changed_files()
        if changed:
            raise GitWorkspaceError(
                "Haupt-Repository ist nicht sauber: " + ", ".join(changed)
            )

    def repository_changed_files(self) -> list[str]:
        raw = self._git_bytes(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=self.repository,
        )
        return self._parse_status_paths(raw)

    def assert_head(self, workspace: Workspace, expected_commit: str) -> None:
        current = self.head_commit(workspace)
        if current != expected_commit:
            raise GitWorkspaceError(
                f"Workspace-HEAD wurde unerlaubt verändert: erwartet {expected_commit}, aktuell {current}"
            )

    def assert_clean(self, workspace: Workspace) -> None:
        changed = self.changed_files(workspace)
        if changed:
            raise GitWorkspaceError(
                "Prüfbefehl veränderte den Workspace: " + ", ".join(changed)
            )

    def cleanup_check_artifacts(self, workspace: Workspace, expected_commit: str) -> None:
        """Remove only untracked check artifacts; reject tracked or HEAD mutations."""
        self.assert_head(workspace, expected_commit)
        for args in (["diff", "--quiet"], ["diff", "--cached", "--quiet"]):
            completed = subprocess.run(
                ["git", *args],
                cwd=workspace.path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.command_timeout_seconds,
                check=False,
            )
            if completed.returncode == 1:
                raise GitWorkspaceError("Prüfbefehl veränderte versionierte Dateien")
            if completed.returncode != 0:
                raise GitWorkspaceError(
                    f"git {' '.join(args)} fehlgeschlagen ({completed.returncode}): "
                    + completed.stdout.decode("utf-8", errors="replace")[-4000:]
                )
        self._git(["clean", "-fd"], cwd=workspace.path)
        self.assert_clean(workspace)

    def cleanup_repository_check_artifacts(self, expected_commit: str) -> None:
        """Restore the pre-test clean condition without deleting ignored files."""
        if self.repository_head() != expected_commit:
            raise GitWorkspaceError("Post-Merge-Prüfung veränderte Repository-HEAD")
        for args in (["diff", "--quiet"], ["diff", "--cached", "--quiet"]):
            completed = subprocess.run(
                ["git", *args],
                cwd=self.repository,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.command_timeout_seconds,
                check=False,
            )
            if completed.returncode == 1:
                raise GitWorkspaceError("Post-Merge-Prüfung veränderte versionierte Dateien")
            if completed.returncode != 0:
                raise GitWorkspaceError(
                    f"git {' '.join(args)} fehlgeschlagen ({completed.returncode}): "
                    + completed.stdout.decode("utf-8", errors="replace")[-4000:]
                )
        self._git(["clean", "-fd"], cwd=self.repository)
        changed = self.repository_changed_files()
        if changed:
            raise GitWorkspaceError(
                "Haupt-Repository ist nach Post-Merge-Prüfung nicht sauber: "
                + ", ".join(changed)
            )

    def diff_files(self, workspace: Workspace, base_commit: str, head_commit: str = "HEAD") -> list[str]:
        self._git(["cat-file", "-e", f"{base_commit}^{{commit}}"], cwd=workspace.path)
        self._git(["cat-file", "-e", f"{head_commit}^{{commit}}"], cwd=workspace.path)
        raw = self._git_bytes(
            ["diff", "--name-only", "-z", f"{base_commit}..{head_commit}"],
            cwd=workspace.path,
        )
        return sorted({
            normalize_repo_path(item)
            for item in raw.decode("utf-8", errors="strict").split("\0")
            if item
        })

    def validate_committed_changes(
        self,
        agent: AgentSpec,
        workspace: Workspace,
        *,
        base_commit: str,
        head_commit: str = "HEAD",
    ) -> list[str]:
        changed = self.diff_files(workspace, base_commit, head_commit)
        violations: list[str] = []
        for path in changed:
            allowed = any(path_matches_scope(path, scope) for scope in agent.allowed_paths)
            denied = any(path_matches_scope(path, scope) for scope in agent.denied_paths)
            if not allowed or denied:
                violations.append(path)
        if violations:
            raise ValidationError(
                "Commit enthält Dateien außerhalb des Schreibbereichs: "
                + ", ".join(violations)
            )
        return changed

    def changed_files(self, workspace: Workspace) -> list[str]:
        raw = self._git_bytes(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=workspace.path,
        )
        return self._parse_status_paths(raw)

    @staticmethod
    def _parse_status_paths(raw: bytes) -> list[str]:
        tokens = raw.decode("utf-8", errors="strict").split("\0")
        changed: set[str] = set()
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if not token:
                index += 1
                continue
            if len(token) < 4:
                raise GitWorkspaceError(f"Unerwartete Git-Statuszeile: {token!r}")
            status = token[:2]
            changed.add(normalize_repo_path(token[3:]))
            if "R" in status or "C" in status:
                index += 1
                if index < len(tokens) and tokens[index]:
                    changed.add(normalize_repo_path(tokens[index]))
            index += 1
        return sorted(changed)

    def rebase_workspace(
        self,
        workspace: Workspace,
        *,
        old_base_commit: str,
        new_base_commit: str,
        expected_head: str,
    ) -> tuple[Workspace, str]:
        """Rebase an already reviewed branch onto CURRENT_STABLE_COMMIT.

        The operation is aborted on conflict. The caller must rerun integration
        tests because commit identities and surrounding code may have changed.
        """
        self.assert_clean(workspace)
        self.assert_head(workspace, expected_head)
        self._git(["cat-file", "-e", f"{old_base_commit}^{{commit}}"], cwd=workspace.path)
        self._git(["cat-file", "-e", f"{new_base_commit}^{{commit}}"], cwd=workspace.path)
        if old_base_commit == new_base_commit:
            return workspace, expected_head
        completed = subprocess.run(
            [
                "git",
                "rebase",
                "--onto",
                new_base_commit,
                old_base_commit,
                workspace.branch,
            ],
            cwd=workspace.path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=self.command_timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=workspace.path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.command_timeout_seconds,
                check=False,
            )
            raise GitWorkspaceError(
                "Git-Rebase fehlgeschlagen oder enthält Konflikte: "
                + completed.stdout[-4000:]
            )
        rebased_head = self.head_commit(workspace)
        self.assert_clean(workspace)
        return (
            Workspace(
                agent_id=workspace.agent_id,
                branch=workspace.branch,
                path=workspace.path,
                base_commit=new_base_commit,
            ),
            rebased_head,
        )

    def abort_rebase_if_needed(self, workspace: Workspace) -> bool:
        """Abort a crash-left rebase. Returns True when an abort was performed."""
        git_dir_raw = self._git(["rev-parse", "--git-dir"], cwd=workspace.path).strip()
        git_dir = Path(git_dir_raw)
        if not git_dir.is_absolute():
            git_dir = (workspace.path / git_dir).resolve()
        in_progress = (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()
        if not in_progress:
            return False
        completed = subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=workspace.path,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=self.command_timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise GitWorkspaceError(
                "Crash-Recovery konnte laufenden Rebase nicht abbrechen: "
                + completed.stdout[-4000:]
            )
        self.assert_clean(workspace)
        return True

    def fast_forward_repository(
        self,
        *,
        branch: str,
        source_branch: str,
        expected_base_commit: str,
    ) -> str:
        self.assert_repository_ready(branch=branch, expected_commit=expected_base_commit)
        source_head = self.branch_head(source_branch)
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", expected_base_commit, source_head],
            cwd=self.repository,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=self.command_timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise GitWorkspaceError(
                "Integrations-Branch basiert nicht auf CURRENT_STABLE_COMMIT"
            )
        self._git(["merge", "--ff-only", source_branch], cwd=self.repository)
        merged = self.repository_head()
        if merged != source_head:
            raise GitWorkspaceError(
                f"Fast-Forward endete auf {merged}, erwartet war {source_head}"
            )
        return merged

    def reset_repository(self, *, expected_current: str, target_commit: str) -> None:
        current = self.repository_head()
        if current != expected_current:
            raise GitWorkspaceError(
                "Rollback verweigert: Repository-HEAD wurde zwischenzeitlich verändert: "
                f"erwartet {expected_current}, aktuell {current}"
            )
        self._git(["reset", "--hard", target_commit], cwd=self.repository)
        self._git(["clean", "-fd"], cwd=self.repository)
        if self.repository_head() != target_commit:
            raise GitWorkspaceError("Git-Rollback auf den vorherigen stabilen Commit fehlgeschlagen")
        changed = self.repository_changed_files()
        if changed:
            raise GitWorkspaceError(
                "Repository ist nach Rollback nicht sauber: " + ", ".join(changed)
            )

    def reset_workspace(
        self,
        workspace: Workspace,
        *,
        expected_current: str,
        target_commit: str,
    ) -> Workspace:
        self.assert_clean(workspace)
        current = self.head_commit(workspace)
        if current != expected_current:
            raise GitWorkspaceError(
                "Workspace-Rollback verweigert: "
                f"erwartet {expected_current}, aktuell {current}"
            )
        self._git(["reset", "--hard", target_commit], cwd=workspace.path)
        self._git(["clean", "-fd"], cwd=workspace.path)
        self.assert_clean(workspace)
        if self.head_commit(workspace) != target_commit:
            raise GitWorkspaceError("Workspace-Rollback fehlgeschlagen")
        return Workspace(
            agent_id=workspace.agent_id,
            branch=workspace.branch,
            path=workspace.path,
            base_commit=target_commit,
        )

    def run_repository_command(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float | None = None,
        env: dict[str, str] | None = None,
    ) -> TestExecution:
        workspace = Workspace(
            agent_id="TECH_AI_ORCHESTRATOR",
            branch=self.repository_branch(),
            path=self.repository,
            base_commit=self.repository_head(),
        )
        return self.run_command(
            workspace,
            command,
            timeout_seconds=timeout_seconds,
            env=env,
        )

    def validate_changes(self, agent: AgentSpec, workspace: Workspace) -> list[str]:
        changed = self.changed_files(workspace)
        violations: list[str] = []
        for path in changed:
            allowed = any(path_matches_scope(path, scope) for scope in agent.allowed_paths)
            denied = any(path_matches_scope(path, scope) for scope in agent.denied_paths)
            if not allowed or denied:
                violations.append(path)
        if violations:
            raise ValidationError(
                "Agent änderte Dateien außerhalb seines Schreibbereichs: "
                + ", ".join(violations)
            )
        return changed

    def run_command(
        self,
        workspace: Workspace,
        command: Sequence[str],
        *,
        timeout_seconds: float | None = None,
        env: dict[str, str] | None = None,
        cancellation_check: Callable[[], None] | None = None,
    ) -> TestExecution:
        if not command or any(not isinstance(part, str) or not part for part in command):
            raise GitWorkspaceError("Befehl muss aus nicht leeren Argumenten bestehen")
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        completed = run_bounded_process(
            list(command),
            cwd=workspace.path,
            env=merged_env,
            timeout_seconds=timeout_seconds or self.command_timeout_seconds,
            output_limit=10_000,
            cancellation_check=cancellation_check,
        )
        if completed.timed_out:
            return TestExecution(
                command=" ".join(command),
                passed=False,
                exit_code=None,
                summary=(
                    completed.output
                    + f"\nTIMEOUT nach {timeout_seconds or self.command_timeout_seconds}s"
                )[-10_000:],
            )
        return TestExecution(
            command=" ".join(command),
            passed=completed.returncode == 0,
            exit_code=completed.returncode,
            summary=completed.output,
        )

    def commit_changes(
        self,
        agent: AgentSpec,
        workspace: Workspace,
        *,
        message: str,
    ) -> str:
        message = message.strip()
        if not message:
            raise GitWorkspaceError("Commit-Nachricht fehlt")
        changed = self.validate_changes(agent, workspace)
        if not changed:
            raise GitWorkspaceError("Keine Änderungen zum Committen")
        self._git(["add", "--all"], cwd=workspace.path)
        self._git(["commit", "-m", message], cwd=workspace.path)
        return self._git(["rev-parse", "HEAD"], cwd=workspace.path).strip()

    def reap_managed_worktrees(
        self,
        *,
        protected_paths: Sequence[str | Path] = (),
        min_age_seconds: float = 3600.0,
        dry_run: bool = True,
    ) -> list[WorktreeReapRecord]:
        """Remove clean, unprotected TankAI worktrees while retaining branches.

        Dirty worktrees are deliberately quarantined rather than deleted. This
        preserves uncommitted evidence after a fence loss. Only direct children
        of the configured workspace root and branches under ``tankai/`` qualify.
        """
        if min_age_seconds < 0:
            raise GitWorkspaceError("min_age_seconds darf nicht negativ sein")
        if not self.workspace_root.exists():
            return []
        root = self.workspace_root.resolve()
        protected = {str(Path(item).resolve()) for item in protected_paths}
        records: list[WorktreeReapRecord] = []
        candidates = sorted(self.workspace_root.iterdir(), key=lambda item: item.name)
        for candidate in candidates:
            branch: str | None = None
            try:
                if candidate.is_symlink():
                    records.append(WorktreeReapRecord(
                        str(candidate), None, "skipped", "symlink_workspace",
                    ))
                    continue
                if not candidate.is_dir():
                    continue
                resolved = candidate.resolve(strict=True)
                if resolved.parent != root:
                    records.append(WorktreeReapRecord(
                        str(resolved), None, "skipped", "outside_workspace_root",
                    ))
                    continue
                if str(resolved) in protected:
                    records.append(WorktreeReapRecord(
                        str(resolved), None, "skipped", "protected_by_project_state",
                    ))
                    continue
                age = max(0.0, time.time() - resolved.stat().st_mtime)
                if age < min_age_seconds:
                    records.append(WorktreeReapRecord(
                        str(resolved), None, "skipped", "younger_than_min_age",
                    ))
                    continue
                branch = self._git(["branch", "--show-current"], cwd=resolved).strip()
                if not branch.startswith("tankai/"):
                    records.append(WorktreeReapRecord(
                        str(resolved), branch or None, "skipped", "unmanaged_branch",
                    ))
                    continue
                head = self._git(["rev-parse", "HEAD"], cwd=resolved).strip()
                workspace = Workspace(
                    agent_id="REAPER",
                    branch=branch,
                    path=resolved,
                    base_commit=head,
                )
                changed = self.changed_files(workspace)
                if changed:
                    records.append(WorktreeReapRecord(
                        str(resolved), branch, "quarantined",
                        "dirty_workspace:" + ",".join(changed[:20]),
                    ))
                    continue
                if dry_run:
                    records.append(WorktreeReapRecord(
                        str(resolved), branch, "candidate", "clean_managed_worktree",
                    ))
                    continue
                with self.workspace_lock():
                    if not resolved.exists():
                        records.append(WorktreeReapRecord(
                            str(resolved), branch, "skipped", "already_removed",
                        ))
                        continue
                    current_branch = self._git(["branch", "--show-current"], cwd=resolved).strip()
                    if current_branch != branch:
                        records.append(WorktreeReapRecord(
                            str(resolved), current_branch or None, "skipped", "branch_changed",
                        ))
                        continue
                    current_workspace = Workspace(
                        agent_id="REAPER",
                        branch=branch,
                        path=resolved,
                        base_commit=self._git(["rev-parse", "HEAD"], cwd=resolved).strip(),
                    )
                    if self.changed_files(current_workspace):
                        records.append(WorktreeReapRecord(
                            str(resolved), branch, "quarantined", "became_dirty",
                        ))
                        continue
                    self._remove_workspace_unlocked(current_workspace, delete_branch=False)
                records.append(WorktreeReapRecord(
                    str(resolved), branch, "removed", "clean_worktree_branch_retained",
                ))
            except (GitWorkspaceError, OSError) as exc:
                records.append(WorktreeReapRecord(
                    str(candidate), branch, "skipped", f"not_a_valid_managed_worktree:{exc}",
                ))
        return records

    def remove_workspace(self, workspace: Workspace, *, delete_branch: bool = False) -> None:
        with self.workspace_lock():
            self._remove_workspace_unlocked(workspace, delete_branch=delete_branch)

    def _remove_workspace_unlocked(
        self, workspace: Workspace, *, delete_branch: bool = False
    ) -> None:
        if workspace.path.exists():
            self._git(
                ["worktree", "remove", "--force", str(workspace.path)],
                cwd=self.repository,
            )
        if delete_branch and self._branch_exists(workspace.branch):
            self._git(["branch", "-D", workspace.branch], cwd=self.repository)
        self._git(["worktree", "prune", "--expire", "now"], cwd=self.repository)
        if self.workspace_root.exists() and not any(self.workspace_root.iterdir()):
            try:
                self.workspace_root.rmdir()
            except OSError:
                pass

    def _branch_name(self, agent: AgentSpec) -> str:
        raw = f"tankai/{agent.agent_id.lower()}/{agent.task_id.lower()}"
        safe = re.sub(r"[^a-z0-9._/-]+", "-", raw)
        safe = re.sub(r"\.{2,}", "-", safe)
        safe = re.sub(r"/+", "/", safe).strip("/.-")
        safe = safe[:180].rstrip("/.")
        if not safe or safe.endswith(".lock"):
            raise GitWorkspaceError("Kein gültiger Agent-Branchname")
        return safe

    def _branch_exists(self, branch: str) -> bool:
        completed = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self.repository,
            check=False,
        )
        return completed.returncode == 0

    def _git(self, args: Sequence[str], *, cwd: Path) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=self.command_timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise GitWorkspaceError(
                f"git {' '.join(args)} fehlgeschlagen ({completed.returncode}): "
                f"{completed.stdout[-4000:]}"
            )
        return completed.stdout

    def _git_bytes(self, args: Sequence[str], *, cwd: Path) -> bytes:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=self.command_timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise GitWorkspaceError(
                f"git {' '.join(args)} fehlgeschlagen ({completed.returncode}): "
                + completed.stdout.decode("utf-8", errors="replace")[-4000:]
            )
        return completed.stdout
