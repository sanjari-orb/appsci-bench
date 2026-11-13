"""Full model I/O capture for the observability trace.

LLM prompts + completions can be large, so they are captured as first-class `ModelCall` records in a
per-source, out-of-band store (`runs/<trace_id>/model_calls-<source>.jsonl`) -- one writer per file,
merged on read, mirroring how `events-<source>.jsonl` works. The event stream only carries lightweight
`MODEL_REQUEST`/`MODEL_RESPONSE` events (a short preview + a `ref` = the ModelCall `call_id`) so the
timeline can place each call and the dashboard can fetch the full I/O on demand.

Every LLM call site (build agent, mini-swe, eval judge, eval + build user-sims) uses the same
`model_call_start()` / `model_call_end()` pair. All functions no-op gracefully when observability is
not enabled (no global publisher), so build/eval/local/test runs are unaffected.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import EventType
from .publisher import get_publisher
from .redact import scrub
from .storage import source_slug

logger = logging.getLogger(__name__)

# Preview length carried in the lightweight request/response events. Kept generous enough that the
# dashboard's scrollable prompt/completion "quick view" shows real content; the full multi-message
# I/O still lives in the ModelCall store (the "full I/O" button), so this only bounds the inline peek.
_PREVIEW_CHARS = 2000

# Ambient context attached to every model call made within its scope (e.g. the eval case a
# user_sim / judge call belongs to). A ContextVar so it propagates correctly across the eval's
# thread pools -- see `model_call_context` and its use in eval/generation.py + eval/scoring.py.
_call_context: contextvars.ContextVar[dict] = contextvars.ContextVar("model_call_context", default={})


@contextlib.contextmanager
def model_call_context(**fields: Any):
    """Tag every model call made in this scope with `fields` (e.g. data_point_id, repeat_index).

    The fields land in the MODEL_REQUEST / MODEL_RESPONSE event payloads and on the stored
    `ModelCall.context`, so the dashboard can group calls by the case that produced them. No-ops
    for observability purposes when no publisher is active; the ContextVar is still set/reset so it
    is always safe to use. Nested scopes merge (inner keys win)."""
    merged = {**_call_context.get(), **{k: v for k, v in fields.items() if v is not None}}
    token = _call_context.set(merged)
    try:
        yield
    finally:
        _call_context.reset(token)


@dataclass
class ModelCall:
    """One model (LLM) call's full input + output -- the system-of-record for model I/O debugging.

    Correlated to the event stream by `call_id` (the referencing MODEL_REQUEST/MODEL_RESPONSE events
    carry it as `ref`) and grouped into a trace by `span_id`/`parent_span_id`.
    """
    call_id: str
    source: str
    caller: str                       # call site: "agent_loop", "mini_swe", "judge", "user_sim", ...
    model: str | None
    messages: list                    # full input prompt (role/content list)
    content: str | None               # completion text
    tool_calls: list                  # completion tool calls [{name, arguments}]
    usage: dict | None
    duration_ms: float | None
    span_id: str | None
    parent_span_id: str | None
    timestamp: str
    context: dict | None = None       # ambient tags (e.g. {"data_point_id", "repeat_index"})
    name: str | None = None           # human-readable role label of the calling agent
    cost_usd: float | None = None     # provider-computed cost, when the caller knows it
    # Why the model stopped, as the provider reported it (litellm normalises to the OpenAI set:
    # "stop", "length", "tool_calls", "content_filter"). None on records written before this field
    # existed, and on transports that do not report it.
    #
    # "length" is the one worth having: it means the response was cut off mid-generation, so its
    # content and any tool arguments are incomplete. Without it a truncated call is indistinguishable
    # from a deliberate one -- a tool call whose arguments arrive as an empty `{}` looks like the model
    # chose to send nothing. That is precisely how the truncation in
    # order_to_cash_claude-sonnet-4-6_b1_l3_20260831_074504 read as 37 turns of the model failing at a
    # trivial call, when the response had simply been cut off each time.
    finish_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ModelCall":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


@dataclass
class _Pending:
    """Handle returned by model_call_start, passed to model_call_end."""
    call_id: str
    source: str
    caller: str
    model: str | None
    messages: list
    parent_span_id: str | None
    started: float
    context: dict | None = None
    name: str | None = None
    # Set by model_call_end so a second call is a no-op. `model_call`'s except-path uses this to tell
    # "the wrapped call already recorded its result and then something else raised" from "the model
    # call itself raised", and so must never overwrite a good record with a failure stub.
    closed: bool = False


def _preview(text: Any, limit: int = _PREVIEW_CHARS) -> str:
    # Scrubbed as well as the full record: a preview is a slice of the same agent output, so a
    # credential can land here too. In the real leak it happened to fall outside the truncation
    # window, which is luck rather than a guarantee.
    s = text if isinstance(text, str) else json.dumps(text, default=str)
    s = scrub(s)
    s = s.strip()
    return s if len(s) <= limit else s[:limit] + "..."


def _last_content(messages: list) -> str:
    for m in reversed(messages or []):
        c = m.get("content") if isinstance(m, dict) else None
        if c:
            return c if isinstance(c, str) else json.dumps(c, default=str)
    return ""


def model_call_start(messages, *, model=None, caller="", source=None, parent_span_id=None, name=None):
    """Mark the start of a model call: emit a MODEL_REQUEST event (model + caller + a preview of the
    latest input message) and return a handle for `model_call_end`. Returns None (records nothing)
    when observability is disabled. `source` overrides the publisher's default source (e.g. mini-swe
    runs in the agent process but should be tagged MINI_SWE_AGENT). `name` is the calling agent's
    role label (e.g. "mini-swe"), overriding the publisher default on the emitted events."""
    publisher = get_publisher()
    if publisher is None:
        return None
    resolved_source = str(source) if source is not None else (publisher.default_source or "")
    call_id = str(uuid.uuid4())
    ctx = dict(_call_context.get()) or None
    try:
        publisher.emit(
            event_type=EventType.MODEL_REQUEST,
            source=source,
            payload={"model": model, "caller": caller, "ref": call_id,
                     "n_messages": len(messages or []), "preview": _preview(_last_content(messages)),
                     **(ctx or {})},
            span_id=call_id,
            parent_span_id=parent_span_id,
            name=name,
        )
    except Exception:  # noqa: BLE001 -- observability must never break a real call
        logger.debug("model_call_start emit failed", exc_info=True)
    return _Pending(call_id=call_id, source=resolved_source, caller=caller,
                    model=model, messages=list(messages or []), parent_span_id=parent_span_id,
                    started=time.monotonic(), context=ctx, name=name)


def model_call_end(pending, *, content=None, tool_calls=None, usage=None, cost_usd=None,
                   finish_reason=None):
    """Complete a model call started by `model_call_start`: append the full `ModelCall` record to the
    per-source store and emit a MODEL_RESPONSE event (preview of the completion + `ref`). No-op if
    `pending` is None (observability disabled) or the publisher went away.

    `cost_usd` is optional and only set by callers whose transport already computed a real price
    (mini-swe-agent does, via litellm's cost calculator). Everyone else leaves it None and the
    reader estimates from tokens; recording the authoritative number when we have it means the
    cost rollup is not uniformly an estimate.

    `finish_reason` is likewise optional. It goes on BOTH the record and the MODEL_RESPONSE event: the
    event carries it so a reader can see that a call was truncated without fetching the full record,
    which matters because the truncated ones are exactly the calls worth finding."""
    if pending is None:
        return
    if pending.closed:
        return                          # idempotent; see `_Pending.closed` and `model_call`
    pending.closed = True
    publisher = get_publisher()
    if publisher is None:
        return
    duration_ms = (time.monotonic() - pending.started) * 1000.0
    call = ModelCall(
        call_id=pending.call_id, source=pending.source, caller=pending.caller, model=pending.model,
        messages=pending.messages, content=content, tool_calls=list(tool_calls or []), usage=usage,
        duration_ms=duration_ms, span_id=pending.call_id, parent_span_id=pending.parent_span_id,
        timestamp=datetime.now(timezone.utc).isoformat(), context=pending.context, name=pending.name,
        cost_usd=cost_usd, finish_reason=finish_reason,
    )
    _append_model_call(publisher, call)
    try:
        tokens = (usage or {}).get("total_tokens") if isinstance(usage, dict) else None
        publisher.emit(
            event_type=EventType.MODEL_RESPONSE,
            source=pending.source or None,
            payload={"model": pending.model, "caller": pending.caller, "ref": pending.call_id,
                     "tokens": tokens, "duration_ms": duration_ms,
                     "n_tool_calls": len(tool_calls or []), "preview": _preview(content or ""),
                     "finish_reason": finish_reason,
                     **(pending.context or {})},
            span_id=pending.call_id,
            parent_span_id=pending.parent_span_id,
            name=pending.name,
        )
    except Exception:  # noqa: BLE001
        logger.debug("model_call_end emit failed", exc_info=True)


@contextlib.contextmanager
def model_call(messages, *, model=None, caller="", source=None, parent_span_id=None, name=None):
    """Scope one model call so its MODEL_REQUEST always gets a matching `ModelCall` record.

    Yields the handle for `model_call_end` (or None when observability is off). Call `model_call_end`
    with the real result on the success path as usual; if the wrapped call raises, this closes the
    span with a `<model call failed: ...>` record and re-raises untouched.

    WHY this exists instead of a try/except per call site: `model_call_start` emits MODEL_REQUEST
    with a `ref`, but `model_call_end` is what WRITES the record that `ref` points at. A raising call
    never reaches end, so the event stream keeps a call the store cannot resolve -- a DANGLING REF.
    The tracer builds its build-phase model-call rows from those events, so the row (and its
    "full I/O ->" button) renders while `/api/model_io?ref=...` 404s: the user clicks and gets an
    empty modal. It also silently drops the captured `messages`, and for a mini-swe sub-agent -- whose
    conversation the tracer resolves from the DEEPEST of its model-call refs, which is exactly the
    failed one -- a single dead step blanks the whole expanded view.

    This was first fixed at one call site (InstrumentedModel.query in mini_swe_agent.py) and the hole
    stayed open at the others, which is measurable on the live box:
      runs/airline_rebooking_20260807_091549 -- 50 of 96 refs dangling (evaluator/user_sim)
      runs/airline_rebooking_20260807_202229 -- 47 of 422 dangling (evaluator/judge)
    Every one of those has a MODEL_REQUEST and NO MODEL_RESPONSE, which is the signature of the call
    raising rather than of a failed record write. Gateway timeouts and "gateway per-user rate limit
    exceeded" (429) both land here, so on a rate-limited sweep this is the common case: in 091549 more
    than half the trace's model I/O was unreachable. Keeping the guarantee in one place means a new
    call site gets it by construction rather than by remembering.
    """
    pending = None
    try:
        pending = model_call_start(messages, model=model, caller=caller, source=source,
                                   parent_span_id=parent_span_id, name=name)
    except Exception:  # noqa: BLE001 -- observability must never break the real call it wraps
        logger.debug("model_call_start failed (observability skipped for this call)", exc_info=True)
    try:
        yield pending
    except BaseException as exc:  # noqa: BLE001 -- re-raised below; we only close the span
        # `closed` guard: if the caller already recorded a result and something *after* it raised,
        # the good record stands. Only a call that died before being recorded gets the failure stub.
        if pending is not None and not pending.closed:
            try:
                model_call_end(pending, content=f"<model call failed: {type(exc).__name__}: {exc}>")
            except Exception:  # noqa: BLE001 -- never mask the caller's exception with a trace error
                logger.debug("model_call_end failed on the error path", exc_info=True)
        raise


def _append_model_call(publisher, call: ModelCall) -> None:
    """Append one ModelCall as a JSON line to this source's model-calls store (beside the event log).

    The record is scrubbed of known credentials first (see :mod:`redact`). This is the single write
    choke point for captured LLM I/O, so redacting here covers messages, completions and tool calls
    at once -- necessary because a secret gets in as *agent output* (a shell command that echoed its
    environment), not through any particular field this module controls.
    """
    try:
        path = publisher.log_path.parent / f"model_calls-{source_slug(call.source)}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(scrub(call.to_dict()), default=str) + "\n")
    except Exception:  # noqa: BLE001 -- never break the real call over a trace-write failure
        logger.debug("failed to append ModelCall", exc_info=True)


def load_model_calls(trace_dir) -> dict[str, dict[str, Any]]:
    """Merge every `model_calls-<source>.jsonl` in a trace dir into `{call_id: ModelCall dict}` for
    the dashboard/tests. Malformed lines skipped; missing dir -> empty.

    Loads EVERY record into memory, so it is only safe when the caller genuinely needs all of them
    (tests, small fixtures). To fetch one call, use `load_model_call` -- these stores reach gigabytes
    on a real build and this function cannot open one. See that function for the details."""
    out: dict[str, dict[str, Any]] = {}
    d = Path(trace_dir)
    if not d.is_dir():
        return out
    for f in sorted(d.glob("model_calls-*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("call_id"):
                    out[rec["call_id"]] = rec
            except Exception:  # noqa: BLE001
                continue
    return out


def load_model_call(trace_dir, ref: str) -> dict[str, Any] | None:
    """Return the single ModelCall whose `call_id` is `ref`, or None.

    Streams the stores a line at a time and stops at the first match. The dict-building sibling above
    cannot be used for this: it calls `read_text()` on every file, so serving one call meant holding
    the entire trace in memory at once, plus a second copy from `splitlines()` and a third as parsed
    records.

    That is not a theoretical limit. A build whose sub-agent runs without prompt caching writes a
    record per step containing the whole conversation so far, so the store grows quadratically:
    `model_calls-mini_swe_agent.jsonl` for
    airline_rebooking_claude-sonnet-4-6_b0_l3_20260831_083903 is 1.83 GB across 823 records. Asking
    the old path for one of them tried to materialise several times that, and the request died before
    it answered -- which the dashboard rendered as "No captured mini-swe conversation", i.e. exactly
    the runs whose sub-agent hit its limits were the runs whose conversations could not be viewed.

    `ref not in line` is a plain substring scan, which skips the JSON parse for the ~99% of records
    that cannot match. Only a candidate line is parsed, so the peak cost is one record, not one file.
    """
    d = Path(trace_dir)
    if not d.is_dir() or not ref:
        return None
    for f in sorted(d.glob("model_calls-*.jsonl")):
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    # Cheap reject before the expensive parse. A record holding the ref anywhere (its
                    # own call_id, or a parent/span field) still has to be parsed to know which, so
                    # this only bounds work -- it never decides the match.
                    if ref not in line:
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if rec.get("call_id") == ref:
                        return rec
        except OSError:
            logger.debug("failed to read %s", f, exc_info=True)
            continue
    return None
