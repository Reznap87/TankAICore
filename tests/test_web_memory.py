"""Real session/CSRF requests; all memory fixtures are local scripted replies."""
import json
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace
from datetime import timedelta
from uuid import uuid4
from urllib.request import urlopen

import pytest

from tankai.core.embeddings import HashingEmbedder, OpenAIEmbedder
from tankai.core.llm import BaseLLM
from tankai.core.models import RunResult, TaskStatus, utcnow
from tankai.web import server as web_server
from test_web_server import Client, _configure


class Scripted(BaseLLM):
    calls = 0
    def complete(self, prompt, **kwargs):
        self.calls += 1
        return json.dumps({"entries": [{"content": "Private fixture text must never appear in status JSON."}]})


def seed(memory, *, prepared=True, eligible=True):
    run_id = str(uuid4())
    memory.store_episode(RunResult(
        goal_id=run_id, final_answer="Original answer and verdict stay unchanged.",
        status=TaskStatus.COMPLETED, verification_passed=eligible, release_ready=eligible,
    ), "Disposable memory recovery fixture")
    if eligible:
        original = memory.vectors.add
        def fail(*args, **kwargs):
            raise OSError("private fixture diagnostic must not escape")
        memory.vectors.add = fail
        try:
            with pytest.raises(OSError):
                memory.consolidate(run_id, Scripted())
        finally:
            memory.vectors.add = original
        if not prepared:
            with memory._conn:
                memory._conn.execute("UPDATE consolidation_jobs SET prepared_entries=NULL WHERE run_id=?", (run_id,))
    return run_id


