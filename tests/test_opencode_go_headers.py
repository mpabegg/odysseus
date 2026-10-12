"""OpenCode Go requires an `x-opencode-session` header on chat requests."""
from src.endpoint_resolver import build_headers
from src.llm_core import (
    OPENCODE_GO_FALLBACK_SESSION,
    _apply_opencode_go_headers,
    _detect_provider,
    _provider_headers,
    _provider_label,
)

GO = "https://opencode.ai/zen/go/v1"
ZEN = "https://opencode.ai/zen/v1"


class TestDetection:
    def test_go_url(self):
        assert _detect_provider(GO) == "opencode-go"
        assert _detect_provider(GO + "/chat/completions") == "opencode-go"
        assert _detect_provider("https://opencode.ai/zen/go") == "opencode-go"

    def test_zen_url(self):
        assert _detect_provider(ZEN) == "opencode-zen"

    def test_path_prefix_must_be_a_segment(self):
        assert _detect_provider("https://opencode.ai/zen/gopher/v1") == "opencode-zen"
        assert _detect_provider("https://opencode.ai/zenith/v1") != "opencode-zen"

    def test_lookalike_host(self):
        assert _detect_provider("https://opencode.ai.example.com/zen/go/v1") != "opencode-go"

    def test_label(self):
        assert _provider_label(GO) == "OpenCode Go"
        assert _provider_label(ZEN) == "OpenCode Zen"


class TestHeaders:
    def test_build_headers_sets_fallback_session(self):
        assert build_headers("sk-go", GO) == {
            "Authorization": "Bearer sk-go",
            "x-opencode-session": OPENCODE_GO_FALLBACK_SESSION,
        }

    def test_build_headers_leaves_zen_alone(self):
        assert build_headers("sk-zen", ZEN) == {"Authorization": "Bearer sk-zen"}

    def test_session_id_overrides_fallback(self):
        h = _provider_headers("opencode-go", build_headers("sk-go", GO), "chat-123")
        assert h["x-opencode-session"] == "chat-123"
        assert h["Authorization"] == "Bearer sk-go"

    def test_no_session_id_keeps_fallback(self):
        h = _provider_headers("opencode-go", build_headers("sk-go", GO))
        assert h["x-opencode-session"] == OPENCODE_GO_FALLBACK_SESSION

    def test_without_endpoint_headers(self):
        h = _provider_headers("opencode-go", None, "chat-123")
        assert h["x-opencode-session"] == "chat-123"

    def test_existing_header_any_case_is_replaced(self):
        h = _apply_opencode_go_headers({"X-OpenCode-Session": "old"}, "new")
        assert h == {"x-opencode-session": "new"}

    def test_existing_header_kept_without_session_id(self):
        h = _apply_opencode_go_headers({"X-OpenCode-Session": "custom"})
        assert h == {"X-OpenCode-Session": "custom"}

    def test_other_providers_untouched(self):
        h = _provider_headers("openai", {"Authorization": "Bearer x"}, "chat-123")
        assert "x-opencode-session" not in h
