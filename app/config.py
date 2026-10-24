"""Config loading for harness.

Reads config.yaml + .env into a single AppConfig dataclass tree.
Env vars are resolved via ${VAR} substitution; missing required vars
cause a clear startup failure (no silent defaulting for secrets).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ENV_REF_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _resolve_env(value: Any) -> Any:
    """Recursively resolve ${VAR} references against the process env."""
    if isinstance(value, str):
        def replace(m: re.Match[str]) -> str:
            var = m.group(1)
            v = os.environ.get(var)
            if v is None:
                raise RuntimeError(
                    f"config: missing env var ${var} referenced in config.yaml"
                )
            return v
        return ENV_REF_RE.sub(replace, value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


@dataclass(frozen=True)
class _RateLimit:
    default: str
    chat: str
    memory: str


@dataclass(frozen=True)
class _SecurityHeaders:
    x_content_type_options: str
    x_frame_options: str
    strict_transport_security: str
    referrer_policy: str
    csp: str


@dataclass(frozen=True)
class _Security:
    headers: _SecurityHeaders
    rate_limit: _RateLimit


@dataclass(frozen=True)
class _BootstrapFile:
    path: str
    max_bytes: int


@dataclass(frozen=True)
class _Bootstrap:
    files: dict[str, _BootstrapFile]
    behavior_on_overflow: str   # "fail" | "truncate"


@dataclass(frozen=True)
class _Prompt:
    """Conditional system-prompt pressure warnings (2026-10-22).

    `pressure_threshold` is the fraction of a cap (identity file bytes,
    or permanent-memory tokens) above which the harness appends a
    warning to the system prompt telling the agent to move detail out
    of the identity files / permanent memory and into memories, skills,
    scripts or docs.

    `pressure_enabled` is the kill switch.
    """
    pressure_enabled: bool
    pressure_threshold: float


@dataclass(frozen=True)
class _AgentLimits:
    max_tool_iterations: int
    max_searches_per_turn: int
    max_pins_per_turn: int
    max_unpins_per_turn: int


@dataclass(frozen=True)
class _Reverie:
    """Nightly reverie turn (2026-10-23).

    The reverie is a REAL agent turn, captured in the turns table like
    any other turn (unlike a heartbeat, which is not a turn at all).
    It runs the agent with its normal last-50-turns of history and asks
    it to:

      * unpin / update stale permanent memory entries,
      * bring the identity files (except the protected ones) and the
        permanent-memory budget down to a target percentage of their
        caps, so the next day does not trip the 85% prompt-pressure
        warning, and
      * finish with reflection + proposals for the user, emitted as
        plain chat.

    It only fires if the agent actually had activity in the window
    (otherwise it would fill the turn history with no-op reveries).
    """
    enabled: bool
    # Agents that get a reverie. Empty tuple = every installed agent.
    agents: tuple[str, ...]
    # Nightly time of day, "HH:MM", local to `timezone`.
    at: str
    timezone: str
    # Target utilisation the reverie must reach (fraction of the cap).
    file_target_pct: float
    memory_target_pct: float
    # How far back to look for real activity before firing.
    activity_window_hours: int
    # The DM partner the reverie turn runs as (the agent's own user).
    user_email: str
    # Optional per-agent instruction file, relative to pie/<agent>/identity/.
    prompt_file: str
    # Identity files the reverie must NOT edit (SOUL.md: user permission only).
    protected_files: tuple[str, ...]
    # Optional cap on the tool iterations of one reverie turn (0 = use
    # the global agent.max_tool_iterations). A reverie should be a
    # focused session, not an open-ended survey.
    max_tool_iterations: int


@dataclass(frozen=True)
class _MemoryChunk:
    max_tokens: int
    min_tokens: int
    overlap_tokens: int


@dataclass(frozen=True)
class _MemoryPermanent:
    max_tokens: int
    warn_overflow: bool
    pin_audit: bool


@dataclass(frozen=True)
class _Memory:
    top_k: int
    vector_limit: int
    trigram_limit: int
    recency_decay_days: int
    chunk: _MemoryChunk
    permanent: _MemoryPermanent


@dataclass(frozen=True)
class _AppTls:
    cert: str
    key: str


@dataclass(frozen=True)
class _App:
    name: str
    bind: str
    tls: _AppTls
    default_agent: str
    agents: tuple[str, ...]            # allocated at install time; not per-user
    pie_root: str                      # absolute or relative path; resolved at boot
    secret_key: str


@dataclass(frozen=True)
class _Db:
    url: str
    operator_role: str
    runtime_role: str
    database: str


@dataclass(frozen=True)
class _Llm:
    adapter: str
    api_key: str
    base_url: str
    model: str
    max_tokens: int
    timeout_s: int
    temperature: float
    # z.ai-specific: thinking control (added 2026-10-19 for agent3).
    # None (default) = omit the field, use API default.
    # dict = pass through (e.g. {"type": "disabled"} or {"type": "enabled", "budget": N}).
    # Only the glm_5_3_flash adapter reads this; other adapters ignore it.
    thinking: dict | None = None


@dataclass(frozen=True)
class _Embedding:
    base_url: str
    model: str
    dimensions: int
    api_key: str
    timeout_s: int


@dataclass(frozen=True)
class AppConfig:
    app: _App
    db: _Db
    llm: _Llm
    # Per-agent LLM overrides. When an agent name is present in this dict,
    # the harness constructs its adapter using the override config instead
    # of the global `llm:` block. Missing agents fall through to `llm:`.
    # The dispatch happens in app.extensions.init_app() (adapter_registry)
    # and app.agent.loop.AgentLoop._adapter_for(agent_name).
    # Added 2026-10-19 for the agent3 z.ai test (agent3 -> glm-5.3-flash,
    # agent1 + agent2 stay on minimax_m3).
    llm_per_agent: dict[str, _Llm] | None
    embedding: _Embedding
    memory: _Memory
    agent: _AgentLimits
    bootstrap: _Bootstrap
    prompt: _Prompt
    reverie: _Reverie
    security: _Security



def _make_llm(d: dict) -> _Llm:
    """Build an _Llm dataclass from a config dict (used for both the
    global llm: block and per-agent llm_per_agent entries).

    The \`thinking\` field is optional (added 2026-10-19 for glm_5_3_flash).
    When present in the config dict, it must be a dict; when absent,
    we default to None (which tells the glm_5_3_flash adapter to omit
    the field from the request and use the API default)."""
    # The thinking field can arrive as a string from ${ENV_VAR}
    # substitution (e.g. CLAWBIE_LLM_THINKING={"type": "disabled"}
    # gets substituted as a string into the YAML). The z.ai API
    # expects a dict here, so JSON-parse it if needed.
    import json
    thinking_raw = d.get("thinking")
    if isinstance(thinking_raw, str) and thinking_raw.strip():
        try:
            thinking = json.loads(thinking_raw)
        except json.JSONDecodeError:
            # Not valid JSON; treat as a "type" string shorthand
            # (e.g. "disabled" -> {"type": "disabled"})
            thinking = {"type": thinking_raw.strip()}
    else:
        thinking = thinking_raw

    return _Llm(
        adapter=d["adapter"],
        api_key=d["api_key"],
        base_url=d["base_url"],
        model=d["model"],
        max_tokens=int(d["max_tokens"]),
        timeout_s=int(d["timeout_s"]),
        temperature=float(d["temperature"]),
        thinking=thinking,
    )


def load_config(path: str | os.PathLike[str] = "config.yaml") -> AppConfig:
    """Load config.yaml (with .env auto-loaded from same dir) into AppConfig."""
    config_path = Path(path)
    env_path = config_path.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    raw = yaml.safe_load(config_path.read_text())
    raw = _resolve_env(raw)

    a = raw["app"]
    db = raw["db"]
    llm = raw["llm"]
    em = raw["embedding"]
    mem = raw["memory"]
    ag = raw["agent"]
    bs = raw["bootstrap"]
    sec = raw["security"]
    pr = raw.get("prompt", {})
    rv = raw.get("reverie", {})

    agents_list = tuple(a.get("agents") or (a["default_agent"],))
    pie_root_raw = a.get("pie_root") or "pie"

    # Per-agent LLM overrides (optional). Each entry is a complete _Llm
    # config block. Missing agents fall through to the global llm: block
    # (dispatch is in app.extensions.init_app + AgentLoop._adapter_for).
    llm_per_agent_raw = llm.get("per_agent") or {}
    llm_per_agent = {name: _make_llm(cfg) for name, cfg in llm_per_agent_raw.items()}

    return AppConfig(
        app=_App(
            name=a["name"],
            bind=a["bind"],
            tls=_AppTls(cert=a["tls"]["cert"], key=a["tls"]["key"]),
            default_agent=a["default_agent"],
            agents=agents_list,
            pie_root=pie_root_raw,
            secret_key=a["secret_key"],
        ),
        db=_Db(
            url=db["url"],
            operator_role=db["operator_role"],
            runtime_role=db["runtime_role"],
            database=db["database"],
        ),
        llm=_make_llm(llm),
        embedding=_Embedding(
            base_url=em["base_url"],
            model=em["model"],
            dimensions=int(em["dimensions"]),
            api_key=em["api_key"],
            timeout_s=int(em["timeout_s"]),
        ),
        memory=_Memory(
            top_k=int(mem["top_k"]),
            vector_limit=int(mem["vector_limit"]),
            trigram_limit=int(mem["trigram_limit"]),
            recency_decay_days=int(mem["recency_decay_days"]),
            chunk=_MemoryChunk(
                max_tokens=int(mem["chunk"]["max_tokens"]),
                min_tokens=int(mem["chunk"]["min_tokens"]),
                overlap_tokens=int(mem["chunk"]["overlap_tokens"]),
            ),
            permanent=_MemoryPermanent(
                max_tokens=int(mem["permanent"]["max_tokens"]),
                warn_overflow=bool(mem["permanent"]["warn_overflow"]),
                pin_audit=bool(mem["permanent"]["pin_audit"]),
            ),
        ),
        agent=_AgentLimits(
            max_tool_iterations=int(ag["max_tool_iterations"]),
            max_searches_per_turn=int(ag["max_searches_per_turn"]),
            max_pins_per_turn=int(ag["max_pins_per_turn"]),
            max_unpins_per_turn=int(ag["max_unpins_per_turn"]),
        ),
        llm_per_agent=llm_per_agent,
        bootstrap=_Bootstrap(
            files={
                k: _BootstrapFile(path=v["path"], max_bytes=int(v["max_bytes"]))
                for k, v in bs["files"].items()
            },
            behavior_on_overflow=bs["behavior_on_overflow"],
        ),
        prompt=_Prompt(
            pressure_enabled=bool(pr.get("pressure_enabled", True)),
            pressure_threshold=float(pr.get("pressure_threshold", 0.85)),
        ),
        reverie=_Reverie(
            enabled=bool(rv.get("enabled", False)),
            agents=tuple(
                a.strip() for a in str(rv.get("agents") or "").split(",") if a.strip()
            ),
            at=str(rv.get("at") or "03:00").strip(),
            timezone=str(rv.get("timezone") or "Australia/Brisbane").strip(),
            file_target_pct=float(rv.get("file_target_pct", 0.60)),
            memory_target_pct=float(rv.get("memory_target_pct", 0.60)),
            activity_window_hours=int(rv.get("activity_window_hours", 24)),
            user_email=str(rv.get("user_email") or "").strip(),
            prompt_file=str(rv.get("prompt_file") or "REVERIE.md").strip(),
            protected_files=tuple(
                f.strip() for f in str(rv.get("protected_files") or "SOUL.md").split(",")
                if f.strip()
            ),
            max_tool_iterations=int(rv.get("max_tool_iterations", 0) or 0),
        ),
        security=_Security(
            headers=_SecurityHeaders(
                x_content_type_options=sec["headers"]["x_content_type_options"],
                x_frame_options=sec["headers"]["x_frame_options"],
                strict_transport_security=sec["headers"]["strict_transport_security"],
                referrer_policy=sec["headers"]["referrer_policy"],
                csp=sec["headers"]["csp"],
            ),
            rate_limit=_RateLimit(
                default=sec["rate_limit"]["default"],
                chat=sec["rate_limit"]["chat"],
                memory=sec["rate_limit"]["memory"],
            ),
        ),
    )
