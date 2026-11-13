"""Event types and schemas for the observability system.

Defines the ObservabilityEvent dataclass and event type constants used
by all instrumented components (AppSciAgent, User Sim, Mini-swe-agent).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """Event type constants for observability events.

    Organized by source component.
    """

    # AppSciAgent events
    AGENT_TURN_START = "agent.turn_start"
    AGENT_LLM_REQUEST = "agent.llm_request"
    AGENT_LLM_RESPONSE = "agent.llm_response"
    AGENT_TOOL_INVOCATION = "agent.tool_invocation"
    AGENT_TOOL_RESULT = "agent.tool_result"
    # Once per build, when the main loop exits: why it stopped (complete, max_turns, a budget, the
    # stuck breaker) and how many turns the provider blocked.
    AGENT_RUN_END = "agent.run_end"

    # User Simulator events
    USERSIM_REQUEST = "usersim.request"
    USERSIM_RESPONSE = "usersim.response"

    # Mini-swe-agent events
    SWE_TASK_START = "swe.task_start"
    SWE_BASH_COMMAND = "swe.bash_command"
    # How that command ended: its exit code, or that it timed out and whether a process it started was
    # left holding its output. One per swe.bash_command, matched by step_num.
    SWE_BASH_RESULT = "swe.bash_result"
    SWE_FILE_WRITE = "swe.file_write"
    SWE_TASK_COMPLETE = "swe.task_complete"

    # Build phase lifecycle events
    BUILD_START = "build.start"
    BUILD_COMPLETE = "build.complete"

    # Evaluation phase events (emitted by the evaluator; correlate to the same trace as BUILD)
    EVAL_START = "eval.start"
    EVAL_CASE_SCORE = "eval.case_score"
    # The standalone elicitation diagnostic: how much of the material only the build-time stakeholder
    # held did the agent actually get out of them? Summary only -- the per-item table lives on the
    # report. Emitted once per run whether or not the task has a rubric, so the timeline always shows
    # the metric was considered rather than leaving "absent" and "not applicable" looking the same.
    EVAL_ELICITATION = "eval.elicitation"
    # The optimization diagnostic: how many times the build's optimize_agent loop ran, whether it
    # stopped early because the scores saturated, how its redeploys went, and how many durable
    # candidates it left. Counted out of the build's own trace rather than measured, so it costs
    # nothing. Emitted once per run whether or not the optimization route was enabled, so the
    # timeline can say "off on this run" instead of leaving absent and not-applicable identical.
    EVAL_OPTIMIZATION = "eval.optimization"
    EVAL_COMPLETE = "eval.complete"

    # Full model I/O (any component/phase). The event payload is small (model, caller, a short
    # preview, and a `ref` to a side-file); the full prompt messages + completion live in
    # runs/<trace_id>/model_io/<event_id>.json so the event stream stays lean and untruncated.
    MODEL_REQUEST = "model.request"
    MODEL_RESPONSE = "model.response"
    # A completion that RAISED (429, timeout, transport, auth, context-length). Its own type rather
    # than a flag on MODEL_RESPONSE so failures are countable: `grep model.error` answers "did this
    # run get rate-limited?" directly. Without it a failed call was visible only as a
    # `<model call failed: ...>` stub inside the model-I/O side-file -- present, but findable only by
    # opening every call, and indistinguishable in aggregate from a healthy run.
    MODEL_ERROR = "model.error"

    # A phase's task environment came up on a container that ALREADY EXISTED, so it holds whatever the
    # last run left in it rather than that phase's own store. Its own event type because it has to be
    # answerable across a whole sweep -- `grep task_env.reused` over the mirrored runs names every cell
    # whose environment was not the fresh one it was supposed to be. It was previously written only to
    # `BenchmarkResult.metadata`, which nothing persists or reads, and logged once near the start of a
    # multi-minute cell whose log the worker keeps only the tail of: a poisoned run scored normally,
    # exited 0, produced a valid report and entered the pass^k estimator with no machine-readable
    # marker at all.
    TASK_ENV_REUSED = "task_env.reused"

    def __str__(self) -> str:
        # Without this, str(EventType.BUILD_START) yields "EventType.BUILD_START"
        # rather than the wire value "build.start" (default __str__ for a
        # (str, Enum) member is the member repr, not its value). The publisher
        # stringifies event types before storage, so consumers depend on this.
        return self.value


class Source(str, Enum):
    """Source component identifiers."""

    APPSCI_AGENT = "appsci_agent"
    USERSIM = "usersim"
    MINI_SWE_AGENT = "mini_swe_agent"
    TASK_ENV = "task_env"
    ORCHESTRATOR = "orchestrator"
    EVALUATOR = "evaluator"
    # The eval-time user sim that drives the finished artifact. A separate source from USERSIM (the
    # build-time customer, which runs in its own container) and from EVALUATOR (the judges), even
    # though it shares the evaluator's process and publisher: the model-call store is sharded by
    # source, and the per-run usage rollup keys off that shard's filename, so a call tagged EVALUATOR
    # is indistinguishable from a judge's in every report. It ran on a different model too (a pinned
    # tau-bench user-sim id vs. the judge model), which is exactly what a per-source row is for.
    ARTIFACT_USERSIM = "artifact_usersim"
    # The elicitation judge, which scores the BUILD conversation rather than the artifact. Its own
    # source for the same reason as ARTIFACT_USERSIM above: it shares the evaluator's process and
    # publisher, so tagged EVALUATOR its calls would be indistinguishable from the artifact judges' in
    # every usage report -- and they are the one part of the eval phase whose cost scales with rubric
    # size rather than with the dataset, which is exactly the thing a per-source row exists to show.
    ELICITATION_JUDGE = "elicitation_judge"

    def __str__(self) -> str:
        return self.value


@dataclass
class ObservabilityEvent:
    """A single observability event emitted by an instrumented component.

    Attributes:
        event_id: Unique identifier for this event (UUID).
        timestamp: ISO8601 UTC timestamp with microseconds.
        source: The component that emitted this event.
        event_type: The type of event (from EventType enum).
        trace_id: Per-run correlation ID (from orchestrator).
        task_id: The task this run is for (e.g. "order_to_cash"). Denormalized
            onto every event so a future shared/consolidated trace DB can filter
            and aggregate by task directly, without parsing it out of trace_id.
        span_id: Per-operation correlation ID.
        parent_span_id: For nested operations (e.g., tool call -> swe steps).
        name: Human-readable role label of the agent that emitted this event
            (e.g. "AppSci agent", "mini-swe", the user-sim persona role). Optional;
            None on older traces / when the emitter didn't set one. Distinct from
            `source` (the coarse component enum) -- the dashboard prefers `name` for
            display and falls back to a source->label map when absent.
        payload: Event-specific data.
    """

    source: str
    event_type: str
    trace_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    span_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_span_id: str | None = None
    task_id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ObservabilityEvent:
        """Create an event from a dictionary."""
        return cls(**data)

    def truncate_payload(self, max_chars: int = 500) -> ObservabilityEvent:
        """Return a new event with payload values truncated.

        Used to prevent excessively large payloads in the database.
        """

        def truncate_value(v: Any, max_len: int) -> Any:
            if isinstance(v, str) and len(v) > max_len:
                return v[:max_len] + "..."
            if isinstance(v, dict):
                return {k: truncate_value(vv, max_len) for k, vv in v.items()}
            if isinstance(v, list):
                return [truncate_value(item, max_len) for item in v]
            return v

        truncated_payload = truncate_value(self.payload, max_chars)
        return ObservabilityEvent(
            event_id=self.event_id,
            timestamp=self.timestamp,
            source=self.source,
            event_type=self.event_type,
            trace_id=self.trace_id,
            task_id=self.task_id,
            span_id=self.span_id,
            parent_span_id=self.parent_span_id,
            name=self.name,
            payload=truncated_payload,
        )


def create_span_id() -> str:
    """Generate a new span ID."""
    return str(uuid.uuid4())
