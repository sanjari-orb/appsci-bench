"""Main agent implementation for the Applied AI Scientist agent.

This module implements the agentic loop that orchestrates tool calls
to build task-specific artifacts.
"""
from __future__ import annotations

import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .config import AgentConfig
from .llm_client import LLMResponse, ToolCall, make_llm_client
from .model_access import model_access_brief
from ..prompts import PromptBuilder, build_system_prompt
from ..tools import (
    BaseTool,
    ToolContext,
    ToolResult,
    TalkToUserTool,
    MiniSweAgentTool,
    DeployArtifactTool,
    CompleteTool,
    OptimizeAgentTool,
    EvalGeneratorTool,
)
from ..tools.workspace_layout import HANDOFF_DIRNAME, OPTIMIZE_DIRNAME, ensure_layout

# Observability imports (optional, graceful degradation if not available)
try:
    from shared.observability import get_publisher
    from shared.observability.publisher import init_publisher
    from shared.observability.events import EventType, Source
    from shared.observability.model_io import model_call, model_call_end
    OBSERVABILITY_AVAILABLE = True
except ImportError:
    OBSERVABILITY_AVAILABLE = False
    get_publisher = None  # type: ignore

logger = logging.getLogger(__name__)


@dataclass
class AgentState:
    """Tracks the agent's state during execution."""
    messages: list[dict[str, Any]] = field(default_factory=list)
    turn_count: int = 0
    total_tokens: int = 0
    start_time: float = 0.0
    done: bool = False
    stop_reason: str | None = None
    build_complete: bool = False
    artifact_endpoint: str | None = None
    # Observability: span_id of the current turn's AGENT_TURN_START, so the turn's own model call
    # and tool invocations parent under it (and sub-agents nest below those) -> a real span tree.
    turn_span_id: str | None = None
    # Consecutive turns that produced no executable tool call -- a response cut off at the output
    # limit, one the provider blocked, an empty one, or one whose tool call the tool rejected as
    # malformed before running anything. Reset by
    # any turn that actually runs a tool. This counts only calls that never reached a tool, so
    # legitimate iteration (a deploy that fails four times while the agent fixes its Dockerfile) does
    # not trip the breaker.
    consecutive_unusable_turns: int = 0
    # Turns the provider blocked (finish_reason=content_filter), over the whole run. Reported in the
    # agent.run_end event so a build that ended on refusals says so instead of reading as an ordinary
    # no-submission.
    content_filtered_turns: int = 0
    # Why the most recent unusable turn was unusable, so the breaker can name what wedged the run.
    last_unusable_reason: str | None = None


#: A finish_reason that means the provider blocked the response. Anthropic's ``stop_reason: refusal``
#: arrives under this name through LiteLLM.
CONTENT_FILTER_FINISH_REASON = "content_filter"

#: finish_reasons that say nothing went wrong. Any other one is stated to the model in the nudge.
_NORMAL_FINISH_REASONS = frozenset({None, "", "stop", "end_turn", "tool_calls"})

CONTINUE_NUDGE = (
    "Please continue working on the task. Use the available tools to make progress. When the "
    "artifact is ready and deployed, call complete to end the BUILD phase."
)


# How many consecutive unusable turns end the build. The loop used to have no such check, so 37
# rejected calls in a row simply ran the turn counter down
# (order_to_cash_claude-sonnet-4-6_b1_l3_20260831_074504 spent 24 of its 40 turns re-sending the same
# empty call). Set well above normal self-correction -- one or two malformed calls followed by a good
# one is ordinary -- and well below the turn ceiling, so a wedged run ends while there is still budget
# to report rather than after silently consuming it all.
MAX_CONSECUTIVE_UNUSABLE_TURNS = 6


#: Cap on a single argument's recorded length, and on the whole `args` blob. An event is written per
#: tool call, and a `task` brief or an eval report pasted into a param can run to many KB -- worth
#: enough to record, not worth letting one call dominate the trace.
_TRACE_ARG_CHARS = 400
_TRACE_ARGS_CHARS = 2000


def _trace_args(params: dict) -> dict:
    """The call's arguments, trimmed, for the invocation event.

    `task` and `question` are omitted because they are emitted as their own fields already. Values are
    stringified and capped: this is a summary for a human reading the run, not a replayable record --
    the model's exact request is in the model-call I/O beside it.
    """
    out: dict[str, str] = {}
    budget = _TRACE_ARGS_CHARS
    for key, value in (params or {}).items():
        if key in ("task", "question") or value is None or budget <= 0:
            continue
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if len(text) > _TRACE_ARG_CHARS:
            text = text[:_TRACE_ARG_CHARS] + "…"
        out[key] = text
        budget -= len(text)
    return out


def _trace_output(tool: BaseTool, result: ToolResult) -> dict:
    """The parts of a tool's result it nominated for the trace (`trace_output_keys`).

    Never raises and never fails the call: a tool result that could not be summarised is worth less
    than the call itself, and the same rule the observability contract applies everywhere else holds
    here. Absent keys are skipped rather than recorded as null, so a reader can tell "this tool does
    not report that" from "it reported nothing".
    """
    keys = getattr(tool, "trace_output_keys", ()) or ()
    if not keys or not isinstance(result.output, dict):
        return {}
    try:
        return {k: result.output[k] for k in keys if k in result.output}
    except Exception:  # noqa: BLE001 -- instrumentation must never break the tool call
        logger.debug("Could not summarise a tool result for the trace", exc_info=True)
        return {}


