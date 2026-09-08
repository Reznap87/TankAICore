#!/bin/sh

set -eu

MODEL_URL=${LOCAL_LLM_MODEL_URL:-}
EXPECTED_SHA256=${LOCAL_LLM_MODEL_SHA256:-}
MODEL_PATH=${LOCAL_LLM_MODEL_PATH:-/models/Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf}
SERVER_BINARY=${LOCAL_LLM_SERVER_BINARY:-/app/llama-server}
START_SERVER=0

fail() {
    printf '%s\n' "local Qwen model init: $*" >&2
    exit 1
}

case "${1:-}" in
    --exec-server)
        START_SERVER=1
        shift
        ;;
    "") ;;
    *) fail "unsupported initializer mode" ;;
esac

case "$MODEL_URL" in
    https://*) ;;
    *) fail "model URL must use HTTPS" ;;
esac

case "$EXPECTED_SHA256" in
    ""|*[!0-9a-fA-F]*) fail "expected SHA-256 must contain exactly 64 hex characters" ;;
esac
[ "${#EXPECTED_SHA256}" -eq 64 ] || \
    fail "expected SHA-256 must contain exactly 64 hex characters"

case "$MODEL_PATH" in
    /*.gguf) ;;
    *) fail "model path must be an absolute GGUF path" ;;
esac
[ ! -L "$MODEL_PATH" ] || fail "model path must not be a symlink"

EXPECTED_SHA256=$(printf '%s' "$EXPECTED_SHA256" | tr '[:upper:]' '[:lower:]')

verify_model() {
    actual_sha256=$(sha256sum -- "$1" | cut -d ' ' -f 1)
    [ "$actual_sha256" = "$EXPECTED_SHA256" ]
}

finish() {
    message=$1
    shift
    printf '%s\n' "local Qwen model init: $message"
    if [ "$START_SERVER" -eq 1 ]; then
        [ -x "$SERVER_BINARY" ] || fail "llama server binary is not executable"
        exec "$SERVER_BINARY" "$@"
    fi
}

if [ -e "$MODEL_PATH" ]; then
    [ -f "$MODEL_PATH" ] || fail "cached model path is not a regular file"
    verify_model "$MODEL_PATH" || fail "cached model SHA-256 mismatch"
    finish "cached model verified" "$@"
    exit 0
fi

temporary_path=$(mktemp "${MODEL_PATH}.part.XXXXXX") || \
    fail "cannot create temporary model file"
cleanup() {
    rm -f -- "$temporary_path"
}
trap cleanup EXIT HUP INT TERM

curl \
    --fail \
    --location \
    --silent \
    --show-error \
    --retry 3 \
    --retry-all-errors \
    --connect-timeout 15 \
    --output "$temporary_path" \
    "$MODEL_URL"

verify_model "$temporary_path" || fail "downloaded model SHA-256 mismatch"
chmod 0444 "$temporary_path"
mv -f -- "$temporary_path" "$MODEL_PATH"
trap - EXIT HUP INT TERM
finish "downloaded model verified and installed" "$@"
