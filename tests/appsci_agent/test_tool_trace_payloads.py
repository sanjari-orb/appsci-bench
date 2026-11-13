"""What a tool call leaves in the trace.

The agent loop used to record two tools' fields and nothing else: `task` (mini_swe_agent's) and
`question` (talk_to_user's) on the invocation, and `success`/`duration_ms`/`answer` on the result. Any
other tool was therefore recorded as having been called with no arguments and having returned "ok" --
including `optimize_agent`, whose entire output is a finding (a score, a saturation state, an
accept/reject verdict, the checkpoint the score belongs to).

These pin the generic mechanism that replaced it: the invocation carries the call's own arguments, and
the result carries whatever the tool nominated through `trace_output_keys`.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from appsci_agent.core.agent import _trace_args, _trace_output
from appsci_agent.tools.base import BaseTool, ToolResult


class _Tool(BaseTool):
    """Minimal concrete tool: only `trace_output_keys` matters here."""

    def __init__(self, keys=()):
        self.trace_output_keys = keys

    @property
    def name(self):
        return "t"

    @property
    def description(self):
        return "d"

    @property
    def parameters(self):
        return {}

    def execute(self, params, context):  # pragma: no cover -- not exercised
        raise NotImplementedError


# --- invocation arguments ------------------------------------------------------------------------

def test_the_calls_other_arguments_are_recorded():
    args = _trace_args({"endpoint_url": "http://c:8080/respond", "score_only": True, "timeout_s": 300})
    assert args["endpoint_url"] == "http://c:8080/respond"
    # Stringified, because this is a summary for a reader, not a replayable record.
    assert args["score_only"] == "true"
    assert args["timeout_s"] == "300"


def test_task_and_question_are_left_to_their_own_fields():
    """They are emitted separately and drive the tracer's one-line preview; duplicating them here
    would double the largest string in the event."""
    args = _trace_args({"task": "a" * 5000, "question": "why?", "context": "keep me"})
    assert "task" not in args and "question" not in args
    assert args["context"] == "keep me"


def test_a_none_argument_is_omitted_rather_than_recorded_as_null():
    """So a reader can tell "not passed" from "passed as null"."""
    assert _trace_args({"endpoint_url": None, "score_only": False}) == {"score_only": "false"}


def test_one_huge_argument_cannot_dominate_the_event():
    args = _trace_args({"blob": "x" * 100_000})
    assert len(args["blob"]) < 500
    assert args["blob"].endswith("…")


def test_many_arguments_are_capped_in_total():
    """An event is written per call, so the whole blob is bounded, not just each value."""
    args = _trace_args({f"k{i}": "y" * 400 for i in range(50)})
    assert sum(len(v) for v in args.values()) < 3000
    assert len(args) < 50                                  # later keys dropped once the budget ran out


def test_no_arguments_is_an_empty_dict_not_a_crash():
    assert _trace_args({}) == {}
    assert _trace_args(None) == {}


def test_an_unserializable_argument_does_not_break_the_call():
    """Params come from the model via JSON, so this is defensive -- but instrumentation may not raise."""
    args = _trace_args({"weird": object()})
    assert "weird" in args                                 # rendered via default=str


# --- result fields -------------------------------------------------------------------------------

def test_a_tool_that_nominates_nothing_records_nothing_extra():
    """The default. success/duration are added by the caller, so this stays empty."""
    res = ToolResult(success=True, output={"headline_metric": 0.9})
    assert _trace_output(_Tool(), res) == {}


def test_the_nominated_fields_are_copied():
    res = ToolResult(success=True, output={
        "headline_metric": 0.42, "saturated": False, "cases": [{"id": "c1"}] * 100,
    })
    out = _trace_output(_Tool(("headline_metric", "saturated")), res)
    assert out == {"headline_metric": 0.42, "saturated": False}
    # The large field is not swept in just because it exists.
    assert "cases" not in out


def test_a_nominated_field_the_tool_did_not_return_is_skipped():
    """Skipped, not recorded as null, so "this tool does not report that" stays distinguishable."""
    res = ToolResult(success=True, output={"status": "saturated"})
    assert _trace_output(_Tool(("status", "cycles_used")), res) == {"status": "saturated"}


@pytest.mark.parametrize("output", [None, "a string", 5, []])
def test_a_non_dict_output_is_survived(output):
    res = ToolResult(success=True, output=output)
    assert _trace_output(_Tool(("status",)), res) == {}


def test_a_tool_without_the_attribute_at_all_is_survived():
    """`getattr` default, so a tool predating this mechanism needs no change."""
    res = ToolResult(success=True, output={"status": "ok"})
    assert _trace_output(SimpleNamespace(), res) == {}


# --- what the two new tools actually nominate ----------------------------------------------------

def test_optimize_agent_traces_its_finding_but_not_its_case_detail():
    """The point of the whole mechanism: the score, the candidate it belongs to, why the loop
    stopped, and what the cycle did -- without the per-case blob, which is on disk."""
    from appsci_agent.tools import OptimizeAgentTool
    keys = set(OptimizeAgentTool.trace_output_keys)
    assert {"headline_metric", "saturated", "cycles_used", "cycles_allowed",
            "budget_exhausted", "checkpoint", "cycle", "awaiting_verification"} <= keys
    assert "cases" not in keys
    # ...but the path to the full history is traced, so a reader can find the case detail.
    assert "history_path" in keys


def test_eval_generator_traces_where_the_harness_landed():
    from appsci_agent.tools import EvalGeneratorTool
    assert {"eval_dir", "files"} <= set(EvalGeneratorTool.trace_output_keys)
