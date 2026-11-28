"""Local code-agent work orders and bound replies; no model or network client."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .code_proposal import (
    CodeProposal, ProposedFile, ProposalChecks, active_code_contract, prepare_code_proposal,
)
from .models import AgentSpec, ProjectState, TaskSpec, WorkerPipelineJob
from .orchestrator import path_matches_scope
from .worker import render_command

MAX_CONTEXT_FILE_BYTES = 8192
MAX_CONTEXT_BYTES = 32768
MAX_REQUEST_BYTES = 65536


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class _Versioned(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Protocol version must be an integer")
        return value


class CodeContextFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1, max_length=240)
    blob_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str = Field(max_length=MAX_CONTEXT_FILE_BYTES)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return ProposedFile.safe_path(value)

    @model_validator(mode="after")
    def valid_bytes(self) -> "CodeContextFile":
        data = self.content.encode("utf-8")
        blob = b"blob " + str(len(data)).encode("ascii") + b"\0" + data
        if "\x00" in self.content or len(data) > MAX_CONTEXT_FILE_BYTES:
            raise ValueError("Context must be bounded UTF-8 text")
        if hashlib.sha256(data).hexdigest() != self.sha256 or hashlib.sha1(blob).hexdigest() != self.blob_sha:
            raise ValueError("Context bytes do not match their hashes")
        return self


class CodeRequest(_Versioned):
    task_id: str = Field(min_length=1, max_length=120)
    agent_id: str = Field(min_length=1, max_length=120)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    goal: str = Field(min_length=1, max_length=20_000)
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    required_tests: tuple[str, ...] = Field(min_length=1)
    task_write_scopes: tuple[str, ...]
    agent_write_scopes: tuple[str, ...]
    denied_write_scopes: tuple[str, ...]
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checks_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_class: Literal["public", "internal"] = "internal"
    context_files: tuple[CodeContextFile, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def bounded_context(self) -> "CodeRequest":
        paths = [item.path.casefold() for item in self.context_files]
        if len(paths) != len(set(paths)):
            raise ValueError("Duplicate context paths")
        if sum(len(item.content.encode("utf-8")) for item in self.context_files) > MAX_CONTEXT_BYTES:
            raise ValueError("Context exceeds total byte limit")
        raw = json.dumps(self.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("Code request exceeds size limit")
        return self

    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class CodeRequestPackage(_Versioned):
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request: CodeRequest

    @model_validator(mode="after")
    def valid_binding(self) -> "CodeRequestPackage":
        if self.request_sha256 != self.request.digest():
            raise ValueError("Code request digest mismatch")
        return self


class CodeResponse(_Versioned):
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal: CodeProposal


class CodeResponseSubmission(_Versioned):
    """Owner/admin handoff; checks are operator input, never agent response fields."""

    package: CodeRequestPackage
    response: CodeResponse
    checks: ProposalChecks
    python_executable: str = Field(default="python3", min_length=1, max_length=500)

    @model_validator(mode="after")
    def bound_response(self) -> "CodeResponseSubmission":
        if self.response.request_sha256 != self.package.request_sha256:
            raise ValueError("Queued code response belongs to another request")
        return self


class QueuedCodeResponsePayload(_Versioned):
    kind: Literal["code_response"] = "code_response"
    pipeline: WorkerPipelineJob
    code_response: CodeResponseSubmission


def prepare_initial_code_submission(
    submission: CodeResponseSubmission, *, state: ProjectState, repository: str | Path,
) -> WorkerPipelineJob:
    """Validate the original handoff; an already started run needs explicit recovery."""
    submission = CodeResponseSubmission.model_validate(submission.model_dump(mode="json"))
    request = submission.package.request
    if state.current_commit != request.base_commit:
        raise ValueError("Code response source is no longer the confirmed project commit")
    task, _ = active_code_contract(state, request.agent_id)
    if task.worker_run_id is not None:
        raise ValueError("Initial code response already has a worker run; use its recovery or repair path")
    return prepare_code_response(
        submission.package, submission.response, state=state, agent_id=request.agent_id,
        repository=repository, checks=submission.checks, python_executable=submission.python_executable,
    )


class _GitSnapshot:
    def __init__(self, repository: str | Path, revision: str):
        self.repository = Path(repository).resolve()
        self.revision = revision
        if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
            raise ValueError("Context needs an exact source commit")
        if self.run("cat-file", "-t", revision).strip() != b"commit":
            raise ValueError("Context source is not a commit")
        root = Path(self.run("rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
        if root != self.repository:
            raise ValueError("Context needs the registered repository root")

    def run(self, *arguments: str) -> bytes:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
        try:
            result = subprocess.run(
                ["git", "--no-replace-objects", "-C", str(self.repository), *arguments],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Local Git context is unavailable") from exc
        if result.returncode:
            raise ValueError("Local Git context is unavailable")
        return result.stdout

    def entry(self, path: str) -> tuple[str, str, str] | None:
        path = ProposedFile.safe_path(path)
        raw = self.run("ls-tree", "-z", self.revision, "--", path).rstrip(b"\0")
        if not raw:
            return None
        metadata, _, name = raw.partition(b"\t")
        if name.decode("utf-8") != path:
            raise ValueError("Context path is not an exact tracked file")
        mode, kind, sha = metadata.decode("ascii").split()
        return mode, kind, sha

    def read_file(self, path: str) -> CodeContextFile:
        entry = self.entry(path)
        if entry is None or entry[0] not in {"100644", "100755"} or entry[1] != "blob":
            raise ValueError("Context needs a tracked regular source file")
        size = int(self.run("cat-file", "-s", entry[2]).strip())
        if size > MAX_CONTEXT_FILE_BYTES:
            raise ValueError("Context file exceeds byte limit")
        raw = self.run("cat-file", "blob", entry[2])
        if len(raw) != size:
            raise ValueError("Context file size changed")
        return CodeContextFile(path=path, blob_sha=entry[2],
                               sha256=hashlib.sha256(raw).hexdigest(), content=raw.decode("utf-8"))


def _readable_context_path(path: str, read_access: Sequence[str]) -> str:
    path = ProposedFile.safe_path(path)
    parts = tuple(part.casefold() for part in path.split("/"))
    protected = {".git", ".tankai", ".npmrc", ".pypirc", "id_rsa", "id_ed25519",
                 "credentials", "credentials.json", "secrets.json"}
    if any(part in protected or part == ".env" or part.startswith(".env.") for part in parts):
        raise ValueError("Credential and runtime paths cannot enter code context")
    if Path(path).suffix.casefold() in {".pem", ".key", ".p12", ".pfx", ".db", ".sqlite", ".sqlite3", ".jsonl"}:
        raise ValueError("Credential and runtime files cannot enter code context")
    if not any(scope.startswith("repository/") and path_matches_scope(path, scope[len("repository/"):])
               for scope in read_access):
        raise ValueError("Code context is outside the agent read access")
    return path


def build_code_request(
    *, state: ProjectState, agent_id: str, repository: str | Path,
    context_paths: Sequence[str], checks: ProposalChecks,
    data_class: Literal["public", "internal"] = "internal",
) -> CodeRequestPackage:
    task, agent = active_code_contract(state, agent_id)
    return _request_for_contract(task=task, agent=agent, repository=repository,
                                 context_paths=context_paths, checks=checks, data_class=data_class)


def _request_for_contract(
    *, task: TaskSpec, agent: AgentSpec, repository: str | Path,
    context_paths: Sequence[str], checks: ProposalChecks,
    data_class: Literal["public", "internal"], context_commit: str | None = None,
) -> CodeRequestPackage:
    """Share the same context bounds; repair callers must validate their journal first."""
    checks = ProposalChecks.model_validate(checks.model_dump(mode="json"))
    if task.base_commit != agent.base_commit:
        raise ValueError("Task and agent source revisions differ")
    required = tuple(dict.fromkeys([*task.required_tests, *agent.required_tests]))
    if not required or not set(required).issubset({render_command(c) for c in checks.test_commands}):
        raise ValueError("Code request must preserve required task tests")
    if not 1 <= len(context_paths) <= 8:
        raise ValueError("Code context needs one to eight explicit paths")
    paths = [_readable_context_path(path, agent.read_access) for path in context_paths]
    if len({path.casefold() for path in paths}) != len(paths):
        raise ValueError("Duplicate context paths")
    snapshot = _GitSnapshot(repository, context_commit or task.base_commit)
    fields = ("task_id", "goal", "base_commit", "assigned_agent_id", "allowed_paths",
              "denied_paths", "required_tests", "acceptance_criteria", "requires_security_review",
              "dependencies", "capability_id", "capability_action", "affected_components")
    agent_fields = ("agent_id", "task_id", "base_commit", "role", "allowed_paths", "denied_paths",
                    "read_access", "required_tests", "acceptance_criteria", "contract_version", "cycle_id")
    contract = {"task": task.model_dump(mode="json", include=set(fields)),
                "agent": agent.model_dump(mode="json", include=set(agent_fields))}
    request = CodeRequest(
        task_id=task.task_id, agent_id=agent.agent_id, base_commit=task.base_commit, goal=task.goal,
        acceptance_criteria=tuple(dict.fromkeys([*task.acceptance_criteria, *agent.acceptance_criteria])),
        required_tests=required, task_write_scopes=tuple(task.allowed_paths),
        agent_write_scopes=tuple(agent.allowed_paths),
        denied_write_scopes=tuple(dict.fromkeys([*task.denied_paths, *agent.denied_paths])),
        contract_sha256=_digest(contract), checks_sha256=_digest(checks.model_dump(mode="json")),
        data_class=data_class, context_files=tuple(snapshot.read_file(path) for path in sorted(paths)),
    )
    return CodeRequestPackage(request_sha256=request.digest(), request=request)


def prepare_code_response(
    package: CodeRequestPackage, response: CodeResponse, *, state: ProjectState,
    agent_id: str, repository: str | Path, checks: ProposalChecks,
    python_executable: str = "python3",
) -> WorkerPipelineJob:
    package = CodeRequestPackage.model_validate(package.model_dump(mode="json"))
    response = CodeResponse.model_validate(response.model_dump(mode="json"))
    request = package.request
    if request.agent_id != agent_id or response.request_sha256 != package.request_sha256:
        raise ValueError("Code response belongs to another request or agent")
    current = build_code_request(
        state=state, agent_id=agent_id, repository=repository, checks=checks,
        context_paths=[item.path for item in request.context_files], data_class=request.data_class,
    )
    if current.request_sha256 != package.request_sha256:
        raise ValueError("Code request contract or checks are stale")
    context = {item.path: item for item in request.context_files}
    snapshot = _GitSnapshot(repository, request.base_commit)
    for change in response.proposal.files:
        if change.path in context:
            if change.expected_sha256 != context[change.path].sha256:
                raise ValueError("Response preimage does not match its source context")
        elif (change.expected_sha256 is not None or snapshot.entry(change.path) is not None
              or any(change.path.casefold() == path.casefold() for path in context)):
            raise ValueError("Response cannot replace a file outside its source context")
    job = prepare_code_proposal(response.proposal, state=state, agent_id=agent_id,
                                checks=checks, python_executable=python_executable)
    job.worker.implementation_summary = (
        f"Code request {package.request_sha256}; proposal {response.proposal.digest()}"
    )
    return job
