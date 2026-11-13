"""Unit tests for the scoring step (eval-fn harness, validation, failures, compatibility)."""
from __future__ import annotations

import pytest

from tests.fixtures.eval_task import eval_fns
from appsci_bench.eval.dataset import parse_dataset
from appsci_bench.eval.scoring import check_compatibility, score_records
from appsci_bench.eval.datatypes import (
    ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn, DataPointMetricResult,
    GenerationRecord, Objective,
)


def _obj(name, fn, positive=True, weight=1.0):
    return Objective(name=name, eval_function_path=f"x.{name}", positive=positive, weight=weight,
                     eval_function=fn)


def _dataset(expected_text=None):
    sample = {"data_point_id": "d1", "single_turn_input": {"text": "q"}}
    if expected_text is not None:
        sample["expected_output"] = {"text": expected_text}
    return parse_dataset({"type": "single_turn"}, [sample])


def _record(resp_text="done", error=None, final_state=None):
    turn = ConversationTurn(ArtifactAgentQuery(text="q"), ArtifactAgentOutput(text=resp_text))
    return GenerationRecord("d1", 0, conversation=[turn], error=error, final_state=final_state)


def test_always_one_scores_one():
    res = score_records([_record()], _dataset(), [_obj("m", eval_fns.always_one)])
    assert res[0].score == 1.0 and res[0].error is None


def test_final_state_is_forwarded_to_eval_functions():
    rec = _record(final_state={"env_state_hash": "abc123"})
    res = score_records([rec], _dataset(), [_obj("m", eval_fns.echo_final_state)])
    assert res[0].score == 1.0
    assert res[0].metadata == {"final_state": {"env_state_hash": "abc123"}}


def test_none_score_is_accepted_not_treated_as_a_failure():
    res = score_records([_record()], _dataset(), [_obj("m", eval_fns.returns_none)])
    assert res[0].score is None and res[0].error is None
    assert res[0].metadata == {"applicable": False}


def test_text_exact_match_uses_expected_output():
    objs = [_obj("m", eval_fns.text_exact_match)]
    hit = score_records([_record("yes")], _dataset(expected_text="yes"), objs)
    miss = score_records([_record("no")], _dataset(expected_text="yes"), objs)
    assert hit[0].score == 1.0 and miss[0].score == 0.0


def test_raising_fn_scored_zero_with_error():
    res = score_records([_record()], _dataset(), [_obj("m", eval_fns.raises)])
    assert res[0].score == 0.0 and "boom" in res[0].error


def test_out_of_range_scored_zero_with_error():
    res = score_records([_record()], _dataset(), [_obj("m", eval_fns.returns_out_of_range)])
    assert res[0].score == 0.0 and "out of range" in res[0].error


def test_bad_shape_and_bad_metadata_scored_zero():
    shape = score_records([_record()], _dataset(), [_obj("m", eval_fns.returns_bad_shape)])
    meta = score_records([_record()], _dataset(), [_obj("m", eval_fns.returns_bad_metadata)])
    assert shape[0].score == 0.0 and "score, metadata" in shape[0].error
    assert meta[0].score == 0.0 and "metadata must be a dict" in meta[0].error


def test_timeout_scored_zero_fast():
    import time
    res = score_records([_record()], _dataset(), [_obj("m", eval_fns.slow)], eval_timeout_s=0.3)
    assert res[0].score == 0.0 and "timed out" in res[0].error


def test_timeout_does_not_block_on_the_abandoned_call():
    # The abandoned worker sleeps 3s (eval_fns.very_slow) against a 0.3s timeout; score_records
    # must not wait for it -- _call_with_timeout's shutdown(wait=False) means the caller returns
    # near the timeout bound, not near the abandoned function's actual runtime.
    import time
    start = time.monotonic()
    res = score_records([_record()], _dataset(), [_obj("m", eval_fns.very_slow)], eval_timeout_s=0.3)
    elapsed = time.monotonic() - start
    assert res[0].score == 0.0 and "timed out" in res[0].error
    assert elapsed < 2.0  # well short of the fixture's 3s sleep


def test_generation_errored_record_short_circuits():
    res = score_records([_record(error="artifact crashed")], _dataset(),
                        [_obj("m", eval_fns.always_one)])
    assert res[0].score == 0.0 and "generation failed" in res[0].error


