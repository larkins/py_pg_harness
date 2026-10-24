"""Smoke test for the minimax_m3 adapter (real API call).

Run with: HARNESS_RUN_LIVE_TESTS=1 pytest tests/test_minimax_adapter.py

Skipped by default because it hits the live MiniMax endpoint and burns
tokens. The smoke test verifies that:
- adapter.complete() returns an LLMResponse
- text is non-empty for a trivial prompt
- tool_calls fires when a tool is described
"""
import os
import pytest

from app.config import load_config
from app.agent.llm import get_adapter
from app.agent.llm.base import Message, ToolCall


pytestmark = pytest.mark.skipif(
    "HARNESS_RUN_LIVE_TESTS" not in os.environ,
    reason="live API test; set HARNESS_RUN_LIVE_TESTS=1 to enable",
)


def test_adapter_loads(config_path):
    cfg = load_config(config_path)
    adapter = get_adapter(cfg)
    assert adapter.adapter_id == "minimax_m3"
    assert adapter.name == "minimax/MiniMax-M3"


def test_basic_complete(config_path):
    import asyncio
    cfg = load_config(config_path)
    adapter = get_adapter(cfg)
    try:
        response = asyncio.run(adapter.complete(
            messages=[Message(role="user", content="Reply with the single word: pong")],
            max_tokens=8,
            temperature=0.0,
        ))
        assert response.text is not None
        assert "pong" in response.text.lower()
        assert response.finish_reason in ("stop", "length")
        assert response.usage.get("input_tokens", 0) > 0
    finally:
        asyncio.run(adapter.aclose())


def test_tool_call(config_path):
    import asyncio
    cfg = load_config(config_path)
    adapter = get_adapter(cfg)

    tool = {
        "name": "memory_recall",
        "description": "Search long-term memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    }
    try:
        response = asyncio.run(adapter.complete(
            messages=[Message(
                role="user",
                content="Call memory_recall with query='test query'.",
            )],
            tools=[tool],
            max_tokens=128,
            temperature=0.0,
        ))
        assert len(response.tool_calls) >= 1, f"expected tool call, got text={response.text!r}"
        assert response.tool_calls[0].name == "memory_recall"
        assert response.tool_calls[0].arguments.get("query")
    finally:
        asyncio.run(adapter.aclose())


def test_format_tool_result(config_path):
    cfg = load_config(config_path)
    adapter = get_adapter(cfg)
    call = ToolCall(id="call_abc", name="memory_recall", arguments={"query": "x"})
    msg = adapter.format_tool_result(call, {"chunks": [], "count": 0})
    assert msg.role == "user"
    # Content is the Anthropic tool_result block.
    assert isinstance(msg.content, list)
    assert msg.content[0]["type"] == "tool_result"
    assert msg.content[0]["tool_use_id"] == "call_abc"
    assert "chunks" in msg.content[0]["content"]
