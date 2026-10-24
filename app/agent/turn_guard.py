"""In-flight turn registry + SIGTERM handling for the harness worker.

Why this exists
---------------
On 2026-10-21 21:12 Mal sent Evie a message ("ok ive made the actual
payment to a customer, please register that in the accounts system...") and
got no reply, ever. The turn was accepted and ran for ~14 minutes
(39 agent iterations, ~55 tool calls) inside a gunicorn gthread worker.
The harness service was then restarted -- twice, 25 seconds apart -- to
deploy a streaming fix, and the restart killed the worker mid-turn.

The user turn was in the turns table. The assistant turn was not. The
UI showed Mal's message and silence, and no amount of refreshing would
change that.

Why a registry
--------------
gunicorn's worker handles SIGTERM by stopping its accept loop
(gunicorn/workers/gthread.py:280). The in-flight request lives in a
ThreadPoolExecutor thread; when the process goes away, that thread
dies with it and the agent loop's `finally` block never gets to write
the partial turn it would otherwise persist on a client disconnect.

So the worker cannot rely on the request thread to clean up after
itself. This module keeps a process-local registry of the turns that
are currently running, and installs a SIGTERM handler that writes an
"interrupted" assistant turn for every registered turn BEFORE gunicorn
gets to exit the process.

Two details that matter:

* The handler CHAINS, it does not replace. gunicorn installs its own
  SIGTERM handler in `Worker.init_signals()` (gunicorn/workers/base.py:169),
  which runs BEFORE `load_wsgi()` (base.py:157) -- so installing our
  handler from `create_app()` chains onto gunicorn's handler rather
  than racing it. We capture the previous handler with
  `signal.getsignal()` and call it after ours so gunicorn still shuts
  down the way it expects.
* The interrupt path writes the turn row WITHOUT chunking/embedding it.
  Chunking calls the embedding server over HTTP; a signal handler must
  not block on the network, and systemd escalates to SIGKILL after
  TimeoutStopSec, which would lose the very turn we are trying to
  save.

If the turn does go on to finish normally after the SIGTERM (the
request thread can outlive the accept loop), the loop replaces the
interrupted marker with the real answer instead of adding a second
assistant turn -- see `AgentLoop._persist_interrupted_turn` and
`AgentLoop.handle_chat_streaming`.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("app.agent.turn_guard")

# conv_id (str) -> TurnState
_REGISTRY: dict[str, "TurnState"] = {}
_REGISTRY_LOCK = threading.RLock()
_INSTALLED = False

# How many NDJSON events to keep per in-flight turn. The browser
# replays these to rebuild the live bubble after a refresh; the tail
# is what matters, so the oldest are dropped first.
MAX_BUFFERED_EVENTS = 400


@dataclass
class TurnState:
    """Bookkeeping for one in-flight agent turn.

    The agent loop keeps this current while it runs (emission_iters,
    final_text, tool_iters). The SIGTERM handler reads it to write an
    interrupted turn before the process goes away.

    `events` is a bounded buffer of the NDJSON events the turn has
    emitted so far (2026-10-22). The streaming bubble in the web chat
    is client-side DOM: if the browser refreshes, the bubble is gone
    even though the turn is still running server-side, and there was
    nothing to re-attach to. This buffer is that missing server-side
    state -- GET /api/v1/entities/<name>/turns/active replays it so
    the UI can rebuild the live bubble after a refresh.
    """

    conv_id: Any
    agent_name: str
    user_id: int | None
    user_turn: Any
    user_text: str
    attachments: list[dict]
    t0: float
    model: str
    request_id: str | None = None
    started_at: float = 0.0
    emission_iters: list[dict] = field(default_factory=list)
    final_text: str | None = None
    tool_iters: int = 0
    # id of the interrupted-marker row, once written. The clean path
    # updates that row instead of inserting a second assistant turn.
    interrupted_turn_id: int | None = None
    events: list[dict] = field(default_factory=list)
    _persist: Callable[[str], int | None] | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def push_event(self, ev: dict) -> None:
        """Buffer one emitted event so a refreshed browser can replay it."""
        with self._lock:
            self.events.append(ev)
            if len(self.events) > MAX_BUFFERED_EVENTS:
                # Drop the oldest, keep the recent tail.
                del self.events[: len(self.events) - MAX_BUFFERED_EVENTS]

    def buffer_snapshot(self) -> dict:
        """Everything the UI needs to rebuild the live bubble."""
        with self._lock:
            return {
                "conversation_id": str(self.conv_id),
                "request_id": self.request_id,
                "agent": self.agent_name,
                "started_at": self.started_at,
                "elapsed_s": int(time.time() - self.started_at) if self.started_at else 0,
                "iter": len(self.emission_iters),
                "tool_calls": sum(
                    len(it.get("tool_calls") or []) for it in self.emission_iters
                ),
                "text": self.final_text or "",
                "events": list(self.events),
            }

    def key(self) -> str:
        return str(self.conv_id)

    def set_progress(
        self,
        *,
        emission_iters: list[dict] | None = None,
        final_text: str | None = None,
        tool_iters: int | None = None,
    ) -> None:
        """Update the live progress of the turn (called by the loop)."""
        with self._lock:
            if emission_iters is not None:
                self.emission_iters = emission_iters
            if final_text is not None:
                self.final_text = final_text
            if tool_iters is not None:
                self.tool_iters = tool_iters

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "conv_id": str(self.conv_id),
                "agent_name": self.agent_name,
                "user_id": self.user_id,
                "request_id": self.request_id,
                "iters": len(self.emission_iters),
                "tool_calls": sum(
                    len(it.get("tool_calls") or []) for it in self.emission_iters
                ),
                "final_text_len": len(self.final_text or ""),
                "interrupted_turn_id": self.interrupted_turn_id,
                "buffered_events": len(self.events),
            }

    def persist_interrupt(self, reason: str) -> int | None:
        """Write the interrupted marker for this turn. Returns the new
        turn id, or None if it could not be written."""
        if self._persist is None:
            return None
        return self._persist(reason)

    def interrupted_id(self) -> int | None:
        """Id of the interrupted-marker row, if one was written."""
        with self._lock:
            return self.interrupted_turn_id

    def mark_interrupted(self, turn_id: int) -> None:
        """Record that the interrupted marker was written."""
        with self._lock:
            self.interrupted_turn_id = turn_id


def register(state: TurnState) -> None:
    """Register an in-flight turn. Called by the agent loop."""
    with _REGISTRY_LOCK:
        _REGISTRY[state.key()] = state
    log.info("turn registered: %s", state.snapshot())


def unregister(conv_id: Any) -> None:
    """Remove a turn from the registry (it finished, one way or another)."""
    with _REGISTRY_LOCK:
        _REGISTRY.pop(str(conv_id), None)


def in_flight() -> list[dict]:
    """Snapshot of the currently in-flight turns (for diagnostics)."""
    with _REGISTRY_LOCK:
        return [s.snapshot() for s in _REGISTRY.values()]


def find(
    agent_name: str | None = None,
    user_id: int | None = None,
    request_id: str | None = None,
) -> "TurnState | None":
    """Find the in-flight turn matching the given selectors.

    The chat route knows the agent, the user and the request id; the
    loop knows the conversation id. Any one of them is enough to find
    the right turn. Most recently registered wins when several match.
    """
    with _REGISTRY_LOCK:
        candidates = list(_REGISTRY.values())
    matches = []
    for st in candidates:
        if agent_name is not None and st.agent_name != agent_name:
            continue
        if user_id is not None and st.user_id != user_id:
            continue
        if request_id is not None and st.request_id != request_id:
            continue
        matches.append(st)
    if not matches:
        return None
    matches.sort(key=lambda s: s.t0)
    return matches[-1]


def persist_all_interrupted(reason: str) -> list[str]:
    """Write an interrupted marker for every in-flight turn.

    Returns the conversation ids that were marked. Best effort: a
    failure on one turn must not stop the others.
    """
    with _REGISTRY_LOCK:
        states = list(_REGISTRY.values())
    marked: list[str] = []
    for st in states:
        try:
            turn_id = st.persist_interrupt(reason)
            if turn_id is not None:
                marked.append(st.key())
        except Exception:
            log.exception("failed to persist interrupted turn for conv=%s", st.key())
    return marked


def install_sigterm_handler() -> bool:
    """Chain a SIGTERM handler that persists in-flight turns before exit.

    Idempotent -- calling it twice is a no-op. Returns True if the
    handler was installed by this call.
    """
    global _INSTALLED
    with _REGISTRY_LOCK:
        if _INSTALLED:
            return False
        _INSTALLED = True

    try:
        prev = signal.getsignal(signal.SIGTERM)
    except Exception:  # pragma: no cover - defensive
        prev = None

    def _handler(signum, frame):
        try:
            marked = persist_all_interrupted(
                f"the harness server is restarting (signal {signum})"
            )
            if marked:
                log.warning(
                    "persisted %d interrupted turn(s) before shutdown: %s",
                    len(marked), marked,
                )
        except Exception:
            log.exception("interrupted-turn persistence failed on signal %s", signum)
        # Chain to whatever was installed before us -- gunicorn's worker
        # handler when running under gunicorn.
        if callable(prev):
            prev(signum, frame)
        else:
            raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _handler)
    except Exception:  # pragma: no cover - not the main thread, etc.
        log.exception("could not install SIGTERM handler")
        return False
    log.info("SIGTERM handler installed (chained to previous=%s)", prev)
    return True