def test_unresolved_objective_scored_zero():
    obj = Objective(name="m", eval_function_path="x.m", positive=True, weight=1.0)  # eval_function=None
    res = score_records([_record()], _dataset(), [obj])
    assert res[0].score == 0.0 and "not resolved" in res[0].error


def test_task_pool_over_multiple_objectives_and_records_deterministic():
    recs = [_record(), GenerationRecord("d0", 0,
            conversation=[ConversationTurn(ArtifactAgentQuery(text="q"), ArtifactAgentOutput(text="x"))])]
    ds = parse_dataset({"type": "single_turn"},
                       [{"data_point_id": "d1", "single_turn_input": {"text": "q"}},
                        {"data_point_id": "d0", "single_turn_input": {"text": "q"}}])
    objs = [_obj("b", eval_fns.always_one), _obj("a", eval_fns.always_one)]
    res = score_records(recs, ds, objs)
    assert [(r.data_point_id, r.objective) for r in res] == [
        ("d0", "a"), ("d0", "b"), ("d1", "a"), ("d1", "b")]   # sorted by (dp, objective)


def test_eval_fn_called_by_keyword_so_param_order_is_not_locked():
    # The orchestrator MUST call eval fns by keyword. Proof: a keyword-only fn that declares its
    # params in a DIFFERENT order than the published contract still binds correctly and scores.
    # (If the call site passed positionally, a keyword-only signature would raise -> score 0.)
    def reordered(*, eval_rubric=None, conversation, expected_output=None, data_point_id, **kwargs):
        return (1.0 if data_point_id == "d1" and conversation else 0.0), {"dp": data_point_id}

    res = score_records([_record()], _dataset(), [_obj("m", reordered)])
    assert res[0].score == 1.0 and res[0].error is None
    assert res[0].metadata == {"dp": "d1"}


def test_check_compatibility_flags_missing_and_inconsistent_expected_output():
    objs = [_obj("m", eval_fns.always_one)]
    assert check_compatibility(_dataset(), objs)              # none have expected_output -> warn
    assert not check_compatibility(_dataset(expected_text="y"), objs)  # all present -> no warn
    mixed = parse_dataset({"type": "single_turn"}, [
        {"data_point_id": "a", "single_turn_input": {"text": "q"}, "expected_output": {"text": "y"}},
        {"data_point_id": "b", "single_turn_input": {"text": "q"}}])
    assert check_compatibility(mixed, objs)                    # inconsistent -> warn


def test_file_path_sourced_expected_output_file_is_materialized_before_the_eval_fn_sees_it(tmp_path):
    p = tmp_path / "ground_truth.txt"
    p.write_bytes(b"the real answer")
    sample = {"data_point_id": "d1", "single_turn_input": {"text": "q"},
             "expected_output": {"files": [{"file_type": "txt", "file_name": "ground_truth.txt",
                                            "file_path": str(p)}]}}
    ds = parse_dataset({"type": "single_turn"}, [sample])

    def reads_expected_file(*, expected_output, **kwargs):
        return (1.0 if expected_output.files[0].file_data == b"the real answer" else 0.0), {}

    res = score_records([_record()], ds, [_obj("m", reads_expected_file)])
    assert res[0].score == 1.0 and res[0].error is None


def test_case_context_reaches_eval_fn_through_timeout_pool():
    # score_records tags each (record, objective) with model_call_context(data_point_id=...); the
    # eval fn runs in _call_with_timeout's worker thread, so the tag must survive copy_context. This
    # is what lets judge calls made inside the eval fn be grouped under their case in the tracer.
    from shared.observability.model_io import _call_context

    def sees_case(*, data_point_id, **kwargs):
        ctx = _call_context.get()
        return (1.0 if ctx.get("data_point_id") == data_point_id else 0.0), {"seen": ctx}

    # A concrete timeout forces the copy_context path (timeout=None would call inline and pass trivially).
    res = score_records([_record()], _dataset(), [_obj("m", sees_case)], eval_timeout_s=30.0)
    assert res[0].score == 1.0 and res[0].error is None
    assert res[0].metadata["seen"]["data_point_id"] == "d1"
