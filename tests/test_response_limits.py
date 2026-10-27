"""Real SDKs and counted in-process response streams; no upstream access."""
from __future__ import annotations

import importlib
import gzip
import json
import subprocess
import sys
from types import SimpleNamespace

import httpx2
import pytest

from tankai.core.llm import AnthropicLLM, BudgetedLLM, LLMCallBudget, OpenAILLM
from tankai.core.monetary_budget import MonetaryBudget, PilotPolicy
from tankai.core.request_dispatch import DispatchDenied, MonetaryDispatch
from tankai.core.request_pricing import TextTariff


NOW = 1_790_000_000
BODY_LIMIT = 256_000


class CountedStream(httpx2.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read_bytes = 0
        self.close_calls = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.read_bytes += len(chunk)
            yield chunk

    def close(self):
        self.close_calls += 1


@pytest.fixture(params=["openai", "anthropic"])
def protected(tmp_path, monkeypatch, request):
    provider, model = request.param, "offline-model-v1"
    endpoint = "https://reserved.example.test/v1"
    path = tmp_path / "budget.sqlite"
    with MonetaryBudget.create(path, PilotPolicy("owner", NOW, NOW + 86_400),
                               clock=lambda: NOW) as ledger:
        ledger.begin_job("job", owner_id="owner")
        tariff = TextTariff("offline-tariff", provider, model, NOW, NOW + 86_400,
            "https://reserved.example.test/pricing", "EUR", "1", "1", "0",
            "1", "0", "0", "0", None, True)
        dispatcher = MonetaryDispatch(ledger, owner_id="owner", job_id="job",
            tariffs={(provider, model): tariff}, endpoints={(provider, model): endpoint},
            input_token_bound=lambda request: 1000, clock=lambda: NOW)
        fixture = SimpleNamespace(provider=provider, ledger=ledger, dispatcher=dispatcher,
                                  sent=[], response=None)

        def intercept(transport, outgoing):
            with MonetaryBudget(path, clock=lambda: NOW) as independent:
                assert independent.snapshot()["held_micro_eur"] == 2000
            fixture.sent.append(outgoing)
            return fixture.response(outgoing)

        monkeypatch.setattr(httpx2.HTTPTransport, "handle_request", intercept)
        if provider == "openai":
            adapter = OpenAILLM(model=model, api_key="synthetic-offline-key", base_url=endpoint,
                                max_tokens=1000, max_retries=3)
        else:
            monkeypatch.setenv("ANTHROPIC_BASE_URL", endpoint)
            adapter = AnthropicLLM(model=model, api_key="synthetic-offline-key",
                                   max_tokens=1000, max_retries=3)
        try:
            fixture.wrapped = BudgetedLLM(adapter, LLMCallBudget(40, monetary_dispatch=dispatcher))
            yield fixture
        finally:
            adapter.client.close()


def reply(provider, text="offline answer"):
    if provider == "openai":
        body = {"id":"offline", "object":"chat.completion", "created":NOW,
            "model":"offline-model-v1", "choices":[{"index":0, "finish_reason":"stop",
                "message":{"role":"assistant", "content":text}}]}
    else:
        body = {"id":"offline", "type":"message", "role":"assistant",
            "model":"offline-model-v1", "content":[{"type":"text", "text":text}],
            "stop_reason":"end_turn", "stop_sequence":None,
            "usage":{"input_tokens":10, "output_tokens":2}}
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def install_stream(fixture, stream, headers=None):
    fixture.response = lambda outgoing: httpx2.Response(200, request=outgoing,
        headers={"content-type":"application/json", **(headers or {})}, stream=stream)


def assert_stopped(fixture):
    assert len(fixture.sent) == 1
    assert fixture.dispatcher.uncertain_upstream
    with pytest.raises(DispatchDenied, match="uncertain upstream"):
        fixture.wrapped.complete("must not retry")
    assert len(fixture.sent) == 1
    state = fixture.ledger.snapshot()
    assert state["attempts"] == state["unresolved_attempts"] == 1
    assert state["held_micro_eur"] == 2000
    assert state["active_jobs"] == 1


@pytest.mark.parametrize("length_header", [None, "1"])
def test_oversized_stream_stops_before_json_parse_and_does_not_retry(protected, length_header):
    payload = reply(protected.provider)
    chunks = [payload, b" " * BODY_LIMIT, b" " * 17]
    stream = CountedStream(chunks)
    headers = {} if length_header is None else {"content-length":length_header}
    install_stream(protected, stream, headers)
    sdk = importlib.import_module(protected.provider)
    with pytest.raises((DispatchDenied, sdk.APIConnectionError)):
        protected.wrapped.complete("bounded response")
    assert stream.read_bytes == len(payload) + BODY_LIMIT
    assert stream.close_calls >= 1
    assert_stopped(protected)


def test_declared_oversize_is_rejected_without_reading_the_body(protected):
    stream = CountedStream([reply(protected.provider)])
    install_stream(protected, stream, {"content-length":str(BODY_LIMIT + 1)})
    sdk = importlib.import_module(protected.provider)
    with pytest.raises((DispatchDenied, sdk.APIConnectionError)):
        protected.wrapped.complete("oversized declaration")
    assert stream.read_bytes == 0
    assert stream.close_calls >= 1
    assert_stopped(protected)


def test_body_at_exact_limit_is_accepted(protected):
    payload = reply(protected.provider)
    stream = CountedStream([payload, b" " * (BODY_LIMIT - len(payload))])
    install_stream(protected, stream, {"content-length":str(BODY_LIMIT)})
    assert protected.wrapped.complete("exact bound") == "offline answer"
    assert stream.read_bytes == BODY_LIMIT
    assert stream.close_calls >= 1
    assert protected.sent[0].headers["accept-encoding"] == "identity"
    assert not protected.dispatcher.uncertain_upstream


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "identity, gzip"])
def test_encoded_body_is_closed_before_reading_or_decompression(protected, encoding):
    # A small compressed input would otherwise inflate beyond the body cap.
    payload = gzip.compress(reply(protected.provider) + b" " * BODY_LIMIT)
    stream = CountedStream([payload])
    install_stream(protected, stream, {"content-encoding":encoding,
                                      "content-length":str(len(payload))})
    sdk = importlib.import_module(protected.provider)
    with pytest.raises((DispatchDenied, sdk.APIConnectionError)):
        protected.wrapped.complete("reject compression")
    assert stream.read_bytes == 0
    assert stream.close_calls >= 1
    assert_stopped(protected)


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "gzip, chunked"])
def test_encoded_transfer_body_is_closed_before_reading_or_decompression(protected, encoding):
    payload = gzip.compress(reply(protected.provider) + b" " * BODY_LIMIT)
    stream = CountedStream([payload])
    install_stream(protected, stream, {"transfer-encoding": encoding})
    sdk = importlib.import_module(protected.provider)
    with pytest.raises((DispatchDenied, sdk.APIConnectionError)):
        protected.wrapped.complete("reject transfer compression")
    assert stream.read_bytes == 0
    assert stream.close_calls >= 1
    assert_stopped(protected)


