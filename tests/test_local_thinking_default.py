"""ODYSSEUS_LOCAL_THINKING_DEFAULT starts local reasoning-model chats with thinking on."""
import pytest

from src.model_profiles import default_thinking_mode

LOCAL = "http://192.168.0.169:8080/v1/chat/completions"
HOSTED = "https://opencode.ai/zen/go/v1/chat/completions"
QWEN = "qwen3.8-flash-next-iq3_xxs"


def test_off_without_opt_in(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_LOCAL_THINKING_DEFAULT", raising=False)
    assert default_thinking_mode(LOCAL, QWEN) == "off"


@pytest.mark.parametrize("url,model,expected", [
    (LOCAL, QWEN, "on"),
    (HOSTED, QWEN, "off"),
    (LOCAL, "llama-3.1-8b", "off"),
    ("", QWEN, "off"),
])
def test_opt_in_only_for_local_toggleable_models(monkeypatch, url, model, expected):
    monkeypatch.setenv("ODYSSEUS_LOCAL_THINKING_DEFAULT", "on")
    assert default_thinking_mode(url, model) == expected
