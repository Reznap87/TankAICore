"""Compile bounded agent file proposals into the existing worker contract.

This module neither calls a model nor submits or executes a job. Check commands
and permissions come from the operator and persisted task, never the proposal.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import (
    AgentStatus, CommandSpec, GateJob, ProjectState, TaskState,
    WorkerIsolationSpec, WorkerJob, WorkerPipelineJob,
)
from .orchestrator import path_matches_scope
from .worker import render_command

MAX_PROPOSAL_BYTES = 8192
_PAYLOAD_ENV = "TANKAI_CODE_PROPOSAL_B64"


class ProposedFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=240)
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content: str = Field(max_length=MAX_PROPOSAL_BYTES)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value):
            raise ValueError("Proposal needs a portable relative file path")
        reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                    *(f"lpt{i}" for i in range(1, 10))}
        for part in value.split("/"):
            if (part in {".", ".."} or part.endswith(".") or part.casefold() == ".git"
                    or part.split(".", 1)[0].casefold() in reserved):
                raise ValueError("Unsafe proposal file path")
        return value

    @field_validator("content")
    @classmethod
    def text_content(cls, value: str) -> str:
        if "\x00" in value or len(value.encode("utf-8")) > MAX_PROPOSAL_BYTES:
            raise ValueError("Proposal content must be bounded UTF-8 text")
        return value


class CodeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=1, max_length=120)
    base_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: tuple[ProposedFile, ...] = Field(min_length=1, max_length=8)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Proposal version must be an integer")
        return value

    @model_validator(mode="after")
    def bounded_distinct_files(self) -> "CodeProposal":
        paths = [item.path.casefold() for item in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("Duplicate proposal paths")
        if any(a.startswith(b + "/") for a in paths for b in paths if a != b):
            raise ValueError("Proposal paths overlap as file and directory")
        if len(self.canonical_bytes()) > MAX_PROPOSAL_BYTES:
            raise ValueError("Encoded proposal exceeds transport limit")
        return self

    def canonical_bytes(self) -> bytes:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False,
                          sort_keys=True, separators=(",", ":")).encode("utf-8")

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class ProposalChecks(BaseModel):
    """Operator-controlled checks; not part of model-generated output."""

    model_config = ConfigDict(extra="forbid")
    test_commands: list[CommandSpec] = Field(min_length=1)
    gates: GateJob
    isolation: WorkerIsolationSpec | None = None


# Self-contained receiver: approved worker images only need Python's stdlib.
# Payload travels in the existing command environment, not the logged argv.
# Git metadata remains masked in containers. This only writes ordinary files.
_APPLY_SCRIPT = r'''
import base64, hashlib, json, os, stat, sys
from pathlib import Path
raw = base64.b64decode(os.environ.pop("TANKAI_CODE_PROPOSAL_B64"), validate=True)
if len(raw) > 8192 or hashlib.sha256(raw).hexdigest() != sys.argv[1]:
    raise RuntimeError("Proposal digest mismatch")
proposal = json.loads(raw)
root = Path.cwd().resolve()
def target(change):
    path = root.joinpath(*change["path"].split("/"))
    if not path.resolve().is_relative_to(root):
        raise RuntimeError("Proposal leaves workspace")
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise RuntimeError("Proposal links are forbidden")
    return path
def preflight(change):
    path = target(change)
    expected = change["expected_sha256"]
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1048576:
            raise RuntimeError("Proposal target is not a bounded regular file")
        with path.open("rb") as stream:
            before = stream.read(1048577)
        if expected is None or hashlib.sha256(before).hexdigest() != expected:
            raise RuntimeError("Proposal preimage mismatch")
    elif expected is not None:
        raise RuntimeError("Proposal preimage missing")
    return path
for change in proposal["files"]:
    preflight(change)
for change in proposal["files"]:
    path = preflight(change)
    path.parent.mkdir(parents=True, exist_ok=True)
    target(change)
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if change["expected_sha256"] is None:
        flags |= os.O_CREAT | os.O_EXCL
    with os.fdopen(os.open(path, flags, 0o600), "r+b") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("Proposal target changed")
        if change["expected_sha256"] is not None:
            before = stream.read(1048577)
            if hashlib.sha256(before).hexdigest() != change["expected_sha256"]:
                raise RuntimeError("Proposal preimage changed")
        stream.seek(0)
        stream.write(change["content"].encode("utf-8"))
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
print(json.dumps({"proposal_sha256": sys.argv[1], "files_written": len(proposal["files"])}))
'''


def load_proposal_json(path: str | Path, *, limit: int = MAX_PROPOSAL_BYTES) -> object:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key in proposal input")
            result[key] = value
        return result

    with Path(path).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Proposal input exceeds size limit")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)


def prepare_code_proposal(
    proposal: CodeProposal, *, state: ProjectState, agent_id: str,
    checks: ProposalChecks, python_executable: str = "python3",
) -> WorkerPipelineJob:
    """Return an unsubmitted v1 job without touching repository or state."""
    # Revalidate even direct callers using model_construct/model_copy.
    try:
        proposal = CodeProposal.model_validate(proposal.model_dump(mode="json"))
        checks = ProposalChecks.model_validate(checks.model_dump(mode="json"))
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid proposal or check contract") from exc
    agent = state.agents.get(agent_id)
    task = state.tasks.get(agent.task_id) if agent else None
    if agent is None or task is None or task.assigned_agent_id != agent_id:
        raise ValueError("Proposal needs an assigned task and agent")
    if agent.status != AgentStatus.ACTIVE or task.state != TaskState.ACTIVE:
        raise ValueError("Proposal task and agent must be active")
    if proposal.task_id != task.task_id or not (
        proposal.base_commit == task.base_commit == agent.base_commit
    ):
        raise ValueError("Proposal task or source revision does not match")
    required = set(task.required_tests) | set(agent.required_tests)
    if not required or not required.issubset({render_command(c) for c in checks.test_commands}):
        raise ValueError("Proposal must preserve all required task tests")
    for change in proposal.files:
        for contract in (task, agent):
            allowed = any(path_matches_scope(change.path, p) for p in contract.allowed_paths)
            denied = any(path_matches_scope(change.path.casefold(), p.casefold())
                         for p in contract.denied_paths)
            if not allowed or denied:
                raise ValueError("Proposal file is outside the assigned write scope")
    digest = proposal.digest()
    command = CommandSpec(
        argv=[python_executable, "-I", "-S", "-c", _APPLY_SCRIPT, digest],
        timeout_seconds=30,
        env={_PAYLOAD_ENV: base64.b64encode(proposal.canonical_bytes()).decode("ascii")},
    )
    return WorkerPipelineJob(
        worker=WorkerJob(
            agent_id=agent_id,
            implementation_summary=f"File proposal {digest} for task {task.task_id}",
            commit_message=f"Implement task {task.task_id}",
            implementation_commands=[command],
            test_commands=checks.test_commands,
        ),
        gates=checks.gates,
        isolation=checks.isolation,
    )
