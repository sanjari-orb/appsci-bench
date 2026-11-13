"""Unit tests for aggregation (inversion, error-forces-0, repeat avg, weighted views)."""
from __future__ import annotations

import pytest

from appsci_bench.eval.aggregate import aggregate
from appsci_bench.eval.datatypes import DataPointMetricResult, Objective


def _r(dp, obj, score, repeat=0, error=None):
    return DataPointMetricResult(dp, obj, repeat, score, {}, error)


def _obj(name, positive, weight):
    return Objective(name=name, eval_function_path=f"x.{name}", positive=positive, weight=weight)


def test_positive_false_inversion():
    rep = aggregate("t", [_r("d1", "m_neg", 0.25)], [_obj("m_neg", False, 1.0)])
    assert rep.views["matrix"]["d1"]["m_neg"] == pytest.approx(0.75)


def test_error_forces_zero_even_for_negative_metric():
    # raw 0 on a negative metric would invert to 1.0; an error must instead force 0.0 (worst).
    rep = aggregate("t", [_r("d1", "m_neg", 0.0, error="boom")], [_obj("m_neg", False, 1.0)])
    assert rep.views["matrix"]["d1"]["m_neg"] == 0.0
    assert rep.final_score == 0.0


def test_repeat_averaging():
    results = [_r("d1", "m", 1.0, repeat=0), _r("d1", "m", 0.0, repeat=1)]
    rep = aggregate("t", results, [_obj("m", True, 1.0)])
    assert rep.views["matrix"]["d1"]["m"] == pytest.approx(0.5)


def test_weighted_final_and_per_data_point():
    # m_pos: 0.8 (w1, +); m_neg: raw 0.25 -> eff 0.75 (w3, -). weighted = (0.8*1 + 0.75*3)/4 = 0.7625
    results = [_r("d1", "m_pos", 0.8), _r("d1", "m_neg", 0.25)]
    objs = [_obj("m_pos", True, 1.0), _obj("m_neg", False, 3.0)]
    rep = aggregate("t", results, objs)
    assert rep.final_score == pytest.approx(0.7625)
    assert rep.views["per_data_point"][0]["score"] == pytest.approx(0.7625)


def test_per_metric_avg_across_data_points():
    results = [_r("d1", "m", 1.0), _r("d2", "m", 0.0)]
    rep = aggregate("t", results, [_obj("m", True, 1.0)])
    pm = {m["objective"]: m["score"] for m in rep.views["per_metric"]}
    assert pm["m"] == pytest.approx(0.5)


def test_final_equals_mean_of_per_data_point():
    results = [_r("d1", "a", 1.0), _r("d1", "b", 0.0), _r("d2", "a", 0.5), _r("d2", "b", 1.0)]
    objs = [_obj("a", True, 2.0), _obj("b", True, 1.0)]
    rep = aggregate("t", results, objs)
    mean_dp = sum(d["score"] for d in rep.views["per_data_point"]) / 2
    assert rep.final_score == pytest.approx(mean_dp)


def test_deterministic_ordering_stable():
    results = [_r("d2", "b", 0.5), _r("d1", "a", 0.5), _r("d2", "a", 0.5), _r("d1", "b", 0.5)]
    objs = [_obj("a", True, 1.0), _obj("b", True, 1.0)]
    r1 = aggregate("t", results, objs)
    r2 = aggregate("t", list(reversed(results)), objs)
    assert [d["data_point_id"] for d in r1.views["per_data_point"]] == ["d1", "d2"]
    assert [m["objective"] for m in r1.views["per_metric"]] == ["a", "b"]
    assert r1.to_dict() == r2.to_dict()           # identical regardless of input order


def test_zero_weights_fall_back_to_simple_mean():
    results = [_r("d1", "a", 0.4), _r("d1", "b", 0.6)]
    objs = [_obj("a", True, 0.0), _obj("b", True, 0.0)]
    rep = aggregate("t", results, objs)
    assert rep.final_score == pytest.approx(0.5)


def test_empty_results():
    rep = aggregate("t", [], [])
    # nothing to aggregate -- None, not a fake 0.0 (there is no score to report).
    assert rep.final_score is None and rep.views["per_metric"] == []


# --- score=None ("not applicable"): excluded from aggregation, not scored/penalized -----------

def test_none_score_is_excluded_from_its_metric_average():
    results = [_r("d1", "m", None), _r("d2", "m", 1.0)]
    rep = aggregate("t", results, [_obj("m", True, 1.0)])
    assert rep.views["matrix"]["d1"] == {}                # excluded, not a 0.0 cell
    assert rep.views["per_metric"][0]["score"] == pytest.approx(1.0)   # averages only d2


