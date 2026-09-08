import hashlib
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]
LOCAL_COMPOSE = ROOT / "docker-compose.local-llm.yml"
PRODUCTION_COMPOSE = ROOT / "docker-compose.yml"
MODEL_INIT = ROOT / "scripts" / "local_qwen_model_init.sh"

LLAMA_IMAGE = (
    "ghcr.io/ggml-org/llama.cpp:server@"
    "sha256:7fa75431b8a78f9528cab4aaf65e8ae3e13da546a3cc7221247ca81bab864d84"
)
MODEL_REVISION = "1f629da0c8bed16b9e50cee91c70693650e66c35"
MODEL_SHA256 = "1664fccab734674a50763490a8c6931b70e3f2f8ec10031b54806d30e5f956b6"


def test_local_runtime_uses_immutable_image_and_model_sources() -> None:
    text = LOCAL_COMPOSE.read_text(encoding="utf-8")

    assert f"image: {LLAMA_IMAGE}" in text
    assert f"/resolve/{MODEL_REVISION}/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf" in text
    assert "/resolve/main/" not in text
    assert f'LOCAL_LLM_MODEL_SHA256: "${{LOCAL_LLM_MODEL_SHA256:-{MODEL_SHA256}}}"' in text
    assert "--model-url" not in text

    guide = (ROOT / "docs" / "LOCAL_QWEN25_CODER.md").read_text(encoding="utf-8")
    assert LLAMA_IMAGE in guide
    assert MODEL_REVISION in guide
    assert MODEL_SHA256 in guide


def test_tankai_waits_for_loaded_model_on_internal_network() -> None:
    text = LOCAL_COMPOSE.read_text(encoding="utf-8")

    assert "condition: service_healthy" in text
    assert 'OPENAI_BASE_URL: "http://llama:8080/v1"' in text
    assert "ports:" not in text
    assert 'expose:\n      - "8080"' in text


def test_model_checksum_gate_completes_before_llama_starts() -> None:
    text = LOCAL_COMPOSE.read_text(encoding="utf-8")

    assert "qwen-model-init:" in text
    assert "./scripts/local_qwen_model_init.sh:" in text
    assert "condition: service_completed_successfully" in text
    assert text.index("condition: service_completed_successfully") < text.index(
        "condition: service_healthy"
    )


def _run_model_init(
    tmp_path: Path,
    *,
    downloaded_content: bytes,
    expected_sha256: str,
    cached_content: bytes | None = None,
    model_url: str = "https://models.example.invalid/model.gguf",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    model_path = tmp_path / "models" / "model.gguf"
    model_path.parent.mkdir()
    if cached_content is not None:
        model_path.write_bytes(cached_content)

    source = tmp_path / "download.gguf"
    source.write_bytes(downloaded_content)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "output=\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    --output) output=$2; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "[ -n \"$output\" ]\n"
        "cp -- \"$FAKE_MODEL_SOURCE\" \"$output\"\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_MODEL_SOURCE": str(source),
        "LOCAL_LLM_MODEL_URL": model_url,
        "LOCAL_LLM_MODEL_SHA256": expected_sha256,
        "LOCAL_LLM_MODEL_PATH": str(model_path),
    }
    result = subprocess.run(
        ["/bin/sh", str(MODEL_INIT)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, model_path


def test_model_init_installs_only_a_verified_download(tmp_path: Path) -> None:
    content = b"verified local model fixture\n"
    expected_sha256 = hashlib.sha256(content).hexdigest()

    result, model_path = _run_model_init(
        tmp_path,
        downloaded_content=content,
        expected_sha256=expected_sha256,
    )

    assert result.returncode == 0, result.stderr
    assert model_path.read_bytes() == content
    assert model_path.stat().st_mode & 0o777 == 0o444
    assert "downloaded model verified and installed" in result.stdout
    assert list(model_path.parent.glob("*.part.*")) == []


def test_model_init_rejects_and_cleans_a_bad_download(tmp_path: Path) -> None:
    result, model_path = _run_model_init(
        tmp_path,
        downloaded_content=b"unexpected content",
        expected_sha256=hashlib.sha256(b"expected content").hexdigest(),
    )

    assert result.returncode != 0
    assert "downloaded model SHA-256 mismatch" in result.stderr
    assert not model_path.exists()
    assert list(model_path.parent.glob("*.part.*")) == []


def test_model_init_fails_closed_for_a_tampered_cached_model(tmp_path: Path) -> None:
    expected_content = b"expected cached model"
    result, model_path = _run_model_init(
        tmp_path,
        downloaded_content=expected_content,
        expected_sha256=hashlib.sha256(expected_content).hexdigest(),
        cached_content=b"tampered cached model",
    )

    assert result.returncode != 0
    assert "cached model SHA-256 mismatch" in result.stderr
    assert model_path.read_bytes() == b"tampered cached model"


def test_model_init_rejects_a_non_https_source_before_download(tmp_path: Path) -> None:
    content = b"model from an insecure source"
    result, model_path = _run_model_init(
        tmp_path,
        downloaded_content=content,
        expected_sha256=hashlib.sha256(content).hexdigest(),
        model_url="http://models.example.invalid/model.gguf",
    )

    assert result.returncode != 0
    assert "model URL must use HTTPS" in result.stderr
    assert not model_path.exists()


def test_local_runtime_does_not_change_production_compose_defaults() -> None:
    text = PRODUCTION_COMPOSE.read_text(encoding="utf-8")

    assert "llama:" not in text
    assert "Qwen2.5" not in text
    assert 'TANKAI_DEV_QUEUE_ENABLED: "${TANKAI_DEV_QUEUE_ENABLED:-0}"' in text

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "Validate local Qwen Compose contract" in ci
    assert (
        "docker compose -f docker-compose.yml -f docker-compose.local-llm.yml "
        "config --quiet"
    ) in ci
