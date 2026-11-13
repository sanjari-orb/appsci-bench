"""Unit tests for the generic LLM-judge call. No network -- `complete()`
is mocked at the import site, mirroring `test_usersim_core.py`'s stubbing pattern.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from pydantic import BaseModel, Field

from appsci_bench.common.model_config import ModelConfig
from appsci_bench.eval.helpers import llm as llm_mod
from appsci_bench.eval.helpers.llm import (
    JudgeParseError,
    JudgeVerdict,
    _describe_output_schema,
    _parse_structured,
    _salvage_fields,
    llm_judge_call,
)

_CFG = ModelConfig(model="anthropic/claude-opus-4-8", api_key="no-token")


def _call(**kwargs):
    return llm_judge_call(
        system_prompt="you are a judge", user_prompt="judge this", config=_CFG, **kwargs)


def test_parses_clean_json():
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "looks correct", "score": 0.9}'):
        verdict = _call()
    assert isinstance(verdict, JudgeVerdict)
    assert verdict.reasoning == "looks correct" and verdict.score == 0.9


def test_parses_fenced_json():
    raw = 'Sure, here is my verdict:\n```json\n{"reasoning": "ok", "score": 0.5}\n```\nHope that helps!'
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=raw):
        verdict = _call()
    assert verdict.score == 0.5


def test_parses_json_embedded_in_prose():
    raw = 'Let me think... {"reasoning": "embedded", "score": 0.2} -- that is my final answer.'
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=raw):
        verdict = _call()
    assert verdict.reasoning == "embedded" and verdict.score == 0.2


def test_extraction_balances_nested_braces_not_just_the_first_close_brace():
    # The embedded-in-prose extraction path (_first_json_object) must depth-track braces -- a
    # naive first-`{`-to-first-`}` match would truncate this at the closing brace of `per_dimension`.
    raw = ('Here is my analysis: {"reasoning": "solid", "score": 0.7, '
           '"per_dimension": {"clarity": 0.8, "coverage": 0.6}} Let me know if you need more.')
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=raw):
        verdict = _call(output_schema=_MultiDimensionVerdict)
    assert verdict.score == 0.7
    assert verdict.per_dimension == {"clarity": 0.8, "coverage": 0.6}


def test_raises_judge_parse_error_on_total_failure():
    with patch("appsci_bench.eval.helpers.llm.complete", return_value="not json at all, sorry"):
        with pytest.raises(JudgeParseError):
            _call()


def test_raises_on_schema_mismatch_even_if_json_is_valid():
    # Valid JSON, but missing the required `score` field -- must not silently coerce/default it.
    with patch("appsci_bench.eval.helpers.llm.complete", return_value='{"reasoning": "no score field"}'):
        with pytest.raises(JudgeParseError):
            _call()


class _MultiDimensionVerdict(BaseModel):
    """A caller-defined schema with extra fields beyond the base JudgeVerdict shape."""
    model_config = {"extra": "forbid"}
    reasoning: str
    score: float
    per_dimension: dict[str, float]


def test_custom_output_schema_with_extra_fields_round_trips():
    raw = ('{"reasoning": "multi-dim", "score": 0.75, '
           '"per_dimension": {"clarity": 0.8, "coverage": 0.7}}')
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=raw):
        verdict = _call(output_schema=_MultiDimensionVerdict)
    assert isinstance(verdict, _MultiDimensionVerdict)
    assert verdict.score == 0.75
    assert verdict.per_dimension == {"clarity": 0.8, "coverage": 0.7}


def test_requests_json_object_output():
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 1.0}') as mock_complete:
        _call()
    _, kwargs = mock_complete.call_args
    assert kwargs.get("json_object") is True


def test_defaults_to_model_config_from_env_when_no_config_given(monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "openai/gpt-5.5")
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 1.0}') as mock_complete:
        llm_judge_call(system_prompt="sys", user_prompt="usr")
    (_, used_config), _ = mock_complete.call_args
    assert used_config.model == "openai/gpt-5.5"


def test_out_of_range_score_is_rejected_not_clamped():
    # 8 looks like "8/10" to a human, but we never guess the model's intended scale.
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "great", "score": 8}'):
        with pytest.raises(JudgeParseError):
            _call()


def test_non_numeric_score_is_rejected():
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "great", "score": "high"}'):
        with pytest.raises(JudgeParseError):
            _call()


def test_out_of_range_score_rejected_even_on_custom_schema_without_its_own_constraint():
    class _UnconstrainedVerdict(BaseModel):
        model_config = {"extra": "forbid"}
        reasoning: str
        score: float  # no Field(ge=0, le=1) -- the generic runtime check must still catch this

    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 42.0}'):
        with pytest.raises(JudgeParseError):
            _call(output_schema=_UnconstrainedVerdict)


def test_user_prompt_accepts_multimodal_content_blocks():
    # Level 2 (file-similarity judge) passes a list of content blocks (text + document/image)
    # instead of a plain string -- llm_judge_call must forward it to complete() unmodified.
    blocks = [
        {"type": "text", "text": "Compare these two files for functional equivalence."},
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "…"}},
    ]
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "same content", "score": 1.0}') as mock_complete:
        verdict = llm_judge_call(system_prompt="sys", user_prompt=blocks, config=_CFG)
    assert verdict.score == 1.0
    (messages, _), _ = mock_complete.call_args
    assert messages[-1]["content"] == blocks


# --- _describe_output_schema --------------------------------------------------------------------
# Regression coverage for a real failure: a model call once omitted a required field (`reasoning`)
# because the prompt never named the schema's keys, only said "matching the requested schema".
# `llm_judge_call` now appends an explicit, generated key list to `system_prompt` so this can't
# happen silently again for ANY schema, not just the one that broke.

class _DummySchema(BaseModel):
    """DOCSTRING_SENTINEL_TEXT -- a distinctive marker that must never reach the model."""
    model_config = {"extra": "forbid"}
    a: str
    b: int | None = None
    c: list[str] = Field(default_factory=list)
    d: str = Field(description="a hint for the model")


def test_describe_output_schema_lists_required_and_optional_fields_with_types():
    desc = _describe_output_schema(_DummySchema)
    assert '"a" (string, required)' in desc
    assert '"b" (integer, optional)' in desc
    assert '"c" (array of string, optional)' in desc
    assert '"d" (string, required) -- a hint for the model' in desc


def test_describe_output_schema_never_includes_the_class_docstring():
    assert "DOCSTRING_SENTINEL_TEXT" not in _describe_output_schema(_DummySchema)


def test_describe_output_schema_for_judge_verdict():
    desc = _describe_output_schema(JudgeVerdict)
    assert '"reasoning" (string, required)' in desc
    assert '"score" (number, required)' in desc


def test_llm_judge_call_appends_schema_description_to_system_prompt():
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"reasoning": "x", "score": 1.0}') as mock_complete:
        _call()
    (messages, _), _ = mock_complete.call_args
    system_content = messages[0]["content"]
    assert messages[0]["role"] == "system"
    assert "you are a judge" in system_content          # the caller's own prompt text, preserved
    assert '"reasoning" (string, required)' in system_content
    assert '"score" (number, required)' in system_content


def test_llm_judge_call_schema_description_reflects_a_custom_output_schema():
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value='{"a": "x", "d": "y"}') as mock_complete:
        llm_judge_call(system_prompt="sys", user_prompt="usr",
                       output_schema=_DummySchema, config=_CFG)
    (messages, _), _ = mock_complete.call_args
    system_content = messages[0]["content"]
    assert '"a" (string, required)' in system_content
    assert '"c" (array of string, optional)' in system_content
    assert "DOCSTRING_SENTINEL_TEXT" not in system_content


# --- last-resort field salvage -------------------------------------------------------------------
#
# A judge that writes its reasoning as prose and then closes with `..., "score": 0.9}` produces no
# parseable object -- the opening brace never arrives. That was 5 of 60 samples in one real
# order_to_cash run, each a silent 0.0 for a metric whose judgment was fine.
#
# Leniency here is deliberately the opposite of the strictness the ARTIFACT is held to: the artifact's
# response shape is the thing being measured, so repairing it would erase the signal; the judge is the
# measuring device, whose formatting carries no signal. The salvage may only read back a value the
# model actually wrote -- never supply one.

@pytest.mark.parametrize("raw,expected", [
    # prose, then the tail of a JSON object whose opening brace never arrived (the observed shape)
    ('The reason names the exact discrepancy.", "score": 0.98}', 0.98),
    # YAML-ish, unquoted keys on their own lines
    ("reasoning: (1) the flag matched. (2) amounts are exact.\n\nscore: 0.97", 0.97),
    # XML-ish tags
    ("<reasoning>\n(1) the flag matched.\n</reasoning>\n<score>0.85</score>", 0.85),
    # a quoted key without the object wrapper
    ('"reasoning": Let me evaluate step by step. ... "score": 0.75', 0.75),
])
def test_parse_structured_salvages_a_score_the_model_actually_wrote(raw, expected):
    assert _parse_structured(raw, JudgeVerdict).score == expected


@pytest.mark.parametrize("raw", [
    "reasoning: it was ok\n\nscore: high",             # no numeric score at all
    "The reason warrants a score: 0.9 in my view.",     # a number in PROSE, not a line-leading pair
    "reasoning: the flag matched but no number given",  # nothing to read
])
def test_parse_structured_never_invents_a_score(raw):
    # The salvage must not manufacture a verdict from output that does not state one. A prose mention
    # is not a stated score: the unquoted form is anchored to a line of its own for exactly this case.
    with pytest.raises(JudgeParseError):
        _parse_structured(raw, JudgeVerdict)


@pytest.mark.parametrize("raw", ["reasoning: great\n\nscore: 9.5", "reasoning: bad\n\nscore: -0.2"])
def test_parse_structured_still_rejects_an_out_of_range_salvaged_score(raw):
    # Salvaging does not weaken the range rule: we never clamp or rescale a judge's number, since
    # guessing its intended scale would be a silent, unverifiable assumption.
    with pytest.raises(JudgeParseError):
        _parse_structured(raw, JudgeVerdict)


def test_salvage_requires_every_field_the_schema_declares_required():
    class TwoRequired(BaseModel):
        model_config = {"extra": "forbid"}
        reasoning: str
        score: float
        confidence: float          # required, and absent from the output below
    with pytest.raises(JudgeParseError):
        _parse_structured("reasoning: ok\n\nscore: 0.5", TwoRequired)


# --- nested-object fields ------------------------------------------------------------------------
# Regression coverage for a measured failure. A nested field used to be described to the model as the
# bare word "value", so the model was asked for an object whose shape it had never been shown. It
# guessed the tool's NAME as a bare string -- `"call_user_tool": "call_discoverable_user_tool"` -- in
# 125 of 125 observed failures, every one of which failed validation. Because a user-simulator turn
# that will not parse aborts the case, that cost 25 of 97 cases in one real banking run, scored as
# zeros across all five objectives for a formatting slip by our own instrument.

class _NestedCall(BaseModel):
    model_config = {"extra": "forbid"}
    name: str
    arguments: dict = Field(default_factory=dict)


class _SchemaWithNested(BaseModel):
    model_config = {"extra": "forbid"}
    reasoning: str
    stopped: bool
    call_user_tool: _NestedCall | None = None


def test_describe_output_schema_renders_a_nested_objects_own_keys():
    desc = _describe_output_schema(_SchemaWithNested)
    assert '"name": string (required)' in desc
    assert '"arguments": object (optional)' in desc


def test_describe_output_schema_never_calls_a_nested_field_value():
    # "value" is the renderer's give-up token; reaching it for a shape we CAN describe is the bug.
    line = [ln for ln in _describe_output_schema(_SchemaWithNested).splitlines()
            if '"call_user_tool"' in ln][0]
    assert "value" not in line


def test_describe_output_schema_unchanged_for_flat_schemas():
    # The fix must not perturb the prompt every judge already gets, or it would move scores.
    desc = _describe_output_schema(JudgeVerdict)
    assert desc == ('Respond with a single JSON object with EXACTLY these keys and no others:\n'
                    '  "reasoning" (string, required)\n'
                    '  "score" (number, required)')


def test_nested_object_shape_is_actually_parseable_when_the_model_follows_it():
    # End-to-end: what the description asks for must be what the schema accepts.
    raw = ('{"reasoning": "acting now", "stopped": true, '
           '"call_user_tool": {"name": "apply_for_credit_card", "arguments": {"card_type": "Gold"}}}')
    parsed = _parse_structured(raw, _SchemaWithNested)
    assert parsed.call_user_tool.name == "apply_for_credit_card"
    assert parsed.call_user_tool.arguments == {"card_type": "Gold"}


def test_the_shape_the_model_used_to_guess_still_fails_loudly():
    # A bare tool name is not silently coerced into an object -- it was never a valid call.
    raw = '{"reasoning": "r", "stopped": true, "call_user_tool": "apply_for_credit_card"}'
    with pytest.raises(JudgeParseError):
        _parse_structured(raw, _SchemaWithNested)


def test_salvage_refuses_rather_than_dropping_a_nested_field_the_model_wrote():
    # The salvage can only read scalars, so a well-formed nested call inside malformed surroundings
    # cannot be recovered. Omitting it would VALIDATE (the field is optional) and the action would
    # vanish with no error and no log -- worse than a parse failure, because a lost customer action
    # is indistinguishable from one never attempted. Refusing keeps the failure visible.
    raw = ('here is my turn, sorry about the format\n'
           '"reasoning": "the agent told me to do it myself", "stopped": true, '
           '"call_user_tool": {"name": "submit_referral", "arguments": {"user_id": "x"}}')
    assert _salvage_fields(raw, _SchemaWithNested) is None


def test_salvage_still_rescues_a_scalar_only_schema():
    # The prose-then-trailing-score rescue this function exists for must keep working.
    out = _salvage_fields('the answer is fine, "score": 0.9, "reasoning": "ok"', JudgeVerdict)
    assert out is not None
    assert '0.9' in out


# --- re-sampling an unparseable judgment ----------------------------------------------------------
#
# Without this, ONE unreadable response scores that case 0 exactly like a wrong answer. Measured across
# the eval reports on disk it happens to 0.4%-3% of cases, which is a downward bias on every score in
# the benchmark for a reason that has nothing to do with what is being judged.
#
# The elicitation judge already re-samples (`eval/elicitation.py`) and its own comment says why: an
# unreadable verdict "needs a fresh sample". These tests hold that behaviour for the task-objective
# judges, and pin the two properties that make it safe rather than merely well-intentioned -- no added
# delay, and no retrying of anything that a fresh sample cannot fix.

_GOOD = '{"reasoning": "looks correct", "score": 0.9}'
_UNPARSEABLE = "not json at all, sorry"


def test_an_unparseable_response_is_re_sampled_and_the_second_attempt_is_used():
    with patch("appsci_bench.eval.helpers.llm.complete",
               side_effect=[_UNPARSEABLE, _GOOD]) as complete_mock:
        verdict = _call()

    assert verdict.score == 0.9
    assert complete_mock.call_count == 2


def test_recovery_on_the_final_allowed_attempt_still_counts():
    responses = [_UNPARSEABLE] * (llm_mod.JUDGE_PARSE_ATTEMPTS - 1) + [_GOOD]
    with patch("appsci_bench.eval.helpers.llm.complete", side_effect=responses) as complete_mock:
        verdict = _call()

    assert verdict.score == 0.9
    assert complete_mock.call_count == llm_mod.JUDGE_PARSE_ATTEMPTS


def test_a_clean_response_costs_exactly_one_call():
    """The 97-98% path. A retry that spent calls it did not need would be a worse trade than the bug."""
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=_GOOD) as complete_mock:
        _call()

    assert complete_mock.call_count == 1


def test_persistent_failure_raises_after_exactly_the_allowed_attempts():
    with (
        patch("appsci_bench.eval.helpers.llm.complete", return_value=_UNPARSEABLE) as complete_mock,
        pytest.raises(JudgeParseError) as excinfo,
    ):
        _call()

    assert complete_mock.call_count == llm_mod.JUDGE_PARSE_ATTEMPTS
    # The count belongs in the message: it lands in the eval report, and a reader needs to know this
    # was not a one-off flake before treating the resulting 0 as anything.
    assert f"all {llm_mod.JUDGE_PARSE_ATTEMPTS} attempts" in str(excinfo.value)


def test_the_failure_is_still_a_judge_parse_error_so_every_caller_keeps_working():
    """Each task's eval file catches JudgeParseError by name to degrade an objective to None.

    Wrapping the final failure in a different type would silently turn a degraded objective into an
    uncaught exception across every task at once.
    """
    with (
        patch("appsci_bench.eval.helpers.llm.complete", return_value=_UNPARSEABLE),
        pytest.raises(JudgeParseError) as excinfo,
    ):
        _call()

    # The original failure is preserved as the cause, so the specific parse complaint is not lost.
    assert isinstance(excinfo.value.__cause__, JudgeParseError)
    assert "JudgeParseError" in repr(excinfo.value)


def test_no_delay_is_introduced_between_attempts():
    """This is the property that keeps the retry safe, and it is why it differs from elicitation's.

    Every (data point, objective) pair is bounded by a per-eval-function ceiling: 180s on the
    orchestrator paths, and DEFAULT_EVAL_TIMEOUT_S = 60s for a direct caller that leaves the module
    default alone. The elicitation judge waits 2s then 6s between attempts, which is right for a
    rate-limit burst but would spend a seventh of the tighter budget on sleeping -- converting a
    recoverable parse failure into a timeout, which is a worse outcome rather than a better one.
    """
    with (
        patch("appsci_bench.eval.helpers.llm.complete", return_value=_UNPARSEABLE),
        patch("time.sleep") as sleep_mock,
        pytest.raises(JudgeParseError),
    ):
        _call()

    sleep_mock.assert_not_called()


def test_the_per_objective_timeout_still_bounds_all_attempts_together():
    """Three attempts must fit inside one scoring's budget, not each get their own."""
    from appsci_bench.eval.scoring import DEFAULT_EVAL_TIMEOUT_S

    # Not an assertion about the retry so much as a guard on the pair of constants: if the attempt
    # count ever grows enough that three slow judge calls cannot plausibly fit in the budget, the two
    # need re-tuning together rather than one silently defeating the other.
    assert llm_mod.JUDGE_PARSE_ATTEMPTS * 15 <= DEFAULT_EVAL_TIMEOUT_S


