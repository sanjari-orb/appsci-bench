"""Agent configuration for the Applied AI Scientist agent.

Supports loading from YAML/JSON files. Authentication env vars:

  APPSCI_AGENT_API_KEY    Explicit key/token. LEAVE UNSET for first-party Anthropic -- LiteLLM reads
                          ANTHROPIC_API_KEY itself. SET it to the bearer token when pointing at a
                          gateway/proxy that needs one.
  APPSCI_AGENT_BASE_URL   Custom endpoint (gateway / self-hosted). Unset => the model's first-party
                          endpoint, which LiteLLM resolves on its own.
  APPSCI_AGENT_CUSTOM_HEADERS
                          Optional extra HTTP headers for that gateway, as one or more
                          ``Name: Value`` lines (newline-separated). Mirrors gateway's
                          ``ANTHROPIC_CUSTOM_HEADERS`` (e.g. ``X-Gateway-User: someone@example.com``),
                          which the gateway uses for per-user identity/attribution and can require
                          (``403 identity_required``). Applies to the agent loop + mini-swe-agent.
  APPSCI_LLM_TIMEOUT_S    Per-request LiteLLM timeout in seconds (default 120). Bounds a single model
                          call so a hung/throttled gateway request fails fast (and is retried) rather
                          than parking on LiteLLM's 6000s default. Read by LLMClient and mini-swe-agent.

Other providers need no vars of their own here: LiteLLM picks the route from the model id's prefix and
reads that provider's standard key itself. Fireworks (``AGENT_MODEL=fireworks_ai/...``) is the case in
use -- set ``FIREWORKS_API_KEY`` and leave all three ``APPSCI_AGENT_*`` vars UNSET, since they are the
endpoint, token and headers this agent's calls use and they outrank the provider the model id names. A
URL left set with no token beside it makes the orchestrator REFUSE the run, because LiteLLM would send
the Fireworks key to that endpoint as the bearer token; the other two leftover combinations only warn
(see ``appsci_bench/core/agent_container.py``). Both LLM callers already behave correctly once the vars
are clear: they pass ``api_key`` / ``base_url`` to LiteLLM only when this config carries them.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: A header name HTTP will actually put on the wire: the RFC 7230 `token` rule, which is what h11
#: (under httpx, under LiteLLM) enforces when it serialises a request. Anything else -- a space, a
#: tab, a colon-less fragment of prose, a UTF-8 dash pasted out of a wiki -- is rejected there as
#: `LocalProtocolError: Illegal header name`, on EVERY call, for the life of the process.
_HEADER_NAME_RE = re.compile(r"^[-!#$%&'*+.^_`|~0-9A-Za-z]+$")
#: A value h11 will serialise: anything but the control characters it refuses (tab is allowed).
_HEADER_VALUE_RE = re.compile(r"^[^\x00-\x08\x0a-\x1f\x7f]*$")


#: The spellings a boolean-ish environment variable may use. Anything else is a hard error rather
#: than a silent truthiness read: `AGENT_OPTIMIZATION_ENABLED=no` read as True would run the control
#: arm while the sweep record claimed the ablation, and nothing downstream would say so.
_ENV_BOOL_TRUE = frozenset({"1", "true", "yes", "on"})
_ENV_BOOL_FALSE = frozenset({"0", "false", "no", "off"})


def _env_bool(name: str) -> bool | None:
    """Read `name` as a boolean, or None when it is unset or empty.

    None means "the caller keeps its own default" and is distinct from False, which is an explicit
    request. Raises ValueError on any other value -- see `_ENV_BOOL_TRUE` for why guessing is worse
    than failing here.
    """
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return None
    if raw in _ENV_BOOL_TRUE:
        return True
    if raw in _ENV_BOOL_FALSE:
        return False
    raise ValueError(
        f"{name} must be a boolean-ish value, got {raw!r}. Use 0/1, true/false, yes/no or on/off."
    )


def parse_custom_headers(raw: str | None) -> dict[str, str]:
    """Parse a ``Name: Value`` header block (one header per line) into a dict.

    The input is whatever a gateway's launcher exported -- gateway's ``ANTHROPIC_CUSTOM_HEADERS`` is
    one or more newline-separated ``Name: Value`` lines. Deliberately total: blank lines, lines
    without a colon, and lines whose name or value could not go on the wire are skipped, whitespace
    is stripped, and nothing raises. A malformed value must degrade to "no extra headers" rather than
    break every LLM call in a long unattended run.

    THAT PROMISE IS WHY THE NAME IS VALIDATED and not merely non-empty. Skipping the parse is not the
    only way to break every call: a name like ``Bad Name`` parsed cleanly here and then died in h11
    at request time (`Illegal header name`), which is a 100% failure rate on a value the docstring
    claimed was survivable -- worse than the crash it was avoiding, because it happened one layer
    away from the env var that caused it. Values are checked for the control characters h11 rejects
    for the same reason. Both are dropped per line, so one bad header does not cost the good ones.

    Splits on the FIRST colon only, so a value that itself contains one (a URL, a version string)
    survives intact.

    NOTE: this is an exact twin of ``appsci_bench.common.model_config.parse_custom_headers``, and the
    duplication is deliberate. `appsci_agent` is the agent under test and must not import
    `appsci_bench` -- its image doesn't even ship that package (see appsci_agent/Dockerfile), so a
    shared import would break both the boundary and the container build. A ~10-line copy is cheaper.
    `tests/test_appsci_agent.py` asserts the two agree on the same inputs, so a rule added to one
    must be added to the other.
    """
    headers: dict[str, str] = {}
    if not raw:
        return headers
    try:
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or ":" not in stripped:
                continue
            name, _, value = stripped.partition(":")
            name = name.strip()
            value = value.strip()
            if not _HEADER_NAME_RE.match(name):
                logger.warning(
                    "dropping a custom header whose name is not a valid HTTP token (%d char(s), "
                    "starts %r); sending the rest. Every request carrying it would have failed.",
                    len(name), name[:16],
                )
                continue
            if not _HEADER_VALUE_RE.match(value):
                logger.warning(
                    "dropping custom header %r: its value holds a control character, which the HTTP "
                    "layer rejects on every request; sending the rest.", name,
                )
                continue
            headers[name] = value
    except Exception:  # noqa: BLE001 -- a bad header must never break the call it decorates
        return {}
    return headers


@dataclass
class AgentConfig:
    """Configuration for the AppSci agent."""

    # LLM settings
    llm_provider: str = "anthropic"
    model: str = "claude-opus-4-8"
    api_key: str | None = None
    base_url: str | None = None
    # Extra HTTP headers for the gateway (parsed from APPSCI_AGENT_CUSTOM_HEADERS). None => send
    # none, so a non-gateway setup's requests are unchanged.
    custom_headers: dict[str, str] | None = None
    # Output ceiling per model call. Raised from 4096 after a build died on it: the agent tried to
    # hand the coding sub-agent a whole server implementation as the task text, spent the entire
    # budget on the tool call's argument JSON, and the call arrived with its arguments empty --
    # nothing downstream could see that the response had been cut off, so the same oversized call was
    # re-sent 37 times until the turn ceiling stopped the run
    # (order_to_cash_claude-sonnet-4-6_b1_l3_20260831_074504).
    #
    # 8192 buys headroom, not immunity: a large enough brief still truncates. What makes truncation
    # survivable is the `length` finish-reason handling in `agent.py` -- this number only decides how
    # often that path is reached.
    #
    # SCOPE: the interviewing agent's own calls ONLY, via `llm_client.chat`. It does NOT reach the
    # coding sub-agent, which builds its own model with its own `model_kwargs` and never sets a token
    # cap, so those calls run at the provider default. That matters because the sub-agent writes whole
    # source files in a single response, and capping it at 8192 would truncate legitimate work rather
    # than prevent a bug. If a cap is ever wanted there it belongs in `appsci_bench.yaml`'s `model`
    # block, deliberately and with its own number -- not by reusing this one.
    max_tokens: int = 8192
    # None => send no temperature at all, which is what every run before this field was wired did.
    #
    # This used to read `temperature: float = 0.0` and was DEAD: the only caller,
    # `agent.py`'s `llm_client.chat(...)`, never passed it, and `llm_client` adds temperature to the
    # request only when a caller supplies one explicitly. So builds have always run at the provider's
    # own default, unrecorded -- and `docs/experiments.md`'s claim that "the agent hardcodes
    # temperature = 0.0" was simply false.
    #
    # Defaulting to None rather than keeping 0.0 is deliberate. Wiring the old default would have
    # started sending `temperature=0.0` on every build, quietly changing the conditions of every
    # future run relative to the airline and gradient numbers already collected -- a behaviour change
    # disguised as a bug fix. Unset stays unset; a sweep that wants sampling asks for it.
    #
    # This is the BUILD-side replicate control. There is no seed anywhere in the harness, so
    # temperature is what makes n independent builds of one cell samples of a distribution rather
    # than repeats of one intention (see appsci_bench/experiments/config.py `builds`).
    temperature: float | None = None

    # Agent behavior
    max_turns: int = 40
    workspace_dir: str = "/workspace"
    # Where the agent writes its deliverable file artifacts. This is a per-task
    # contract sourced from the spec's `artifacts.output_dir`; the value here is
    # only the fallback when a spec omits it. The evaluator's FileArtifact and
    # the run's artifact snapshot both read from the same location.
    artifact_dir: str = "/work/out"

    # mini-swe-agent settings.
    #
    # None => use the agent's own model. mini-swe does the implementation work and is the bulk of a
    # run's spend, so a fixed id here would silently decide most of what a run measures: swapping the
    # agent's model would leave the coding sub-agent on the old one, and a model-comparison sweep
    # would report a difference far smaller than the models'. Set it explicitly only to study the
    # sub-agent separately, and say so beside the number.
    mini_swe_model: str | None = None
    mini_swe_max_turns: int = 50

    # Ceiling on how many optimization cycles `optimize_agent` may spend in a BUILD session, or None
    # for "no ceiling -- keep going until the eval scores stop moving". A cycle is one attempt to
    # change the candidate, and it costs a sub-agent run plus one or two docker builds, so this is the
    # knob that bounds the most expensive part of a run. The task spec's `budgets.build.optimize_cycles`
    # takes precedence over this fallback, the same way `budgets.build.turns` does over `max_turns`.
    optimize_cycles: int | None = None

    # Whether the agent gets its self-evaluation tools at all: `eval_generator` (write an internal eval
    # harness) and `optimize_agent` (score the candidate against it and improve it). False removes BOTH
    # from the tool registry AND from every prompt surface, leaving the four-tool agent that existed
    # before those tools did.
    #
    # This is the ABLATION control, and it is deliberately separate from `optimize_cycles`. They answer
    # different questions: `optimize_cycles=0` still builds an eval and still measures the candidate, it
    # just never changes it -- so it isolates "did the optimization loop help?" while keeping the
    # measurement. `optimization_enabled=False` removes the capability entirely -- so it isolates "did
    # having these tools at all change how the agent works?", which is the comparison against a run
    # from before they landed. Collapsing the two into one knob would make it impossible to tell an
    # agent that measured and chose not to act from one that could not measure.
    #
    # On the name, which has been read as narrower than it is (raised in review on #135): the
    # implication runs one way only. No internal eval means no optimization, because the loop scores
    # against that harness -- but no optimization does NOT mean no internal eval, which is exactly what
    # `optimize_cycles=0` is. So this flag is the wider of the two, and turning it off takes the
    # measurement away with the loop. `internal_eval_enabled` would name the half that implies the
    # other; this name names the stage the ablation is about, which is how the sweep configs and the
    # plan doc refer to it. Renaming is not free either: the key appears in `sweep.sample.yaml`, the
    # orchestrator CLI, `AGENT_OPTIMIZATION_ENABLED` in the container env and the sweep worker's
    # `MANIFEST_ENV_NAMES`, so an unaliased rename would leave an old sweep YAML silently running the
    # wrong arm.
    optimization_enabled: bool = True

    # Whether the build-time stakeholder exists at all.
    #
    # True (the default) is the benchmark as designed: the task's requirement documents live only
    # inside the user simulator, and `talk_to_user` is the only route to them, so what the agent
    # knows is a function of what it thought to ask.
    #
    # False is the NO-USER ablation arm. `talk_to_user` is removed from the tool registry and from
    # every prompt surface, and the same documents the simulator would have been given are mounted
    # read-only into this container instead (see `requirements_dir`). Same task, same facts, handed
    # over rather than asked for -- which is what isolates the cost of elicitation from everything
    # else the agent does.
    #
    # Removing the tool and adding the documents are ONE flag on purpose. An agent with neither a
    # stakeholder nor the documents is not an ablation of elicitation, it is a harder task; an agent
    # with both is the control plus a shortcut. Neither is an arm anyone wants to run, so neither is
    # reachable from here.
    #
    # Note what this arm does to the persona axis: with the whole document tree mounted, the bound
    # persona decides nothing, so L1/L2/L3 collapse to a single condition. The orchestrator names
    # such a run `nouser` in the persona slot rather than letting it claim a level it did not use.
    user_sim_enabled: bool = True

    # Where the task's requirement documents are mounted in THIS container, or None when they are not
    # mounted at all. Set by the orchestrator to the exact path it bound (`APPSCI_REQUIREMENTS_DIR`)
    # rather than hardcoded here, so the prompt can never name a directory that does not exist: the
    # mount and the sentence describing it come from one value. Only ever set when `user_sim_enabled`
    # is False.
    requirements_dir: str | None = None

    # The two halves of `requirements_dir`, each set only when that half was actually mounted.
    #
    # A stakeholder's knowledge comes from two places and they are different in kind, so the prompt
    # names them separately rather than saying "read everything under /requirements": the documents
    # are what the persona was handed whole (output contracts, policy files), and the policy extracts
    # are the rules it otherwise held inline and released only when asked. An agent that knows which
    # is which can tell an interface contract from a decision rule without opening both.
    #
    # Either may be None while the other is set -- a task can have one half and not the other -- and
    # both None is the "no documents at all" state `_requirements_instruction` already handles.
    requirements_docs_dir: str | None = None
    requirements_policies_dir: str | None = None

    def __post_init__(self) -> None:
        if self.mini_swe_model is None:
            self.mini_swe_model = self.model
        # Fail closed on the combination that would silently corrupt the control arm: a run that
        # keeps the stakeholder AND mounts the documents it was supposed to hold is neither arm of
        # the experiment, and nothing downstream would say so -- the elicitation score would just
        # come back implausibly high. The orchestrator only ever sets the two together, so reaching
        # this means a hand-built config or a stray environment variable.
        if self.user_sim_enabled and (
            self.requirements_dir or self.requirements_docs_dir or self.requirements_policies_dir
        ):
            raise ValueError(
                "requirements_dir is set while user_sim_enabled is True. Mounting the "
                "stakeholder's own documents into the agent container while the stakeholder is "
                "still reachable removes the elicitation the control arm exists to measure. Set "
                "user_sim_enabled=False (AGENT_USER_SIM_ENABLED=0) for the no-user arm, or unset "
                "APPSCI_REQUIREMENTS_DIR."
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentConfig":
        """Create config from a dictionary."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    @classmethod
    def from_yaml(cls, path: str | Path) -> "AgentConfig":
        """Load config from a YAML file."""
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls.from_dict(data or {})

    @classmethod
    def from_json(cls, path: str | Path) -> "AgentConfig":
        """Load config from a JSON file."""
        import json
        with open(path) as f:
            data = json.load(f)
        return cls.from_dict(data)

    @classmethod
    def from_env(cls) -> "AgentConfig":
        """Load config from environment variables, use defaults for everything else.

        Environment variables:
        - APPSCI_AGENT_API_KEY / APPSCI_AGENT_BASE_URL: explicit gateway/proxy config
          Falls back to ANTHROPIC_API_KEY (which LiteLLM reads automatically) if unset
        - APPSCI_AGENT_CUSTOM_HEADERS: optional `Name: Value` header lines for that gateway
        - AGENT_MODEL: optional model override. The mini-swe sub-agent follows it, since it
          inherits the agent's model unless a config file names one for it explicitly.
        - AGENT_MAX_TURNS: optional max turns override
        - AGENT_OPTIMIZE_CYCLES: optional cap on optimize_agent cycles (0 forbids any attempt)
        - AGENT_OPTIMIZATION_ENABLED: set falsy to remove eval_generator + optimize_agent entirely
        - AGENT_USER_SIM_ENABLED: set falsy to remove talk_to_user entirely (the no-user arm). The
          orchestrator pairs this with APPSCI_REQUIREMENTS_DIR; neither is meaningful alone.
        - APPSCI_REQUIREMENTS_DIR: directory the task's requirement documents are mounted at, when
          they are mounted. Rejected unless AGENT_USER_SIM_ENABLED is falsy.
        - AGENT_TEMPERATURE: optional sampling temperature for BOTH build-side agents (this one and
          the mini-swe sub-agent). Unset => no temperature is sent at all, which is how every run
          before this variable existed behaved. An unparseable value is a hard error rather than a
          silent fall back to unset: a sweep that asked for temperature 1.0 and got the provider
          default would report replicate spread it never actually sampled.

        For full config customization, use from_yaml() or from_json().
        """
        api_key = (os.getenv("APPSCI_AGENT_API_KEY") or "").strip() or None
        base_url = (os.getenv("APPSCI_AGENT_BASE_URL") or "").strip() or None
        headers = parse_custom_headers(os.getenv("APPSCI_AGENT_CUSTOM_HEADERS"))

        # Optional overrides (typically set by orchestrator)
        kwargs: dict[str, Any] = {"api_key": api_key, "base_url": base_url,
                                  "custom_headers": headers or None}
        if os.getenv("AGENT_MODEL"):
            kwargs["model"] = os.environ["AGENT_MODEL"]
        if os.getenv("AGENT_MAX_TURNS"):
            kwargs["max_turns"] = int(os.environ["AGENT_MAX_TURNS"])
        raw_cycles = (os.getenv("AGENT_OPTIMIZE_CYCLES") or "").strip()
        if raw_cycles:
            try:
                cycles = int(raw_cycles)
            except ValueError as e:
                raise ValueError(
                    f"AGENT_OPTIMIZE_CYCLES must be an integer, got {raw_cycles!r}"
                ) from e
            if cycles < 0:
                raise ValueError(
                    f"AGENT_OPTIMIZE_CYCLES must be >= 0, got {cycles}. Use 0 to forbid any "
                    "optimization attempt, or leave it unset to run until the scores saturate."
                )
            kwargs["optimize_cycles"] = cycles
        opt_enabled = _env_bool("AGENT_OPTIMIZATION_ENABLED")
        if opt_enabled is not None:
            kwargs["optimization_enabled"] = opt_enabled
        user_sim_enabled = _env_bool("AGENT_USER_SIM_ENABLED")
        if user_sim_enabled is not None:
            kwargs["user_sim_enabled"] = user_sim_enabled
        # The path the orchestrator actually bound the requirement documents at. Carried as a value
        # rather than assumed, so the prompt names a directory that exists or names none at all.
        # `__post_init__` rejects it arriving alongside a live stakeholder.
        requirements_dir = (os.getenv("APPSCI_REQUIREMENTS_DIR") or "").strip() or None
        if requirements_dir:
            kwargs["requirements_dir"] = requirements_dir
        # Each half carries its own variable, set by the container manager only when that half was
        # bound. Read separately rather than derived by joining a subdirectory name onto the parent:
        # a name assembled here could drift from the bind target, which is the failure the parent
        # path's own comment exists to prevent.
        docs_dir = (os.getenv("APPSCI_REQUIREMENTS_DOCS_DIR") or "").strip() or None
        if docs_dir:
            kwargs["requirements_docs_dir"] = docs_dir
        policies_dir = (os.getenv("APPSCI_REQUIREMENTS_POLICIES_DIR") or "").strip() or None
        if policies_dir:
            kwargs["requirements_policies_dir"] = policies_dir
        raw_temperature = (os.getenv("AGENT_TEMPERATURE") or "").strip()
        if raw_temperature:
            try:
                kwargs["temperature"] = float(raw_temperature)
            except ValueError as e:
                raise ValueError(
                    f"AGENT_TEMPERATURE must be a number, got {raw_temperature!r}"
                ) from e

        return cls(**kwargs)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "AgentConfig":
        """Load config from file (if provided) or environment variables."""
        if path is None:
            return cls.from_env()
        path = Path(path)
        if path.suffix in (".yaml", ".yml"):
            return cls.from_yaml(path)
        elif path.suffix == ".json":
            return cls.from_json(path)
        else:
            raise ValueError(f"Unsupported config file format: {path.suffix}")

    def get_litellm_model(self) -> str:
        """Get the model string formatted for litellm."""
        # litellm uses provider/model format for some providers
        if self.llm_provider == "anthropic":
            return self.model  # litellm recognizes claude-* models directly
        elif self.llm_provider == "openai":
            return self.model  # litellm recognizes gpt-* models directly
        else:
            return f"{self.llm_provider}/{self.model}"
