"""A live writer must never lose its lock merely because time elapsed."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tankai.dev_orchestrator.state_store import ProjectStateStore, StateStoreError
from tankai.dev_orchestrator.release_publication import PublicationLedgerStore, PublicationError


@pytest.mark.parametrize("factory,error", [(ProjectStateStore, StateStoreError),
                                           (PublicationLedgerStore, PublicationError)])
def test_old_live_lock_cannot_be_stolen_by_another_store(tmp_path, factory, error):
    path = tmp_path / "state.json"
    owner = factory(path, lock_timeout_seconds=0.1, stale_lock_seconds=0.01)
    contender = factory(path, lock_timeout_seconds=0.1, stale_lock_seconds=0.01)
    with owner._process_lock():
        os.utime(owner.lock_path, (1, 1))
        with pytest.raises(error, match="Timeout"):
            with contender._process_lock():
                pytest.fail("A live writer's lock was stolen")
    with contender._process_lock():
        pass


def test_process_termination_releases_lock_without_age_based_recovery(tmp_path):
    from tankai.dev_orchestrator.local_lock import exclusive_local_lock, LocalLockTimeout
    path = tmp_path / "kernel.lock"
    script = ("import sys\nfrom pathlib import Path\n"
              "from tankai.dev_orchestrator.local_lock import exclusive_local_lock\n"
              "with exclusive_local_lock(Path(sys.argv[1]),timeout_seconds=1):\n"
              " print('LOCKED',flush=True)\n sys.stdin.readline()\n")
    child = subprocess.Popen([sys.executable, "-u", "-c", script, str(path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(LocalLockTimeout):
            with exclusive_local_lock(path, timeout_seconds=0.05):
                pytest.fail("Independent process acquired an occupied lock")
        child.terminate()
        child.wait(timeout=5)
        with exclusive_local_lock(path, timeout_seconds=0.2):
            pass
        assert path.is_file()  # Stable anchor remains, not an occupied lock.
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_mutation_timeout_is_not_misreported_as_lock_timeout(tmp_path):
    store = PublicationLedgerStore(tmp_path / "publication.json")
    store.create({"counter": 0})
    def fail(payload):
        raise TimeoutError("fixture mutation timeout")
    with pytest.raises(TimeoutError, match="fixture mutation"):
        store.update(fail)
    assert store.load() == {"counter": 0}


def test_git_lock_contention_never_signals_the_current_owner(tmp_path, monkeypatch):
    from test_job_queue import init_repo
    from tankai.dev_orchestrator.git_workspace import GitWorkspaceManager, GitWorkspaceError
    repo = tmp_path / "repository"
    init_repo(repo)
    manager = GitWorkspaceManager(repo, tmp_path / "worktrees")
    def forbidden_signal(*args):
        # Never execute the actual call: os.kill(pid, 0) terminates the target
        # on Windows instead of being the POSIX existence probe.
        raise AssertionError("Lock contention must not signal the owning process")
    monkeypatch.setattr(os, "kill", forbidden_signal)
    with manager.workspace_lock():
        with pytest.raises(GitWorkspaceError, match="Timeout"):
            with manager.workspace_lock(timeout_seconds=0.05):
                pytest.fail("Concurrent Git metadata writers")
