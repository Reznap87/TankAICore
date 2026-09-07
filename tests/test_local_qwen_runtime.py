from pathlib import Path


ROOT = Path(__file__).parents[1]
LOCAL_COMPOSE = ROOT / "docker-compose.local-llm.yml"
PRODUCTION_COMPOSE = ROOT / "docker-compose.yml"

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
