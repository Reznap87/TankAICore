from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

import test_worker_pipeline
from tankai.dev_orchestrator import WorkerExecutionError, WorkerPipelineJob, WorkerPipelineRunner, WorkerRunState
from tankai.dev_orchestrator.cli import main
from tankai.dev_orchestrator.code_proposal import CodeProposal, ProposalChecks, ProposedFile
from tankai.dev_orchestrator.code_request import (
    CodeContextFile, CodeRequestPackage, CodeResponse, build_code_request, prepare_code_response,
)

SOURCE = "backend/src/auth/service.py"
ORIGINAL = "def authenticate(token):\n    return False\n"
FIXED = "def authenticate(token):\n    return token == 'ok'\n"


def git(path, *arguments, data=None):
    return subprocess.run(["git", *arguments], cwd=path, input=data, capture_output=True,
                          check=True).stdout


@pytest.fixture
def source_contract(tmp_path, monkeypatch):
    original_init = test_worker_pipeline.init_repo

    def with_source(path):
        original_init(path)
        git(path, "config", "core.autocrlf", "false")
        (path / SOURCE).write_bytes(ORIGINAL.encode())
        (path / "backend/src/auth/other.py").write_bytes(b"private = 1\n")
        (path / "large.txt").write_bytes(b"x" * 8193)
        (path / "binary.bin").write_bytes(b"\x00\xff\x01")
        (path / ".env").write_text("EXAMPLE_ONLY=not-a-real-secret\n", encoding="utf-8")
        git(path, "add", "--all")
        link_blob = git(path, "hash-object", "-w", "--stdin", data=b"../../outside").decode().strip()
        git(path, "update-index", "--add", "--cacheinfo", f"120000,{link_blob},linked.py")
        git(path, "commit", "-m", "Source context fixture")
        return git(path, "rev-parse", "HEAD").decode().strip()

    monkeypatch.setattr(test_worker_pipeline, "init_repo", with_source)
    repository, orchestrator, manager, template = test_worker_pipeline.make_pipeline(tmp_path, security=True)
    checks = ProposalChecks(test_commands=template.worker.test_commands, gates=template.gates)
    return repository, orchestrator, manager, template.worker.agent_id, checks


def request(source_contract, *, paths=(SOURCE,), state=None):
    repository, orchestrator, _, agent, checks = source_contract
    return build_code_request(state=state or orchestrator.state(), agent_id=agent,
                              repository=repository, context_paths=paths, checks=checks)


def response_for(package, *, path=SOURCE, content=FIXED, expected="context"):
    if expected == "context":
        expected = package.request.context_files[0].sha256
    return CodeResponse(request_sha256=package.request_sha256, proposal=CodeProposal(
        task_id=package.request.task_id, base_commit=package.request.base_commit,
        files=(ProposedFile(path=path, content=content, expected_sha256=expected),),
    ))


def prepare(source_contract, package, response, *, state=None, checks=None):
    repository, orchestrator, _, agent, original_checks = source_contract
    return prepare_code_response(package, response, state=state or orchestrator.state(),
                                  agent_id=agent, repository=repository,
                                  checks=checks or original_checks, python_executable=sys.executable)


def test_request_uses_committed_bytes_and_actual_goal_without_mutations(source_contract):
    repository, orchestrator, _, _, _ = source_contract
    before = orchestrator.state().model_dump_json()
    (repository / SOURCE).write_text("dirty working file must not be shared", encoding="utf-8")
    package = request(source_contract)
    item = package.request.context_files[0]
    assert item.content == ORIGINAL
    assert item.sha256 == hashlib.sha256(ORIGINAL.encode()).hexdigest()
    assert package.request.goal == orchestrator.state().tasks["AUTH-001"].goal
    assert package.request.data_class == "internal"
    assert package.request_sha256 == package.request.digest()
    assert CodeRequestPackage.model_validate_json(package.model_dump_json()) == package
    assert orchestrator.state().model_dump_json() == before
    assert (repository / SOURCE).read_text() == "dirty working file must not be shared"


