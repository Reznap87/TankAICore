"""Trust is operator-owned; a rejected signature must never start a container."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from tankai.dev_orchestrator import CommandSpec, ContainerRuntimeError, DockerCommandExecutor, WorkerIsolationSpec
from tankai.dev_orchestrator import image_signatures as signatures
from tankai.dev_orchestrator import container_runtime as runtime
from tankai.dev_orchestrator.process_control import BoundedProcessResult, run_bounded_process
from test_container_runtime import make_workspace


REPOSITORY = "ghcr.io/reznap87/tankaicore-worker"
DIGEST = "sha256:" + "a" * 64
IMAGE = REPOSITORY + "@" + DIGEST
IDENTITY = "https://github.com/Reznap87/TankAICore/.github/workflows/worker-image-sign.yml@refs/heads/main"
ISSUER = "https://token.actions.githubusercontent.com"


def claim(*, repository=REPOSITORY, digest=DIGEST):
    return [{"critical": {"type": "cosign container image signature",
                          "identity": {"docker-reference": repository},
                          "image": {"docker-manifest-digest": digest}}}]


@pytest.fixture
def operator(tmp_path, monkeypatch):
    directory = tmp_path / "operator"
    directory.mkdir()
    binary = directory / "cosign"
    binary.write_bytes(b"fake verifier binary; never executed")
    policy = {"schema_version": 1, "cosign_binary": str(binary),
              "cosign_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "rules": [{"repository": REPOSITORY, "identity": IDENTITY, "issuer": ISSUER}]}
    path = directory / "trust.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("TANKAI_WORKER_IMAGE_TRUST_POLICY", str(path))
    calls = []
    def verify(argv, **kwargs):
        calls.append((argv, kwargs))
        return BoundedProcessResult(0, json.dumps(claim()), False)
    monkeypatch.setattr(signatures, "run_bounded_process", verify)
    return path, policy, binary, calls


def test_cosign_checks_exact_identity_digest_and_does_not_inherit_credentials(operator, tmp_path, monkeypatch):
    path, _, binary, calls = operator
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-marker")
    monkeypatch.setenv("DOCKER_AUTH_CONFIG", "private-test-marker")
    monkeypatch.setenv("HTTPS_PROXY", "http://private-test-marker")
    result = signatures.CosignImageVerifier.from_env().verify(IMAGE, workspace=tmp_path / "workspace")
    assert result.image == IMAGE and result.signer == IDENTITY and result.issuer == ISSUER
    argv, options = calls[0]
    assert argv[0] == str(binary) and argv[-1] == IMAGE
    assert "--check-claims=true" in argv
    assert "--certificate-identity=" + IDENTITY in argv
    assert "--certificate-oidc-issuer=" + ISSUER in argv
    assert not any("insecure" in arg or "regexp" in arg or "ignore" in arg for arg in argv)
    assert "private-test-marker" not in json.dumps(options)
    assert options["stderr_to_stdout"] is False and options["timeout_seconds"] == 45
    assert options["env"]["HOME"] == options["cwd"]


@pytest.mark.parametrize("image", ["worker:latest", "sha256:" + "a" * 64,
    "ghcr.io/attacker/worker@" + DIGEST, REPOSITORY + "@sha256:" + "A" * 64,
    IMAGE + "\n--insecure-ignore-tlog"])
def test_untrusted_or_mutable_reference_never_invokes_verifier(operator, tmp_path, image):
    path, _, _, calls = operator
    with pytest.raises(signatures.ImageSignatureError):
        signatures.CosignImageVerifier(path).verify(image, workspace=tmp_path / "workspace")
    assert not calls


def test_binary_tampering_is_rejected_before_execution(operator, tmp_path):
    path, _, binary, calls = operator
    binary.write_bytes(b"tampered")
    with pytest.raises(signatures.ImageSignatureError, match="Prüfsumme"):
        signatures.CosignImageVerifier(path).verify(IMAGE, workspace=tmp_path / "workspace")
    assert not calls


@pytest.mark.parametrize("identity", [IDENTITY, "keyless@projectsigstore.iam.gserviceaccount.com"])
def test_current_cosign_v3_claim_format_and_exact_service_identity(operator, tmp_path, monkeypatch, identity):
    path, policy, _, _ = operator
    policy["rules"][0]["identity"] = identity
    path.write_text(json.dumps(policy), encoding="utf-8")
    modern = claim(repository=IMAGE)
    modern[0]["critical"]["type"] = "https://sigstore.dev/cosign/sign/v1"
    monkeypatch.setattr(signatures, "run_bounded_process", lambda *a, **k:
                        BoundedProcessResult(0, json.dumps(modern), False))
    result = signatures.CosignImageVerifier(path).verify(IMAGE, workspace=tmp_path / "workspace")
    assert result.signer == identity and result.digest == DIGEST


@pytest.mark.parametrize("change", ["duplicate", "unknown", "ambiguous", "workspace", "boolean-version"])
def test_policy_is_strict_and_outside_untrusted_workspace(operator, tmp_path, change):
    path, policy, _, calls = operator
    workspace = tmp_path / "workspace"
    if change == "duplicate":
        path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    else:
        if change == "unknown":
            policy["insecure_ignore_tlog"] = True
        if change == "ambiguous":
            policy["rules"] *= 2
        if change == "boolean-version":
            policy["schema_version"] = True
        if change == "workspace":
            workspace.mkdir()
            path = workspace / "trust.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(signatures.ImageSignatureError):
        signatures.CosignImageVerifier(path).verify(IMAGE, workspace=workspace)
    assert not calls


@pytest.mark.parametrize("code,timed_out,output", [
    (1, False, json.dumps(claim())), (0, True, json.dumps(claim())),
    (0, False, "not-json"), (0, False, "[]"), (0, False, "null"),
    (0, False, json.dumps(claim(digest="sha256:" + "b" * 64))),
    (0, False, json.dumps(claim(repository="ghcr.io/attacker/worker"))),
    (0, False, '[{"critical":{},"critical":{}}]'),
])
def test_failed_or_wrong_claims_never_authorize_image(operator, tmp_path, monkeypatch, code, timed_out, output):
    path, _, _, _ = operator
    monkeypatch.setattr(signatures, "run_bounded_process", lambda *a, **k: BoundedProcessResult(code, output, timed_out))
    with pytest.raises(signatures.ImageSignatureError):
        signatures.CosignImageVerifier(path).verify(IMAGE, workspace=tmp_path / "workspace")


def execute(executor, workspace, **kwargs):
    return executor.execute(workspace, CommandSpec(argv=["python", "-V"]),
                            WorkerIsolationSpec(image=IMAGE, user="10001:10001"),
                            allowed_paths=[], read_only_workspace=True,
                            run_id="signed-worker-test", phase="review", **kwargs)


def test_rootless_signature_requirement_cannot_be_disabled_by_constructor(tmp_path, monkeypatch):
    monkeypatch.delenv("TANKAI_WORKER_IMAGE_TRUST_POLICY", raising=False)
    monkeypatch.setattr(runtime, "run_bounded_process", lambda *a, **k: pytest.fail("Container started unsigned"))
    executor = DockerCommandExecutor(require_rootless=True, require_image_signatures=False)
    with pytest.raises(ContainerRuntimeError, match="Signaturrichtlinie"):
        execute(executor, make_workspace(tmp_path))


def test_signed_commands_revalidate_and_do_not_reuse_previous_success(operator, tmp_path, monkeypatch):
    path, policy, _, calls = operator
    launched = []
    monkeypatch.setattr(runtime, "run_bounded_process", lambda argv, **k:
                        launched.append(argv) or BoundedProcessResult(0, "worker output", False))
    executor = DockerCommandExecutor(require_rootless=True)
    workspace = make_workspace(tmp_path)
    first = execute(executor, workspace)
    assert first.passed and "Image-Signatur verifiziert" in first.summary
    assert launched[0][-3:] == [IMAGE, "python", "-V"]
    assert len(calls) == len(launched) == 1
    policy["rules"][0]["repository"] = "ghcr.io/another/image"
    path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(ContainerRuntimeError):
        execute(executor, workspace)
    assert len(calls) == len(launched) == 1


def test_cancellation_after_signature_blocks_container(operator, tmp_path, monkeypatch):
    checks = []
    def cancel():
        checks.append(1)
        if len(checks) == 2:
            raise RuntimeError("lease lost")
    monkeypatch.setattr(runtime, "run_bounded_process", lambda *a, **k: pytest.fail("Container started after lease loss"))
    with pytest.raises(RuntimeError, match="lease lost"):
        execute(DockerCommandExecutor(require_rootless=True), make_workspace(tmp_path), cancellation_check=cancel)


def test_bounded_json_verifier_can_discard_stderr_without_leaking_it():
    result = run_bounded_process(
        [sys.executable, "-c", 'import sys; print("[]"); sys.stderr.write("private-test-marker")'],
        timeout_seconds=10, stderr_to_stdout=False,
    )
    assert result.returncode == 0 and json.loads(result.output) == []
    assert "private-test-marker" not in result.output