def _requirements_instruction(
    user_sim_enabled: bool,
    requirements_dir: str | None,
    requirements_policies_dir: str | None = None,
) -> str:
    """The one sentence in the task message that tells the agent where its requirements come from.

    Two arms, one place. The control points at the stakeholder; the NO-USER arm points at the mount.
    The no-user wording mentions no user, absent or otherwise: that arm is not "the run where the
    stakeholder was taken away", it is a run whose requirements arrive as files, and an agent told
    that a stakeholder would normally exist has been told something about the BENCHMARK rather than
    about its task -- which it can only act on by looking for one. Kept as a single function because the
    task message states this twice (the ablated and the full workflow both end with it), and two
    hand-maintained copies of an arm-dependent sentence is exactly how a prompt ends up advertising a
    tool the registry does not have.

    `requirements_dir` None in the no-user arm is a real, reachable state -- a task with no documents
    to hand over -- and it must not render as a path-shaped blank. The wording drops to the mounts the
    agent does have rather than naming a directory that is not there. (The orchestrator refuses that
    combination up front; this is the second line of defence, for a hand-built config.)
    """
    if user_sim_enabled:
        return ("Clarify any unclear requirements with the user via talk_to_user and inspect the "
                "environment only as needed -- do not over-explore.")
    # The POLICY half is called out by name here and the documents half is not, deliberately. The
    # task message is short and the agent reads it last; the failure worth spending a clause on is
    # the one where an agent reads the output contract, believes it has the requirements, and never
    # opens the file holding the decision rules. Absent that half, the sentence stays as it was.
    policies = (f" The decision rules and thresholds are under `{requirements_policies_dir}` and are "
                "part of the requirements, not background reading."
                if requirements_policies_dir else "")
    where = (f"They are in `{requirements_dir}`, which is mounted read-only in your container; read "
             f"them in full before you design the artifact.{policies}"
             if requirements_dir else
             "They are in the files already mounted in your container.")
    return (f"Your requirements are written down and provided to you in full. {where} They are the "
            "complete statement of what a correct artifact must do, so build against them and "
            "inspect the rest of the environment only as needed -- do not over-explore.")


def _resolve_optimize_cycles(build_budgets: dict, config: Any) -> int | None:
    """How many optimization cycles `optimize_agent` may spend, or None for "until saturation".

    The task spec's `budgets.build.optimize_cycles` wins over the config fallback, the same precedence
    `turns` has over `max_turns`. Resolved with an explicit None check rather than `or`, because 0 is a
    real value here -- forbid any optimization attempt -- and `or` would read it as unset and hand back
    the fallback, silently doing the opposite of what was asked.

    A negative or non-integer value is ignored rather than honoured: it cannot mean anything sensible,
    and treating it as 0 would quietly disable optimization over a typo.
    """
    for candidate in (build_budgets.get("optimize_cycles"), getattr(config, "optimize_cycles", None)):
        if isinstance(candidate, bool) or not isinstance(candidate, int):
            continue
        if candidate < 0:
            logger.warning(
                "Ignoring optimize_cycles=%s: it must be >= 0 (0 forbids any optimization attempt, "
                "absent means run until the scores saturate).", candidate,
            )
            continue
        return candidate
    return None