@pytest.mark.parametrize("path", [".env", ".env.local", "credentials.json", "id_rsa", "runtime/data.db",
                                 ".tankai/state.json", ".git/config", "config/private.key"])
def test_credential_and_runtime_context_paths_are_refused(source_contract, path):
    with pytest.raises(ValueError):
        request(source_contract, paths=(path,))


@pytest.mark.parametrize("path", ["large.txt", "binary.bin", "linked.py", "backend", "absent.py"])
def test_context_needs_bounded_regular_utf8_files(source_contract, path):
    with pytest.raises(ValueError):
        request(source_contract, paths=(path,))


def test_context_requires_explicit_read_access_and_distinct_paths(source_contract):
    _, orchestrator, _, agent, _ = source_contract
    state = orchestrator.state()
    state.agents[agent].read_access = ["repository/README.md"]
    with pytest.raises(ValueError, match="read access"):
        request(source_contract, state=state)
    with pytest.raises(ValueError, match="Duplicate"):
        request(source_contract, paths=(SOURCE, SOURCE.upper()))
    with pytest.raises(ValueError, match="one to eight"):
        request(source_contract, paths=())


@pytest.mark.parametrize("changed_field,value", [
    ("goal", "A different task"), ("allowed_paths", ["backend/src/auth/service.py"]),
    ("acceptance_criteria", ["A new acceptance contract"]), ("requires_security_review", False),
    ("dependencies", ["NEW-DEPENDENCY"]),
])
def test_responses_are_rejected_after_task_contract_changes(source_contract, changed_field, value):
    package = request(source_contract)
    state = source_contract[1].state()
    setattr(state.tasks["AUTH-001"], changed_field, value)
    with pytest.raises(ValueError, match="stale"):
        prepare(source_contract, package, response_for(package), state=state)


def test_changed_operator_checks_and_other_request_replies_are_refused(source_contract):
    package = request(source_contract)
    response = response_for(package)
    different = response.model_copy(update={"request_sha256": "0" * 64})
    with pytest.raises(ValueError, match="another request"):
        prepare(source_contract, package, different)
    checks = source_contract[4].model_copy(deep=True)
    checks.gates.review_commands[0].timeout_seconds += 1
    with pytest.raises(ValueError, match="stale"):
        prepare(source_contract, package, response, checks=checks)


def test_recomputed_hashes_do_not_authorize_fabricated_source_context(source_contract):
    original = request(source_contract)
    fake_bytes = b"fabricated source"
    fake = CodeContextFile(path=SOURCE, content=fake_bytes.decode(),
                           sha256=hashlib.sha256(fake_bytes).hexdigest(),
                           blob_sha=hashlib.sha1(b"blob " + str(len(fake_bytes)).encode() + b"\0" + fake_bytes).hexdigest())
    changed = original.request.model_copy(update={"context_files": (fake,)})
    package = CodeRequestPackage(request=changed, request_sha256=changed.digest())
    with pytest.raises(ValueError, match="stale"):
        prepare(source_contract, package, response_for(package))


def test_context_integrity_and_reply_authority_are_strict(source_contract):
    package = request(source_contract)
    with pytest.raises(ValidationError):
        CodeRequestPackage(request=package.request, request_sha256="0" * 64)
    with pytest.raises(ValidationError):
        CodeResponse.model_validate({**response_for(package).model_dump(mode="json"),
                                     "test_commands": [{"argv": ["true"]}]})
    with pytest.raises(ValidationError):
        CodeResponse.model_validate({**response_for(package).model_dump(mode="json"), "schema_version": True})
    with pytest.raises(ValueError, match="preimage"):
        prepare(source_contract, package, response_for(package, expected="0" * 64))


@pytest.mark.parametrize("expected", [None, hashlib.sha256(b"private = 1\n").hexdigest()])
def test_existing_files_not_shared_in_context_cannot_be_replaced(source_contract, expected):
    package = request(source_contract)
    response = response_for(package, path="backend/src/auth/other.py", expected=expected)
    with pytest.raises(ValueError, match="outside its source context"):
        prepare(source_contract, package, response)