@pytest.fixture
def memory_api(tmp_path, monkeypatch):
    store = _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("TANKAI_EMBEDDER", "hashing")
    monkeypatch.setenv("TANKAI_REQUIRE_INDEPENDENT_CRITIC", "0")
    identities = []
    for name in ("owner", "foreign"):
        _, tenant, workspace = store.create_user_with_tenant(
            email=name + "@example.com", password="Memory-test-password-123",
            display_name=name, tenant_name=name,
        )
        identities.append((tenant, workspace))
    app = web_server.AppContext.from_env("127.0.0.1")
    server = web_server.ThreadedHTTPServer(("127.0.0.1", 0), web_server.Handler, app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    clients = []
    for name in ("owner", "foreign"):
        client = Client(base)
        status, _, login = client.post("/api/auth/login", {"email": name + "@example.com", "password": "Memory-test-password-123"})
        assert status == 200
        clients.append((client, login["csrf_token"]))
    runtime = app.runtimes.get(tenant_id=identities[0][0], workspace_id=identities[0][1])
    try:
        yield app, runtime, clients, base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_memory_resume_is_authenticated_scoped_csrf_protected_and_idempotent(memory_api, monkeypatch):
    app, runtime, clients, base = memory_api
    (owner, csrf), (foreign, foreign_csrf) = clients
    memory = runtime.tank.ltm
    run_id = seed(memory)
    original_history = {"goal_id": run_id, "status": "completed", "verification_passed": True,
                        "final_answer": "Original answer and verdict stay unchanged."}
    runtime.history.append(original_history)
    path = "/api/memory/consolidation/" + run_id
    before = runtime.tank.llm_call_budget.snapshot()
    def forbidden(*args, **kwargs):
        raise AssertionError("Recovery must not invoke a language model")
    monkeypatch.setattr(BaseLLM, "complete", forbidden)
    monkeypatch.setattr(runtime.tank.llm, "complete", forbidden)
    monkeypatch.setattr(runtime.tank.critic_llm, "complete", forbidden)
    assert Client(base).get(path)[0] == 401
    assert Client(base).post(path + "/resume", {})[0] == 401
    assert owner.post(path + "/resume", {})[0] == 403
    assert owner.post(path + "/resume", {}, csrf="wrong")[0] == 403
    assert foreign.get(path)[0] == 404
    assert foreign.post(path + "/resume", {}, csrf=foreign_csrf)[0] == 404
    status, _, current = owner.get(path)
    assert status == 200
    state = current["memory_consolidation"]
    assert state["state"] == "prepared" and state["can_resume"]
    assert state["provider_calls_allowed"] is False
    assert "Private fixture" not in json.dumps(current)
    status, _, result = owner.post(path + "/resume", {}, csrf=csrf)
    assert status == 200
    assert result["entries_created"] == 1
    assert result["memory_consolidation"]["state"] == "completed"
    assert result["memory_consolidation"]["entries_count"] == 1
    assert not result["memory_consolidation"]["can_resume"]
    assert owner.post(path + "/resume", {}, csrf=csrf)[2]["entries_created"] == 0
    assert owner.get(path)[2]["memory_consolidation"]["state"] == "completed"
    assert runtime.tank.llm_call_budget.snapshot() == before
    assert len(memory.retrieve("Private fixture", min_score=0)) == 1
    assert runtime.history.list_recent(1) == [original_history]
    # A member's different active workspace cannot address the first workspace.
    status, _, created = owner.post("/api/workspaces", {"name": "Other workspace"}, csrf=csrf)
    assert status == 201
    assert owner.post("/api/workspaces/select", {"workspace_id": created["workspace"]["id"]}, csrf=csrf)[0] == 200
    assert owner.get(path)[0] == 404
    assert owner.post(path + "/resume", {}, csrf=csrf)[0] == 404


@pytest.mark.parametrize("case", ["no_checkpoint", "not_eligible", "running", "paid_index", "unknown_index"])
def test_memory_resume_fails_closed_without_local_prepared_work(memory_api, case):
    _, runtime, clients, _ = memory_api
    owner, csrf = clients[0]
    memory = runtime.tank.ltm
    run_id = seed(memory, prepared=case != "no_checkpoint", eligible=case != "not_eligible")
    if case == "running":
        with memory._conn:
            memory._conn.execute("UPDATE consolidation_jobs SET status='running', lease_until=? WHERE run_id=?",
                                 ((utcnow() + timedelta(minutes=5)).isoformat(), run_id))
    if case in {"paid_index", "unknown_index"}:
        class UnknownIndex(HashingEmbedder):
            def embed(self, text):
                raise AssertionError("A provider or unknown embedder must never be called")
        if case == "paid_index":
            # The real adapter type with a rejecting client; no SDK or API key.
            embedder = object.__new__(OpenAIEmbedder)
            embedder.model = "fixture"
            embedder.client = SimpleNamespace(embeddings=SimpleNamespace(create=UnknownIndex().embed))
        else:
            embedder = UnknownIndex()
        memory.vectors.embedder = embedder
        with pytest.raises(ValueError, match="local hashing index"):
            memory.consolidate(run_id, prepared_only=True)
    path = "/api/memory/consolidation/" + run_id
    status, _, current = owner.get(path)
    assert status == 200
    assert not current["memory_consolidation"]["can_resume"]
    status, _, denied = owner.post(path + "/resume", {}, csrf=csrf)
    assert status == 409
    assert "private fixture diagnostic" not in json.dumps(denied)
    assert not memory.vectors.ids


def test_resume_rejects_extra_options_and_does_not_leak_internal_errors(memory_api, monkeypatch):
    _, runtime, clients, _ = memory_api
    owner, csrf = clients[0]
    run_id = seed(runtime.tank.ltm)
    path = "/api/memory/consolidation/" + run_id
    assert owner.post(path + "/resume", {"llm": "paid", "workspace_id": str(uuid4())}, csrf=csrf)[0] == 400
    assert owner.get("/api/memory/consolidation/" + "-" * 36)[0] == 400
    assert owner.get("/api/memory/consolidation/" + str(uuid4()))[0] == 404
    def fail(*args, **kwargs):
        raise OSError("private diagnostic must not appear in the response")
    monkeypatch.setattr(runtime.tank.ltm.vectors, "add", fail)
    status, _, error = owner.post(path + "/resume", {}, csrf=csrf)
    assert status == 500
    assert "private diagnostic" not in json.dumps(error)
    assert owner.get(path)[2]["memory_consolidation"]["can_resume"]
    assert not runtime.tank.ltm._conn.execute("SELECT id FROM memory_entries WHERE memory_type='semantic'").fetchall()


def test_run_response_and_history_include_the_stable_memory_run_identifier(memory_api):
    _, _, clients, _ = memory_api
    owner, csrf = clients[0]
    status, _, result = owner.post("/api/run", {"goal": "Local mock fixture"}, csrf=csrf)
    assert status == 200
    assert result["goal_id"]
    assert owner.get("/api/history")[2][0]["goal_id"] == result["goal_id"]
    state = owner.get("/api/memory/consolidation/" + result["goal_id"])[2]["memory_consolidation"]
    assert state["state"] == "not_eligible"
    assert not state["can_resume"]


def test_container_html_smoke_accepts_current_page_and_still_bounds_oversized_reads(memory_api, monkeypatch):
    app, _, _, base = memory_api
    # Match the container's public-page headers; no further login is performed.
    app.cookie_secure = True
    app.hsts_seconds = 31_536_000
    path = Path(__file__).parents[1] / ".github/scripts/production_web_container_smoke.py"
    spec = importlib.util.spec_from_file_location("memory_html_smoke", path)
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    smoke.html_contract(base)
    with urlopen(base) as response:
        headers = response.headers
    reads = []
    class OversizedResponse:
        status = 200
        def __init__(self):
            self.headers = headers
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, amount):
            reads.append(amount)
            return b"x" * amount
    monkeypatch.setattr(smoke.OPENER, "open", lambda *args, **kwargs: OversizedResponse())
    with pytest.raises(smoke.Failure, match="HTML response too large"):
        smoke.html_contract(base)
    assert reads == [smoke.HTML_LIMIT + 1]
