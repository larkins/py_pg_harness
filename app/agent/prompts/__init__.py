"""System prompts indexed by adapter_id.

Each adapter can have its own variant (different tool descriptions,
different style instructions, different XML-vs-Markdown preferences). If
a specific adapter has no prompt, we fall back to the minimax_m3 prompt
('agent1') as the canonical default — most modern models will read it
without complaint.
"""
from . import minimax_m3

# Adapters without a specific prompt get the canonical agent1 prompt.
DEFAULT_PROMPT = minimax_m3.SYSTEM_PROMPT

_PROMPTS: dict[str, str] = {
    "minimax_m3": minimax_m3.SYSTEM_PROMPT,
    # 'kimi_k3' falls through to the default (same prompt works fine).
    # Future: "kimi_k3": kimi_k3.SYSTEM_PROMPT, ...
}


def get(adapter_id: str) -> str:
    return _PROMPTS.get(adapter_id, DEFAULT_PROMPT)
