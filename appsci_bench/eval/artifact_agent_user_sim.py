"""Artifact agent user sim -- Part 3 of the evaluator system.

Drives a multi-turn conversation against a finished artifact during eval: the sim initiates and
leads every turn toward the sample's `user_task`/`user_persona`, deciding for itself when
`user_stop_criteria` has been met. This is the opposite direction from the build-time
appsci-agent-user-sim (which only answers, never leads, never stops itself) -- see
docs/evaluator_system_design.md for the full behavioral contrast between the two.

`ArtifactAgentUserSimulator` is what `eval/generation.py` drives a `multi_turn` case through.
`ScriptedArtifactAgentUserSim` is a deterministic test stub (replays a fixed message list, then
stops) for tests/scripts that want a real session lifecycle without an LLM call.

MODEL CHOICE: this sim runs on tau-bench's user-simulator model, not the repo-wide `MODEL_*` Claude
default -- see `TAU_BENCH_USER_SIM_MODEL` and `resolve_user_sim_config()` below.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from typing import Any, Callable, Protocol, runtime_checkable

from jinja2 import Template
from pydantic import BaseModel, Field, model_validator

from appsci_bench.common.model_config import ModelConfig
from shared.observability.events import Source

from .datatypes import ArtifactAgentQuery, ConversationTurn, MultiTurnInput, UserTool
from .helpers.llm import llm_judge_call
from .helpers.transcript import render_conversation_for_judge
from .step_budget import StepBudget, check_budget

logger = logging.getLogger(__name__)

# Which shard of the model-call store this sim's calls are filed under. It runs inside the evaluator's
# process, on the evaluator's publisher, so without this override its turns are recorded as the
# evaluator's own -- and since the per-run usage rollup groups by the shard filename, they are then
# added to the judges' line with no way to separate them after the fact (one airline run reported 376
# "evaluator" calls that were in truth judges plus this sim, on two different models at two different
# prices). Hard import, like `eval_system.py`'s: the enum is a plain constant, not a live publisher.
_MODEL_CALL_SOURCE = Source.ARTIFACT_USERSIM

# The model tau-bench uses to simulate the user, adopted here so our eval-time user simulator is
# comparable to the benchmark our tasks are converted from.
#
# THE AUTHORITY IS THE LEADERBOARD SUBMISSIONS, NOT `src/tau2/config.py`. This used to track tau2's
# `DEFAULT_LLM_USER` (the default for its `--user-llm` flag), which is still `gpt-4.1-2025-04-14` at
# v1.0.1 -- but that constant is only a default, and the published runs override it. Reading the
# submission JSONs the leaderboard renders from, EVERY text airline submission at `tau2_bench_version`
# 1.0.1 declares `user_simulator: "gpt-5.2"` -- including both rows docs/baselines.md holds up as our
# targets (Claude Opus 4.5 at airline pass_1 84.0, Qwen3.5-397B at 81.5). The rows still declaring
# `gpt-4.1-2025-04-14` are all on v0.1.3/0.1.3, the tau2 era; rows declaring `v1.0` are the voice board,
# where the field carries a sim *version* rather than a model. Matching a code default we are not
# measured against, instead of the sim the published numbers actually ran, is the wrong kind of
# faithfulness -- so this follows the declared field. No changelog entry announces the switch; it is a
# submission-time run choice, which is precisely why reading the code was misleading here.
#
# FLOATING ON PURPOSE, reversing the previous rule. The old pin was a dated snapshot specifically so an
# alias could not silently re-point and move eval scores with no code change. `gpt-5.2` has no snapshot
# suffix in any submission, so a dated pin would name something the leaderboard does not, and drift from
# the thing we are trying to match. We accept the floating id and inherit its risk: if OpenAI re-points
# gpt-5.2, our scores can move without a commit here. That is now a known uncontrolled variable rather
# than a guarantee (see docs/baselines.md).
#
# Deliberately not Claude like the other three roles (`AGENT_MODEL`, `APPSCI_USERSIM_MODEL`,
# `JUDGE_MODEL` -- see common/model_config.py's per-role table): the agent, the build-time customer
# sim, and the judges are ours to choose, while the entity being *measured against* a public
# benchmark's user simulator matches that benchmark instead.
TAU_BENCH_USER_SIM_MODEL = "openai/gpt-5.2"

# This role's model var. Unlike its siblings (`APPSCI_USERSIM_MODEL`/`JUDGE_MODEL`, which go through
# `config_for()` and so fall back to `MODEL_NAME` over shared credentials), this one does NOT fall back
# and has no key/base_url pair: credentials are left to LiteLLM's provider-standard resolution
# (`OPENAI_API_KEY` for the default; `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` etc. for an override).
# Both differences follow from the same fact -- this is the one role pinned cross-provider, so
# inheriting an Anthropic gateway's `MODEL_BASE_URL` would point an `openai/` id at the wrong endpoint.
_MODEL_ENV_VAR = "ARTIFACT_USERSIM_MODEL"

# Gateway ids that serve the pinned model under a different name. `the setup steps in README.md`
# has to name `openai/openai-gpt-5.2` because the gateway rejects the first-party id
# (`unknown model "gpt-5.2"` -- confirmed against the live gateway, which advertises the `openai-`
# prefixed name and answers on it), so a strict `!=` against the constant would flag the documented
# internal path as non-comparable on every run -- crying wolf on the common case and teaching readers to
# skip `report.warnings`. These are NEAR-parity, deliberately not folded into
# `TAU_BENCH_USER_SIM_MODEL`: same model, but what the gateway actually routes an alias to is not
# verifiable from our side, which is exactly the distinction `parity_status()` keeps.
#
# The gpt-4.1 ids this used to hold are deliberately NOT kept here. They now classify as DIVERGED, so an
# environment still exporting the old override gets a warning and a line on the report rather than
# silent acceptance -- the failure this switch is most likely to produce is a stale `ARTIFACT_USERSIM_MODEL`
# somewhere, and it should be loud.
_NEAR_PARITY_ALIASES = frozenset({"openai/openai-gpt-5.2"})

# The three states a resolved model can be in relative to the pin. One function so the resolution-time
# log and the report-time note can never disagree about what counts as comparable.
PARITY_EXACT = "exact"      # the pinned snapshot itself
PARITY_NEAR = "near"        # a known gateway alias for it -- same family, snapshot unverifiable
PARITY_DIVERGED = "diverged"  # a genuinely different model: scores are not tau-bench-comparable


def parity_status(model: str) -> str:
    """Classify `model` against `TAU_BENCH_USER_SIM_MODEL`: exact / near / diverged."""
    if model == TAU_BENCH_USER_SIM_MODEL:
        return PARITY_EXACT
    if model in _NEAR_PARITY_ALIASES:
        return PARITY_NEAR
    return PARITY_DIVERGED


def resolve_user_sim_config(env: dict[str, str] | None = None) -> ModelConfig:
    """This sim's model config: `TAU_BENCH_USER_SIM_MODEL`, overridable via `ARTIFACT_USERSIM_MODEL`.

    Two kinds of override, graded by `parity_status()` rather than treated alike: a known gateway alias
    for the pinned model (`_NEAR_PARITY_ALIASES`, e.g. what gateway calls gpt-5.2) is near-parity and
    logs at INFO; any other model makes eval-time user behavior no longer tau-bench-comparable, which
    logs a warning AND lands on the report -- fine for local debugging, not for a reported score.

    An override reaches a gateway through the PROVIDER-STANDARD vars for whatever provider it names
    (``ANTHROPIC_BASE_URL``/``ANTHROPIC_AUTH_TOKEN`` for an ``anthropic/`` id, ``OPENAI_BASE_URL``/
    ``OPENAI_API_KEY`` for an ``openai/`` one) -- NOT through ``MODEL_BASE_URL``/``MODEL_API_KEY``,
    which this config never reads (see ``_MODEL_ENV_VAR``). Getting that wrong surfaces as an
    immediate auth error, not a silently wrong model.
    """
    env = os.environ if env is None else env
    model = (env.get(_MODEL_ENV_VAR) or "").strip() or TAU_BENCH_USER_SIM_MODEL
    status = parity_status(model)
    if status == PARITY_DIVERGED:
        # Loud on purpose: comparability to tau-bench is the only reason this model is pinned, and a
        # divergence is invisible in the resulting numbers. `run_evaluation` also records this one in
        # the report's `warnings` -- reserved for this case so the signal keeps discriminating.
        logger.warning(
            "eval-time user sim is on %r, NOT tau-bench's %r (%s override) -- this run's scores are "
            "not tau-bench-comparable", model, TAU_BENCH_USER_SIM_MODEL, _MODEL_ENV_VAR)
    elif status == PARITY_NEAR:
        # Informational, not a warning: the gateway alias is the documented internal path, so this
        # fires on the common case. The caveat is real but small -- same model family, snapshot behind
        # the alias unverifiable -- and it is stated once in docs rather than shouted per run.
        logger.info(
            "eval-time user sim is on %r, a gateway alias for tau-bench's %r -- near-parity (same "
            "model; what the gateway routes the alias to is not verifiable from here)",
            model, TAU_BENCH_USER_SIM_MODEL)
    return ModelConfig(model=model)


class _UserToolCall(BaseModel):
    """One tool the simulated user chose to invoke themselves this turn."""
    model_config = {"extra": "forbid"}
    name: str
    arguments: dict = Field(default_factory=dict)


class _SimTurn(BaseModel):
    """One simulated user turn: a stop judgment and the next message, from a single model call.

    `model_config = {"extra": "forbid"}` matches Part 2's `JudgeVerdict` drift-detection
    convention (`helpers/llm.py`) -- a hallucinated or renamed field must fail loud, not vanish
    silently. The `message` KEY is always required so the generated schema tells the model to emit
    it. An active turn may leave its VALUE empty only while calling a user-held tool: the simulator
    is called again after the result is available so the artifact never receives an empty message.
    `attach_files` may name a file already sent in an earlier turn -- the transcript already shows
    what's been sent and when, so resending is a legitimate persona choice ("let me send that
    again"), not something this schema forbids.

    `call_user_tool` is the customer acting for themselves, in the same turn as speaking. It stays
    optional and unmentioned in the prompt unless the case grants the user tools, so a task that does
    not model user-side actions produces exactly the turns it always did.
    """
    model_config = {"extra": "forbid"}
    reasoning: str
    stopped: bool
    # No default on purpose: `str | None = None` makes JSON Schema call this field optional even
    # though every response must include the key. Required-but-nullable keeps stopped turns and
    # tool-only steps representable while advertising the real output contract to the model.
    message: str | None
    attach_files: list[str] = Field(default_factory=list)
    call_user_tool: _UserToolCall | None = None

    @model_validator(mode="after")
    def _message_or_tool_required_unless_stopped(self) -> "_SimTurn":
        if not self.stopped and not self.message and self.call_user_tool is None:
            raise ValueError(
                "message is required when stopped is False unless call_user_tool is present")
        return self


_SYSTEM_PROMPT = (
    "You are role-playing a real end-user of a software product, in conversation with that "
    "product (the artifact). You initiate and drive the conversation toward your task -- the "
    "artifact only responds to you, it does not lead. Speak the way a real person would: "
    "naturally, informally, and imperfectly, not like a QA test script. You know your own "
    "available files, and any files the artifact has sent you, by name and type only -- you "
    "cannot see file contents in either direction, only that a file exists. Do not invent "
    "knowledge of what is inside a file you have not been shown. You may attach an available "
    "file more than once across turns if that's what a real person would do (e.g. resending on "
    "request) -- the conversation so far already shows what you've sent and when. Reason about "
    'whether your stop criteria has been met; always include the "message" key, using null only when '
    "you are stopping."
)

_TOOL_INSTRUCTIONS = (
    " You also hold tools of your own, listed below, and using one is part of playing your role: when "
    'your task says to do something yourself, DO it (set "call_user_tool") rather than only talking '
    "about it. Your task description is the authority on whether and when to act. Only ever use a "
    "tool from your own list, never the product's and do not invent arguments you were not given -- "
    "if you genuinely lack what a tool needs, ask for it instead. You may act and speak on the same "
    'turn. If you need the tool result before speaking, set "message" to an empty string while '
    "calling the tool; you will receive the result before choosing what to say."
)

_TEMPLATE = Template(
    "Your task: {{ user_task }}\n"
    "Your persona: {{ user_persona }}\n"
    "Judge `stopped: true` once this is met: {{ user_stop_criteria }}\n"
    "\n"
    "{% if is_opening_turn %}"
    "The conversation has not started yet -- this is your opening message. `stopped` must be "
    "`false`.\n"
    "{% else %}"
    "Conversation so far:\n"
    "{{ transcript }}\n"
    "{% endif %}"
    "\n"
    "Your files available to attach (name, type):\n"
    "{% if available_files %}"
    "{% for f in available_files %}"
    "- {{ f.file_name }} ({{ f.file_type }})\n"
    "{% endfor %}"
    "{% else %}"
    "(none)\n"
    "{% endif %}"
    "{% if user_tools %}"
    "\nYour own tools, which you may call yourself:\n"
    "{% for t in user_tools %}"
    "- {{ t.name }}{% if t.description %}: {{ t.description }}{% endif %}\n"
    "{% if t.required %}    required arguments: {{ t.required | join(', ') }}\n{% endif %}"
    "{% if t.optional %}    optional arguments: {{ t.optional | join(', ') }}\n{% endif %}"
    "{% if not t.required and not t.optional %}    takes no arguments\n{% endif %}"
    "{% endfor %}"
    "{% if used_tools %}"
    "\nTools you have already used in this conversation:\n"
    "{% for u in used_tools %}"
    "- {{ u.name }}({{ u.arguments }}) -> {{ u.outcome }}\n"
    "{% endfor %}"
    "{% else %}"
    "\nYou have not used any of your own tools yet.\n"
    "{% endif %}"
    "{% endif %}"
)


def user_tool_view(tools: list[UserTool]) -> list[dict]:
    """One descriptor per granted tool: name, description, and its required/optional parameter NAMES.

    Reads the signature the CASE carries (`MultiTurnInput.user_tools`). A tool declared as a bare name
    renders with no arguments at all, which is what every caller got before signatures existed -- and
    which is why cases needing a customer-side action used to fail. A simulator handed only a name
    cannot fill a tool's arguments: its own instructions tell it not to invent values, so it either
    declines to act or guesses parameter names and is rejected by the environment with a 400. Names
    alone are a silent no-op for any tool that takes arguments; the one tool banking has that takes
    none is the one the simulator reliably called.
    """
    out: list[dict] = []
    for tool in tools:
        params = (tool.parameters or {}).get("properties") or {}
        required = list((tool.parameters or {}).get("required") or [])
        out.append({
            "name": tool.name,
            "description": (tool.description or "").strip().split("\n")[0],
            "required": required,
            "optional": [k for k in params if k not in required],
        })
    return out


def render_sim_prompt(
    *,
    sample: MultiTurnInput,
    conversation: list[ConversationTurn],
    used_tools: list[dict] | None = None,
) -> str:
    """Render this turn's user-prompt content: task/persona/stop-criteria, the transcript so far
    (via `render_conversation_for_judge` -- files reduced to name+type, never bytes, in both
    directions), and every one of the sample's files (name+type only). The available-files list
    is not filtered by send history -- whether a file has already been sent is visible in the
    transcript itself, so there is no separate "not yet sent" state to track or drift out of sync.

    The user-tools block appears ONLY when the case grants the user tools, so the prompt a task
    without them renders is unchanged. Each granted tool's signature comes from the sample itself.
    `used_tools` is this session's own call history: a tool call never appears in the transcript (it is
    not something said to the artifact), so without it the sim has no way to know it has already acted
    and would re-submit on every turn.
    """
    return _TEMPLATE.render(
        user_task=sample.user_task,
        user_persona=sample.user_persona,
        user_stop_criteria=sample.user_stop_criteria,
        is_opening_turn=not conversation,
        transcript=render_conversation_for_judge(conversation),
        available_files=[{"file_name": f.file_name, "file_type": f.file_type} for f in sample.files],
        user_tools=user_tool_view(sample.user_tools),
        used_tools=used_tools or [],
    )


def _coerce_tool_arguments(sample: MultiTurnInput, tool_name: str, arguments: dict) -> dict:
    """Match model-produced values to the granted tool's declared JSON schema.

    The structured turn schema can describe ``call_user_tool.arguments`` only as a generic object;
    the concrete signature lives on the selected ``UserTool`` in the sample. In particular, banking's
    ``call_discoverable_user_tool.arguments`` is a JSON-encoded string, while models commonly emit the
    more natural nested object. Serialize that object before the environment validates the call.
    Unknown tools and values that already match their declared type pass through unchanged.
    """
    granted = next((tool for tool in sample.user_tools if tool.name == tool_name), None)
    properties = ((granted.parameters if granted else {}) or {}).get("properties") or {}
    coerced = dict(arguments)
    for name, value in coerced.items():
        if properties.get(name, {}).get("type") == "string" and isinstance(value, (dict, list)):
            coerced[name] = json.dumps(value, separators=(",", ":"))
    return coerced


@runtime_checkable
class ArtifactAgentUserSimulator(Protocol):
    """What the multi-turn generation loop drives a `multi_turn` case through, once wired up."""

    def create_session(self, sample: MultiTurnInput) -> str: ...

    def ask(
        self, session_id: str, conversation: list[ConversationTurn],
        *, step_budget: StepBudget | None = None,
    ) -> ArtifactAgentQuery | None: ...

    def delete_session(self, session_id: str) -> None: ...


class ArtifactAgentUserSim:
    """The real, LLM-backed artifact agent user sim.

    Session-based so many concurrent eval workers (`generation.py`'s thread pool, one worker per
    (data point, repeat) unit) can each drive their own conversation without sharing mutable
    state: a session just maps a `session_id` to the sample it was created for, so
    `create_session`/`delete_session` (plain dict insert/delete) are the only lock-guarded
    operations -- there is no other per-session state to protect, since which files have been
    sent is derived from the caller-owned transcript, not tracked here. An unknown or
    already-deleted `session_id` passed to `ask`/`delete_session` always raises `ValueError` -- a
    caller passing a stale or made-up id is a bug, not a no-op case (same convention as the
    appsci-agent-user-sim's unknown-resource-name errors).

    `config=None` (the normal case) resolves via `resolve_user_sim_config()`, i.e. the tau-bench
    user-simulator model -- NOT `llm_judge_call`'s own `MODEL_*` fallback, which is the
    Claude-defaulted namespace the eval judges and the build-time sim use. Resolution happens here in
    `__init__` (pure env read, no network) so the model in effect is fixed for the sim's whole
    lifetime rather than re-read per turn.
    """

    def __init__(self, config: ModelConfig | None = None,
                 tool_executor: "Callable[[str, dict], Any] | None" = None):
        self._config = config or resolve_user_sim_config()
        self._sessions: dict[str, MultiTurnInput] = {}
        # Per-session record of the user's OWN tool calls. Kept here rather than in the transcript
        # because a user-side call is not something said to the artifact -- it never belongs in the
        # conversation the artifact sees or a judge reads. Without it the sim cannot tell it has
        # already acted, and would submit the same application on every turn.
        self._tool_calls: dict[str, list[dict]] = {}
        # Supplied by the run when the task's environment can execute user-held tools. None (the
        # default, and every task but banking today) means the sim can only talk: a requested tool
        # call is recorded as unavailable rather than silently dropped.
        #
        # NOTE the asymmetry with the tools' SIGNATURES, which are not passed here: an executor is a
        # live capability of the running environment, so only the run can supply it, while a signature
        # is static per-case data and rides in the sample (`MultiTurnInput.user_tools`).
        self._tool_executor = tool_executor
        self._lock = threading.Lock()

    @property
    def model(self) -> str:
        """The LiteLLM model id this sim is running on -- read by `run_evaluation` so a non-parity
        run says so on its report (see `TAU_BENCH_USER_SIM_MODEL`)."""
        return self._config.model

    def create_session(self, sample: MultiTurnInput) -> str:
        session_id = str(uuid.uuid4())
        with self._lock:
            self._sessions[session_id] = sample
            self._tool_calls[session_id] = []
        return session_id

    def ask(
        self, session_id: str, conversation: list[ConversationTurn],
        *, step_budget: StepBudget | None = None,
    ) -> ArtifactAgentQuery | None:
        """Use only the caller's configured budget; None leaves simulation steps unlimited.

        Generation supplies a shared budget when max_steps is configured and counts spoken/stop
        messages itself. This method charges only tool attempts that the environment did not count.
        """
        sample = self._get_session(session_id)
        system_prompt = _SYSTEM_PROMPT + (_TOOL_INSTRUCTIONS if sample.user_tools else "")
        budget = step_budget
        while True:
            if budget is not None:
                check_budget(budget)
            prompt = render_sim_prompt(sample=sample, conversation=conversation,
                                       used_tools=self._tool_calls.get(session_id, []))
            turn = llm_judge_call(
                system_prompt=system_prompt, user_prompt=prompt, output_schema=_SimTurn,
                config=self._config, caller="user_sim", source=_MODEL_CALL_SOURCE)
            if turn.call_user_tool is not None:
                before = budget.snapshot()["steps"] if budget is not None else None
                self._run_user_tool(session_id, sample, turn.call_user_tool)
                if budget is not None:
                    if not budget.tools_counted_externally or budget.snapshot()["steps"] == before:
                        # Refused/unavailable tools and transport failures never reach the environment,
                        # but still represent a simulator request and an error result.
                        budget.consume(2)
                    check_budget(budget)
            if turn.stopped:
                return None
            if turn.message:
                attached = [f for f in sample.files if f.file_name in turn.attach_files]
                return ArtifactAgentQuery(text=turn.message, files=attached)

            # The schema permits an empty message only alongside a tool call. Ask the simulator
            # again with the newly recorded result rather than sending an empty user message to the
            # artifact. This is the user-side equivalent of an ordinary model/tool/model loop.

    def _run_user_tool(self, session_id: str, sample: MultiTurnInput,
                       call: "_UserToolCall") -> None:
        """Execute one user-held tool, recording the outcome for later turns.

        Never raises. A tool that errors, is not granted to this case, or has no executor wired is a
        graded outcome the conversation should carry on around -- the customer learns it did not work
        and can say so -- not a reason to abort a case. The record is what the next turn reads.
        """
        arguments = _coerce_tool_arguments(sample, call.name, call.arguments)
        record: dict[str, Any] = {"name": call.name, "arguments": arguments}
        if call.name not in sample.user_tool_names:
            # The model invented a tool, or reached for one this case does not grant.
            record["outcome"] = "refused: not one of your tools"
        elif self._tool_executor is None:
            record["outcome"] = "unavailable: no tool executor wired for this run"
            logger.warning(
                "user sim tried to call %r but no tool executor is wired -- a case whose golden "
                "outcome depends on a user-side action cannot be reached", call.name)
        else:
            try:
                result = self._tool_executor(call.name, arguments)
                record["outcome"] = f"ok: {result}"
            except Exception as exc:  # noqa: BLE001 - a failed user action is graded, not fatal
                record["outcome"] = f"error: {exc}"
                logger.warning("user sim tool %r failed: %s", call.name, exc)
        with self._lock:
            self._tool_calls.setdefault(session_id, []).append(record)

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            if session_id not in self._sessions:
                raise ValueError(f"unknown or deleted session_id: {session_id!r}")
            del self._sessions[session_id]
            self._tool_calls.pop(session_id, None)

    def _get_session(self, session_id: str) -> MultiTurnInput:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise ValueError(f"unknown or deleted session_id: {session_id!r}") from None


class ScriptedArtifactAgentUserSim:
    """Deterministic test stub: replays a fixed list of messages per session, then stops.

    Not tied to any sample field -- the same script plays regardless of `user_task`/`user_persona`,
    so this is for exercising the session lifecycle/generation loop shape, not persona behavior.
    Session-shaped like the real sim so callers can swap between them without touching call sites.
    """

    def __init__(self, messages: list[ArtifactAgentQuery | str]):
        self._messages = [
            m if isinstance(m, ArtifactAgentQuery) else ArtifactAgentQuery(text=m) for m in messages
        ]
        self._sessions: dict[str, int] = {}  # session_id -> next message index to deliver
        self._lock = threading.Lock()

    def create_session(self, sample: MultiTurnInput) -> str:
        del sample  # the script doesn't depend on the sample
        session_id = str(uuid.uuid4())
        with self._lock:
            self._sessions[session_id] = 0
        return session_id

    def ask(
        self, session_id: str, conversation: list[ConversationTurn],
        *, step_budget: StepBudget | None = None,
    ) -> ArtifactAgentQuery | None:
        del conversation  # the script is driven by position, not conversation content
        idx = self._get_index(session_id)
        if idx >= len(self._messages):
            return None
        self._sessions[session_id] = idx + 1
        return self._messages[idx]

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            if session_id not in self._sessions:
                raise ValueError(f"unknown or deleted session_id: {session_id!r}")
            del self._sessions[session_id]

    def _get_index(self, session_id: str) -> int:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise ValueError(f"unknown or deleted session_id: {session_id!r}") from None
