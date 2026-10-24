"""pytest fixtures for harness tests."""
import os
import sys
import pytest

# Make the app package importable when running pytest from the repo root.
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + "/.."))


@pytest.fixture
def config_path(tmp_path):
    """A minimal config.yaml + .env pointing at a throwaway test config."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
app:
  name: harness
  bind: 127.0.0.1:0
  tls: { cert: /dev/null, key: /dev/null }
  default_agent: agent1
  secret_key: test-secret
db:
  url: postgresql://harness_app:test@localhost:5432/harness_test
  operator_role: peristyle
  runtime_role: harness_app
  database: harness_test
llm:
  adapter: minimax_m3
  api_key: test-key
  base_url: https://api.minimax.io/anthropic
  model: MiniMax-M3
  max_tokens: 256
  timeout_s: 10
  temperature: 0.0
embedding:
  base_url: http://127.0.0.1:8080/v1
  model: Qwen/Qwen3-Embedding-4B
  dimensions: 1024
  api_key: ''
  timeout_s: 5
memory:
  top_k: 10
  vector_limit: 30
  trigram_limit: 30
  recency_decay_days: 30
  chunk: { max_tokens: 800, min_tokens: 20, overlap_tokens: 50 }
  permanent: { max_tokens: 10000, warn_overflow: true, pin_audit: true }
agent:
  max_tool_iterations: 5
  max_searches_per_turn: 5
  max_pins_per_turn: 3
  max_unpins_per_turn: 5
bootstrap:
  files: {}
  behavior_on_overflow: fail
security:
  headers:
    x_content_type_options: nosniff
    x_frame_options: DENY
    strict_transport_security: "max-age=15552000; includeSubDomains"
    referrer_policy: strict-origin-when-cross-origin
    csp: "default-src 'self'"
  rate_limit: { default: "100/minute", chat: "30/minute", memory: "60/minute" }
""")
    return str(cfg)
