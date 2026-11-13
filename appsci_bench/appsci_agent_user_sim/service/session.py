"""The one conversation session the appsci-agent-user-sim service holds.

One service process == one session (the orchestrator starts/stops the process to open/close it). The
Session owns the run's transcript and turn counter, wrapping the stateless ``AppsciAgentLLMUserSim``: each
``ask`` appends the agent's question, calls the sim with the accumulated history, appends the reply,
and bumps the turn count. This is what makes ``/ask`` history-aware while the core stays stateless.
"""
from __future__ import annotations

import time

from ..core.usersim_core import ROLE_AGENT, ROLE_SIM, AppsciAgentLLMUserSim

# Observability imports (optional, graceful degradation if not available)
try:
    from shared.observability.publisher import init_publisher, get_publisher
    from shared.observability.events import EventType, Source
    OBSERVABILITY_AVAILABLE = True
except ImportError:
    OBSERVABILITY_AVAILABLE = False
    get_publisher = None  # type: ignore
    init_publisher = None  # type: ignore


class Session:
    """Holds the single run's conversation history around a bound AppsciAgentLLMUserSim."""

    def __init__(self, sim: AppsciAgentLLMUserSim, name: str | None = None) -> None:
        self._sim = sim
        self._history: list[dict] = []
        self._turn = 0

        # Initialize observability publisher (checks env vars for DB path). `name` is the user-sim's
        # role label (persona role); it rides on every usersim.* event + the user-sim model call.
        self._obs_publisher = None
        if OBSERVABILITY_AVAILABLE and init_publisher:
            self._obs_publisher = init_publisher(source=Source.USERSIM, name=name)

    @property
    def turn(self) -> int:
        """Number of completed exchanges so far."""
        return self._turn

    @property
    def system_prompt(self) -> str:
        """The bound persona's rendered system prompt (read-only; for inspection/debugging)."""
        return self._sim.system_prompt

    def ask(self, question: str, parent_span_id: str | None = None) -> tuple[str, int]:
        """Record the agent's question, get the stakeholder's reply, and return (message, turn).

        The sim is stateless; this session supplies the accumulated history each call so the reply
        is aware of the whole conversation so far.

        ``parent_span_id`` (the agent's talk_to_user tool-invocation span, forwarded over /ask) parents
        this exchange -- both the request/response events and the underlying model call -- under the
        turn that asked, so the tracer nests it there instead of floating it at top level.
        """
        # Emit request event. The whole question verbatim -- no length limit of any kind. These events
        # are the scored input to the elicitation diagnostic, and a clipped message reads as a fact the
        # persona never gave. The exchange parents under the asking turn.
        if self._obs_publisher:
            self._obs_publisher.emit(
                event_type=EventType.USERSIM_REQUEST,
                payload={"question": question, "turn": self._turn + 1},
                parent_span_id=parent_span_id,
                truncate=False,
            )

        # Time the LLM call. answer() gets the PRIOR history; _to_llm_messages appends this question
        # as the final user turn, so appending it to history first would duplicate it in the prompt.
        start_time = time.time()
        message = self._sim.answer(question, self._history, parent_span_id=parent_span_id)
        duration_ms = (time.time() - start_time) * 1000

        self._history.append({"role": ROLE_AGENT, "content": question})
        self._history.append({"role": ROLE_SIM, "content": message})
        self._turn += 1

        # Emit response event -- the whole answer verbatim, for the same reason as the request above.
        if self._obs_publisher:
            self._obs_publisher.emit(
                event_type=EventType.USERSIM_RESPONSE,
                payload={
                    "answer": message,
                    "turn": self._turn,
                    "duration_ms": duration_ms,
                },
                parent_span_id=parent_span_id,
                truncate=False,
            )

        return message, self._turn
