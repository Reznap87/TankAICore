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
