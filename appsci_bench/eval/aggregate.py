"""Step 3 of the evaluation: aggregate per-(data point, objective, repeat) scores.

Pipeline (design: Evaluation System, step 3):
  1. normalize each raw score to higher-is-better: positive objective -> score; negative -> 1 - score.
     A result with an `error` is forced to 0.0 (the worst contribution) regardless of direction --
     otherwise a negative-objective failure (raw 0) would invert to 1.0 and reward a crash. A
     `score=None` result (the metric doesn't apply to this data point -- design: Task-Specific
     Evaluation Function) is EXCLUDED here instead: it contributes nothing to any average, at any
     level, rather than being treated as a value. This is the opposite of an error on purpose --
     an error is a penalty (forced worst-case), a `None` is an exclusion (no opinion).
  2. average across repeats -> one cell per (data point, objective); a cell with zero non-None
     values simply doesn't exist in the matrix.
  3. build the four views: the matrix, per-data-point (weighted avg of metrics), per-metric
     (avg across data points), and the final task-wide score (weighted avg over metrics). Any of
     these can legitimately end up `None` too, if every contributing cell was excluded -- see
     `_weighted_mean`. When a metric is `None` across every data point, or a data point is `None`
     across every metric, a warning is logged and recorded on the report (not a failure).

All output is in deterministic order (data points by id, metrics by name); individual scores need
not be reproducible, but ordering must be.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from .datatypes import DataPointMetricResult, EvalReport, Objective

logger = logging.getLogger(__name__)

_WEIGHT_ZERO_HINT = (
    "If this is intentional -- the metric genuinely never applies to this task's data -- use "
    "weight: 0 on its objective in spec.yaml instead of relying on the eval function always "
    "returning None. The two have the same effect on the final score, but weight: 0 is explicit "
    "and discoverable by reading the spec; an always-None eval function looks identical to a bug."
)


def aggregate(task_id: str, results: list[DataPointMetricResult], objectives: list[Objective],
              *, run_id: str | None = None, reward_objective: str | None = None) -> EvalReport:
    """Run the 3-step pipeline described in the module docstring over `results`, returning the
    populated `EvalReport` (matrix + per-data-point/per-metric/final-score views, in deterministic
    order). `run_id` is stamped onto the report as-is; the caller owns generating it.

    `reward_objective` names the task's BINARY pass/fail objective, when it declares one
    (`evaluation_data.reward_objective`). It adds a `reward` view -- per data point, did this run pass
    -- which is what pass^k is computed from across repeated trials. Absent for tasks that have no
    such objective, and those tasks get no pass^k rather than a thresholded imitation of one."""
    obj_by_name = {o.name: o for o in objectives}

    # 1 + 2: normalize to higher-is-better (excluding None), then average across repeats into cells.
    acc: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in results:
        obj = obj_by_name.get(r.objective)
        positive = obj.positive if obj else True
        if r.error:
            effective = 0.0
        elif r.score is None:
            continue                                     # not applicable -- excluded, not scored
        else:
            effective = r.score if positive else 1.0 - r.score
        acc[(r.data_point_id, r.objective)].append(effective)

    data_points = sorted({r.data_point_id for r in results})
    metrics = sorted({r.objective for r in results})

    matrix: dict[str, dict[str, float]] = {}
    for dp in data_points:
        row: dict[str, float] = {}
        for m in metrics:
            vals = acc.get((dp, m))
            if vals:
                row[m] = sum(vals) / len(vals)
        matrix[dp] = row

    warnings: list[str] = []

    # 3a: per metric -- average the metric's cell across data points.
    per_metric = []
    for m in metrics:
        cells = [matrix[dp][m] for dp in data_points if m in matrix[dp]]
        obj = obj_by_name.get(m)
        if not cells:
            msg = (f"objective {m!r}: every data point returned score=None (excluded from "
                   f"aggregation) -- this metric contributes nothing to the final score. "
                   + _WEIGHT_ZERO_HINT)
            logger.warning(msg)
            warnings.append(msg)
        per_metric.append({
            "objective": m,
            "score": (sum(cells) / len(cells)) if cells else None,
            "weight": obj.weight if obj else 0.0,
            "positive": obj.positive if obj else True,
        })

    # 3b: per data point -- weighted average of its metric cells.
    per_data_point = []
    for dp in data_points:
        pairs = [(matrix[dp][m], (obj_by_name[m].weight if m in obj_by_name else 0.0))
                 for m in metrics if m in matrix[dp]]
        if not pairs:
            msg = (f"data point {dp!r}: every objective returned score=None (excluded from "
                   f"aggregation) -- no objective applied to this data point.")
            logger.warning(msg)
            warnings.append(msg)
        per_data_point.append({"data_point_id": dp, "score": _weighted_mean(pairs)})

    # 3c: final task-wide score -- weighted mean over metrics that have a score at all (a fully-None
    # metric contributes neither a value nor its weight, per the exclusion semantics above).
    final_score = _weighted_mean(
        [(pm["score"], pm["weight"]) for pm in per_metric if pm["score"] is not None])

    views = {"matrix": matrix, "per_data_point": per_data_point, "per_metric": per_metric}
    if reward_objective:
        reward_view, reward_warnings = _reward_view(reward_objective, results, data_points, obj_by_name)
        views["reward"] = reward_view
        warnings.extend(reward_warnings)
    ordered = sorted(results, key=lambda r: (r.data_point_id, r.objective, r.repeat_index))
    return EvalReport(task_id=task_id, results=ordered, views=views,
                      final_score=final_score, warnings=warnings, run_id=run_id)


def _reward_view(
    name: str,
    results: list[DataPointMetricResult],
    data_points: list[str],
    obj_by_name: dict[str, Objective],
) -> tuple[dict, list[str]]:
    """Per-data-point pass/fail for the task's declared binary reward objective.

    Reads the RAW per-repeat results, not the aggregated matrix, and that distinction is the whole
    correctness of the field. The matrix averages across `evaluation_data.repeat`, so on a task with
    `repeat: 2` a case that passed once and failed once arrives here as 0.5 -- which is not in {0, 1},
    so the binariness check would declare a genuinely binary objective non-binary, suppress pass^k
    entirely and warn that the task's reward is continuous. All three of those statements would be
    false, and the only visible symptom is a missing curve.

    Error and None semantics match the aggregation documented at the top of this module: an ERRORED
    result is a forced 0.0, i.e. a fail (an artifact that crashed on a case did not do the task), while
    a `None` is an exclusion, i.e. pass unknown (the metric never applied, which says nothing about the
    artifact). `pass_hat_k` excludes an unknown rather than counting it against the build.

    When a case's repeats DISAGREE, its outcome for this run is unknown rather than averaged: within one
    run there is no single answer to "did this case pass", and inventing one either way would feed a
    reliability metric a number that is not an outcome. Named in the warnings so it is visible rather
    than merely absent.

    `is_binary` is what keeps pass^k honest. `scoring_kind: deterministic` does NOT mean binary:
    order_to_cash's `flag_accuracy` and compliance_review's `violation_set_f1` are continuous, and
    pointing this field at one of those would produce a "reliability" curve that really tracks an
    invented cutoff. So the values are checked against {0.0, 1.0} and, when they are not, the view says
    so and carries no passes at all.
    """
    warnings: list[str] = []
    if name not in obj_by_name:
        msg = (f"evaluation_data.reward_objective names {name!r}, which is not one of this task's "
               f"objectives ({sorted(obj_by_name)}); no pass/fail can be derived")
        logger.warning(msg)
        return {"objective": name, "is_binary": False, "values": {}, "passes": {},
                "unavailable_reason": msg}, [msg]

    positive = obj_by_name[name].positive
    # Per data point, the effective score of EVERY repeat of this objective -- same normalisation the
    # matrix applies (direction flip, error forced to worst) but without collapsing the repeats.
    per_repeat: dict[str, list[float]] = {dp: [] for dp in data_points}
    for r in results:
        if r.objective != name or r.data_point_id not in per_repeat:
            continue
        if r.error:
            per_repeat[r.data_point_id].append(0.0)
        elif r.score is None:
            continue                                      # not applicable -- excluded, not scored
        else:
            per_repeat[r.data_point_id].append(r.score if positive else 1.0 - r.score)

    observed = [v for values in per_repeat.values() for v in values]
    is_binary = all(v in (0.0, 1.0) for v in observed)
    if observed and not is_binary:
        msg = (f"reward objective {name!r} returned values outside {{0, 1}} "
               f"(e.g. {sorted({v for v in observed if v not in (0.0, 1.0)})[:3]}); it is not a binary "
               f"pass/fail, so no pass^k is derived from it. A continuous objective needs a threshold "
               f"to become a pass, and a thresholded curve measures the threshold.")
        logger.warning(msg)
        warnings.append(msg)

    passes: dict[str, bool | None] = {}
    split: list[str] = []
    for dp, values in per_repeat.items():
        if not values:
            passes[dp] = None                             # no usable outcome for this case
        elif len(set(values)) > 1:
            passes[dp] = None
            split.append(dp)
        else:
            passes[dp] = values[0] == 1.0
    if split:
        msg = (f"reward objective {name!r} disagreed across repeats for {len(split)} data point(s) "
               f"({', '.join(sorted(split)[:5])}): no single pass/fail exists for them in this run, so "
               f"they are excluded from pass^k rather than averaged into one")
        logger.warning(msg)
        warnings.append(msg)

    return {
        "objective": name,
        "is_binary": is_binary,
        # The repeat-collapsed value per data point, for display: present only where the repeats agree.
        "values": {
            dp: (values[0] if values and len(set(values)) == 1 else None)
            for dp, values in per_repeat.items()
        },
        # `None` where the reward is unknown, and NOT False: a case the metric did not apply to has
        # not been failed by the artifact, and pass^k has to be able to tell those apart.
        "passes": passes if is_binary else {},
    }, warnings


def _weighted_mean(pairs: list[tuple[float, float]]) -> float | None:
    """Weighted mean of (value, weight). `None` if there's nothing to aggregate (empty `pairs`) --
    distinct from `0.0`, which would falsely claim a score exists. Falls back to a simple mean when
    all weights are 0 (so a task that leaves weights unset still gets a sensible number rather than
    div-by-zero)."""
    if not pairs:
        return None
    total_w = sum(w for _, w in pairs)
    if total_w == 0:
        return sum(v for v, _ in pairs) / len(pairs)
    return sum(v * w for v, w in pairs) / total_w
