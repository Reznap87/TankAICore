# Local Qwen2.5 Coder runtime

TankAICore can use a local GGUF model through its existing OpenAI-compatible provider adapter. The local runtime is isolated in `docker-compose.local-llm.yml` so the default production compose file remains unchanged.

## Model

- Model: `Qwen2.5-Coder-7B-Instruct`
- Quantization: `Q4_K_M`
- GGUF source: `bartowski/Qwen2.5-Coder-7B-Instruct-GGUF`
- Default model revision: `1f629da0c8bed16b9e50cee91c70693650e66c35`
- Default model URL: `https://huggingface.co/bartowski/Qwen2.5-Coder-7B-Instruct-GGUF/resolve/1f629da0c8bed16b9e50cee91c70693650e66c35/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf`
- Model SHA-256: `1664fccab734674a50763490a8c6931b70e3f2f8ec10031b54806d30e5f956b6`
- Runtime: `llama.cpp` OpenAI-compatible server, pinned as `ghcr.io/ggml-org/llama.cpp:server@sha256:7fa75431b8a78f9528cab4aaf65e8ae3e13da546a3cc7221247ca81bab864d84`
- API model alias: `qwen2.5-coder-7b-instruct`

The image digest resolves an official multi-architecture OCI index for Linux amd64, arm64 and
s390x. The model revision and image digest are immutable so the default stack cannot silently
change between starts. The model volume is persistent. On the first start, the one-shot
`qwen-model-init` service downloads the GGUF into that volume, verifies the configured SHA-256
and atomically installs the file. Every later Compose start verifies the cached file again.

## Start

From the TankAICore repository:

If the normal TankAI `.env` does not exist yet, create and configure it first:

```bash
cp .env.example .env
```

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.local-llm.yml \
  up -d --build
```

The model is about 4.68 GB, so the first start requires enough free disk space and can take
longer while the model is downloaded, verified and loaded. `llama` starts only after
`qwen-model-init` exits successfully. A partial download, a wrong checksum, a non-HTTPS model
URL or a modified cached file stops the stack before inference starts.

The official server image contains a `/health` check. It reports HTTP 503 while the model is
loading and HTTP 200 only when inference is ready. Compose waits for that healthy state before it
starts TankAICore. The complete startup chain is therefore:

```text
verified model file -> healthy llama.cpp server -> TankAI
```

The `llama` service is only exposed to the internal Compose network. TankAICore reaches it at:

```text
http://llama:8080/v1
```

The override sets:

```dotenv
TANKAI_LLM=openai
OPENAI_API_KEY=local-tankai
OPENAI_MODEL=qwen2.5-coder-7b-instruct
OPENAI_BASE_URL=http://llama:8080/v1
TANKAI_LLM_TIMEOUT_SECONDS=120
```

`OPENAI_API_KEY` is a non-secret placeholder required by the OpenAI client. No external OpenAI request is made when `OPENAI_BASE_URL` points at the local `llama` service.

## Check status

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.local-llm.yml \
  ps
```

Check the integrity initializer and then watch server startup logs:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.local-llm.yml \
  logs qwen-model-init

docker compose \
  -f docker-compose.yml \
  -f docker-compose.local-llm.yml \
  logs -f llama
```

Check the OpenAI-compatible model endpoint from inside the Compose network:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.local-llm.yml \
  exec tankai python - <<'PY'
import json
import urllib.request

with urllib.request.urlopen('http://llama:8080/v1/models', timeout=10) as response:
    print(json.dumps(json.load(response), indent=2))
PY
```

## Resource tuning

The defaults are conservative for a CPU server:

```dotenv
LOCAL_LLM_CTX_SIZE=4096
LOCAL_LLM_PARALLEL=1
LOCAL_LLM_TIMEOUT_SECONDS=120
```

Increase context or parallel slots only when the server has enough RAM. The GGUF itself is about 4.68 GB; runtime memory is higher because model state, KV cache, request buffers, TankAICore and the operating system also consume memory.

To override the model source without editing Compose:

```dotenv
LOCAL_LLM_MODEL_URL=https://example.invalid/model.gguf
LOCAL_LLM_MODEL_SHA256=REPLACE_WITH_EXACT_64_CHARACTER_SHA256
```

URL and checksum must be changed together. The initializer accepts HTTPS only and will not start
`llama.cpp` unless the downloaded or cached file matches the configured checksum. An override
deliberately leaves the pinned default contract; verify the replacement artifact's origin before
starting it.

## Stop

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.local-llm.yml \
  down
```

The named model volume is retained. Remove it only if the downloaded model should also be deleted.
