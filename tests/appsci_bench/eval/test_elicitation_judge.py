"""The per-item elicitation judge: message assembly, cache-prefix stability, verdict parsing.

The model is mocked throughout -- these tests are about what we send and what we accept back.

The message-level assertions are the ones that matter most. The caching design says the conversation
is identical across every item's call and therefore cacheable; nothing in the code enforces that, and
a drift would not fail, it would just quietly cost several times more. So the stability of the system
message and the first user block is asserted mechanically, item against item, rather than reasoned
about.

The parsing tests pin where the judge is strict and where it is not, which is not uniform: the two
booleans decide the verdict and cannot be defaulted, while the quoted evidence is audit metadata. See
the comments on each -- one of them records a real limit of the shared judge helper.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from appsci_bench.common.model_config import ModelConfig
from appsci_bench.eval.build_transcript import BuildTranscript, Exchange, render_for_judge
from appsci_bench.eval.elicitation import (
    JUDGE_CALLER,
    ElicitationVerdict,
    cache_usage,
    item_block,
    judge_item,
)
from appsci_bench.eval.elicitation_rubric import RubricItem
from appsci_bench.eval.helpers.llm import JudgeParseError

_CFG = ModelConfig(model="anthropic/claude-opus-4-8", api_key="no-token")

_ITEM = RubricItem(
    item_id="fault_taxonomy",
    fact="The six fault_type values, spelled exactly: PRICE_DELTA, QTY_MISMATCH, TOTAL_MATH.",
    objectives=("classification_correctness",),
    judge_hint="all six values, spelled as written; a paraphrase is not this item")
_OTHER_ITEM = RubricItem(
    item_id="cancellation_window",
    fact="All four cancellation eligibility conditions, including the 24-hour window.",
    objectives=("coverage",))

_TRANSCRIPT = BuildTranscript(
    exchanges=(Exchange(turn=1, question="Which fault types matter?",
                        answer="PRICE_DELTA, QTY_MISMATCH and TOTAL_MATH."),),
    source="runs/demo", status="ok")
_TEXT = render_for_judge(_TRANSCRIPT)

_GOOD = ('{"reasoning": "the stakeholder listed them", "asked": true, "elicited": true, '
         '"evidence": "PRICE_DELTA, QTY_MISMATCH and TOTAL_MATH."}')


def _call(item=_ITEM, raw=_GOOD, text=_TEXT):
    """Judge one item against a mocked model; returns (verdict, captured call kwargs)."""
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=raw) as mock:
        verdict = judge_item(item, text, config=_CFG)
    return verdict, mock.call_args


# --- what we send ----------------------------------------------------------------------------

def test_one_system_message_and_one_two_block_user_message():
    _, call = _call()
    messages = call.args[0]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert isinstance(messages[0]["content"], str)
    blocks = messages[1]["content"]
    assert isinstance(blocks, list) and len(blocks) == 2
    assert all(b["type"] == "text" for b in blocks)


def test_cache_control_marks_the_transcript_block_only():
    # The boundary goes after the conversation, so the conversation is the shared prefix and the item
    # is what varies. Marking the second block too would cache a per-item suffix -- pointless writes.
    _, call = _call()
    first, second = call.args[0][1]["content"]
    assert first["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in second


def test_the_first_user_block_is_the_rendered_transcript_byte_for_byte():
    _, call = _call()
    assert call.args[0][1]["content"][0]["text"] == _TEXT


def test_the_second_user_block_carries_the_fact_and_the_hint():
    _, call = _call()
    block = call.args[0][1]["content"][1]["text"]
    assert _ITEM.fact in block
    assert _ITEM.judge_hint in block


def test_the_item_block_carries_nothing_that_could_vary_per_run_or_position():
    # Anything positional or run-specific here would be harmless to correctness and expensive: it is
    # the reason the block is built by hand rather than dumped from the item.
    block = item_block(_ITEM)
    assert _ITEM.item_id not in block
    for leak in ("1", "2", "item 1", "of 2", "runs/", "demo"):
        assert leak not in block, f"item block carries {leak!r}"
    assert "objectives" not in block and "classification_correctness" not in block


def test_the_item_block_omits_the_hint_section_when_there_is_no_hint():
    assert "WHAT COUNTS" not in item_block(_OTHER_ITEM)
    assert _OTHER_ITEM.fact in item_block(_OTHER_ITEM)


def test_the_cached_prefix_is_identical_across_two_different_items():
    # THE test that protects the caching design. Same conversation, different items: everything before
    # the boundary must match byte for byte, or every call pays full price with no error to notice.
    _, first = _call(_ITEM)
    _, second = _call(_OTHER_ITEM)
    assert first.args[0][0] == second.args[0][0]                        # system message
    assert first.args[0][1]["content"][0] == second.args[0][1]["content"][0]  # transcript block
    assert first.args[0][1]["content"][1] != second.args[0][1]["content"][1]  # the item did change


def test_the_item_block_is_stable_across_repeated_renders():
    assert item_block(_ITEM) == item_block(_ITEM)


def test_calls_are_labelled_so_the_usage_rollup_can_separate_them():
    _, call = _call()
    assert call.kwargs["caller"] == JUDGE_CALLER


def test_the_system_prompt_states_the_rules_the_schema_cannot():
    # Four rules live only in the prompt: asked=false implies elicited=false, judge against the stated
    # fact, a broad question still counts, and an item is all-or-nothing. Assert they are present at
    # all, not their wording -- prose can be rewritten, the rules cannot be dropped.
    _, call = _call()
    prompt = call.args[0][0]["content"].lower()
    assert "cannot have been conveyed" in prompt          # asked=false implies elicited=false
    assert "confident and wrong" in prompt                # judged against the truth
    assert "broad" in prompt                              # a sweeping question still counts
    assert "all of them have to have come back" in prompt  # all-or-nothing
    assert "never your own knowledge" in prompt           # no outside inference


def test_the_output_key_list_is_appended_by_the_helper_not_by_us():
    # The helper generates the key list from the schema; re-stating the keys in our own prompt would
    # give them a second chance to drift out of sync.
    _, call = _call()
    system = call.args[0][0]["content"]
    assert "EXACTLY these keys" in system
    for key in ("reasoning", "asked", "elicited", "evidence"):
        assert f'"{key}"' in system


# --- what we accept back ---------------------------------------------------------------------

def test_a_clean_verdict_parses():
    verdict, _ = _call()
    assert isinstance(verdict, ElicitationVerdict)
    assert (verdict.asked, verdict.elicited) == (True, True)
    assert verdict.evidence.startswith("PRICE_DELTA")


def test_a_missed_item_parses_with_empty_evidence():
    raw = '{"reasoning": "never came up", "asked": false, "elicited": false, "evidence": ""}'
    verdict, _ = _call(raw=raw)
    assert (verdict.asked, verdict.elicited, verdict.evidence) == (False, False, "")


def test_asked_but_not_elicited_parses():
    raw = ('{"reasoning": "the stakeholder declined", "asked": true, "elicited": false, '
           '"evidence": "sim > I would have to check with finance."}')
    verdict, _ = _call(raw=raw)
    assert (verdict.asked, verdict.elicited) == (True, False)


def test_a_verdict_schema_with_no_score_field_parses():
    # This judge answers yes/no twice, so its schema has no `score`. The shared helper range-checks a
    # score only when one is declared -- locked here because the whole design rests on it.
    verdict, _ = _call()
    assert not hasattr(verdict, "score")


@pytest.mark.parametrize("raw", [
    '{"reasoning": "r", "elicited": true, "evidence": "e"}',   # no asked
    '{"reasoning": "r", "asked": true, "evidence": "e"}',      # no elicited
    'The engineer never asked about this at all.',             # not JSON, no fields
    '',
])
def test_a_verdict_missing_a_decision_field_raises(raw):
    # The two booleans ARE the verdict. Nothing may default them: a dropped field has to surface as an
    # error the scorer counts, not as a quiet "missed".
    with pytest.raises(JudgeParseError):
        _call(raw=raw)


def test_a_contradictory_verdict_raises():
    raw = '{"reasoning": "found it", "asked": false, "elicited": true, "evidence": "x"}'
    with pytest.raises(JudgeParseError, match="contradictory"):
        _call(raw=raw)


def test_prose_around_the_json_is_tolerated():
    raw = ('Here is my verdict:\n```json\n' + _GOOD + '\n```\nHappy to expand.')
    verdict, _ = _call(raw=raw)
    assert verdict.elicited is True


def test_a_dropped_evidence_field_is_recovered_rather_than_rejected():
    """Records a real limit of the shared judge helper rather than a decision of this module.

    That helper's last-resort salvage fills a missing required free-text field with the model's whole
    raw output, so a verdict that omits its quote comes back with a blob in place of one instead of
    raising. The two booleans are unaffected -- they cannot be salvaged from prose -- so the verdict
    itself is still the model's own, and the damage is confined to the audit column, where a JSON blob
    is conspicuous. Not worked around here: the fix belongs in the helper, where the salvage path could
    be gated on no object having been parsed at all.
    """
    raw = '{"reasoning": "the stakeholder listed them", "asked": true, "elicited": true}'
    verdict, _ = _call(raw=raw)
    assert (verdict.asked, verdict.elicited) == (True, True)
    assert verdict.evidence.startswith("{")  # the blob, not a quote


def test_an_extra_field_is_dropped_by_the_helper_rather_than_rejected():
    """Same helper limit, other direction: `extra="forbid"` is defeated for a schema whose fields are
    all scalars, because the salvage rebuilds the object from the declared keys only. Pinned so the
    behaviour is visible rather than assumed to be strict."""
    raw = ('{"reasoning": "r", "asked": true, "elicited": true, "evidence": "e", '
           '"confidence": 0.9}')
    verdict, _ = _call(raw=raw)
    assert verdict.elicited is True
    assert not hasattr(verdict, "confidence")


# --- the cache-usage helper (read by the manual passthrough gate) ----------------------------

def test_cache_usage_reads_both_anthropic_counts():
    usage = {"input_tokens": 12, "cache_creation_input_tokens": 4096, "cache_read_input_tokens": 4096}
    assert cache_usage(usage) == (4096, 4096)


def test_cache_usage_returns_zeros_when_the_provider_says_nothing():
    # A short conversation falls below the minimum cacheable length and will not cache. Normal, not an
    # error -- so this reports zero rather than raising, and the gate reads it as "no caching happened".
    assert cache_usage({"prompt_tokens": 900, "completion_tokens": 40}) == (0, 0)


@pytest.mark.parametrize("usage", [None, {}, "not a dict", 7, []])
def test_cache_usage_tolerates_a_missing_or_odd_usage_payload(usage):
    assert cache_usage(usage) == (0, 0)


@pytest.mark.parametrize("value", [None, -1, "many", True, False])
def test_cache_usage_treats_an_unusable_count_as_zero(value):
    assert cache_usage({"cache_read_input_tokens": value}) == (0, 0)


def test_cache_usage_falls_back_to_the_openai_shaped_read_count():
    # The gateway may normalise to the OpenAI shape, which reports the read count nested and no write
    # count at all. Missing that would read as "caching is not working" when it is.
    assert cache_usage({"prompt_tokens_details": {"cached_tokens": 3072}}) == (0, 3072)


def test_a_top_level_read_count_wins_over_the_nested_one():
    usage = {"cache_read_input_tokens": 4096, "prompt_tokens_details": {"cached_tokens": 0}}
    assert cache_usage(usage) == (0, 4096)


def test_the_judge_tags_its_own_model_call_source():
    """The usage rollup groups by model and by SOURCE, and a source is the filename of the shard a record
    lands in -- nothing aggregates by caller. Left on the evaluator's default, these calls would merge
    with the artifact judges in every cost report, which is the defect the eval-time artifact sim already
    had to fix the same way."""
    from shared.observability.events import Source

    _, call = _call()
    assert call.kwargs["source"] == Source.ELICITATION_JUDGE
    assert str(Source.ELICITATION_JUDGE) == "elicitation_judge"


def test_the_judges_calls_land_in_their_own_shard(tmp_path, monkeypatch):
    """Asserted against the real store rather than the kwarg, because the shard FILENAME is the thing the
    rollup reads -- passing the argument and landing in the right file are two different claims."""
    import shared.observability.publisher as pub
    from shared.observability.events import Source

    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OBSERVABILITY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("OBSERVABILITY_TRACE_ID", "tr")
    monkeypatch.setenv("OBSERVABILITY_TASK", "demo")
    try:
        pub.init_publisher(source=Source.EVALUATOR)   # the judge runs under the evaluator's publisher
        with patch("appsci_bench.common.model_config.litellm.completion") as completion:
            completion.return_value = _fake_completion(_GOOD)
            judge_item(_ITEM, _TEXT, config=_CFG)
    finally:
        p = pub.get_publisher()
        if p is not None:
            p.close()
        pub._publisher = None

    own = tmp_path / "tr" / "model_calls-elicitation_judge.jsonl"
    assert own.is_file(), sorted(x.name for x in (tmp_path / "tr").iterdir())
    # And NOT mixed into the evaluator's shard, where the artifact judges live.
    evaluator_shard = tmp_path / "tr" / "model_calls-evaluator.jsonl"
    assert not evaluator_shard.exists() or "elicitation" not in evaluator_shard.read_text()


def _fake_completion(content: str):
    """A LiteLLM-shaped response, so the real transport path runs and the store sees a real record."""
    import types

    usage = types.SimpleNamespace(model_dump=lambda: {"prompt_tokens": 10, "completion_tokens": 2})
    message = types.SimpleNamespace(content=content)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=usage)
