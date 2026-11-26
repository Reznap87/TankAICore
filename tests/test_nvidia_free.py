import pytest

from tankai.core.nvidia_free import select_nvidia_key, nvidia_free_enabled
from tankai.core.request_dispatch import DispatchDenied


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TANKAI_NVIDIA_FREE_ENABLED", raising=False)
    monkeypatch.setenv("NVIDIA_API_KEYa", "dummy")
    assert not nvidia_free_enabled()
    with pytest.raises(DispatchDenied):
        select_nvidia_key()


@pytest.mark.parametrize("slot", list("abcdefgh"))
def test_existing_slots(monkeypatch, slot):
    monkeypatch.setenv("TANKAI_NVIDIA_FREE_ENABLED", "1")
    monkeypatch.setenv("TANKAI_NVIDIA_KEY_SLOT", slot)
    monkeypatch.setenv("NVIDIA_API_KEY" + slot, "dummy")
    assert select_nvidia_key() == "dummy"


def test_no_key_rotation(monkeypatch):
    monkeypatch.setenv("TANKAI_NVIDIA_FREE_ENABLED", "1")
    monkeypatch.setenv("TANKAI_NVIDIA_KEY_SLOT", "a")
    monkeypatch.delenv("NVIDIA_API_KEYa", raising=False)
    monkeypatch.setenv("NVIDIA_API_KEYb", "dummy")
    with pytest.raises(DispatchDenied):
        select_nvidia_key()


def test_invalid_slot(monkeypatch):
    monkeypatch.setenv("TANKAI_NVIDIA_FREE_ENABLED", "1")
    monkeypatch.setenv("TANKAI_NVIDIA_KEY_SLOT", "other")
    with pytest.raises(DispatchDenied):
        select_nvidia_key()


@pytest.fixture
def fake_nvidia(monkeypatch):
    import sys
    from types import SimpleNamespace
    from tankai.core.nvidia_free import NVIDIA_MODEL, NVIDIA_URL

    events = []
    state = SimpleNamespace(model=NVIDIA_MODEL, events=events)

    class Transport:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Client:
        def __init__(self, **kwargs):
            self._client = kwargs.get("http_client")
            self.max_retries = kwargs.get("max_retries", 0)
            self.base_url = NVIDIA_URL
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def with_options(self, **kwargs):
            return Client(**kwargs)

        def create(self, **kwargs):
            events.append(("request", kwargs))
            return SimpleNamespace(model=state.model, choices=[SimpleNamespace(
                message=SimpleNamespace(content="synthetic response"))])

    def factory(**kwargs):
        assert kwargs["base_url"] == NVIDIA_URL
        assert kwargs["api_key"] == "synthetic-key"
        assert kwargs["max_retries"] == 0
        return Client(**kwargs)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(
        OpenAI=factory, DefaultHttpxClient=Transport))
    monkeypatch.setenv("TANKAI_NVIDIA_FREE_ENABLED", "1")
    monkeypatch.setenv("TANKAI_NVIDIA_KEY_SLOT", "a")
    monkeypatch.setenv("NVIDIA_API_KEYa", "synthetic-key")
    return state


def test_adapter_requires_admission_before_network(fake_nvidia):
    from tankai.core.nvidia_free import NvidiaFreeLLM

    def deny():
        raise DispatchDenied("synthetic denial")

    adapter = NvidiaFreeLLM(admit=deny)
    with pytest.raises(DispatchDenied):
        adapter.complete("synthetic prompt")
    assert fake_nvidia.events == []


def test_adapter_bounds_request_and_confirms_identity(fake_nvidia):
    from tankai.core.nvidia_free import NvidiaFreeLLM, NVIDIA_MODEL
    from tankai.core.llm import llm_response_identity

    adapter = NvidiaFreeLLM(admit=lambda: fake_nvidia.events.append(("admit", None)))
    assert adapter.complete("synthetic prompt") == "synthetic response"
    assert [e[0] for e in fake_nvidia.events] == ["admit", "request"]
    request = fake_nvidia.events[1][1]
    assert request["model"] == NVIDIA_MODEL
    assert request["max_tokens"] == 512
    assert llm_response_identity(adapter).startswith("nvidia_free:" + NVIDIA_MODEL + ":")
    with pytest.raises(DispatchDenied):
        adapter.complete("synthetic prompt", max_tokens=999)
    assert len(fake_nvidia.events) == 2


def test_adapter_rejects_unconfirmed_model(fake_nvidia):
    from tankai.core.nvidia_free import NvidiaFreeLLM
    from tankai.core.llm import llm_response_identity

    fake_nvidia.model = "unknown/model"
    adapter = NvidiaFreeLLM(admit=lambda: None)
    with pytest.raises(DispatchDenied):
        adapter.complete("synthetic prompt")
    assert llm_response_identity(adapter) is None


def test_glm_uses_same_key_and_bounded_model(fake_nvidia):
    from tankai.core.nvidia_free import NvidiaFreeLLM, NVIDIA_GLM_MODEL
    from tankai.core.llm import llm_response_identity
    fake_nvidia.model = NVIDIA_GLM_MODEL
    adapter = NvidiaFreeLLM(model=NVIDIA_GLM_MODEL, admit=lambda: None)
    assert adapter.complete("synthetic prompt") == "synthetic response"
    request = fake_nvidia.events[0][1]
    assert request["model"] == NVIDIA_GLM_MODEL
    assert request["max_tokens"] == 1024
    assert request["extra_body"]["reasoning_effort"] == "low"
    assert llm_response_identity(adapter).startswith("nvidia_free:" + NVIDIA_GLM_MODEL + ":")


@pytest.mark.parametrize("model", ["paid/model", "", None, []])
def test_unlisted_model_rejected(fake_nvidia, model):
    from tankai.core.nvidia_free import NvidiaFreeLLM
    with pytest.raises(DispatchDenied):
        NvidiaFreeLLM(model=model, admit=lambda: None)
    assert fake_nvidia.events == []
