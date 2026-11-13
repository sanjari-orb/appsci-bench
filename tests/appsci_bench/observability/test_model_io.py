"""Full model I/O capture: ModelCall trace object (per-source store) + lightweight referencing
events, and the complete() choke-point instrumentation that captures eval/user-sim model calls."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import shared.observability.publisher as pub
from shared.observability.publisher import init_publisher
from shared.observability.events import Source, EventType
from shared.observability import model_io
from shared.observability.storage import get_source_log_path


def _reset():
    p = pub.get_publisher()
    if p is not None:
        p.close()
    pub._publisher = None


def _init(d, trace, source=Source.EVALUATOR):
    import pathlib
    (pathlib.Path(d) / trace).mkdir(parents=True, exist_ok=True)
    init_publisher(runs_dir=d, trace_id=trace, source=source, task_id="t")


def test_model_call_records_full_io_and_emits_ref_events(tmp_path):
    _init(str(tmp_path), "tr")
    try:
        h = model_io.model_call_start(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello world"}],
            model="claude-x", caller="judge")
        model_io.model_call_end(h, content="the answer",
                                tool_calls=[{"name": "t", "arguments": {"a": 1}}],
                                usage={"total_tokens": 42})
    finally:
        _reset()

    # events carry only ref + preview (small); full I/O is out-of-band
    evs = [json.loads(l) for l in get_source_log_path("tr", Source.EVALUATOR, str(tmp_path)).read_text().splitlines() if l.strip()]
    types = [e["event_type"] for e in evs]
    assert str(EventType.MODEL_REQUEST) in types and str(EventType.MODEL_RESPONSE) in types
    resp = next(e for e in evs if e["event_type"] == str(EventType.MODEL_RESPONSE))
    assert resp["payload"]["preview"] == "the answer" and resp["payload"]["ref"]
    assert "messages" not in resp["payload"]                      # full input NOT in the event

    # the ModelCall store has the full prompt + completion, keyed by call_id
    calls = model_io.load_model_calls(tmp_path / "tr")
    assert len(calls) == 1
    call = calls[resp["payload"]["ref"]]                          # ref resolves to the ModelCall
    assert call["caller"] == "judge" and call["model"] == "claude-x"
    assert call["messages"][1]["content"] == "hello world"        # FULL input preserved
    assert call["content"] == "the answer"
    assert call["tool_calls"] == [{"name": "t", "arguments": {"a": 1}}]
    assert call["source"] == "evaluator" and call["duration_ms"] is not None


def test_model_call_noop_without_publisher(tmp_path):
    _reset()
    assert model_io.model_call_start([{"role": "user", "content": "x"}], caller="judge") is None
    model_io.model_call_end(None, content="y")                    # must not raise
    assert model_io.load_model_calls(tmp_path) == {}


def test_source_override_writes_to_that_sources_store(tmp_path):
    # mini-swe runs in the agent process but tags its calls MINI_SWE_AGENT.
    _init(str(tmp_path), "tr", source=Source.APPSCI_AGENT)
    try:
        h = model_io.model_call_start([{"role": "user", "content": "q"}], model="m",
                                      caller="mini_swe", source=Source.MINI_SWE_AGENT)
        model_io.model_call_end(h, content="a")
    finally:
        _reset()
    calls = list(model_io.load_model_calls(tmp_path / "tr").values())
    assert len(calls) == 1 and calls[0]["source"] == "mini_swe_agent"
    assert (tmp_path / "tr" / "model_calls-mini_swe_agent.jsonl").is_file()


def test_complete_captures_model_call(tmp_path):    # complete() is the single eval/user-sim choke point; with a publisher live it records a ModelCall.
    from appsci_bench.common.model_config import complete, ModelConfig

    class _Msg:  content = "judged: 0.9"
    class _Choice: message = _Msg()
    class _Resp:  choices = [_Choice()]; usage = None

    _init(str(tmp_path), "tr")
    try:
        with patch("appsci_bench.common.model_config.litellm.completion", return_value=_Resp()):
            out = complete([{"role": "user", "content": "grade this"}],
                           ModelConfig(model="claude-x", api_key=None, base_url=None), caller="judge")
        assert out == "judged: 0.9"
    finally:
        _reset()
    calls = list(model_io.load_model_calls(tmp_path / "tr").values())
    assert len(calls) == 1
    assert calls[0]["caller"] == "judge" and calls[0]["content"] == "judged: 0.9"
    assert calls[0]["messages"][0]["content"] == "grade this"


def test_complete_files_the_call_under_an_explicit_source(tmp_path):
    """`complete()` must be able to name the source it is calling AS, not just inherit the process's.

    This is the seam the eval-time user sim rides on: it runs on the evaluator's publisher, and
    without an override every one of its turns is stored as the evaluator's own. Same override
    mini-swe uses to stay separable from the agent process it runs inside.
    """
    from appsci_bench.common.model_config import complete, ModelConfig

    class _Msg:  content = "rebook me on the 8am"
    class _Choice: message = _Msg()
    class _Resp:  choices = [_Choice()]; usage = None

    _init(str(tmp_path), "tr", source=Source.EVALUATOR)
    try:
        with patch("appsci_bench.common.model_config.litellm.completion", return_value=_Resp()):
            complete([{"role": "user", "content": "your turn"}],
                     ModelConfig(model="openai/gpt-5.2"), caller="user_sim",
                     source=Source.ARTIFACT_USERSIM)
    finally:
        _reset()

    calls = list(model_io.load_model_calls(tmp_path / "tr").values())
    assert len(calls) == 1 and calls[0]["source"] == "artifact_usersim"
    # The shard filename is what the usage rollup groups by, so it is the assertion that matters.
    assert (tmp_path / "tr" / "model_calls-artifact_usersim.jsonl").is_file()
    assert not (tmp_path / "tr" / "model_calls-evaluator.jsonl").exists()


def test_eval_time_user_sim_is_filed_apart_from_the_judges(tmp_path):
    """The eval-phase user sim and the judges share one process; they must not share one usage line.

    Both talk to a model through `complete()` on the evaluator's publisher, so with the publisher
    default their records go to the same `model_calls-evaluator.jsonl` -- and the per-run usage
    rollup keys off that filename. That is why one real airline run reported 376 "evaluator" calls:
    judges plus user-sim turns added together, on two different models at two different prices, with
    nothing in the report able to tell them apart.
    """
    from appsci_bench.common.model_config import ModelConfig
    from appsci_bench.eval.artifact_agent_user_sim import ArtifactAgentUserSim
    from appsci_bench.eval.datatypes import MultiTurnInput
    from appsci_bench.eval.helpers.llm import llm_judge_call

    def _resp(content, total_tokens):
        class _Msg:  pass
        class _Choice: pass
        class _Resp: pass
        _Msg.content = content
        _Choice.message = _Msg()
        _Resp.choices = [_Choice()]
        _Resp.usage = {"total_tokens": total_tokens}
        return _Resp

    sim_reply = json.dumps({"reasoning": "still going", "stopped": False, "message": "rebook me"})
    judge_reply = json.dumps({"reasoning": "it rebooked her", "score": 1.0})

    _init(str(tmp_path), "tr", source=Source.EVALUATOR)
    try:
        with patch("appsci_bench.common.model_config.litellm.completion",
                   side_effect=[_resp(sim_reply, 120), _resp(judge_reply, 7)]):
            sim = ArtifactAgentUserSim(config=ModelConfig(model="openai/gpt-5.2"))
            session = sim.create_session(MultiTurnInput(user_task="rebook", user_persona="Emma"))
            assert sim.ask(session, []) is not None
            llm_judge_call(system_prompt="grade it", user_prompt="the transcript",
                           config=ModelConfig(model="claude-x"))
    finally:
        _reset()

    def _sources(path):
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]

    sim_calls = _sources(tmp_path / "tr" / "model_calls-artifact_usersim.jsonl")
    judge_calls = _sources(tmp_path / "tr" / "model_calls-evaluator.jsonl")
    assert [c["caller"] for c in sim_calls] == ["user_sim"]
    assert [c["caller"] for c in judge_calls] == ["judge"]
    # Tokens follow the split, which is the point: the two roles run different models at different
    # prices, so a blended per-source line cannot be priced against any single rate card.
    assert sim_calls[0]["usage"] == {"total_tokens": 120}
    assert sim_calls[0]["model"] == "openai/gpt-5.2"
    assert judge_calls[0]["usage"] == {"total_tokens": 7}
    assert judge_calls[0]["model"] == "claude-x"


def test_model_call_context_tags_record_and_events(tmp_path):
    # model_call_context tags every call in its scope with the eval case, landing in the ModelCall
    # record + both referencing events -- this is what lets the dashboard group calls per case.
    _init(str(tmp_path), "tr")
    try:
        with model_io.model_call_context(data_point_id="tau2_airline_1", repeat_index=0):
            h = model_io.model_call_start([{"role": "user", "content": "grade"}], caller="judge")
            model_io.model_call_end(h, content="ok")
        # outside the scope, no tags leak
        h2 = model_io.model_call_start([{"role": "user", "content": "plain"}], caller="judge")
        model_io.model_call_end(h2, content="ok2")
    finally:
        _reset()

    evs = [json.loads(l) for l in get_source_log_path("tr", Source.EVALUATOR, str(tmp_path)).read_text().splitlines() if l.strip()]
    tagged = [e for e in evs if e["payload"].get("data_point_id") == "tau2_airline_1"]
    assert len(tagged) == 2                                        # request + response both tagged
    assert all(e["payload"].get("repeat_index") == 0 for e in tagged)

    calls = list(model_io.load_model_calls(tmp_path / "tr").values())
    tagged_call = next(c for c in calls if c["messages"][0]["content"] == "grade")
    plain_call = next(c for c in calls if c["messages"][0]["content"] == "plain")
    assert tagged_call["context"] == {"data_point_id": "tau2_airline_1", "repeat_index": 0}
    assert not plain_call["context"]                              # None or {} -> falsy


def test_model_call_context_noop_without_publisher(tmp_path):
    # The context manager is always safe to enter, even with observability disabled.
    _reset()
    with model_io.model_call_context(data_point_id="dp"):
        assert model_io.model_call_start([{"role": "user", "content": "x"}], caller="judge") is None
    assert model_io.load_model_calls(tmp_path) == {}


def test_a_failed_model_call_still_records_its_conversation(tmp_path):
    """A raising query must not orphan its span, or the tracer has nothing to render.

    The incident: an expanded mini-swe dispatch in an airline_rebooking trace showed
    "(13 internal steps)" and then "No captured mini-swe conversation." The steps were gone, not
    merely mis-rendered.

    Cause: `model_call_start` emits MODEL_REQUEST and returns a handle holding the messages, but
    `model_call_end` is what writes the `ModelCall` record. When the underlying query RAISED, end was
    never reached -- no record, and the conversation captured at start went with it. The tracer
    resolves a sub-agent's conversation from the deepest of its model-call refs, which is precisely
    the failed one, so a single dead step blanked the whole expanded view.

    Gateway timeouts and `gateway per-user rate limit exceeded` (429) both land on this path, which
    makes it the common case rather than an exotic one -- we tripped it repeatedly ourselves while
    running the very sweep whose trace was then unreadable. It also means historical logs omit
    exactly the throttled calls, so any request-rate measured from them is a lower bound.

    Pinned here on `model_io`, the seam that owns the guarantee: a record must exist and must carry
    the messages even when the call failed. `InstrumentedModel` in appsci_agent/tools/mini_swe_agent.py
    is the caller obliged to honour it, in its `except` before re-raising.
    """
    _reset()
    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    init_publisher(runs_dir=str(tmp_path), trace_id="tr", source=Source.MINI_SWE_AGENT, task_id="t")

    messages = [
        {"role": "system", "content": "you are mini-swe"},
        {"role": "user", "content": "explore the environment"},
    ]
    pending = model_io.model_call_start(
        messages, model="claude-opus-4-8", caller="mini-swe", source=Source.MINI_SWE_AGENT
    )
    assert pending is not None

    # Exactly what the caller must do in its `except`: close the span, then re-raise.
    model_io.model_call_end(pending, content="<model call failed: RateLimitError: 429>")

    calls = model_io.load_model_calls(tmp_path / "tr")
    assert len(calls) == 1, "a failed call must still produce a ModelCall record"
    call = next(iter(calls.values()))
    # The conversation is what the tracer renders, and what was being lost.
    assert call["messages"] == messages
    assert "429" in (call["content"] or ""), "the failure should be legible in the record"
    # Usage and cost honestly absent rather than zero -- the call never completed.
    assert not call.get("usage")
    assert call.get("cost_usd") is None


# ---------------------------------------------------------------------------
# The trace-level invariant: no dangling refs.
#
# WHY these live here, and why the failed-call test above was not enough. That test asserts that
# `model_call_end` CAN record a failed call -- a capability of the seam -- and its own docstring names
# `InstrumentedModel` as "the caller obliged to honour it". Nothing asserted the obligation, so when
# the same hole stayed open at the other two call sites (agent_loop in appsci_agent/core/agent.py, and
# the eval judge / user-sim through appsci_bench/common/model_config.py) the suite stayed green while
# the dashboard broke.
#
# What broke, concretely: `model_call_start` emits MODEL_REQUEST carrying a `ref`; `model_call_end`
# writes the record that `ref` points at. A raising call produced the event and no record. The tracer
# builds its build-phase model-call rows FROM those events, so a row and its "full I/O ->" button
# rendered for a call whose I/O did not exist, and /api/model_io 404'd into an empty modal.
#
# Measured on the live box before the fix:
#   runs/airline_rebooking_20260807_091549 -- 50 of 96 refs dangling (evaluator/user_sim)
#   runs/airline_rebooking_20260807_202229 -- 47 of 422 dangling (evaluator/judge)
# every one a MODEL_REQUEST with no MODEL_RESPONSE, i.e. the call raised. In 091549 that is more than
# half of the run's model I/O unreachable from the UI.
#
# So these assert the invariant the VIEW depends on -- every advertised ref resolves -- rather than any
# one call site's internals. A future call site that forgets `model_call` fails the invariant test.
# ---------------------------------------------------------------------------


def _refs_and_records(tmp_path, trace="tr"):
    """Every `ref` the event stream advertises, plus the records available to resolve them.

    Mirrors what the dashboard does: rows (hence "full I/O ->" buttons) come from the events, and the
    modal's content comes from `load_model_calls`.
    """
    refs = []
    for log in sorted((tmp_path / trace).glob("events-*.jsonl")):
        for line in log.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e["event_type"] == str(EventType.MODEL_REQUEST) and e["payload"].get("ref"):
                refs.append(e["payload"]["ref"])
    return refs, model_io.load_model_calls(tmp_path / trace)


def test_model_call_scope_records_a_raising_call_and_reraises(tmp_path):
    """`model_call` must close the span on the way out and let the original exception through."""
    _reset()
    _init(str(tmp_path), "tr", source=Source.APPSCI_AGENT)
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "build it"}]
    try:
        with pytest.raises(RuntimeError, match="gateway per-user rate limit exceeded"):
            with model_io.model_call(messages, model="claude-opus-4-8", caller="agent_loop"):
                raise RuntimeError("gateway per-user rate limit exceeded")
    finally:
        _reset()

    refs, calls = _refs_and_records(tmp_path)
    assert len(refs) == 1, "the request event is emitted before the call, so it must exist"
    assert refs[0] in calls, "a raising call must still leave a record for the ref it advertised"
    call = calls[refs[0]]
    # The conversation is the whole point: captured at start, and what was being dropped.
    assert call["messages"] == messages
    assert "RuntimeError" in call["content"] and "rate limit" in call["content"]
    assert call["caller"] == "agent_loop"
    # Honestly absent rather than zero -- the call never completed, so there is no usage or price.
    assert not call.get("usage")
    assert call.get("cost_usd") is None


def test_model_call_scope_keeps_the_real_record_when_a_later_line_raises(tmp_path):
    """A success already recorded must not be overwritten by the failure stub.

    Guards the `_Pending.closed` flag. The scope wraps more than the network call (response unpacking,
    the empty-content check in model_config.complete), and those raise AFTER `model_call_end` has
    written the good record. Without the flag this would find "<model call failed: ...>" where the real
    completion should be -- a worse bug than the one being fixed, since it corrupts good traces.
    """
    _reset()
    _init(str(tmp_path), "tr", source=Source.APPSCI_AGENT)
    try:
        with pytest.raises(ValueError):
            with model_io.model_call([{"role": "user", "content": "q"}], caller="agent_loop") as mc:
                model_io.model_call_end(mc, content="the real completion",
                                        usage={"total_tokens": 11}, cost_usd=0.25)
                raise ValueError("raised after the call was already recorded")
    finally:
        _reset()

    refs, calls = _refs_and_records(tmp_path)
    call = calls[refs[0]]
    assert call["content"] == "the real completion"
    assert call["usage"] == {"total_tokens": 11}
    assert call["cost_usd"] == 0.25, "the cost_usd accounting must survive the error path"


def test_model_call_end_is_idempotent(tmp_path):
    """Two ends for one call must not append two records for the same ref: reads are last-write-wins,
    so the duplicate is invisible in the UI while the store grows one extra row per call."""
    _reset()
    _init(str(tmp_path), "tr", source=Source.APPSCI_AGENT)
    try:
        with model_io.model_call([{"role": "user", "content": "q"}], caller="agent_loop") as mc:
            model_io.model_call_end(mc, content="first")
            model_io.model_call_end(mc, content="second")
    finally:
        _reset()

    lines = [l for l in (tmp_path / "tr" / "model_calls-appsci_agent.jsonl").read_text().splitlines()
             if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["content"] == "first"


def test_no_dangling_refs_invariant_across_mixed_outcomes(tmp_path):
    """THE contract the dashboard depends on: every ref the events advertise resolves to a record.

    A throttled run is a mix of successes and rate-limited failures, and that mix is what produced a
    half-unreadable trace on the live box. Asserted as a set difference so any future call site that
    emits a request without recording a result fails here, whichever site it is.
    """
    _reset()
    _init(str(tmp_path), "tr", source=Source.APPSCI_AGENT)
    try:
        for i in range(6):
            if i % 2:
                with pytest.raises(RuntimeError):
                    with model_io.model_call([{"role": "user", "content": f"q{i}"}],
                                             caller="agent_loop"):
                        raise RuntimeError(f"429 on call {i}")
            else:
                with model_io.model_call([{"role": "user", "content": f"q{i}"}],
                                         caller="agent_loop") as mc:
                    model_io.model_call_end(mc, content=f"a{i}", usage={"total_tokens": 5})
    finally:
        _reset()

    refs, calls = _refs_and_records(tmp_path)
    assert len(refs) == 6
    assert set(refs) - set(calls) == set(), "a dangling ref renders a 'full I/O' button with no I/O"
    # The failures stay legible as failures rather than looking like empty completions.
    failed = [c for c in calls.values() if str(c["content"]).startswith("<model call failed")]
    assert len(failed) == 3
    assert all(c["messages"] for c in calls.values()), "every record keeps its prompt"


def test_model_call_scope_is_safe_without_a_publisher(tmp_path):
    """Observability off: the scope yields None and never interferes with the wrapped call."""
    _reset()
    with model_io.model_call([{"role": "user", "content": "q"}], caller="agent_loop") as mc:
        assert mc is None
    with pytest.raises(RuntimeError):
        with model_io.model_call([{"role": "user", "content": "q"}], caller="agent_loop"):
            raise RuntimeError("boom")
    assert model_io.load_model_calls(tmp_path) == {}


# ---------------------------------------------------------------------------
# The other half of the same contract, from the opposite direction.
#
# Everything above pins "a call that RAISED still leaves a resolvable record". This pins "a call that
# SUCCEEDED is not turned into a failure by the code measuring it" -- README.md's rule that the call
# never fails because of observability.
#
# The hazard is specific to `model_call` being a context manager that re-raises: it stubs the span
# `<model call failed: ...>` and lets the exception through. So an exception thrown by the
# INSTRUMENTATION, after the model call already returned, is indistinguishable downstream from the
# model call itself failing -- and it also kills the mini-swe step. `InstrumentedModel.query` reads
# three levels into a litellm object it does not own (`extra` -> `response` -> `usage`), which is
# where a library shape change lands.
# ---------------------------------------------------------------------------


class _NoMapping:
    """A response object with no `.get` and no `usage`, i.e. a litellm shape we did not anticipate."""


def _instrumented_model_returning(payload, tmp_path, monkeypatch):
    """An InstrumentedModel whose underlying query returns `payload`, with tracing live."""
    from minisweagent.models.litellm_model import LitellmModel

    from appsci_agent.tools.mini_swe_agent import InstrumentedModel

    monkeypatch.setattr(LitellmModel, "query", lambda self, messages, **kw: payload)
    # __init__ would reach for provider credentials; the instrumentation is all we are exercising.
    model = InstrumentedModel.__new__(InstrumentedModel)
    model.config = type("C", (), {"model_name": "claude-opus-4-8"})()
    model._parent_span_id = None
    return model


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"content": "ok", "extra": {"response": _NoMapping()}}, id="response-no-get"),
        # A CONTROL, not a guard case: `(None or {})` was already safe, so this one passes with the
        # guard removed too. It is here to pin that the pre-existing `or {}` keeps working, and it is
        # labelled so nobody reads it as evidence the guard does something.
        pytest.param({"content": "ok", "extra": {"response": None}}, id="response-missing-control"),
        pytest.param("not even a dict", id="result-not-a-dict"),
    ],
)
def test_a_successful_call_survives_an_unexpected_response_shape(tmp_path, monkeypatch, payload):
    """The step must not die because the usage read found something it did not expect.

    Each payload is a shape that would raise inside the instrumentation. Before the guard, that
    exception left `query` through `model_call`, which re-raised it -- so mini-swe saw a failed step
    for a model call that had already returned successfully, and the trace recorded it as
    `<model call failed: AttributeError ...>`.
    """
    _reset()
    _init(str(tmp_path), "tr", source=Source.MINI_SWE_AGENT)
    model = _instrumented_model_returning(payload, tmp_path, monkeypatch)

    # The call returns, unchanged, rather than raising.
    assert model.query([{"role": "user", "content": "go"}]) == payload

    # And the span was closed as a success: a record exists and is NOT a failure stub.
    _reset()
    calls = list(model_io.load_model_calls(tmp_path / "tr").values())
    assert len(calls) == 1, f"expected exactly one model-call record, got {calls}"
    content = calls[0].get("content") or ""
    assert "model call failed" not in content, (
        "a call that succeeded was recorded as a failure because its instrumentation raised"
    )


def test_usage_is_still_recorded_when_the_response_is_an_object_not_a_dict(tmp_path, monkeypatch):
    """The guard must not have been bought by giving up on the measurement.

    litellm hands back a `ModelResponse`, which supports `.get()` in 1.88.1 -- so the ordinary path
    still has to capture tokens. A guard that swallowed everything would pass the test above and
    silently stop recording BUILD-phase spend, which is the bulk of a run.
    """
    _reset()
    _init(str(tmp_path), "tr", source=Source.MINI_SWE_AGENT)
    usage = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
    payload = {"content": "ok", "extra": {"response": {"usage": usage}, "cost": 0.25}}
    model = _instrumented_model_returning(payload, tmp_path, monkeypatch)

    model.query([{"role": "user", "content": "go"}])

    _reset()
    calls = list(model_io.load_model_calls(tmp_path / "tr").values())
    assert len(calls) == 1
    assert calls[0]["usage"]["total_tokens"] == 16
    assert calls[0]["cost_usd"] == 0.25


# ---------------------------------------------------------------------------
# Single-call lookup. The dashboard fetches ONE model call to render a sub-agent
# conversation; doing that by loading the whole store made the gigabyte traces written
# by an uncached sub-agent unreadable, which the UI showed as "No captured mini-swe
# conversation" -- so the runs that most needed inspecting were the ones that could not be.
# ---------------------------------------------------------------------------

def _write_store(tmp_path, source, records):
    d = tmp_path / "tr"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"model_calls-{source}.jsonl").open("w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return d


def test_load_model_call_returns_the_requested_record(tmp_path):
    d = _write_store(tmp_path, "mini_swe_agent", [
        {"call_id": "a", "messages": [{"role": "user", "content": "first"}]},
        {"call_id": "b", "messages": [{"role": "user", "content": "second"}]},
    ])
    assert model_io.load_model_call(d, "b")["messages"][0]["content"] == "second"
    assert model_io.load_model_call(d, "a")["messages"][0]["content"] == "first"


def test_load_model_call_misses_return_none_rather_than_raising(tmp_path):
    d = _write_store(tmp_path, "mini_swe_agent", [{"call_id": "a"}])
    assert model_io.load_model_call(d, "nope") is None
    assert model_io.load_model_call(d, "") is None            # no ref -> no scan
    assert model_io.load_model_call(tmp_path / "absent", "a") is None


def test_load_model_call_searches_every_source_store(tmp_path):
    d = _write_store(tmp_path, "appsci_agent", [{"call_id": "outer"}])
    _write_store(tmp_path, "mini_swe_agent", [{"call_id": "inner"}])
    assert model_io.load_model_call(d, "outer")["call_id"] == "outer"
    assert model_io.load_model_call(d, "inner")["call_id"] == "inner"


def test_load_model_call_matches_on_call_id_not_a_mention_elsewhere(tmp_path):
    """The substring pre-filter is an optimisation, never the match.

    A ref legitimately appears in other records (as `parent_span_id`, or quoted inside a message), so
    a scan that returned the first line *containing* it would hand back the wrong conversation.
    """
    d = _write_store(tmp_path, "mini_swe_agent", [
        {"call_id": "decoy", "parent_span_id": "wanted",
         "messages": [{"role": "user", "content": "mentions wanted"}]},
        {"call_id": "wanted", "messages": [{"role": "user", "content": "the real one"}]},
    ])
    got = model_io.load_model_call(d, "wanted")
    assert got["call_id"] == "wanted"
    assert got["messages"][0]["content"] == "the real one"


def test_load_model_call_skips_malformed_lines(tmp_path):
    d = tmp_path / "tr"
    d.mkdir(parents=True, exist_ok=True)
    (d / "model_calls-mini_swe_agent.jsonl").write_text(
        "{not json but mentions wanted\n" + json.dumps({"call_id": "wanted", "ok": True}) + "\n"
    )
    assert model_io.load_model_call(d, "wanted")["ok"] is True


def test_load_model_call_never_reads_a_whole_store_into_memory(tmp_path):
    """The regression guard, and the reason this function exists.

    `read_text()` on a multi-gigabyte store is what broke the dashboard, so the lookup must not use
    it -- and asserting on peak memory would be flaky. Breaking `read_text` instead states the
    contract exactly: find the record without ever materialising the file.
    """
    d = _write_store(tmp_path, "mini_swe_agent", [{"call_id": "a"}, {"call_id": "wanted", "ok": True}])

    from pathlib import Path as _Path

    def _explode(*a, **k):
        raise AssertionError("load_model_call must stream, not read the whole store")

    with patch.object(_Path, "read_text", _explode):
        assert model_io.load_model_call(d, "wanted")["ok"] is True


# ---------------------------------------------------------------------------
# finish_reason. Truncated calls were indistinguishable from deliberate ones: a tool
# call whose arguments arrived as `{}` looked like the model choosing to send nothing,
# so a hit output ceiling read as the model failing at a trivial call, 37 times.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", ["length", "stop", "tool_calls", "content_filter"])
def test_finish_reason_is_recorded_on_the_call_and_the_event(tmp_path, reason):
    _init(str(tmp_path), "tr")
    try:
        h = model_io.model_call_start([{"role": "user", "content": "go"}], model="m", caller="c")
        model_io.model_call_end(h, content="out", finish_reason=reason)
    finally:
        _reset()

    call = model_io.load_model_call(tmp_path / "tr", h.call_id)
    assert call["finish_reason"] == reason

    # Also on the event, so a reader can spot a truncated call without fetching the record --
    # the truncated ones being exactly the calls worth finding.
    evs = [json.loads(l) for l
           in get_source_log_path("tr", Source.EVALUATOR, str(tmp_path)).read_text().splitlines()
           if l.strip()]
    resp = next(e for e in evs if e["event_type"] == str(EventType.MODEL_RESPONSE))
    assert resp["payload"]["finish_reason"] == reason


def test_finish_reason_defaults_to_none_when_the_transport_does_not_report_one(tmp_path):
    """Absent and "the provider never said" must stay distinguishable from a real value."""
    _init(str(tmp_path), "tr")
    try:
        h = model_io.model_call_start([{"role": "user", "content": "go"}], model="m", caller="c")
        model_io.model_call_end(h, content="out")
    finally:
        _reset()

    assert model_io.load_model_call(tmp_path / "tr", h.call_id)["finish_reason"] is None


def test_records_written_before_the_field_existed_still_load(tmp_path):
    """Every mirrored run in S3 predates this field; reading one must not break."""
    d = _write_store(tmp_path, "mini_swe_agent", [{"call_id": "old", "content": "x"}])
    got = model_io.load_model_call(d, "old")
    assert got["content"] == "x"
    assert model_io.ModelCall.from_dict(got).finish_reason is None
