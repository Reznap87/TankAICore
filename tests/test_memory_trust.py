"""Only usable, valid knowledge may enter future task context."""
import pytest

from tankai.core.long_term_memory import LongTermMemory


def add(memory, validity="valid", retention="hot", kind="semantic", content="TankAI bestätigtes Testwissen"):
    return memory._add_entry(content=content, source="test", memory_type=kind,
                             validity=validity, confidence=0.8, retention_policy=retention,
                             index_vector=True)


@pytest.fixture
def memory():
    value = LongTermMemory(in_memory=True)
    try:
        yield value
    finally:
        value.close()


@pytest.mark.parametrize("validity", ["invalid", "unknown", "conflicting", "", "VALID"])
def test_non_valid_memory_never_becomes_context_or_gets_access_credit(memory, validity):
    entry = add(memory, validity=validity)
    assert memory.retrieve("TankAI", min_score=-1) == []
    assert memory._conn.execute("SELECT access_count FROM memory_entries WHERE id=?", (entry.id,)).fetchone()[0] == 0


def test_excluded_top_hits_do_not_starve_valid_lower_ranked_knowledge(memory):
    for _ in range(8):
        add(memory, validity="invalid", content="TankAI Testwissen")
    valid = add(memory, content="TankAI Testwissen mit zusätzlichen bestätigten Informationen")
    hits = memory.retrieve("TankAI Testwissen", k=1, min_score=-1)
    assert [item["id"] for item in hits] == [valid.id]


def test_warm_knowledge_remains_available_but_cold_and_wrong_types_do_not(memory):
    add(memory, retention="cold")
    warm = add(memory, retention="warm")
    procedure = add(memory, kind="procedural")
    assert {item["id"] for item in memory.retrieve("TankAI", min_score=-1)} == {warm.id, procedure.id}
    assert [item["id"] for item in memory.retrieve("TankAI", memory_types=["procedural"], min_score=-1)] == [procedure.id]
    assert not memory.retrieve("TankAI", memory_types=["episodic"], min_score=-1)


def test_empty_or_zero_result_request_needs_no_embedding_inference(memory, monkeypatch):
    add(memory, validity="invalid")
    def forbidden(*args):
        raise AssertionError("No eligible knowledge; embedding must not be requested")
    monkeypatch.setattr(memory.vectors.embedder, "embed", forbidden)
    assert memory.retrieve("anything", min_score=-1) == []
    assert memory.retrieve("anything", k=0) == []


def test_revoking_previously_valid_knowledge_takes_effect_on_next_retrieval(memory):
    entry = add(memory)
    assert memory.retrieve("TankAI", min_score=-1)
    with memory._conn:
        memory._conn.execute("UPDATE memory_entries SET validity='invalid' WHERE id=?", (entry.id,))
    assert not memory.retrieve("TankAI", min_score=-1)