# --- what must NOT be retried ---------------------------------------------------------------------


def test_a_transport_failure_is_not_re_sampled():
    """`complete()` owns transport retries, with backoff. Re-sampling here would multiply them."""
    with (
        patch("appsci_bench.eval.helpers.llm.complete",
              side_effect=RuntimeError("connection reset")) as complete_mock,
        pytest.raises(RuntimeError, match="connection reset"),
    ):
        _call()

    assert complete_mock.call_count == 1


def test_a_transport_failure_on_a_later_attempt_propagates_as_itself():
    """It must not be reported as a parse failure -- the two need different reading."""
    with (
        patch("appsci_bench.eval.helpers.llm.complete",
              side_effect=[_UNPARSEABLE, RuntimeError("gateway 503")]) as complete_mock,
        pytest.raises(RuntimeError, match="gateway 503"),
    ):
        _call()

    assert complete_mock.call_count == 2


def test_a_model_config_error_is_not_re_sampled():
    """A misconfigured model will not fix itself, and every case would pay three calls to learn that."""
    from appsci_bench.common.model_config import ModelConfigError

    with (
        patch("appsci_bench.eval.helpers.llm.complete",
              side_effect=ModelConfigError("JUDGE_MODEL is unset")) as complete_mock,
        pytest.raises(ModelConfigError),
    ):
        _call()

    assert complete_mock.call_count == 1


