"""Constructor failures must explicitly release their already opened SQLite DB."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from tankai.core import long_term_memory as ltm_module
from tankai.core.long_term_memory import LongTermMemory
from tankai.core.memory import Memory


class InitFailure(RuntimeError):
    pass


@pytest.fixture
def sqlite_tracker(monkeypatch):
    original_connect = sqlite3.connect
    connections = []
    settings = SimpleNamespace(sql_prefix=None, commit_error=False, close_error=False,
                               fail_disk_connect=False)
    failure = InitFailure("injected initialization failure")

    class TrackedConnection(sqlite3.Connection):
        close_calls = 0

        def execute(self, sql, *args, **kwargs):
            if settings.sql_prefix and sql.strip().startswith(settings.sql_prefix):
                raise failure
            return super().execute(sql, *args, **kwargs)

        def commit(self):
            if settings.commit_error:
                raise failure
            return super().commit()

        def close(self):
            self.close_calls += 1
            super().close()
            if settings.close_error:
                raise RuntimeError("injected cleanup failure")

    def connect(database, *args, **kwargs):
        if settings.fail_disk_connect and str(database) != ":memory:":
            raise sqlite3.OperationalError("injected disk connection failure")
        connection = original_connect(database, *args, factory=TrackedConnection, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    return SimpleNamespace(connections=connections, settings=settings, failure=failure)


def construct(kind, tmp_path):
    if kind == "memory":
        return Memory(tmp_path / "memory.db")
    if kind == "ltm":
        return LongTermMemory(db_path=tmp_path / "ltm.db", vector_path=tmp_path / "vectors.npz",
                              cold_dir=tmp_path / "cold", embedder="hashing")
    raise AssertionError("unknown fixture kind")


def assert_connection_closed_once(tracker):
    assert len(tracker.connections) == 1
    connection = tracker.connections[0]
    assert connection.close_calls == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize("kind", ["memory", "ltm"])
@pytest.mark.parametrize("stage", ["schema", "commit"])
def test_schema_or_commit_failure_closes_connection(tmp_path, sqlite_tracker, kind, stage):
    if stage == "schema":
        sqlite_tracker.settings.sql_prefix = "CREATE TABLE"
    else:
        sqlite_tracker.settings.commit_error = True
    with pytest.raises(InitFailure) as error:
        construct(kind, tmp_path)
    assert error.value is sqlite_tracker.failure
    assert_connection_closed_once(sqlite_tracker)


def test_memory_existing_row_load_failure_closes_connection(tmp_path, sqlite_tracker, monkeypatch):
    def fail_load(self):
        raise sqlite_tracker.failure

    monkeypatch.setattr(Memory, "_load_from_db", fail_load)
    with pytest.raises(InitFailure) as error:
        construct("memory", tmp_path)
    assert error.value is sqlite_tracker.failure
    assert_connection_closed_once(sqlite_tracker)


@pytest.mark.parametrize("stage", ["embedder", "vectors"])
def test_ltm_post_schema_failure_closes_connection(tmp_path, sqlite_tracker, monkeypatch, stage):
    def fail(*args, **kwargs):
        raise sqlite_tracker.failure

    monkeypatch.setattr(ltm_module, "get_embedder" if stage == "embedder" else "VectorStore", fail)
    with pytest.raises(InitFailure) as error:
        construct("ltm", tmp_path)
    assert error.value is sqlite_tracker.failure
    assert_connection_closed_once(sqlite_tracker)


def test_ltm_fallback_connection_is_closed_if_later_init_fails(tmp_path, sqlite_tracker, monkeypatch):
    sqlite_tracker.settings.fail_disk_connect = True

    def fail(*args, **kwargs):
        raise sqlite_tracker.failure

    monkeypatch.setattr(ltm_module, "get_embedder", fail)
    with pytest.raises(InitFailure) as error:
        LongTermMemory(db_path=tmp_path / "ltm.db", vector_path=tmp_path / "vectors.npz",
                       cold_dir=tmp_path / "cold", embedder="hashing", allow_in_memory_fallback=True)
    assert error.value is sqlite_tracker.failure
    assert_connection_closed_once(sqlite_tracker)



@pytest.mark.parametrize("kind", ["memory", "ltm"])
def test_cleanup_error_does_not_replace_initialization_error(tmp_path, sqlite_tracker, kind):
    sqlite_tracker.settings.sql_prefix = "CREATE TABLE"
    sqlite_tracker.settings.close_error = True
    with pytest.raises(InitFailure) as error:
        construct(kind, tmp_path)
    assert error.value is sqlite_tracker.failure
    assert_connection_closed_once(sqlite_tracker)


def test_interrupted_memory_initialization_also_closes_connection(tmp_path, sqlite_tracker, monkeypatch):
    def interrupt(self):
        raise KeyboardInterrupt

    monkeypatch.setattr(Memory, "_load_from_db", interrupt)
    with pytest.raises(KeyboardInterrupt):
        construct("memory", tmp_path)
    assert_connection_closed_once(sqlite_tracker)


@pytest.mark.parametrize("kind", ["memory", "ltm"])
def test_successful_initialization_keeps_connection_open_and_double_close_is_safe(tmp_path, sqlite_tracker, kind):
    resource = construct(kind, tmp_path)
    assert len(sqlite_tracker.connections) == 1
    connection = sqlite_tracker.connections[0]
    assert connection.close_calls == 0
    assert connection.execute("SELECT 1").fetchone()[0] == 1
    resource.close()
    resource.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
