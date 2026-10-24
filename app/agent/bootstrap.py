"""Workspace bootstrap loader.

Loads SOUL.md, USER.md, AGENTS.md, IDENTITY.md, MEMORY.md from the
workspace directory at app startup. Each file has its own byte cap
(config.bootstrap.files). behavior_on_overflow controls behavior when
a file exceeds its cap:

    'fail'     — refuse to start; log FATAL with file name + size + cap
    'truncate' — log warning + truncate to cap bytes (not recommended)

Per-agent overrides: <workspace_root>/<agent_name>/<file> takes
precedence over <workspace_root>/<file>. v0.1 (skipped here).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class BootstrapOverflowError(Exception):
    """Raised at boot if any bootstrap file exceeds its cap in fail mode."""

    def __init__(self, path: Path, size: int, cap: int) -> None:
        super().__init__(
            f"bootstrap file {path} is {size} bytes; cap is {cap} bytes. "
            f"Refusing to start harness. Operator must trim the file OR raise the cap in config.yaml."
        )
        self.path = path
        self.size = size
        self.cap = cap


class BootstrapLoader:
    """Loads and caches bootstrap content. Built once at app startup.

    Raises BootstrapOverflowError at construction time if behavior='fail'
    and any configured file exceeds its cap. This is by design — we want
    the overflow to be visible at boot, not at the first chat request.
    """

    def __init__(
        self,
        config: Any,
        workspace_root: str | Path | None = None,
    ) -> None:
        self.config = config
        # Default workspace root: ~/.openclaw/workspace/
        # (mirrors OpenClaw layout — see MEMORY.md workspace structure)
        if workspace_root is None:
            workspace_root = Path.home() / ".openclaw" / "workspace"
        self.workspace_root = Path(workspace_root)
        self.behavior = config.bootstrap.behavior_on_overflow
        # Eager load — raises on overflow in fail mode.
        self._contents: dict[str, str] = self._load_all()
        log.info(
            "bootstrap: loaded %d file(s) from %s (behavior_on_overflow=%s)",
            len(self._contents), self.workspace_root, self.behavior,
        )

    def file_usage(self) -> list[dict]:
        """Per-file bytes vs cap, for the prompt-pressure check.

        2026-10-22: the harness warns the agent in its system prompt when
        an identity file approaches its cap, so the agent can move detail
        out on its own initiative instead of the file silently tripping
        the boot-time overflow check.
        """
        out: list[dict] = []
        for name, file_cfg in self.config.bootstrap.files.items():
            cap = int(getattr(file_cfg, "max_bytes", 0) or 0)
            content = self._contents.get(name)
            size = len(content.encode("utf-8")) if content is not None else 0
            out.append({
                "name": name,
                "bytes": size,
                "cap": cap,
                "pct": (size / cap) if cap else 0.0,
            })
        return out

    def refresh(self) -> None:
        """Re-read every identity file from disk, in place.

        Called before each turn (via BootstrapRegistry.get). The files
        are small (tens of KB) and page-cached, so re-reading them costs
        microseconds and removes a whole class of staleness bug:

          * an agent that edits its own identity file (the nightly
            reverie does) sees the change on its NEXT turn, not at the
            next restart;
          * an operator who hand-edits a file sees the change
            immediately;
          * a file that was over its cap at boot (the harness refuses to
            start in that case) can no longer silently keep being
            served stale.

        A file that has become unreadable, or has been pushed OVER its
        cap, keeps its last good content for that turn and logs an
        error - a bad edit degrades instead of breaking the turn. A file
        that has genuinely been DELETED is dropped from the prompt (that
        is the truth).

        RAM: this keeps the strings in memory exactly as before (53 KB
        for all three agents = 0.07% of the harness process). The point
        is correctness, not footprint.
        """
        out: dict[str, str] = {}
        for name, file_cfg in self.config.bootstrap.files.items():
            path = self.workspace_root / file_cfg.path
            try:
                if not path.exists():
                    # Genuinely gone -> it leaves the prompt.
                    continue
                size = path.stat().st_size
                if size > file_cfg.max_bytes:
                    raise BootstrapOverflowError(path, size, file_cfg.max_bytes)
                out[name] = path.read_text(encoding="utf-8")
            except BootstrapOverflowError as e:
                log.error(
                    "bootstrap: %s is over its cap (%d > %d bytes); keeping the "
                    "last good content for this turn", name, e.size, e.cap,
                )
                prev = self._contents.get(name)
                if prev is not None:
                    out[name] = prev
            except (OSError, UnicodeDecodeError) as e:
                log.error(
                    "bootstrap: could not re-read %s (%s); keeping the last good content",
                    name, e,
                )
                prev = self._contents.get(name)
                if prev is not None:
                    out[name] = prev
        self._contents = out

    def _load_all(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, file_cfg in self.config.bootstrap.files.items():
            content = self._load_one(name, file_cfg)
            if content is not None:
                out[name] = content
        return out

    def _load_one(self, name: str, file_cfg: Any) -> str | None:
        path = self.workspace_root / file_cfg.path

        # Guard (added 2026-10-20): enforce base-level-only loading.
        # The cold-files convention is identity/_cold/ = on-disk but
        # NOT auto-injected. Refuse any config.bootstrap.files entry
        # whose path resolves into a subfolder. Logs a warning so
        # the operator sees what happened instead of a silent skip.
        try:
            rel = path.relative_to(self.workspace_root)
            if len(rel.parts) > 1:
                log.warning(
                    "bootstrap: %s path=%r resolves to subfolder %s; "
                    "cold files must live under identity/_cold/ and must "
                    "NOT be added to config.bootstrap.files. Skipping.",
                    name, file_cfg.path, rel.parent,
                )
                return None
        except ValueError:
            # path is outside workspace_root entirely — should not happen
            # since we joined workspace_root / file_cfg.path, but guard
            # against it anyway. Treat as a config bug.
            log.error(
                "bootstrap: %s path=%r escapes workspace_root %s; refusing",
                name,
                file_cfg.path,
                self.workspace_root,
            )
            return None

        if not path.exists():
            log.info("bootstrap: %s not found at %s; skipping", name, path)
            return None

        size = path.stat().st_size
        if size > file_cfg.max_bytes:
            msg = f"FATAL: bootstrap file {path} is {size} bytes; cap is {file_cfg.max_bytes} bytes"
            if self.behavior == "fail":
                log.error(msg)
                raise BootstrapOverflowError(path, size, file_cfg.max_bytes)
            else:  # 'truncate'
                log.warning("%s — truncating to %d bytes", msg, file_cfg.max_bytes)
                with open(path, "rb") as f:
                    raw = f.read(file_cfg.max_bytes)
                # Decode safely; truncation may land mid-multi-byte char.
                return raw.decode("utf-8", errors="replace")

        content = path.read_text(encoding="utf-8")
        log.info(
            "bootstrap: loaded %s (%d bytes, cap %d) from %s",
            name, size, file_cfg.max_bytes, path,
        )
        return content

    def render(self) -> str:
        """Render the bootstrap block for injection into the prompt.

        Format (in document order per config.bootstrap.files):
            <bootstrap>
              <SOUL.md>...</SOUL.md>
              <USER.md>...</USER.md>
              ...
            </bootstrap>
        """
        if not self._contents:
            return ""
        parts = ["<bootstrap>"]
        for name in self.config.bootstrap.files.keys():
            if name in self._contents:
                parts.append(f"<{name}>")
                parts.append(self._contents[name])
                parts.append(f"</{name}>")
        parts.append("</bootstrap>")
        return "\n".join(parts)

    def reload(self) -> None:
        """Re-read all files. Use after operator edits + SIGHUP.
        Re-runs the same overflow checks as the constructor."""
        self._contents = self._load_all()

    def list_missing(self) -> list[str]:
        """For diagnostic: which configured files don't exist on disk."""
        missing = []
        for name, file_cfg in self.config.bootstrap.files.items():
            p = self.workspace_root / file_cfg.path
            if not p.exists():
                missing.append(name)
        return missing



