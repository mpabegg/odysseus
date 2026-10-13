"""A blocked tool call must never replace the model's final answer."""
import json

import src.agent_loop as al
from tests.test_agent_rounds_exhausted import _collect, _patch_common, _types

BLOCKED = "The exact operation is disabled by user or server request authority policy."
ANSWER = "Open Settings, then Email, add a Gmail account and paste an app password."


def test_blocked_ui_control_does_not_replace_final_answer(monkeypatch):
    _patch_common(monkeypatch)
    streams = 0

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal streams
        streams += 1
        if streams == 1:
            yield "data: " + json.dumps({"type": "tool_calls", "calls": [
                {"id": "toggles", "name": "ui_control", "arguments": json.dumps({"action": "get_toggles"})}]}) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": ANSWER}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def _blocked_exec(block, *args, **kwargs):
        return f"{block.tool_type}: BLOCKED", {
            "output": BLOCKED, "error": BLOCKED, "exit_code": 1, "blocked": True,
            "failure_kind": "request_authority_denied"}

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setattr(al, "execute_tool_block", _blocked_exec, raising=False)
    monkeypatch.setattr(al, "_ody_qwen_terminal_tool_summary", lambda *a, **k: BLOCKED)

    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "How do I connect my email?"}],
        max_rounds=3, relevant_tools={"ui_control"},
    )))
    finals = [e["content"] for e in events if e.get("type") == "final_response"]
    assert BLOCKED not in finals
    assert any(ANSWER in (e.get("delta") or "") for e in events)


SCAN = "Found 25 unsubscribe candidate(s) from 169 scanned emails.\n1. **Weekly digest** From: news@example.com"
SYNTHESIS = "You have 25 newsletters; the noisiest are the weekly digests. Want me to unsubscribe from any?"


def test_unsubscribe_scan_dump_does_not_replace_model_synthesis(monkeypatch):
    _patch_common(monkeypatch)
    streams = 0

    async def _fake_stream(_candidates, messages, **kwargs):
        nonlocal streams
        streams += 1
        if streams == 1:
            yield "data: " + json.dumps({"type": "tool_calls", "calls": [
                {"id": "scan", "name": "scan_email_unsubscribes", "arguments": "{}"}]}) + "\n\n"
        else:
            yield "data: " + json.dumps({"delta": SYNTHESIS}) + "\n\n"
        yield "data: [DONE]\n\n"

    async def _scan_exec(block, *args, **kwargs):
        return f"{block.tool_type}: ok", {"output": SCAN, "exit_code": 0}

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    monkeypatch.setattr(al, "execute_tool_block", _scan_exec, raising=False)
    # Without an MCP manager the native alias of the MCP scan stays disabled;
    # this test is about the terminal answer, not alias policy.
    monkeypatch.setattr(al, "email_tool_policy_names", lambda name: frozenset({name}))

    events = _types(_collect(al.stream_agent_loop(
        "http://x/v1", "m",
        [{"role": "user", "content": "Which newsletters should I unsubscribe from?"}],
        max_rounds=3, relevant_tools={"scan_email_unsubscribes"},
    )))
    finals = [e["content"] for e in events if e.get("type") == "final_response"]
    assert not any(SCAN.splitlines()[0] in final for final in finals)
    assert any(SYNTHESIS in (e.get("delta") or "") for e in events)