# --- which parse failures count as re-samplable -----------------------------------------------------


def test_valid_json_of_the_wrong_shape_is_re_sampled():
    """A dropped required field is the same class of failure as unreadable text."""
    with patch("appsci_bench.eval.helpers.llm.complete",
               side_effect=['{"reasoning": "no score field"}', _GOOD]) as complete_mock:
        verdict = _call()

    assert verdict.score == 0.9
    assert complete_mock.call_count == 2


def test_an_out_of_range_score_is_re_sampled():
    """Treated as a parse failure rather than clamped, so it should get the same second chance."""
    with patch("appsci_bench.eval.helpers.llm.complete",
               side_effect=['{"reasoning": "x", "score": 7}', _GOOD]) as complete_mock:
        verdict = _call()

    assert verdict.score == 0.9
    assert complete_mock.call_count == 2


# --- the retry must not change the question ---------------------------------------------------------


def test_every_attempt_sends_the_same_messages():
    """A fresh sample of the SAME question.

    Nudging the prompt between attempts ("return ONLY json") would make the attempts samples of two
    different questions, so a judgment that succeeded on attempt two would not be comparable to one
    that succeeded on attempt one.
    """
    with patch("appsci_bench.eval.helpers.llm.complete",
               side_effect=[_UNPARSEABLE, _UNPARSEABLE, _GOOD]) as complete_mock:
        _call()

    messages = [call.args[0] for call in complete_mock.call_args_list]
    assert len(messages) == 3
    assert messages[0] == messages[1] == messages[2]


