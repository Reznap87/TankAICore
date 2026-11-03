"""Short-lived stores must release OS handles without waiting for Python GC."""
import sqlite3

import pytest

from tankai.dev_orchestrator.fencing import LeaseFenceStore
from tankai.dev_orchestrator.job_queue import DevelopmentJobQueue
from tankai.web.auth import AuthStore


@pytest.fixture
def connections(monkeypatch):
    original = sqlite3.connect
    retained = []  # Strong references expose leaks even when GC timing varies.
    settings = {"fail_pragma":False}
    class Tracked(sqlite3.Connection):
        closed = False
        def execute(self, sql, *args, **kwargs):
            if settings["fail_pragma"] and sql.startswith("PRAGMA"):
                raise sqlite3.OperationalError("synthetic configuration failure")
            return super().execute(sql, *args, **kwargs)
        def close(self):
            super().close()
            self.closed = True
    def connect(*args, **kwargs):
        connection = original(*args, factory=Tracked, **kwargs)
        retained.append(connection)
        return connection
    monkeypatch.setattr(sqlite3, "connect", connect)
    yield retained, settings
    for connection in retained:
        connection.close()


def store_for(kind, tmp_path):
    path = tmp_path/(kind+".db")
    if kind == "auth": return AuthStore(path)
    if kind == "fence": return LeaseFenceStore(path)
    return DevelopmentJobQueue(path, repository_base=tmp_path/"repositories",
        workspace_base=tmp_path/"workspaces", state_base=tmp_path/"state")


@pytest.mark.parametrize("kind", ["auth", "fence", "queue"])
def test_schema_initialization_releases_every_connection(kind, tmp_path, connections):
    retained, _ = connections
    store_for(kind, tmp_path)
    assert retained and all(connection.closed for connection in retained)


@pytest.mark.parametrize("kind", ["auth", "fence", "queue"])
def test_commit_is_durable_and_connection_is_closed_at_context_exit(kind, tmp_path, connections):
    retained, _ = connections
    store = store_for(kind, tmp_path)
    with store._connect() as connection:
        connection.execute("CREATE TABLE lifetime_probe(value TEXT)")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO lifetime_probe VALUES('committed')")
    assert connection.closed
    with store._connect() as reader:
        assert reader.execute("SELECT value FROM lifetime_probe").fetchall()[0][0] == "committed"
    assert all(connection.closed for connection in retained)


@pytest.mark.parametrize("kind", ["auth", "fence", "queue"])
def test_exception_rolls_back_and_releases_connection(kind, tmp_path, connections):
    retained, _ = connections
    store = store_for(kind, tmp_path)
    with store._connect() as setup:
        setup.execute("CREATE TABLE lifetime_probe(value TEXT)")
    with pytest.raises(ValueError, match="synthetic"):
        with store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO lifetime_probe VALUES('must rollback')")
            raise ValueError("synthetic operation failure")
    assert connection.closed
    with store._connect() as reader:
        assert reader.execute("SELECT COUNT(*) FROM lifetime_probe").fetchone()[0] == 0
    assert all(connection.closed for connection in retained)


@pytest.mark.parametrize("kind", ["auth", "fence", "queue"])
def test_configuration_failure_closes_connection_before_context_entry(kind, tmp_path, connections):
    retained, settings = connections
    store = store_for(kind, tmp_path)
    settings["fail_pragma"] = True
    with pytest.raises(sqlite3.OperationalError, match="synthetic"):
        with store._connect():
            pytest.fail("connection configuration should have failed")
    assert retained[-1].closed


def test_fence_database_can_be_renamed_and_removed_with_store_still_alive(tmp_path, connections):
    store = store_for("fence", tmp_path)
    lease = store.acquire(scope_key="synthetic", job_id="job-1", owner_id="worker-1",
                          lease_token="synthetic-token", lease_seconds=60)
    assert store.current("synthetic").epoch == lease.epoch
    # Windows refuses this while a connection holds an OS handle. Keep both
    # the store and all connection objects alive during the operation.
    renamed = store.path.with_name("renamed.db")
    store.path.rename(renamed)
    renamed.unlink()
    assert not renamed.exists()
