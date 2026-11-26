"""Opt-in NVIDIA hosted prototype endpoint. No automatic key rotation."""
from __future__ import annotations

import os
from typing import Callable

from .llm import OpenAILLM, _openai_text, _record_response_model
from .request_dispatch import DispatchDenied
from .response_limits import bounded_text, bounded_transport

NVIDIA_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "openai/gpt-oss-20b"
NVIDIA_GLM_MODEL = "z-ai/glm-5.3-flash"
NVIDIA_MODELS = frozenset({NVIDIA_MODEL, NVIDIA_GLM_MODEL})


def nvidia_free_enabled() -> bool:
    return os.environ.get("TANKAI_NVIDIA_FREE_ENABLED", "").strip() == "1"


def select_nvidia_key() -> str:
    if not nvidia_free_enabled():
        raise DispatchDenied("NVIDIA prototype is disabled")
    slot = os.environ.get("TANKAI_NVIDIA_KEY_SLOT", "a").strip()
    if slot not in tuple("abcdefgh"):
        raise DispatchDenied("Invalid NVIDIA key slot")
    key = os.environ.get("NVIDIA_API_KEY" + slot, "").strip()
    if not key:
        raise DispatchDenied("NVIDIA prototype key is missing")
    return key


class NvidiaFreeLLM(OpenAILLM):
    """Only allowlisted, documented NVIDIA prototype models; explicit admission per call."""
    provider_name = "nvidia_free"
    requires_request_admission = True

    def __init__(self, *, admit: Callable[[], None], model: str = NVIDIA_MODEL) -> None:
        if type(model) is not str or model not in NVIDIA_MODELS:
            raise DispatchDenied("NVIDIA model not allowlisted")
        if not callable(admit):
            raise DispatchDenied("NVIDIA requires server admission")
        self._admit = admit
        super().__init__(model=model, api_key=select_nvidia_key(),
                         base_url=NVIDIA_URL, max_tokens=1024 if model == NVIDIA_GLM_MODEL else 512,
                         timeout_seconds=30.0, max_retries=0)

    def complete(self, prompt: str, *, system: str = "", **kwargs) -> str:
        if kwargs or not nvidia_free_enabled() or self.model not in NVIDIA_MODELS:
            raise DispatchDenied("Invalid NVIDIA prototype request")
        from openai import DefaultHttpxClient
        import httpx2
        self.observed_model = ""
        messages = ([{"role": "system", "content": system}] if system else [])
        messages.append({"role": "user", "content": prompt})
        wire = bounded_transport(httpx2.HTTPTransport(retries=0, trust_env=False))
        with DefaultHttpxClient(transport=wire, follow_redirects=False, trust_env=False) as transport:
            client = self.client.with_options(max_retries=0, timeout=30.0, http_client=transport)
            if getattr(client, "_client", None) is not transport or client.max_retries != 0:
                raise DispatchDenied("NVIDIA transport is not bounded")
            self._admit()
            options = {"extra_body": {"reasoning_effort": "low", "chat_template_kwargs": {"clear_thinking": True}}} if self.model == NVIDIA_GLM_MODEL else {}
            response = client.chat.completions.create(
                model=self.model, messages=messages, temperature=0.2,
                max_tokens=1024 if self.model == NVIDIA_GLM_MODEL else 512, **options
            )
        if getattr(response, "model", None) != self.model:
            raise DispatchDenied("NVIDIA did not confirm the requested model")
        _record_response_model(self, response)
        return bounded_text(_openai_text(response))