def test_observability_labels_survive_a_retry():
    """Retried calls must stay filed under the same source, not silently fall back to the default."""
    with patch("appsci_bench.eval.helpers.llm.complete",
               side_effect=[_UNPARSEABLE, _GOOD]) as complete_mock:
        _call(caller="reason_quality", source="evaluator")

    for call in complete_mock.call_args_list:
        assert call.kwargs["caller"] == "reason_quality"
        assert call.kwargs["source"] == "evaluator"
        assert call.kwargs["json_object"] is True


def test_a_custom_schema_round_trips_through_a_retry():
    class Extra(BaseModel):
        reasoning: str
        score: float = Field(ge=0.0, le=1.0)
        label: str

    good = '{"reasoning": "ok", "score": 0.5, "label": "partial"}'
    with patch("appsci_bench.eval.helpers.llm.complete", side_effect=[_UNPARSEABLE, good]):
        verdict = _call(output_schema=Extra)

    assert verdict.label == "partial"


def test_the_attempt_count_is_honoured_when_changed(monkeypatch):
    """One attempt means no retry -- the knob has to actually be the knob."""
    monkeypatch.setattr(llm_mod, "JUDGE_PARSE_ATTEMPTS", 1)
    with (
        patch("appsci_bench.eval.helpers.llm.complete", return_value=_UNPARSEABLE) as complete_mock,
        pytest.raises(JudgeParseError),
    ):
        _call()

    assert complete_mock.call_count == 1
