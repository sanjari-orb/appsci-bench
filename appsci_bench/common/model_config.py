"""Model configuration + the single LiteLLM call.

A shared leaf utility (`appsci_bench.common`): originally lived inside `appsci_agent_user_sim/`, moved
here once `appsci_bench.eval.helpers`'s LLM-judge helpers (Part 2) started reusing it too, since
neither user is a good "owner" of a generic transport the other also depends on. This module itself
still depends on nothing but litellm -- callers (appsci_agent_user_sim, eval/helpers) can each vendor it
standalone if they ever need to deploy independently.

Thin, provider-agnostic plumbing. Three responsibilities and nothing else:

  1. ``ModelConfig.from_env()`` -- read the model/endpoint/key from environment variables;
  2. ``config_for(model_var)`` -- the same, with the model id overridden by one role's own var;
  3. ``complete(messages, config)`` -- make ONE chat call through LiteLLM and return the text.

All provider routing lives in LiteLLM (the SDK, never a proxy). Set ``MODEL_NAME`` to a
provider-prefixed id (``anthropic/claude-opus-4-8``, ``openai/gpt-5.5``, or any gateway id) and
LiteLLM dispatches. Authentication is deliberately minimal and neutral:

  MODEL_NAME       LiteLLM model id (default a current Claude id).
  MODEL_API_KEY    Explicit key/token. LEAVE UNSET for a first-party provider -- LiteLLM reads the
                    provider-standard var itself (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...). SET it
                    to the bearer token when pointing at a gateway/proxy that needs one.
  MODEL_BASE_URL   Custom endpoint (gateway / self-hosted). Unset => the model's first-party
                    endpoint, which LiteLLM resolves on its own.
  MODEL_CUSTOM_HEADERS
                    Optional extra HTTP headers for the gateway, as one or more ``Name: Value``
                    lines (newline-separated). Mirrors what gateway exports as
                    ``ANTHROPIC_CUSTOM_HEADERS`` (e.g. ``X-Gateway-User: someone@example.com``),
                    which the gateway uses for per-user identity/attribution and can require
                    (``403 identity_required``). Unset => no extra headers are sent.

This covers both a barebone first-party API (set ``ANTHROPIC_API_KEY`` and go) and any gateway (set
``MODEL_BASE_URL`` + ``MODEL_API_KEY``) without baking in a single provider's assumptions. We do
NOT send a temperature -- it is left at the provider default (some current models reject other
values). Keeping the call isolated here means swapping providers is a one-file change.

PER-ROLE MODELS. A run has four LLM roles, and each has its own model var so one can be swapped
without touching the others:

  Role                                    Model var                  Credentials
  agent under test (build)                AGENT_MODEL                APPSCI_AGENT_BASE_URL/_API_KEY,
                                                                     or a provider-standard key when
                                                                     the model id names its own
                                                                     provider (FIREWORKS_API_KEY for
                                                                     a ``fireworks_ai/`` id -- and
                                                                     then the APPSCI_AGENT_* pair
                                                                     must be unset, since an endpoint
                                                                     given for the agent's own calls
                                                                     outranks the model id's provider)
  appsci-agent-user-sim (build customer)  APPSCI_USERSIM_MODEL       MODEL_API_KEY/MODEL_BASE_URL
  eval-side LLM judges                    JUDGE_MODEL                MODEL_API_KEY/MODEL_BASE_URL
  artifact-agent-user-sim (eval user)     ARTIFACT_USERSIM_MODEL     provider-standard only

``MODEL_NAME`` remains the shared DEFAULT for the two benchmark-side Claude roles: unset
``APPSCI_USERSIM_MODEL``/``JUDGE_MODEL`` fall back to it, so "put everything on one Claude id through
one gateway" is still a single variable. ``config_for()`` below implements exactly that -- a per-role
model id over the shared credentials. The eval-time artifact-agent-user-sim is the one role NOT on
this helper: it is pinned cross-provider to tau-bench's user-simulator model, so inheriting an
Anthropic gateway's ``MODEL_BASE_URL`` would point an ``openai/`` id at the wrong endpoint. It builds
its own ``ModelConfig`` and leaves credentials to LiteLLM (see
``appsci_bench/eval/artifact_agent_user_sim.py``).

See docs/user_simulator_api.md for the agent-facing HTTP contract (that doc predates this module's
move; the contract itself is unaffected).
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
from dataclasses import dataclass, replace
from typing import Any

import litellm

logger = logging.getLogger(__name__)

# LiteLLM emits an INFO line on every completion() call ("LiteLLM completion() model=...; provider=...")
# plus a "Give Feedback / Get Help" banner beside it -- pure per-call noise that buries real output
# (a single eval scoring pass makes dozens of judge calls). Quiet both here, at the one place we own
# the litellm import, so every caller (eval judges, user-sims) inherits the silence. WARNING still
# lets genuine litellm errors/warnings through.
litellm.suppress_debug_info = True
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

# Default model when MODEL_NAME is unset. A current Claude id (LiteLLM's anthropic/ prefix).
_DEFAULT_MODEL = "anthropic/claude-opus-4-8"
# Retry count handed to LiteLLM for transient provider errors.
_NUM_RETRIES = 3
#: Per-request wall-clock bound, applied by LiteLLM to each attempt. Without it a call parks on
#: LiteLLM's 6000s default, which matters wherever a caller enforces its own deadline and moves on: the
#: abandoned request keeps running and billing, and a worker thread still inside it blocks interpreter
#: exit. Mirrors appsci_agent/core/llm_client.py, which already sets this for the same reason.
_TIMEOUT_S = float(os.getenv("APPSCI_LLM_TIMEOUT_S", "120"))


class ModelConfigError(RuntimeError):
    """Raised when the model returns no usable text content (a bad/empty completion response)."""


#: A header name HTTP will actually put on the wire: the RFC 7230 `token` rule, which is what h11
#: (under httpx, under LiteLLM) enforces when it serialises a request. Anything else -- a space, a
#: tab, a colon-less fragment of prose, a UTF-8 dash pasted out of a wiki -- is rejected there as
#: `LocalProtocolError: Illegal header name`, on EVERY call, for the life of the process.
_HEADER_NAME_RE = re.compile(r"^[-!#$%&'*+.^_`|~0-9A-Za-z]+$")
#: A value h11 will serialise: anything but the control characters it refuses (tab is allowed).
_HEADER_VALUE_RE = re.compile(r"^[^\x00-\x08\x0a-\x1f\x7f]*$")


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

    NOTE (an exact twin of this helper lives in ``appsci_agent/core/config.py``): ``appsci_agent`` is
    the agent under test and must not import ``appsci_bench`` -- its image doesn't even ship this
    package (see appsci_agent/Dockerfile). A ~10-line duplicate is the lesser evil vs. an import that
    would break the boundary and the container build. `tests/test_appsci_agent.py` asserts the two
    agree on the same inputs, so a rule added to one must be added to the other.
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
        logger.debug("could not parse custom headers; sending none", exc_info=True)
        return {}
    return headers


@dataclass(frozen=True)
class ModelConfig:
    """Resolved model configuration for one caller (appsci-agent-user-sim service or eval-side judge).

    ``api_key`` / ``base_url`` are optional: when ``None`` they are simply not passed to LiteLLM, so
    LiteLLM resolves the provider-standard key and first-party endpoint itself. ``custom_headers`` is
    likewise omitted when empty.
    """
    model: str
    api_key: str | None = None
    base_url: str | None = None
    custom_headers: dict[str, str] | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ModelConfig":
        """Read config from env vars (see the module docstring for the four knobs).

        No validation/fail-fast here: a missing or wrong key surfaces as LiteLLM's own auth error on
        the first call, which is clearer and provider-correct. Resolution is pure -- it reads only
        the given ``env`` (or ``os.environ``), so it is deterministic and provider-neutral.
        """
        env = os.environ if env is None else env
        model = env.get("MODEL_NAME", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
        api_key = (env.get("MODEL_API_KEY") or "").strip() or None
        base_url = (env.get("MODEL_BASE_URL") or "").strip() or None
        headers = parse_custom_headers(env.get("MODEL_CUSTOM_HEADERS"))
        return cls(model=model, api_key=api_key, base_url=base_url,
                   custom_headers=headers or None)


def config_for(model_var: str, env: dict[str, str] | None = None) -> ModelConfig:
    """The shared ``MODEL_*`` config with the model id overridden by ``model_var`` when it is set.

    This is how a single role (``JUDGE_MODEL``, ``APPSCI_USERSIM_MODEL``) gets a dedicated model knob
    without a dedicated credential pair: the two roles that use this are both Claude-on-the-same-gateway
    by default, so sharing ``MODEL_API_KEY``/``MODEL_BASE_URL`` is correct and duplicating them per
    role would just be two more things to keep in sync.

    The one thing to know when overriding across providers: the shared credentials still apply, so
    pointing a role at a different provider than ``MODEL_BASE_URL`` serves means clearing that var (or
    letting LiteLLM's provider-standard resolution handle it) -- see the module docstring's note on why
    the eval-time user sim doesn't use this helper at all.
    """
    env = os.environ if env is None else env
    override = (env.get(model_var) or "").strip()
    cfg = ModelConfig.from_env(env)
    return replace(cfg, model=override) if override else cfg


def _log_model_error(*, caller: str, model: str, exc: BaseException,
                     source: Any = None, parent_span_id: str | None = None) -> None:
    """Report a failed model call to the run log and the event stream, then return.

    Never raises: a broken reporting path must not replace the caller's real exception with a
    less useful one, and the caller re-raises the original immediately after this returns.

    The status code is pulled out separately where the provider exposes one, because the single
    question asked of these events is "were we rate-limited?", and answering it should not require
    parsing a provider's prose.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    detail = str(exc)
    logger.warning("model call failed (caller=%s model=%s status=%s): %s: %s",
                   caller, model, status, type(exc).__name__, detail[:500])
    try:
        from shared.observability.events import EventType
        from shared.observability.publisher import emit
        emit(
            event_type=EventType.MODEL_ERROR,
            payload={
                "model": model,
                "caller": caller,
                "error_type": type(exc).__name__,
                "status_code": str(status) if status is not None else None,
                "error": detail[:2000],
            },
            source=source,
            parent_span_id=parent_span_id,
        )
    except Exception:  # noqa: BLE001 -- observability must never mask the real failure
        logger.debug("model.error emit failed", exc_info=True)


def complete(messages: list[dict], config: ModelConfig, *, json_object: bool = False,
             caller: str = "model", parent_span_id: str | None = None,
             source: Any = None) -> str:
    """Make ONE chat-completion call via LiteLLM and return the assistant's text content.

    This is the only place any caller (the appsci-agent-user-sim, eval-side LLM judges, or the
    artifact-agent-user-sim) talks to a model. LiteLLM owns connection pooling, provider
    transport, and retries, so there is no hand-rolled HTTP client here.

    Args:
        messages: OpenAI-style chat messages ([{role, content}, ...]).
        config: resolved ModelConfig.
        json_object: if True, request JSON-object output via response_format where the provider
            supports it (best-effort hint; the caller's parser is the real backstop).
        caller: a short label for observability (e.g. "judge", "user_sim") identifying the call site
            in the model-I/O trace; has no effect on the request itself.
        parent_span_id: optional span to parent this call's model-I/O under (e.g. the agent turn /
            tool call that triggered a user-sim reply), so the tracer nests it there.
        source: optional observability source this call belongs to, overriding the calling process's
            default. Needed because the model-call store is sharded per source and the usage rollup
            groups by that shard: the eval-time user sim runs in the evaluator's process, so without
            an override its calls are filed as the evaluator's and no report can tell them from a
            judge's. Same mechanism mini-swe uses to stay separable from the agent it runs inside.

    Returns:
        The text content of the model's first choice.

    Raises:
        ModelConfigError: the model returned no usable text content.
        (LiteLLM exceptions propagate for transient/auth/transport errors after its own retries.
        Each one is logged at WARNING and emitted as a ``model.error`` event on the way out, so a
        run that lost calls to gateway 429s or timeouts is distinguishable from one that merely
        scored badly.)
    """
    kwargs: dict = {
        "model": config.model,
        "messages": messages,
        "num_retries": _NUM_RETRIES,
        "timeout": _TIMEOUT_S,
    }
    # Pass api_key/base_url ONLY when set, so first-party providers let LiteLLM resolve them itself.
    if config.api_key is not None:
        kwargs["api_key"] = config.api_key
    if config.base_url is not None:
        kwargs["base_url"] = config.base_url
    # Same rule for gateway headers: omit the parameter entirely when there are none, so a
    # non-gateway user's request is byte-identical to what it was before this existed.
    #
    # Redaction: `shared/observability/redact.py` scrubs env values whose NAME looks credential-ish
    # (API_KEY/TOKEN/SECRET/...). `MODEL_CUSTOM_HEADERS` deliberately does NOT match, so its value
    # can appear in plaintext in model_calls-*.jsonl (mirrored to S3) if the agent ever echoes its
    # env. That is accepted: the value is an identity (a user email + a client version), not a
    # credential -- per-user attribution is the whole point of the header, and the same email is
    # already all over run metadata. Auth still lives entirely in MODEL_API_KEY, which IS redacted.
    if config.custom_headers:
        kwargs["extra_headers"] = dict(config.custom_headers)
    if json_object:
        kwargs["response_format"] = {"type": "json_object"}

    # Observability (optional): capture the FULL model I/O as a ModelCall when a publisher is active.
    # Lazy, guarded import keeps this module a dependency-free transport leaf when observability is
    # absent, and it no-ops when no publisher is running (local/tests).
    #
    # Scoped with `model_call` so a RAISING completion still writes its record. Previously the emitted
    # model.request left a `ref` nothing in the model_calls store resolved, and the dashboard's
    # "full I/O ->" on such a call 404'd into an empty modal. This is the biggest offender on the live
    # box -- runs/airline_rebooking_20260807_091549 had 50 of 96 refs dangling (all evaluator/user_sim)
    # and 202229 had 47 (evaluator/judge), every one a model.request with no model.response, i.e. the
    # completion raised. gateway 429s and gateway timeouts arrive here in bulk.
    _mc_ctx = contextlib.nullcontext(None)
    _model_call_end = None
    try:
        from shared.observability.model_io import model_call, model_call_end as _model_call_end
        _mc_ctx = model_call(messages, model=config.model, caller=caller,
                             parent_span_id=parent_span_id, source=source)
    except Exception:  # noqa: BLE001 -- observability must never break a real call, but don't lose the error
        logger.debug("model_call setup failed (observability skipped for this call)", exc_info=True)

    with _mc_ctx as _mc:
        try:
            response = litellm.completion(**kwargs)
        except Exception as exc:
            # Surface the failure before re-raising. A raising completion already got a
            # `<model call failed: ...>` stub from the `model_call` scope above, but that stub lives
            # inside a per-call side-file: nothing in the run log said a call had failed, and nothing
            # in the event stream could be COUNTED, so a sweep that lost half its calls to gateway
            # 429s read as a clean run with poor scores. Both channels here are additive -- the
            # exception propagates untouched, so retry/failure semantics are unchanged.
            _log_model_error(caller=caller, model=config.model, exc=exc,
                             source=source, parent_span_id=parent_span_id)
            raise

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, KeyError) as exc:
            raise ModelConfigError(f"unexpected LiteLLM response shape: {response!r}") from exc

        if _mc is not None and _model_call_end is not None:
            try:
                usage = getattr(response, "usage", None)
                if usage is not None and hasattr(usage, "model_dump"):
                    usage = usage.model_dump()
                elif usage is not None and not isinstance(usage, dict):
                    usage = None
                # Same reason the agent loop records it: a judge or user-sim reply cut off at the
                # output limit is a truncated reply, not a short one, and reading it as a real answer
                # would silently score against half a response.
                finish_reason = getattr(response.choices[0], "finish_reason", None)
                _model_call_end(_mc, content=content, usage=usage, finish_reason=finish_reason)
            except Exception:  # noqa: BLE001 -- never break the real call; log so the error isn't lost
                logger.debug("model_call_end failed (model I/O not recorded for this call)", exc_info=True)

    if not content:
        raise ModelConfigError("LiteLLM returned empty content")
    return content
