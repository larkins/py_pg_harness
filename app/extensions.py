"""Shared extensions — db pool, embedding client, LLM adapter, agent loop.

Constructed once at app startup; lives for the process lifetime.

Multi-agent (2026-10-15): per the pie-directory design, the harness hosts
N agents (configured by `app.agents` in config.yaml) in a single process.
We build a per-agent `BootstrapLoader` (loading from
`pie/<agent>/identity/`) and `MemoryAPI` (scoped to the agent's schema)
at startup, then look up the active pair per request via the
`BootstrapRegistry` and a `memory_registry` dict.

The active agent comes from the request — first the Flask session
(`session["active_agent_name"]`), then the JSON body (`data["agent"]`),
then the config default (`app.default_agent`), then the first entry in
`app.agents`. The HMI top-bar dropdown writes the session value.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

from .agent.bootstrap import BootstrapLoader, BootstrapRegistry
from .agent.embedding import EmbeddingClient
from .agent.llm import get_adapter
from .agent.loop import AgentLoop
from .agent.memory import MemoryAPI
from .agent.tools import all_tools as _register_tools   # noqa: F401  (side-effect import)
from .models.group import GroupStore

log = logging.getLogger(__name__)


def _resolve_pie_root(config: Any) -> Path:
    """Resolve cfg.app.pie_root to an absolute Path.

    Relative paths are resolved against the harness root (parent of
    config.yaml's directory, which is the same parent as the .env).
    """
    raw = config.app.pie_root
    p = Path(raw)
    if not p.is_absolute():
        # Anchor under the harness root (parent of config.yaml). The
        # config file path is captured at load time on config._config_path
        # by the app factory; if that's not available, fall back to cwd.
        anchor = getattr(config, "_config_dir", None) or Path.cwd()
        p = (anchor / p).resolve()
    return p


def build_extensions(config: Any) -> dict[str, Any]:
    """Construct the singletons. Called once from the app factory.

    Raises BootstrapLoader's BootstrapOverflowError if any bootstrap
    file exceeds its cap with behavior_on_overflow=fail for any agent
    that has files present. Missing bootstrap dirs/files are tolerated
    (the loader logs and renders empty) so a not-yet-migrated agent
    doesn't block startup.

    Returns a dict with:
        pool, embedding, adapter, bootstrap_registry, memory_registry, loop,
        group_store.
    """
    # Tool side-effect import (registers memory_recall/pin/unpin).
    import app.agent.tools  # noqa: F401

    pool = ConnectionPool(
        config.db.url,
        min_size=1,
        max_size=4,
        kwargs={"autocommit": False},
        open=True,
    )
    log.info("db pool: opened (min=1, max=4)")

    # Group chat store (2026-10-23) -- shares the same pool.
    group_store = GroupStore(pool)
    log.info("group store: ready (public.groups & friends)")

    embedding = EmbeddingClient(config)
    log.info("embedding client: base_url=%s model=%s", config.embedding.base_url, config.embedding.model)

    adapter = get_adapter(config)
    log.info("llm adapter: id=%s name=%s model=%s", adapter.adapter_id, adapter.name, config.llm.model)

    # Per-agent registries. Build a BootstrapLoader + MemoryAPI for each
    # agent in `app.agents`, anchored at pie_root/<agent>/identity/ and
    # pie_root/<agent>/ (the latter for the MemoryAPI's agent_name scope).
    pie_root = _resolve_pie_root(config)
    agents: tuple[str, ...] = config.app.agents
    log.info("agents: %s (pie_root=%s)", list(agents), pie_root)

    bootstrap_loaders: dict[str, BootstrapLoader] = {}
    memory_apis: dict[str, MemoryAPI] = {}
    for agent_name in agents:
        identity_dir = pie_root / agent_name / "identity"
        # BootstrapLoader: paths in config.yaml are relative to identity_dir
        try:
            loader = BootstrapLoader(config, workspace_root=identity_dir)
            bootstrap_loaders[agent_name] = loader
            log.info(
                "bootstrap loader: agent=%s root=%s files=%s",
                agent_name, identity_dir, list(loader._contents.keys()),
            )
        except Exception as e:
            # BootstrapOverflowError or other — log and skip this agent.
            # The agent will be unavailable until the operator fixes the
            # files, but the harness still boots with the other agents.
            log.error(
                "bootstrap loader: agent=%s FAILED to build (%s: %s); agent will be unavailable",
                agent_name, type(e).__name__, e,
            )

        # MemoryAPI: each agent gets its own (same DB pool, agent_name scopes queries)
        memory_apis[agent_name] = MemoryAPI(
            pool=pool, embedding_client=embedding,
            agent_name=agent_name, config=config,
        )
        log.info("memory api: agent=%s schema=per-agent", agent_name)

    bootstrap_registry = BootstrapRegistry(bootstrap_loaders)
    # Keep the default-agent memory accessible as `memory` for backward compat
    default_memory = memory_apis.get(config.app.default_agent) or next(iter(memory_apis.values()))
    default_bootstrap = bootstrap_loaders.get(config.app.default_agent) or next(iter(bootstrap_loaders.values()), None)

    # Per-agent LLM adapter registry (added 2026-10-19 for the agent3
    # z.ai test). For each agent that has an entry in config.llm_per_agent,
    # build a separate adapter using the override config. Missing agents
    # fall through to the global `adapter` (passed to AgentLoop as the
    # default — see AgentLoop._adapter_for).
    adapter_registry: dict[str, BaseLLM] = {}
    if config.llm_per_agent:
        for agent_name, override_llm_cfg in config.llm_per_agent.items():
            # Build a shallow-copied config object with the override llm
            # so the adapter's __init__ reads the right api_key/base_url/etc.
            from dataclasses import replace
            override_config = replace(config, llm=override_llm_cfg)
            try:
                override_adapter = get_adapter(override_config)
                adapter_registry[agent_name] = override_adapter
                log.info(
                    "llm adapter override: agent=%s id=%s name=%s model=%s base_url=%s",
                    agent_name, override_adapter.adapter_id,
                    override_adapter.name, override_llm_cfg.model, override_llm_cfg.base_url,
                )
            except Exception as e:
                log.error(
                    "llm adapter override: agent=%s FAILED to build (%s: %s); agent will fall back to global LLM",
                    agent_name, type(e).__name__, e,
                )

    loop = AgentLoop(
        adapter=adapter,
        memory=default_memory,
        bootstrap=default_bootstrap,
        config=config,
        bootstrap_registry=bootstrap_registry,
        memory_registry=memory_apis,
        adapter_registry=adapter_registry,
    )
    log.info(
        "agent loop: ready (default=%s, %d agent(s) registered)",
        config.app.default_agent, len(bootstrap_loaders),
    )

    return {
        "pool": pool,
        "embedding": embedding,
        "adapter": adapter,
        "bootstrap": default_bootstrap,             # back-compat: default agent's loader
        "bootstrap_registry": bootstrap_registry,   # NEW: per-agent lookup
        "memory": default_memory,                   # back-compat: default agent's api
        "memory_registry": memory_apis,             # NEW: per-agent lookup
        "loop": loop,
        "group_store": group_store,                  # NEW: group chats
    }
