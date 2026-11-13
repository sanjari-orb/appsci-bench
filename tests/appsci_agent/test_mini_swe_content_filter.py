"""LLM-call finish reasons are surfaced, not masked, when a sub-task's turns come back empty.

When the coding sub-agent's response carries no bash tool call -- because a content filter blocked it,
because it was truncated, or because the model just omitted the call -- mini-swe records a format
error and, after enough in a row, ends the sub-task with `RepeatedFormatError`. Two models are then in
the dark about WHY the turns were empty:

  1. the sub-agent itself, which used to get a generic "you forgot a tool call" nudge regardless of
     the real reason; and
  2. the meta-agent above it, which used to see only the opaque `RepeatedFormatError` exit status.

The fix is general, not tied to one error: the rendered format-error prompt states whatever
finish_reason the call actually returned, and the tool annotates the meta-agent-facing error with the
finish reasons behind the empty turns. The block that motivated this was an opus-5 run whose calls
came back `content_filter` (crm_insights_claude-opus-5_b2_l3_20260920_175834), reported to neither
model at the time.
"""
from __future__ import annotations

from jinja2 import StrictUndefined, Template

from appsci_agent.tools.mini_swe_agent import _abnormal_finish_reasons, load_appsci_config


def _render_format_error(**extra) -> str:
    template = load_appsci_config()["model"]["format_error_template"]
    # StrictUndefined mirrors how mini-swe renders it: a template naming a missing variable would
    # raise on every step, so rendering under strictness is the point of the test.
    return Template(template, undefined=StrictUndefined).render(**extra)


def test_format_error_states_the_actual_finish_reason():
    """content_filter is reported verbatim, not lumped into the generic missing-tool-call message."""
    rendered = _render_format_error(error="boom", finish_reason="content_filter")
    assert "finish_reason=content_filter" in rendered
    # It must not mislead a filtered turn into thinking it just forgot the tool-call syntax.
    assert "MUST include at least one bash tool call" not in rendered
    # Any other abnormal reason is surfaced the same general way.
    other = _render_format_error(error="boom", finish_reason="something_else")
    assert "finish_reason=something_else" in other


def test_truncation_still_gets_the_be_concise_hint():
    cut_off = _render_format_error(error="boom", finish_reason="length")
    assert "finish_reason=length" in cut_off
    assert "cut off" in cut_off


def test_plain_missing_tool_call_keeps_the_ordinary_guidance():
    """No finish_reason (or a normal one) -> the generic branch, unchanged."""
    rendered = _render_format_error(error="boom")  # no finish_reason
    assert "Tool Call Error" in rendered
    assert "finish_reason" not in rendered

    normal = _render_format_error(error="boom", finish_reason="stop")
    assert "Tool Call Error" in normal
    assert "finish_reason" not in normal


class _Agent:
    """Stand-in for mini-swe's DefaultAgent: only `messages` is read by the counter."""

    def __init__(self, messages):
        self.messages = messages


def _empty_turn(finish_reason: str) -> dict:
    """A format-error message as litellm leaves it: the raw provider response under extra.response."""
    return {
        "role": "user",
        "content": "**Tool Call Error:** No tool calls found in the response.",
        "extra": {"response": {"choices": [{"finish_reason": finish_reason}]}},
    }


def test_counter_reports_abnormal_finish_reasons_by_frequency():
    agent = _Agent(
        [
            {"role": "system", "content": "..."},
            {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "bash"}}]},
            _empty_turn("content_filter"),
            _empty_turn("content_filter"),
            _empty_turn("length"),
        ]
    )
    assert _abnormal_finish_reasons(agent) == {"content_filter": 2, "length": 1}


def test_counter_ignores_normal_finishes_and_never_raises():
    # Normal completions are not "abnormal" and are not reported.
    assert _abnormal_finish_reasons(_Agent([_empty_turn("stop"), _empty_turn("tool_calls")])) == {}
    # Missing / oddly-shaped fields degrade to an empty mapping rather than raising.
    assert _abnormal_finish_reasons(_Agent([])) == {}
    assert _abnormal_finish_reasons(_Agent([{"role": "user", "content": "x"}])) == {}
    assert _abnormal_finish_reasons(_Agent([{"role": "user", "extra": {"response": {}}}])) == {}
    assert _abnormal_finish_reasons(_Agent(["not-a-dict", None])) == {}
    assert _abnormal_finish_reasons(_Agent(None)) == {}
