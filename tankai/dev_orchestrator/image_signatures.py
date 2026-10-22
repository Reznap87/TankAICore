"""Operator-owned Cosign identity policy for immutable OCI worker images.

Trust never comes from a submitted job. Cosign verifies the signature, Fulcio
identity and transparency evidence; this adapter additionally binds its JSON
claims to the exact image passed to the runtime. No result is cached.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .process_control import run_bounded_process


class ImageSignatureError(RuntimeError):
    pass


_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
_REPOSITORY = re.compile(r"[a-z0-9][a-z0-9.:-]*/[a-z0-9._/-]+")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ImageSignatureError("Doppelter Schlüssel in Signaturdaten")
        result[key] = value
    return result


def _regular_file(path: Path, *, maximum: int) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise ImageSignatureError("Signaturdatei benötigt einen absoluten regulären Pfad")
    try:
        with path.open("rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise ImageSignatureError("Signaturdatei ist unzulässig oder zu groß")
            content = handle.read(maximum + 1)
        if len(content) > maximum:
            raise ImageSignatureError("Signaturdatei ist zu groß")
        return content
    except OSError as exc:
        raise ImageSignatureError("Signaturdatei ist nicht lesbar") from exc


def _binary_hash(path: Path) -> str:
    if not path.is_absolute() or path.is_symlink():
        raise ImageSignatureError("Signaturprüfer benötigt einen absoluten regulären Pfad")
    try:
        with path.open("rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 256_000_000:
                raise ImageSignatureError("Signaturprüfer ist unzulässig oder zu groß")
            digest = hashlib.sha256()
            size = 0
            while chunk := handle.read(1_048_576):
                size += len(chunk)
                if size > 256_000_000:
                    raise ImageSignatureError("Signaturprüfer ist zu groß")
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise ImageSignatureError("Signaturprüfer ist nicht lesbar") from exc


@dataclass(frozen=True)
class SignerRule:
    repository: str
    identity: str
    issuer: str


@dataclass(frozen=True)
class VerifiedImage:
    image: str
    digest: str
    signer: str
    issuer: str


class CosignImageVerifier:
    def __init__(self, policy_path: str | Path):
        self.policy_path = Path(policy_path)

    @classmethod
    def from_env(cls) -> "CosignImageVerifier":
        path = os.environ.get("TANKAI_WORKER_IMAGE_TRUST_POLICY", "")
        if not path:
            raise ImageSignatureError("Worker-Images benötigen eine Betreiber-Signaturrichtlinie")
        return cls(path)

    def verify(self, image: str, *, workspace: Path,
               cancellation_check: Callable[[], None] | None = None) -> VerifiedImage:
        if cancellation_check:
            cancellation_check()
        repository, separator, digest = image.partition("@")
        if not separator or not _REPOSITORY.fullmatch(repository) or not _DIGEST.fullmatch(digest):
            raise ImageSignatureError("Signierte Worker benötigen eine vollständige OCI-Referenz mit Digest")
        root = workspace.resolve()
        if self.policy_path.resolve().is_relative_to(root):
            raise ImageSignatureError("Die Signaturrichtlinie darf nicht aus dem Worker-Workspace stammen")
        try:
            policy = json.loads(_regular_file(self.policy_path, maximum=65_536), object_pairs_hook=_object)
            if (set(policy) != {"schema_version", "cosign_binary", "cosign_sha256", "rules"}
                    or type(policy["schema_version"]) is not int or policy["schema_version"] != 1
                    or not isinstance(policy["rules"], list) or not 1 <= len(policy["rules"]) <= 32):
                raise ValueError
            binary = Path(policy["cosign_binary"])
            expected_hash = policy["cosign_sha256"]
            if not isinstance(expected_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
                raise ValueError
            if binary.resolve().is_relative_to(root):
                raise ImageSignatureError("Der Signaturprüfer darf nicht aus dem Worker-Workspace stammen")
            rules = []
            for raw in policy["rules"]:
                if set(raw) != {"repository", "identity", "issuer"}:
                    raise ValueError
                rule = SignerRule(**raw)
                if (not all(isinstance(v, str) and 0 < len(v) <= 1000 and not any(ord(c) < 33 for c in v)
                            for v in (rule.repository, rule.identity, rule.issuer))
                        or not _REPOSITORY.fullmatch(rule.repository)
                        or not rule.issuer.startswith("https://")
                        or not (rule.identity.startswith("https://") or re.fullmatch(
                            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", rule.identity))):
                    raise ValueError
                rules.append(rule)
            matching = [r for r in rules if r.repository == repository]
            if len(matching) != 1:
                raise ImageSignatureError("Keine eindeutige Signaturfreigabe für dieses Worker-Image")
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise ImageSignatureError("Ungültige Betreiber-Signaturrichtlinie") from exc
        if _binary_hash(binary) != expected_hash:
            raise ImageSignatureError("Der Signaturprüfer stimmt nicht mit der freigegebenen Prüfsumme überein")
        rule = matching[0]
        # Do not inherit provider credentials, Docker auth, proxy/TLS overrides,
        # OIDC tokens or workspace-controlled configuration into the verifier.
        with tempfile.TemporaryDirectory(prefix="tankai-cosign-") as directory:
            env = {"PATH": os.defpath, "HOME": directory, "USERPROFILE": directory}
            if os.name == "nt" and "SystemRoot" in os.environ:
                env["SystemRoot"] = os.environ["SystemRoot"]
            try:
                checked = run_bounded_process(
                    [str(binary), "verify", "--check-claims=true", "--output=json",
                     "--certificate-identity=" + rule.identity,
                     "--certificate-oidc-issuer=" + rule.issuer, image],
                    cwd=directory, env=env, timeout_seconds=45, output_limit=100_000,
                    stderr_to_stdout=False, cancellation_check=cancellation_check,
                )
            except OSError as exc:
                raise ImageSignatureError("Signaturprüfung konnte nicht gestartet werden") from exc
        if checked.timed_out or checked.returncode != 0:
            raise ImageSignatureError("Worker-Imagesignatur nicht erfolgreich geprüft")
        try:
            claims = json.loads(checked.output, object_pairs_hook=_object)
            if not isinstance(claims, list) or not 1 <= len(claims) <= 32:
                raise ValueError
            for claim in claims:
                critical = claim["critical"]
                reference = critical["identity"]["docker-reference"]
                kind = critical["type"]
                valid_reference = (
                    kind == "cosign container image signature" and reference in {repository, image}
                ) or (kind == "https://sigstore.dev/cosign/sign/v1" and reference == image)
                if (not valid_reference or critical["image"]["docker-manifest-digest"] != digest):
                    raise ValueError
        except (TypeError, ValueError, KeyError) as exc:
            raise ImageSignatureError("Signaturnachweis passt nicht zur angeforderten Image-Referenz") from exc
        if cancellation_check:
            cancellation_check()
        return VerifiedImage(image=image, digest=digest, signer=rule.identity, issuer=rule.issuer)
