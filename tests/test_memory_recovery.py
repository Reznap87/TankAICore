"""Durable memory checkpoints use scripted models only, without network access."""
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from tankai.core.llm import BaseLLM
from tankai.core.long_term_memory import LongTermMemory
from tankai.core.models import RunResult, TaskStatus, utcnow


class ScriptedMemory(BaseLLM):
    def __init__(self, response=None, error=None):
        self.response = response or json.dumps({"entries": [
            {"content": "47 multiplied by 19 is equal to 893.", "confidence": 0.8, "tags": ["math"]}
        ]})
        self.error = error
        self.calls = 0

    def complete(self, prompt, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


def open_memory(tmp_path):
    return LongTermMemory(tmp_path / "memory.db", tmp_path / "vectors.npz",
                          cold_dir=tmp_path / "cold")


def episode(memory, **overrides):
    data = dict(goal_id=str(uuid4()), final_answer="47 multiplied by 19 is equal to 893.",
                status=TaskStatus.COMPLETED, verification_passed=True, release_ready=True,
                execution_mode="live")
    data.update(overrides)
    result = RunResult(**data)
    memory.store_episode(result, "Calculate 47 times 19")
    return result.goal_id


def test_pending_work_survives_restart_and_retry_is_idempotent(tmp_path):
    memory = open_memory(tmp_path)
    run_id = episode(memory)
    model = ScriptedMemory(error=RuntimeError("private credential must not persist"))
    with pytest.raises(RuntimeError):
        memory.consolidate(run_id, model)
    assert model.calls == 1
    pending = memory.pending_consolidations()
    assert pending[0]["attempts"] == 1
    assert pending[0]["last_error_type"] == "RuntimeError"
    assert "private credential" not in json.dumps(pending)
    memory.close()

    memory = open_memory(tmp_path)
    try:
        assert memory.pending_consolidations() == pending
        model = ScriptedMemory()
        entries = memory.consolidate(run_id, model)
        assert len(entries) == 1
        assert memory.consolidate(run_id, model) == []
        assert model.calls == 1
        assert not memory.pending_consolidations()
        assert len(memory.retrieve("47 multiplied by 19", min_score=0)) == 1
        assert memory._conn.execute(
            "SELECT COUNT(*) FROM memory_entries WHERE memory_type = 'semantic'"
        ).fetchone()[0] == 1
    finally:
        memory.close()


@pytest.mark.parametrize("overrides", [
    {"status": TaskStatus.FAILED}, {"verification_passed": False},
    {"release_ready": False}, {"execution_mode": "simulation"},
])
def test_unreleased_runs_are_not_promoted_or_sent_to_a_model(tmp_path, overrides):
    memory = open_memory(tmp_path)
    try:
        run_id = episode(memory, **overrides)
        model = ScriptedMemory()
        assert memory.consolidate(run_id, model) == []
        assert model.calls == 0
        assert not memory.pending_consolidations()
        assert not memory.vectors.ids
    finally:
        memory.close()


@pytest.mark.parametrize("raw", [
    "This unstructured provider error is not reusable knowledge.", "[]", "{}",
    '{"entries":null}', '{"entries":[null]}',
    '{"entries":[{"content":"short"}]}',
    '{"entries":[{"content":"This is long enough to be a fact.","confidence":NaN}]}',
    '{"entries":[{"content":"This is long enough to be a fact.","confidence":true}]}',
    '{"entries":[{"content":"This is long enough to be a fact.","tags":"invalid"}]}',
    '{"entries":[{"content":"This first entry would have been saved."},{"content":7}]}',
])
def test_invalid_batch_is_deferred_without_partial_or_raw_knowledge(tmp_path, raw):
    memory = open_memory(tmp_path)
    try:
        run_id = episode(memory)
        with pytest.raises(ValueError, match="invalid structured data"):
            memory.consolidate(run_id, ScriptedMemory(raw))
        assert not memory.vectors.ids
        assert not memory._conn.execute(
            "SELECT id FROM memory_entries WHERE memory_type = 'semantic'"
        ).fetchall()
        pending = memory.pending_consolidations()[0]
        assert pending["last_error_type"] == "ValueError"
        assert not pending["prepared"]
    finally:
        memory.close()


def test_index_failure_resumes_checkpoint_without_second_inference(tmp_path, monkeypatch):
    memory = open_memory(tmp_path)
    run_id = episode(memory)
    original_add = memory.vectors.add
    def fail(*args, **kwargs):
        original_add(*args, **kwargs)
        raise OSError("simulated interrupted index write")
    monkeypatch.setattr(memory.vectors, "add", fail)
    model = ScriptedMemory()
    with pytest.raises(OSError):
        memory.consolidate(run_id, model)
    assert memory.pending_consolidations()[0]["prepared"]
    assert not memory.retrieve("47 multiplied", min_score=0)
    memory.close()

    memory = open_memory(tmp_path)
    try:
        assert len(memory.consolidate(run_id, prepared_only=True)) == 1
        assert memory.consolidate(run_id, prepared_only=True) == []
        assert len(memory.vectors.ids) == 1
        assert len(memory.retrieve("47 multiplied", min_score=0)) == 1
    finally:
        memory.close()


def test_active_claim_blocks_duplicate_inference_and_expired_claim_can_resume(tmp_path):
    memory = open_memory(tmp_path)
    run_id = episode(memory)
    model = ScriptedMemory(error=RuntimeError("rate limit"))
    with pytest.raises(RuntimeError):
        memory.consolidate(run_id, model)
    with memory._conn:
        memory._conn.execute(
            "UPDATE consolidation_jobs SET status='running', claim_token='old', lease_until=?",
            ((utcnow() + timedelta(minutes=5)).isoformat(),),
        )
    other = open_memory(tmp_path)
    try:
        model = ScriptedMemory()
        with pytest.raises(RuntimeError, match="already running"):
            other.consolidate(run_id, model)
        assert model.calls == 0
        with memory._conn:
            memory._conn.execute("UPDATE consolidation_jobs SET lease_until=?",
                                 ((utcnow() - timedelta(seconds=1)).isoformat(),))
        assert len(other.consolidate(run_id, model)) == 1
        assert model.calls == 1
    finally:
        other.close()
        memory.close()


def test_retry_does_not_silently_switch_to_heuristic_or_infer_legacy_verification(tmp_path):
    memory = open_memory(tmp_path)
    try:
        run_id = episode(memory)
        with pytest.raises(RuntimeError):
            memory.consolidate(run_id, ScriptedMemory(error=RuntimeError("limited")))
        with pytest.raises(ValueError, match="mode cannot change"):
            memory.consolidate(run_id)
        with pytest.raises(ValueError, match="inference is not permitted"):
            memory.consolidate(run_id, prepared_only=True)
        legacy = episode(memory)
        with memory._conn:
            memory._conn.execute("UPDATE runs SET metadata='{}' WHERE id=?", (legacy,))
        assert memory.consolidate(legacy, ScriptedMemory(error=AssertionError())) == []
        assert len(memory.pending_consolidations()) == 1
    finally:
        memory.close()


def test_sql_batch_failure_rolls_back_all_entries_and_resumes_without_inference(tmp_path):
    memory = open_memory(tmp_path)
    try:
        run_id = episode(memory)
        with memory._conn:
            memory._conn.execute("""
                CREATE TRIGGER fail_second_entry BEFORE INSERT ON memory_entries
                WHEN NEW.source = 'consolidation:llm' AND NEW.content LIKE 'Second%'
                BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END
            """)
        model = ScriptedMemory(json.dumps({"entries": [
            {"content": "First entry from an accepted run is ready."},
            {"content": "Second entry from an accepted run is ready."},
        ]}))
        with pytest.raises(Exception, match="simulated disk failure"):
            memory.consolidate(run_id, model)
        assert model.calls == 1
        assert not memory._conn.execute(
            "SELECT id FROM memory_entries WHERE memory_type='semantic'"
        ).fetchall()
        assert not memory.retrieve("accepted run", min_score=0)
        with memory._conn:
            memory._conn.execute("DROP TRIGGER fail_second_entry")
        assert len(memory.consolidate(run_id, prepared_only=True)) == 2
        assert len(memory.retrieve("accepted run", min_score=0)) == 2
        assert len(memory.vectors.ids) == 2
    finally:
        memory.close()


def test_schema_upgrade_preserves_episodes_and_does_not_invent_pending_jobs(tmp_path):
    memory = open_memory(tmp_path)
    run_id = episode(memory)
    with memory._conn:
        memory._conn.execute("DROP TABLE consolidation_jobs")
        memory._conn.execute("PRAGMA user_version = 2")
    memory.close()
    memory = open_memory(tmp_path)
    try:
        assert memory._conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert memory._conn.execute("SELECT final_answer FROM runs WHERE id=?", (run_id,)).fetchone()
        assert not memory.pending_consolidations()
        assert len(memory.consolidate(run_id, ScriptedMemory())) == 1
    finally:
        memory.close()


def test_stale_claim_cannot_publish_or_clear_new_owners_job(tmp_path):
    memory = open_memory(tmp_path)
    try:
        run_id = episode(memory)
        class ReplacedOwner(ScriptedMemory):
            def complete(self, prompt, **kwargs):
                with memory._conn:
                    memory._conn.execute(
                        "UPDATE consolidation_jobs SET claim_token='replacement' WHERE run_id=?", (run_id,)
                    )
                return super().complete(prompt, **kwargs)
        with pytest.raises(RuntimeError, match="claim expired"):
            memory.consolidate(run_id, ReplacedOwner())
        job = memory._conn.execute("SELECT * FROM consolidation_jobs WHERE run_id=?", (run_id,)).fetchone()
        assert job["claim_token"] == "replacement"
        assert job["status"] == "running"
        assert job["prepared_entries"] is None
        assert not memory.vectors.ids
    finally:
        memory.close()