def test_new_files_still_need_the_existing_write_scope(source_contract):
    package = request(source_contract)
    allowed = response_for(package, path="backend/src/auth/new.py", expected=None)
    assert prepare(source_contract, package, allowed).worker.agent_id == package.request.agent_id
    denied = response_for(package, path="other/new.py", expected=None)
    with pytest.raises(ValueError, match="write scope"):
        prepare(source_contract, package, denied)


def test_bound_reply_reaches_real_worker_and_passes_required_tests(source_contract):
    repository, orchestrator, manager, _, _ = source_contract
    package = request(source_contract)
    job = prepare(source_contract, package, response_for(package))
    original_head = manager.repository_head()
    result = WorkerPipelineRunner(orchestrator, manager).run(job)
    assert result.run.state == WorkerRunState.READY_TO_INTEGRATE
    assert (result.workspace.path / SOURCE).read_bytes() == FIXED.encode()
    assert all(item.passed for item in result.run.test_executions)
    assert package.request_sha256 in job.worker.implementation_summary
    assert manager.repository_head() == original_head
    assert (repository / SOURCE).read_bytes() == ORIGINAL.encode()


def test_checkout_transform_is_blocked_instead_of_weakening_preimage_checks(source_contract):
    repository, orchestrator, manager, _, _ = source_contract
    package = request(source_contract)
    job = prepare(source_contract, package, response_for(package))
    git(repository, "config", "core.autocrlf", "true")
    with pytest.raises(WorkerExecutionError) as error:
        WorkerPipelineRunner(orchestrator, manager).run(job)
    record = orchestrator.state().worker_runs[error.value.run_id]
    assert "Proposal preimage mismatch" in record.implementation_executions[-1].summary
    assert not record.test_executions and not record.review_executions
    assert (repository / SOURCE).read_bytes() == ORIGINAL.encode()


def test_cli_round_trip_is_local_and_preserves_state_and_existing_files(source_contract, tmp_path, capsys):
    repository, orchestrator, _, agent, checks = source_contract
    checks_path = tmp_path / "checks.json"
    checks_path.write_text(checks.model_dump_json(), encoding="utf-8")
    request_path, response_path, output = (tmp_path / name for name in ("request.json", "response.json", "job.json"))
    state_path = tmp_path / "state.json"
    before = state_path.read_bytes()
    command = ["--state", str(state_path), "prepare-code-request", "--agent", agent,
               "--repository", str(repository), "--context-file", SOURCE,
               "--checks", str(checks_path), "--output", str(request_path)]
    assert main(command) == 0
    metadata = json.loads(capsys.readouterr().out)
    assert metadata["request_prepared"] and not metadata["model_called"] and not metadata["executed"]
    package = CodeRequestPackage.model_validate_json(request_path.read_text(encoding="utf-8"))
    response_path.write_text(response_for(package).model_dump_json(), encoding="utf-8")
    arguments = ["--state", str(state_path), "prepare-code-response", "--agent", agent,
                 "--repository", str(repository), "--request", str(request_path),
                 "--response", str(response_path), "--checks", str(checks_path),
                 "--output", str(output), "--python-executable", sys.executable]
    assert main(arguments) == 0
    metadata = json.loads(capsys.readouterr().out)
    assert metadata["prepared"] and not metadata["model_called"] and not metadata["submitted"]
    WorkerPipelineJob.model_validate_json(output.read_text(encoding="utf-8"))
    assert state_path.read_bytes() == before and not orchestrator.state().worker_runs
    preserved = request_path.read_bytes(), output.read_bytes()
    assert main(command) == 2 and main(arguments) == 2
    assert (request_path.read_bytes(), output.read_bytes()) == preserved


def test_response_schema_is_available_without_state(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["code-response-schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["additionalProperties"] is False
    assert "request_sha256" in schema["required"]
    assert not (tmp_path / ".tankai").exists()