class BootstrapRegistry:
    """Per-agent bootstrap registry. One BootstrapLoader per agent in cfg.app.agents.

    The harness loads bootstrap files from `pie/<agent>/identity/` (per the
    pie-directory design: each agent's identity/ is its own self-contained
    git repo at <owner>/<agent>). The registry is built once at app
    startup; per-request dispatch picks the loader for the active agent.

    Falls back to a per-request warning + empty render if the active agent
    isn't in the registry (e.g., typo in the request, or a removed agent).
    """

    def __init__(self, loaders: dict[str, "BootstrapLoader"]) -> None:
        self._loaders: dict[str, BootstrapLoader] = dict(loaders)
        self._agent_order: tuple[str, ...] = tuple(loaders.keys())
        log.info(
            "bootstrap_registry: %d agent(s) loaded: %s",
            len(self._loaders), ", ".join(self._loaders.keys()),
        )

    def get(self, agent_name: str) -> "BootstrapLoader | None":
        """Return the loader for `agent_name`, or None if not registered.

        The loader is REFRESHED first: the identity files are re-read
        from disk, so an edit (by the agent's own reverie, or by an
        operator) takes effect on the next turn instead of the next
        restart. The files are small and page-cached, so this is free.
        """
        loader = self._loaders.get(agent_name)
        if loader is not None:
            try:
                loader.refresh()
            except Exception as e:  # noqa: BLE001 - never break a turn
                log.error("bootstrap_registry: refresh failed for agent=%s: %s", agent_name, e)
        return loader

    def render(self, agent_name: str) -> str:
        """Render the bootstrap block for the given agent, or empty on miss."""
        loader = self.get(agent_name)
        if loader is None:
            log.warning(
                "bootstrap_registry: no loader for agent=%s; rendering empty bootstrap. "
                "Available: %s",
                agent_name, ", ".join(self._agent_order),
            )
            return ""
        return loader.render()

    def list_agents(self) -> list[str]:
        return list(self._agent_order)

    def reload_all(self) -> None:
        """Re-read every loader's files. Used after operator edits."""
        for name, loader in self._loaders.items():
            try:
                loader.reload()
            except Exception as e:
                log.error("bootstrap_registry: reload %s failed: %s", name, e)