class AppSciAgent:
    """Applied AI Scientist agent that builds task-specific artifacts.

    The agent follows this workflow:
    1. Receive task brief and environment access
    2. Explore environment via mini_swe_agent
    3. Gather requirements via talk_to_user (absent in the no-user arm, where they are
       mounted in the container instead -- see AgentConfig.user_sim_enabled)
    4. Design and implement artifact
    5. Deploy artifact
    6. Test artifact
    7. Signal completion via complete
    """

    def __init__(
        self,
        config: AgentConfig | None = None,
        system_prompt: str | None = None,
    ):
        self.config = config or AgentConfig.from_env()
        self.llm_client = make_llm_client(self.config)
        self.prompt_builder = PromptBuilder()

        # Tools. optimize_agent (PRD A.9) is a first-class stage the agent calls after it has a
        # working candidate and before it calls complete: it scores that candidate on the agent's own
        # internal eval, then uses the failing cases to improve it (prompt / backing model /
        # topology), reverting an attempt that does not help. It is meant to be called REPEATEDLY --
        # each call reports whether the score trajectory has saturated, which is the agent's signal
        # to stop and call complete.
        #
        # `talk_to_user` is first and conditional. Conditional because the NO-USER arm removes it
        # from the REGISTRY, not just the prompt: an unadvertised tool the model names anyway still
        # executes, and an arm whose whole claim is "there was nobody to ask" cannot leave a working
        # line to the simulator behind it. In that arm the orchestrator mounts the documents the
        # simulator would have held at `config.requirements_dir` instead (see AgentConfig). First
        # because insertion order is the order the tool schemas reach the model, and a control run
        # must stay byte-identical to one from before this knob existed.
        self.tools: dict[str, BaseTool] = {}
        if getattr(self.config, "user_sim_enabled", True):
            self.tools["talk_to_user"] = TalkToUserTool()
        self.tools |= {
            "mini_swe_agent": MiniSweAgentTool(),
            # deploy_artifact turns the authored artifact dir (Dockerfile + server entry) into a
            # healthy, reachable endpoint via the orchestrator's host-side deployer -- the agent no
            # longer hand-writes docker build/run. It's a thin HTTP client; under local/in-process
            # mode (no ORCHESTRATOR_SUBMIT_URL) it returns a clear "only under the orchestrator" error.
            "deploy_artifact": DeployArtifactTool(),
            # eval_generator turns the agent's own understanding of the task (captured in a handoff
            # file it writes via mini_swe_agent) into a runnable eval/ harness scored against the
            # deployed candidate. It's a thin wrapper: it delegates the actual authoring to
            # mini_swe_agent and only checks the precondition/verifies the output.
            "complete": CompleteTool(),
        }
        # The self-evaluation pair, present unless this run ablates them away. Removed from the
        # REGISTRY, not just the prompt: an unadvertised tool the model happens to name still executes,
        # and an ablation that left them callable would not be an ablation.
        if getattr(self.config, "optimization_enabled", True):
            self.tools["eval_generator"] = EvalGeneratorTool()
            self.tools["optimize_agent"] = OptimizeAgentTool()

        # System prompt (can be overridden or built from task context)
        self._system_prompt = system_prompt
        self._warn_if_prompt_advertises_absent_tools(system_prompt)

        # Initialize observability publisher (checks env vars for DB path)
        self._obs_publisher = None
        if OBSERVABILITY_AVAILABLE:
            self._obs_publisher = init_publisher(source=Source.APPSCI_AGENT, name="AppSci agent")

    def _warn_if_prompt_advertises_absent_tools(self, system_prompt: str | None) -> None:
        """Complain loudly when a pre-built prompt names a tool this agent does not have.

        A caller that hands over a system prompt bypasses `_build_system_prompt` entirely, so the
        registry-derived gating there does not apply -- and the two can drift apart silently. That
        happened: the production path (`AppliedScienceAgent.build`) built its prompt without
        `optimization_enabled`, so an ablation run advertised `eval_generator` and `optimize_agent`
        while the registry had already dropped them. The agent called one twice, got "Unknown tool"
        twice, and adapted around it -- so the run measured something that was neither arm of the
        experiment, and nothing in the logs said so.

        Logged rather than raised: a legitimate custom prompt might mention a tool name in prose (to
        explain its absence, say), and refusing to construct the agent over a substring match would be
        worse than the drift. The wrapper passing the flag is the actual fix; this is what makes the
        next instance of the same mistake visible instead of silent.
        """
        if not system_prompt:
            return
        absent = [name for name in ("eval_generator", "optimize_agent", "deploy_artifact",
                                    "talk_to_user", "mini_swe_agent", "complete")
                  if name not in self.tools and name in system_prompt]
        if absent:
            logger.error(
                "The system prompt advertises tool(s) this agent does not have: %s. The agent will "
                "call them and get \"Unknown tool\", wasting turns. Registered: %s. If this is an "
                "ablation, the prompt was built without matching the registry -- pass "
                "`optimization_enabled` to build_system_prompt.",
                ", ".join(absent), ", ".join(sorted(self.tools)),
            )

    def _build_system_prompt(
        self,
        task_description: str,
        environment: dict | None = None,
        budgets: dict | None = None,
    ) -> str:
        """Build the system prompt using the prompt builder.

        If a system prompt was provided at initialization, use that. Otherwise render it from the
        task context via `prompts/system_prompt.jinja2`.

        Raises whatever the render failed with. There is deliberately NO fallback prompt: the system
        prompt is what tells the agent its tools and the deploy/submit contract, so silently
        substituting a different one would run the whole BUILD phase against instructions nobody
        reviewed -- and it would surface as a mysteriously bad artifact score rather than as the
        template error it is. A broken template is a bug in this repo, not a condition to degrade
        around, so fail loudly and immediately.
        """
        if self._system_prompt:
            return self._system_prompt

        return self.prompt_builder.build_system_prompt(
            task_brief=task_description,
            environment=environment,
            budgets=budgets,
            # Same model-access section the AppliedScienceAgent path renders, so an agent constructed
            # without a pre-built prompt is told the same facts rather than having to find them.
            model_access=model_access_brief(self.config),
            # Read off the REGISTRY, not the config, so the prompt cannot describe a tool surface the
            # agent does not have. If those two ever disagree the prompt is the one that is wrong.
            optimization_enabled="optimize_agent" in self.tools,
            # Same rule: read off the REGISTRY, not the config. `requirements_dir` does come off the
            # config, because it is a path the orchestrator chose and no registry knows it -- but it
            # is only ever rendered when `talk_to_user` is absent, so the two cannot disagree about
            # whether this run has a stakeholder.
            user_sim_enabled="talk_to_user" in self.tools,
            requirements_dir=getattr(self.config, "requirements_dir", None),
            requirements_docs_dir=getattr(self.config, "requirements_docs_dir", None),
            requirements_policies_dir=getattr(self.config, "requirements_policies_dir", None),
            # Reviewer-requested: state the output-length and stuck-run limits once, generically, in
            # the system prompt, instead of `_process_response` explaining each failure type as it
            # happens. See that method for what still runs regardless of whether the model reads this:
            # a cut-off turn is discarded and counted toward the breaker either way.
            max_output_tokens=self.config.max_tokens,
            max_consecutive_unusable_turns=MAX_CONSECUTIVE_UNUSABLE_TURNS,
            # Named, not described. The prompt tells the agent where to write the eval handoff, and
            # "your workspace directory" was read as the agent's WORKDIR (/work) on a live run --
            # eval_generator then rejected the call because the file was not where it looks.
            workspace_dir=self.config.workspace_dir,
            # Rendered rather than hardcoded in the template, so the prompt cannot name a location the
            # layout has moved away from.
            handoff_dirname=HANDOFF_DIRNAME,
            optimize_dirname=OPTIMIZE_DIRNAME,
        )

    def get_tools_schema(self) -> list[dict[str, Any]]:
        """Get tool schemas in OpenAI format for litellm."""
        return [tool.to_openai_tool() for tool in self.tools.values()]

    def run(
        self,
        task_description: str,
        environment: dict | None = None,
        max_turns: int | None = None,
        budgets: dict | None = None,
    ) -> dict[str, Any]:
        """Run the agent to completion.

        Args:
            task_description: The task brief/description
            environment: Environment resources (databases, files, etc.)
            max_turns: Override max turns from config
            budgets: Budget constraints (turns, tokens, cost, time)

        Returns:
            Dict with artifact_endpoint, status, and conversation history
        """
        # Build the workspace tree HERE, before any tool runs. The eval handoff is written by a
        # mini_swe_agent call that precedes both tools that own this layout, and a sub-agent's own
        # `mkdir -p` would create the whole chain as the unprivileged user -- which would let it rename
        # the tool's state directory later. Root has to win that race, and this is the first point that
        # can. Idempotent, and both tools call it again defensively.
        ensure_layout(self.config.workspace_dir)

        # Extract budget limits from budgets dict (with fallbacks). `or` (not .get-default) so an
        # explicit null in the spec (no cap) coerces to 0/the config default rather than None.
        build_budgets = (budgets or {}).get("build") or {}
        max_turns = max_turns or build_budgets.get("turns") or self.config.max_turns
        max_tokens = build_budgets.get("tokens") or 0  # 0 means no limit
        max_wall_clock_s = build_budgets.get("wall_clock_s") or 0  # 0 means no limit
        # Optimization-cycle ceiling. Resolved separately from the `or` chain above because 0 is a
        # meaningful value here -- "forbid any optimization attempt" -- and `or` would silently turn it
        # into the config fallback, giving an operator who asked for no optimization the default
        # instead.
        optimize_cycles = _resolve_optimize_cycles(build_budgets, self.config)

        # Initialize tool context
        # Note: user_sim is accessed via AppsciAgentUserSimClient which reads env vars
        tool_context = ToolContext(
            workspace_dir=self.config.workspace_dir,
            artifact_dir=self.config.artifact_dir,
            environment=environment or {},
            config=self.config,
            optimize_cycles_allowed=optimize_cycles,
        )

        # Initialize state
        state = AgentState()
        state.start_time = time.time()

        # Build system prompt from task context
        system_prompt = self._build_system_prompt(
            task_description=task_description,
            environment=environment,
            budgets=budgets,
        )

        # Build initial messages
        state.messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",
             "content": self._format_task_message(
                 task_description, environment, optimize_cycles=optimize_cycles,
                 optimization_enabled="optimize_agent" in self.tools,
                 user_sim_enabled="talk_to_user" in self.tools,
                 requirements_dir=getattr(self.config, "requirements_dir", None),
                 requirements_policies_dir=getattr(self.config, "requirements_policies_dir", None),
                 workspace_dir=self.config.workspace_dir)},
        ]

        # Get tools schema
        tools_schema = self.get_tools_schema()

        # Main agentic loop
        while not state.done and state.turn_count < max_turns:
            # Check wall clock time budget before starting a new turn
            if max_wall_clock_s > 0:
                elapsed = time.time() - state.start_time
                if elapsed >= max_wall_clock_s:
                    logger.warning(f"Wall clock budget exceeded: {elapsed:.1f}s >= {max_wall_clock_s}s")
                    state.stop_reason = "budget_exceeded_time"
                    break

            state.turn_count += 1
            logger.info(f"Turn {state.turn_count}/{max_turns}")

            # Emit turn start event; keep its span so the turn's model call + tool invocations
            # parent under it (completing the turn -> tool -> sub-agent span tree).
            state.turn_span_id = None
            if self._obs_publisher:
                state.turn_span_id = self._obs_publisher.emit(
                    event_type=EventType.AGENT_TURN_START,
                    payload={"turn": state.turn_count, "max_turns": max_turns},
                )

            # Capture the FULL model I/O (input messages + completion) into the trace as a ModelCall;
            # emits model.request now and model.response after (with a ref to the stored full call).
            # Gate on the live publisher (set only when the obs module imported AND a run enabled it),
            # same as the emit() calls below -- so all observability here keys off one condition.
            #
            # Scoped with `model_call` so a RAISING chat() still writes its record. Without it the
            # emitted model.request left a `ref` the model_calls store could not resolve, and the
            # tracer builds its build-phase rows from those events: the row and its "full I/O ->"
            # button rendered, then /api/model_io 404'd and the modal came up empty. gateway 429s and
            # gateway timeouts land here, so on a throttled run this was routine, not exotic.
            with (model_call(state.messages, model=self.config.model, caller="agent_loop",
                             parent_span_id=state.turn_span_id)
                  if self._obs_publisher else contextlib.nullcontext(None)) as _mc:

                # Call LLM
                #
                # `temperature` is passed only when the config carries one: `llm_client` adds it to
                # the request only if a caller supplies it, so omitting the kwarg leaves the request
                # exactly as it was before this knob existed (provider default). Passing
                # `self.config.temperature` unconditionally would send `temperature=None` and reach
                # litellm as an explicit null.
                extra: dict[str, Any] = {}
                if self.config.temperature is not None:
                    extra["temperature"] = self.config.temperature
                response = self.llm_client.chat(
                    messages=state.messages,
                    tools=tools_schema,
                    **extra,
                )

                if _mc is not None:
                    model_call_end(
                        _mc,
                        content=response.content,
                        tool_calls=[{"name": tc.name, "arguments": tc.arguments}
                                    for tc in response.tool_calls],
                        usage=response.usage,
                        # Recorded so a truncated turn is visible in the trace. The loop acts on this
                        # below, but the trace could not show it: a tool call whose arguments arrived
                        # as `{}` read as the model deliberately sending nothing, which is how a
                        # length-capped response looked like 37 turns of incompetence rather than one
                        # ceiling being hit.
                        finish_reason=response.finish_reason,
                    )

            # Track token usage
            if response.usage:
                state.total_tokens += response.usage.get("total_tokens", 0)

            # Check token budget after LLM call
            if max_tokens > 0 and state.total_tokens >= max_tokens:
                logger.warning(f"Token budget exceeded: {state.total_tokens} >= {max_tokens}")
                state.stop_reason = "budget_exceeded_tokens"
                # Still process this response, but exit after
                self._process_response(response, state, tool_context, tools_schema)
                break

            # Process response
            self._process_response(response, state, tool_context, tools_schema)

            # Stop a wedged run rather than letting it spend the remaining turns. `_process_response`
            # keeps this counter: it rises only on turns that reached no tool at all (truncated,
            # blocked or empty responses, calls rejected as malformed) and resets the moment any tool
            # actually runs.
            # Without this the loop's only floor was the turn ceiling, which is how one truncation cost
            # 24 turns in order_to_cash_claude-sonnet-4-6_b1_l3_20260831_074504.
            if state.consecutive_unusable_turns >= MAX_CONSECUTIVE_UNUSABLE_TURNS:
                logger.error(
                    "Aborting build: %d consecutive turns produced no executable tool call "
                    "(limit %d). The agent is not reaching its tools; continuing would only consume "
                    "the remaining %d turns.",
                    state.consecutive_unusable_turns, MAX_CONSECUTIVE_UNUSABLE_TURNS,
                    max_turns - state.turn_count,
                )
                state.stop_reason = (
                    "stuck_content_filter"
                    if state.last_unusable_reason == CONTENT_FILTER_FINISH_REASON
                    else "stuck_no_executable_tool_calls"
                )
                break

        # Determine final stop reason if not already set
        if state.stop_reason is None:
            if state.build_complete:
                state.stop_reason = "complete"
            elif state.turn_count >= max_turns:
                state.stop_reason = "max_turns"
                logger.warning(f"Agent reached max turns ({max_turns}) without completing the build")

        elapsed_time = time.time() - state.start_time

        # The run's own account of how it ended. The orchestrator records only whether a submission
        # arrived, so without this a build that spent its turns on provider refusals
        # (crm_insights_claude-opus-5_b2_l3_20260920_175834) was indistinguishable from any other
        # clean no-submission.
        if self._obs_publisher:
            self._obs_publisher.emit(
                event_type=EventType.AGENT_RUN_END,
                payload={
                    "stop_reason": state.stop_reason,
                    "turns_used": state.turn_count,
                    "max_turns": max_turns,
                    "tokens_used": state.total_tokens,
                    "content_filtered_turns": state.content_filtered_turns,
                    "consecutive_unusable_turns": state.consecutive_unusable_turns,
                },
            )

        return {
            "status": "complete" if state.build_complete else "incomplete",
            "stop_reason": state.stop_reason,
            "artifact_endpoint": state.artifact_endpoint,
            "turns_used": state.turn_count,
            "tokens_used": state.total_tokens,
            "wall_clock_s": elapsed_time,
            "messages": state.messages,
        }

    def _process_response(
        self,
        response: LLMResponse,
        state: AgentState,
        tool_context: ToolContext,
        tools_schema: list[dict[str, Any]],
    ) -> None:
        """Process an LLM response, executing any tool calls."""
        # A response cut off at the output limit is incomplete by definition, so nothing in it is
        # trustworthy -- including a tool call that happens to parse. Streamed tool arguments are
        # accumulated as partial JSON, and a cut-off argument object can arrive as a syntactically
        # valid but semantically empty `{}` with the model's real text silently gone, which is exactly
        # what happened in order_to_cash_claude-sonnet-4-6_b1_l3_20260831_074504: ~4000 tokens of task
        # text vanished and the tool saw no arguments at all.
        #
        # The truncated assistant turn is NOT written to history. Recording it made the transcript
        # show the model deliberately calling a tool with empty arguments, and the next request
        # replayed that as an example to follow -- so the model kept reproducing the shape it saw. A
        # rejection the model can act on is worth nothing if the history teaches the opposite.
        #
        # There used to also be a reactive per-occurrence message here explaining what happened.
        # Review feedback on this PR pointed out that's one failure type handled one-off, and asked
        # for the general "responses are capped, and cut off = discarded" guidance to live once in the
        # system prompt instead (now there -- see system_prompt.jinja2's `max_output_tokens` section).
        # Dropped the reactive message rather than keep both: with nothing appended, the next request
        # replays the exact same messages, so a model that ignores the prompt guidance retries the
        # identical call under the identical prompt -- still bounded by the breaker below, just with no
        # extra nudge in between.
        if response.finish_reason == "length":
            state.consecutive_unusable_turns += 1
            attempted = ", ".join(sorted({tc.name for tc in response.tool_calls})) or "none"
            logger.warning(
                "Response truncated at the output limit (max_tokens=%s); discarding the partial turn "
                "instead of recording it. Tools attempted: %s. Consecutive unusable turns: %d",
                self.config.max_tokens, attempted, state.consecutive_unusable_turns,
            )
            return

        # A response the provider blocked is cut off where the block landed, so like a truncated one
        # nothing in it is trusted: a tool call that parses may still be partial, and recording the
        # turn would replay the blocked exchange on every later request. It goes to the no-tool-call
        # branch below, which counts it toward the breaker and tells the model why nothing ran.
        blocked = response.finish_reason == CONTENT_FILTER_FINISH_REASON

        if response.has_tool_calls and not blocked:
            # Add assistant message with tool calls to history
            state.messages.append(
                self.llm_client.format_assistant_message_with_tool_calls(
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )

            # Execute each tool call
            ran_a_tool = False
            for tool_call in response.tool_calls:
                result = self._execute_tool(tool_call, tool_context,
                                            parent_span_id=state.turn_span_id)
                # A tool that rejected its arguments never ran, so it says nothing about whether the
                # agent is making progress -- only that this call was unusable. A tool that ran and
                # failed (a deploy whose image will not build) IS progress of a kind: the agent gets a
                # real error to work from, and runs like that recover, so they must not feed the
                # breaker.
                if not result.invalid_call:
                    ran_a_tool = True

                # Add tool result to messages
                state.messages.append(
                    self.llm_client.format_tool_result(
                        tool_call_id=tool_call.id,
                        result=result.to_dict(),
                    )
                )

                # Check if the agent signalled completion
                if tool_call.name == "complete" and result.success:
                    state.done = True
                    state.build_complete = True
                    state.artifact_endpoint = tool_context.artifact_endpoint
                    break

            if ran_a_tool:
                state.consecutive_unusable_turns = 0
            else:
                state.consecutive_unusable_turns += 1
                logger.warning(
                    "Turn produced no executable tool call (every call was rejected as malformed). "
                    "Consecutive unusable turns: %d", state.consecutive_unusable_turns,
                )
        else:
            # No tool calls - add assistant message and prompt to continue
            if response.content and not blocked:
                state.messages.append({
                    "role": "assistant",
                    "content": response.content,
                })
            else:
                # A blocked or empty turn carries nothing to act on. These used to get only the nudge
                # below and never reached the breaker, so 29 refusals in a row ran the 40-turn ceiling
                # down in crm_insights_claude-opus-5_b2_l3_20260920_175834. A text-only reply is still
                # not counted: the model is saying something, and the nudge answers it.
                state.consecutive_unusable_turns += 1
                state.last_unusable_reason = response.finish_reason if blocked else "empty_response"
                if blocked:
                    state.content_filtered_turns += 1
                logger.warning(
                    "Turn produced %s (finish_reason=%s); nothing ran. Consecutive unusable turns: %d",
                    "a response the provider blocked" if blocked else "an empty response",
                    response.finish_reason, state.consecutive_unusable_turns,
                )

            # LLM didn't use any tools - prompt it to continue working. An abnormal finish_reason is
            # stated, as the sub-agent's format-error prompt does, so the model is not left to guess
            # why its turn produced nothing.
            nudge = CONTINUE_NUDGE
            if response.finish_reason not in _NORMAL_FINISH_REASONS:
                nudge = (f"Your last response ended with finish_reason={response.finish_reason} and "
                         f"was discarded, so nothing ran. " + nudge)
            state.messages.append({"role": "user", "content": nudge})

    def _format_task_message(
        self,
        description: str,
        environment: dict | None,
        optimize_cycles: int | None = None,
        optimization_enabled: bool = True,
        user_sim_enabled: bool = True,
        requirements_dir: str | None = None,
        requirements_policies_dir: str | None = None,
        workspace_dir: str = AgentConfig.workspace_dir,
    ) -> str:
        """Format the initial task message for the agent.

        `workspace_dir` is where the eval handoff must be written, and it is stated as a PATH rather
        than as "your workspace directory" -- the agent's WORKDIR is /work, it read that phrase as
        /work on a live run, and `eval_generator` rejected the call because the file was not where it
        looks. Passed in with the config default rather than read off `self.config`, matching
        `optimize_cycles` above, because this is also called on an instance that has no config.

        `optimize_cycles` decides which stopping rule the agent is given for `optimize_agent`: run
        until the scores saturate (None), or spend at most that many cycles. Passed in rather than read
        off the config, because the run's `budgets.build` overrides the config default and only the
        caller has resolved that.

        `optimization_enabled` False drops every mention of the self-evaluation tools, for a run that
        ablates them away. Defaults True so a caller that forgets it gets the full instructions rather
        than silently withholding half the workflow.

        `user_sim_enabled` False is the NO-USER arm: there is no stakeholder to ask, so every
        instruction to ask one is replaced with a pointer to `requirements_dir`, the mount holding the
        documents that stakeholder would have held. Both default to the control for the same reason
        as above -- a caller that forgets gets the benchmark as designed, not a silently altered arm.
        """
        parts = [f"## Task\n\n{description}"]

        if environment:
            resources = environment.get("resources", [])
            if resources:
                parts.append("\n## Available Resources\n")
                for r in resources:
                    # The agent is shown resource name/type/description only -- never host
                    # `location` paths (the spec is redacted before it reaches the agent).
                    label = r.get("name") or r.get("type", "resource")
                    parts.append(f"- **{label}** ({r.get('type', 'unknown')})")
                    if r.get("description"):
                        parts.append(f"  - {r['description']}")

        parts.append("\n## Instructions\n")
        parts.append("Your objective is to BUILD and DEPLOY the agent artifact described above "
                     "(follow the interface/architecture in the brief). Author a server entry "
                     "(server.py exposing GET /health and POST /respond) plus a Dockerfile whose CMD "
                     "launches it bound to 0.0.0.0:<port>, then call deploy_artifact to stand it up. "
                     "You do NOT run docker yourself -- deploy_artifact builds, runs, and "
                     "health-checks the artifact host-side under the container name / port declared "
                     "for this task.")
        parts.append("Once deploy_artifact reports the endpoint healthy, call complete (no "
                     "arguments) to end the BUILD phase. What is then evaluated is the FINAL CONTENT "
                     "of your artifact output directory, which the harness rebuilds and redeploys at "
                     "the handover -- NOT a snapshot frozen at your last deploy_artifact call. "
                     "deploy_artifact is a health check that proves your code builds and answers; it "
                     "does not decide what you submit, so an edit made after it still ships. Leave "
                     "the output directory in the state you want judged. If a deploy fails, it tells "
                     "you which step broke; fix your code and call deploy_artifact again "
                     "(redeploying is safe and idempotent).")
        if not optimization_enabled:
            # No self-evaluation tools this run (an ablation). Say nothing about them: an agent told to
            # call eval_generator and then handed no such tool wastes turns on "Unknown tool".
            parts.append(_requirements_instruction(user_sim_enabled, requirements_dir,
                                                  requirements_policies_dir)
                         + " Start building early; once you have a working deployed candidate, "
                         "call complete.")
            return "\n".join(parts)

        parts.append("Once you have a solid understanding of the task -- what it should do and how "
                     "to tell a correct response from an incorrect one -- ask mini_swe_agent to "
                     # The PATH, not "your workspace directory". Named because the agent has a
                     # WORKDIR of /work and read the phrase as that, wrote the handoff to
                     # /work/eval_handoff.md, and eval_generator rejected the call -- the location has
                     # to be as concrete here as the artifact output directory it replaced.
                     f"write that understanding down as `{workspace_dir}/{HANDOFF_DIRNAME}/eval_handoff.md` "
                     "-- that exact directory, NOT the artifact output directory (which holds only "
                     "the candidate itself) and NOT the agent's working directory. Start it with a "
                     "line stating the interaction pattern explicitly: "
                     "`Interaction pattern: single-turn` or `Interaction pattern: multi-turn`. Then "
                     "cover: the task, the candidate's interface, and the general criteria for "
                     "judging a response, drawing on anything you found relevant during exploration. "
                     "If the candidate holds a multi-turn conversation rather than answering one "
                     "request at a time, describe the user's persona/goal, what ends the "
                     "conversation, and how to judge the outcome -- derive that judgment by checking "
                     "the actual environment/rules for a given scenario, not by "
                     "inventing a plausible-looking answer. Do NOT have it invent specific test "
                     "cases, scenarios, or a scoring script -- that's eval_generator's job, from the "
                     "handoff you wrote. eval_generator will derive its scoring metrics from the "
                     "judging criteria you write here and record that derivation in the eval's "
                     "README, so keep those criteria concrete enough to score against. Then call "
                     "eval_generator to turn it into a runnable eval you "
                     "can use to check your own work. Do this before you rely on eval_generator: it "
                     "fails immediately if the handoff file isn't there yet.")

        parts.append("Building an internal eval is REQUIRED, not optional: you must call "
                     "eval_generator at least once before you call complete. Without it you have no "
                     "measurement of your own work, and optimize_agent has nothing to score "
                     "against, so it will refuse to run.")
        parts.append("Once eval_generator has produced a runnable eval and your candidate is "
                     "deployed, call optimize_agent. Each call runs your eval against the deployed "
                     "candidate itself and records the score against the checkpoint it was measured "
                     "on, then hands the report to a coding sub-agent that diagnoses the failures "
                     "and edits the candidate, which is redeployed for you.")
        parts.append("A change is NOT measured by the call that makes it -- the eval queries the "
                     "deployed container, so an edit only becomes measurable after the rebuild that "
                     "follows. When a call reports `awaiting_verification: true`, call "
                     "optimize_agent again: that next call scores the change and keeps it or rolls "
                     "it back. NEVER call complete while awaiting_verification is true -- the edit is "
                     "already in your output directory, and that directory is what gets submitted, so "
                     "you would ship a change nothing has measured rather than simply leaving it "
                     "behind.")
        if optimize_cycles is None:
            parts.append("Keep calling optimize_agent until it reports `saturated: true`; that is "
                         "the signal the scores have stopped moving and it is time to call "
                         "complete. Do not stop after one call just because the score went up, and "
                         "do not keep calling once it reports saturation.")
        elif optimize_cycles <= 0:
            parts.append("This run allows NO optimization cycles, so optimize_agent will score your "
                         "candidate and record the result but will not try to improve it. Call it "
                         "once to get your score, then call complete.")
        else:
            parts.append(
                f"This run allows {optimize_cycles} optimization cycle(s). A cycle is spent only "
                "when a call actually CHANGES the candidate -- a call that only measures it, that "
                "is rejected before doing any work, or that reports saturation costs nothing. So "
                f"do NOT count your calls against the {optimize_cycles}: keep calling optimize_agent "
                "and read `cycles_used` / `cycles_allowed` off each result to see what is left. "
                "Stop when it reports `budget_exhausted: true` (the allowance is spent) or "
                "`saturated: true` (the scores stopped moving before you spent it), whichever comes "
                "first, then call complete. Do not stop after one call just because the score went "
                "up, and do not stop early because you have made several calls -- a rejected or "
                "measure-only call has left the whole allowance intact. The allowance is a CEILING, "
                "not a quota: if the candidate is where you want it and you have nothing further "
                "worth trying, say so in your reasoning and call complete with cycles left. Spending "
                "a cycle on a change you do not believe in is worse than not spending it -- but "
                "NEVER stop while a change is awaiting verification."
            )
        # NOTE: keep this in sync with prompts/system_prompt.jinja2 -- neither surface may advertise
        # a tool that is absent from the registry above (an unregistered tool gets called and returns
        # "Unknown tool: ...", wasting a turn).
        parts.append(_requirements_instruction(user_sim_enabled, requirements_dir,
                                              requirements_policies_dir)
                     + " Start building early; once you have a working deployed candidate, "
                     "call complete.")

        return "\n".join(parts)

    @staticmethod
    def _log_tool_input(tool_name: str, params: dict) -> None:
        """One concise INFO line describing what the agent is asking the tool to do (build narrative)."""
        def t(x, n=240):
            s = "" if x is None else str(x)
            return s if len(s) <= n else s[:n] + "…"
        if tool_name == "talk_to_user":
            logger.info("  ask user: %s", t(params.get("question")))
        elif tool_name == "mini_swe_agent":
            logger.info("  code task: %s", t(params.get("task")))
        elif tool_name == "optimize_agent":
            logger.info("  optimize: endpoint=%s score_only=%s",
                        params.get("endpoint_url"), bool(params.get("score_only")))
        elif tool_name == "eval_generator":
            logger.info("  eval_generator: context=%s", t(params.get("context")))
        elif tool_name == "complete":
            logger.info("  complete build (deployed endpoint, ready for eval)")

    @staticmethod
    def _log_tool_output(tool_name: str, result: ToolResult) -> None:
        """One concise INFO line summarizing the tool's result (build narrative)."""
        def t(x, n=300):
            s = "" if x is None else str(x)
            return s if len(s) <= n else s[:n] + "…"
        if not result.success:
            logger.info("  -> FAILED: %s", t(result.error))
            return
        out = result.output or {}
        if tool_name == "talk_to_user":
            logger.info("  -> user replied: %s", t(out.get("answer")))
        elif tool_name == "mini_swe_agent":
            logger.info("  -> code result: %s", t(out.get("result")))
        elif tool_name == "optimize_agent":
            opt = out.get("optimization") or {}
            logger.info(
                "  -> optimize iter=%s: headline_metric=%s num_cases=%s saturated=%s "
                "attempt=%s accepted=%s checkpoint=%s",
                out.get("iteration"), out.get("headline_metric"), out.get("num_cases"),
                out.get("saturated"), opt.get("technique"), opt.get("accepted"),
                (out.get("checkpoint") or {}).get("name"),
            )
        elif tool_name == "eval_generator":
            logger.info("  -> eval harness: %s", t(out.get("eval_dir")))
        elif tool_name == "complete":
            logger.info("  -> build complete: %s", t(out.get("endpoint_url")))

    def _execute_tool(
        self,
        tool_call: ToolCall,
        context: ToolContext,
        parent_span_id: str | None = None,
    ) -> ToolResult:
        """Execute a single tool call."""
        tool_name = tool_call.name
        params = tool_call.arguments

        logger.info(f"Executing tool: {tool_name}")
        self._log_tool_input(tool_name, params)
        logger.debug(f"Tool params: {json.dumps(params, indent=2)}")

        # Emit tool invocation event
        tool_span_id = None
        tool_start_time = time.time()
        if self._obs_publisher:
            tool_span_id = self._obs_publisher.emit(
                event_type=EventType.AGENT_TOOL_INVOCATION,
                payload={
                    "tool": tool_name,
                    # `task` and `question` keep their own fields: they are what the tracer reads for
                    # a call's one-line preview, and every existing run already has them.
                    "task": params.get("task"),
                    "question": params.get("question"),
                    # Every OTHER argument, so a call is not recorded as having been made with none.
                    # `optimize_agent` is what showed this up: it appeared in the trace as
                    # {"tool": ..., "task": null, "question": null}, which reads like a tool invoked
                    # with no arguments rather than one whose arguments nothing thought to record.
                    "args": _trace_args(params),
                },
                parent_span_id=parent_span_id,
            )

        # Thread this invocation's span down so a tool that opens its own sub-spans
        # (mini-swe's swe.task_start) parents them under it -- the trace then forms a real
        # turn -> tool -> task tree instead of leaning on event ordering to associate a
        # dispatched mini-swe run with the turn that spawned it. None when observability is off.
        context.parent_span_id = tool_span_id

        if tool_name not in self.tools:
            result = ToolResult(
                success=False,
                output={},
                error=f"Unknown tool: {tool_name}",
                # Nothing ran, so this is an unusable call, not work that failed. Without the flag a
                # model looping on a hallucinated tool name would look like progress to the loop's
                # breaker and could run the turn budget down -- the exact wedge the breaker exists to
                # stop.
                invalid_call=True,
            )
            # Emit tool result event for unknown tool
            if self._obs_publisher:
                self._obs_publisher.emit(
                    event_type=EventType.AGENT_TOOL_RESULT,
                    span_id=tool_span_id,
                    payload={"success": False, "error": result.error},
                )
            return result

        tool = self.tools[tool_name]

        try:
            result = tool.execute(params, context)
            logger.info(f"Tool {tool_name} completed: success={result.success}")
            self._log_tool_output(tool_name, result)

            # Emit tool result event
            tool_duration_ms = (time.time() - tool_start_time) * 1000
            if self._obs_publisher:
                self._obs_publisher.emit(
                    event_type=EventType.AGENT_TOOL_RESULT,
                    span_id=tool_span_id,
                    payload={
                        "success": result.success,
                        "duration_ms": tool_duration_ms,
                        "answer": result.output.get("answer") if result.output else None,
                        # The reason a call FAILED. Absent before, so a tool that refused cleanly (as
                        # opposed to raising) recorded only `success: false` -- and the actual reason,
                        # which is the whole point of a refusal that carries an instruction, could only
                        # be recovered by digging it out of the next model call's input.
                        "error": result.error,
                        # Whatever THIS tool nominated as worth recording. Without it the trace kept
                        # only success + duration + talk_to_user's `answer`, so a tool whose entire
                        # point is a finding -- optimize_agent's score, saturation state and
                        # accept/reject verdict -- was recorded as "ok".
                        **_trace_output(tool, result),
                    },
                )

            return result
        except Exception as e:
            logger.exception(f"Tool {tool_name} failed with exception")
            result = ToolResult(
                success=False,
                output={},
                error=f"Tool execution failed: {str(e)}",
                # Deliberately NOT invalid_call: the tool ran and raised, so the agent made real
                # contact with the environment and has a real error to work from. Runs recover from
                # that, and counting it would let ordinary iteration trip the breaker.
            )

            # Emit tool result event for exception
            tool_duration_ms = (time.time() - tool_start_time) * 1000
            if self._obs_publisher:
                self._obs_publisher.emit(
                    event_type=EventType.AGENT_TOOL_RESULT,
                    span_id=tool_span_id,
                    payload={
                        "success": False,
                        "duration_ms": tool_duration_ms,
                        "error": str(e),
                    },
                )

            return result
