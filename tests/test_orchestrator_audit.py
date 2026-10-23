"""Terminal task state must survive new start requests."""
import pytest
from test_development_orchestrator import make_orchestrator, create_task
from tankai.dev_orchestrator import DevelopmentRole, TaskState, TransitionError


def test_unassigned_cancelled_task_cannot_be_started_again(tmp_path):
    orchestrator = make_orchestrator(tmp_path)
    create_task(orchestrator, "CANCELLED", ["backend/cancelled/**"])
    orchestrator.cancel_task("CANCELLED", reason="Operator stopped this work")
    before = orchestrator.state().revision
    with pytest.raises(TransitionError):
        orchestrator.start_agent("CANCELLED", DevelopmentRole.BACKEND)
    state = orchestrator.state()
    assert state.revision == before
    assert state.tasks["CANCELLED"].state == TaskState.CANCELLED
    assert not state.agents and not state.file_locks