def test_metric_fully_none_aggregates_to_none_and_is_excluded_from_final_score():
    results = [_r("d1", "always_none", None), _r("d1", "real", 0.6)]
    objs = [_obj("always_none", True, 5.0), _obj("real", True, 1.0)]
    rep = aggregate("t", results, objs)
    pm = {m["objective"]: m["score"] for m in rep.views["per_metric"]}
    assert pm["always_none"] is None
    # final_score reflects ONLY the real metric -- a fully-None metric contributes neither a
    # value nor its (large) weight, so it must not dilute/skew the result.
    assert rep.final_score == pytest.approx(0.6)


def test_metric_fully_none_across_dataset_emits_a_warning_suggesting_weight_zero():
    # a second objective (`other`) keeps every data point non-empty, isolating the per-metric
    # warning from the per-data-point one tested separately below.
    results = [_r("d1", "always_none", None), _r("d2", "always_none", None),
              _r("d1", "other", 0.5), _r("d2", "other", 0.5)]
    rep = aggregate("t", results, [_obj("always_none", True, 1.0), _obj("other", True, 1.0)])
    assert len(rep.warnings) == 1
    assert "always_none" in rep.warnings[0] and "weight: 0" in rep.warnings[0]


def test_partial_none_metric_produces_no_warning():
    # d1's `other` objective keeps d1 non-empty, isolating the per-metric check: `m` is None for
    # d1 but real for d2 -- partial, not fully-None, so no warning should fire for it.
    results = [_r("d1", "m", None), _r("d2", "m", 1.0), _r("d1", "other", 0.5)]
    rep = aggregate("t", results, [_obj("m", True, 1.0), _obj("other", True, 1.0)])
    assert rep.warnings == []


def test_data_point_fully_none_scores_none_not_zero_and_does_not_skew_others():
    results = [_r("d1", "m", None), _r("d2", "m", 0.8)]
    rep = aggregate("t", results, [_obj("m", True, 1.0)])
    pdp = {d["data_point_id"]: d["score"] for d in rep.views["per_data_point"]}
    assert pdp["d1"] is None
    assert pdp["d2"] == pytest.approx(0.8)
    assert any("d1" in w for w in rep.warnings)


def test_error_result_is_still_penalized_not_excluded_like_none():
    # an error forces the worst score (0.0); None excludes entirely. These must not be conflated.
    results = [_r("d1", "m", 0.0, error="boom")]
    rep = aggregate("t", results, [_obj("m", True, 1.0)])
    assert rep.views["per_metric"][0]["score"] == 0.0
    assert rep.warnings == []               # an error is not the "fully None" condition


# --- score neutrality of the elicitation diagnostic -------------------------------------------

def _elicitation(rate):
    from appsci_bench.eval.datatypes import ElicitationResult, ItemResult
    return ElicitationResult(
        task_id="t", status="scored", total_items=2, elicited_rate=rate,
        asked_not_elicited_rate=0.0, missed_rate=1 - rate, num_exchanges=3,
        items=[ItemResult(item_id="a", verdict="elicited"),
               ItemResult(item_id="b", verdict="missed")])


def test_aggregate_output_is_byte_identical_whether_or_not_elicitation_is_attached():
    """The structural guarantee: the diagnostic is attached AFTER aggregation, so it is not an input to
    any view. If someone ever routes it through aggregate(), this fails."""
    import json

    def score_part(rep) -> str:
        return json.dumps({k: v for k, v in rep.to_dict().items() if k != "elicitation"},
                          sort_keys=True)

    results = [_r("d1", "m", 0.6), _r("d2", "m", 0.4)]
    objectives = [_obj("m", True, 1.0)]

    plain = aggregate("t", results, objectives)
    attached = aggregate("t", results, objectives)
    attached.elicitation = _elicitation(0.0)

    assert score_part(plain) == score_part(attached)
    assert attached.views == plain.views
    assert plain.to_dict()["elicitation"] is None


@pytest.mark.parametrize("rate", [0.0, 0.5, 1.0])
def test_final_score_is_unchanged_by_any_elicitation_result(rate):
    # A weighted average of "did the artifact work" and "did the agent ask" would answer neither
    # question, so the two numbers must not touch -- including the 0.0 case, the tempting one to
    # let drag a score down.
    rep = aggregate("t", [_r("d1", "m", 0.8)], [_obj("m", True, 1.0)])
    baseline = rep.final_score
    rep.elicitation = _elicitation(rate)
    assert rep.final_score == baseline == pytest.approx(0.8)
    assert "elicitation" not in rep.views
    assert not any("elicit" in str(v).lower() for v in rep.views.values())


def test_the_diagnostic_appears_in_no_aggregation_view():
    rep = aggregate("t", [_r("d1", "m", 0.8)], [_obj("m", True, 1.0)])
    rep.elicitation = _elicitation(1.0)
    assert [pm["objective"] for pm in rep.views["per_metric"]] == ["m"]
    assert set(rep.views["matrix"]["d1"]) == {"m"}
