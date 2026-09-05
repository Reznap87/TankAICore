# Local Qwen2.5 Coder runtime

TankAICore can use a local GGUF model through its existing OpenAI-compatible provider adapter. The local runtime is isolated in `docker-compose.local-llm.yml` so the default production compose file remains unchanged.

## Model

- Model: `Qwen2.5-Coder-7B-Instruct`
- Quantization: `Q4_K_M`
- GGUF source: `bartowski/Qwen2.5-Coder-7B-Instruct-GGUF`
- Default model URL: `https://huggingface.co/bartowski/Qwen2.5-Coder-7B-Instruct-GGUF/resolve/main/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf`
- Runtime: `llama.cpp` OpenAI-compatible server
- API model alias: `qwen2.5-coder-7b-instruct`

The model volume is persistent. On the first start, `llama.cpp` downloads the GGUF into the named Docker volume; later restarts reuse the local file.

## Start

From the TankAICore repository:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.local-llm.yml \
  up -d --build
```

The model is about 4.68 GB, so the first start requires enough free disk space and can take longer while the model is downloaded and loaded.

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

Watch model startup/download logs:

```bash
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
```

## Stop

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.local-llm.yml \
  down
```

The named model volume is retained. Remove it only if the downloaded model should also be deleted.