def test_chunked_transfer_is_counted_and_accepted(protected):
    payload = reply(protected.provider)
    stream = CountedStream([payload[:13], payload[13:]])
    install_stream(protected, stream, {"transfer-encoding": "chunked"})
    assert protected.wrapped.complete("bounded chunked response") == "offline answer"
    assert stream.read_bytes == len(payload)
    assert stream.close_calls >= 1
    assert not protected.dispatcher.uncertain_upstream


@pytest.mark.parametrize("length", ["-1", "unknown", "1, 1", "9" * 100])
def test_malformed_length_is_closed_without_body_read(protected, length):
    stream = CountedStream([reply(protected.provider)])
    install_stream(protected, stream, {"content-length":length})
    sdk = importlib.import_module(protected.provider)
    with pytest.raises((DispatchDenied, sdk.APIConnectionError)):
        protected.wrapped.complete("reject length")
    assert stream.read_bytes == 0
    assert stream.close_calls >= 1
    assert_stopped(protected)


@pytest.mark.parametrize("extra", [0, 1])
def test_output_limit_counts_utf8_bytes_after_sdk_parsing(protected, extra):
    text = "ä" * (64_000 + extra)
    payload = reply(protected.provider, text)
    assert len(payload) < BODY_LIMIT
    stream = CountedStream([payload])
    install_stream(protected, stream)
    if extra:
        with pytest.raises(DispatchDenied, match="text byte limit"):
            protected.wrapped.complete("text boundary")
        assert_stopped(protected)
    else:
        assert protected.wrapped.complete("text boundary") == text
        assert not protected.dispatcher.uncertain_upstream
    assert stream.read_bytes == len(payload)
    assert stream.close_calls >= 1


def test_already_buffered_oversize_is_rejected_before_sdk_parsing(protected):
    payload = reply(protected.provider) + b" " * BODY_LIMIT
    protected.response = lambda outgoing: httpx2.Response(200, request=outgoing,
        headers={"content-type":"application/json"}, content=payload)
    sdk = importlib.import_module(protected.provider)
    with pytest.raises((DispatchDenied, sdk.APIConnectionError)):
        protected.wrapped.complete("preloaded body")
    assert_stopped(protected)


def test_surrogate_output_is_rejected_without_returning_invalid_utf8(protected):
    payload = reply(protected.provider, "invalid-output").replace(b"invalid-output", b"\\ud800")
    install_stream(protected, CountedStream([payload]))
    with pytest.raises(DispatchDenied, match="valid UTF-8"):
        protected.wrapped.complete("invalid unicode")
    assert_stopped(protected)


def test_default_web_import_does_not_require_optional_sdk_transport():
    code = '''
import sys
class RejectSDK:
    def find_spec(self, name, *args):
        if name.split('.')[0] in {'httpx2', 'openai', 'anthropic'}:
            raise AssertionError('default web import requires an optional SDK')
sys.meta_path.insert(0, RejectSDK())
import tankai.web.server
print('OPTIONAL_SDK_NOT_IMPORTED')
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OPTIONAL_SDK_NOT_IMPORTED"
