"""LLM adapter registry + factory.

Adding a new model = adding a row here AND writing app/agent/llm/<id>.py
that exposes `LLM(config)`. The agent loop never imports concrete adapters
directly — only BaseLLM from .base.
"""
from __future__ import annotations

import importlib
from typing import Any

from .base import BaseLLM, LLMResponse, Message, ToolCall

ADAPTERS: dict[str, str] = {
    "minimax_m3":         "app.agent.llm.minimax_m3",
    "minimax_m31_flash":  "app.agent.llm.minimax_m31_flash",
    "kimi_k3":            "app.agent.llm.kimi_k3",
    "glm_5_3_flash":      "app.agent.llm.glm_5_3_flash",
}

__all__ = ["BaseLLM", "LLMResponse", "Message", "ToolCall", "ADAPTERS", "get_adapter"]


def get_adapter(config: Any) -> BaseLLM:
    """Load the configured adapter module and instantiate it.

    Fails fast at startup if LLM_ADAPTER is unknown or the module is
    missing. This is by design — we want broken models to be visible
    at boot, not at the first user request.
    """
    adapter_id = config.llm.adapter
    if adapter_id not in ADAPTERS:
        raise ValueError(
            f"Unknown LLM_ADAPTER: {adapter_id!r}. "
            f"Available: {sorted(ADAPTERS)}"
        )
    module = importlib.import_module(ADAPTERS[adapter_id])
    if not hasattr(module, "LLM"):
        raise ValueError(
            f"Adapter module {ADAPTERS[adapter_id]} does not export LLM class"
        )
    return module.LLM(config)
