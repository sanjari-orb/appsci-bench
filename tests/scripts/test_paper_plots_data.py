"""Tests for scripts/paper_plots/data.py, the loader behind the paper figures.

Loaded by path, since `scripts/` is not a package. The loader imports nothing from plotly, so these run
without the `plots` extra. A figure drawn from a mistyped CSV looks as authoritative as a correct one,
so every check here is one that turns a bad edit into an error instead of a wrong cell.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "paper_plots" / "data.py"
_spec = importlib.util.spec_from_file_location("paper_plots_data", _PATH)
assert _spec is not None and _spec.loader is not None
data = importlib.util.module_from_spec(_spec)
sys.modules["paper_plots_data"] = data
_spec.loader.exec_module(data)

HEADER = (
    "task,model,opt_enabled,k_builds,score_pow_k_mean,score_pow_k_std,"
    "score_pow_k_random_trial,worst_k3,trial_mean,trial_std,note\n"
)


def _write(tmp_path: pathlib.Path, *rows: str) -> pathlib.Path:
    path = tmp_path / "results.csv"
    path.write_text(HEADER + "".join(r + "\n" for r in rows), encoding="utf-8")
    return path


def test_committed_table_loads_and_both_arms_are_complete():
    results = data.load_results()
    for opt in (True, False):
        cells = data.grid(results, opt_enabled=opt)
        assert len(cells) == len(data.MODELS) * len(data.TASKS)


def test_blank_score_is_a_missing_cell_not_zero(tmp_path):
    path = _write(tmp_path, "crm_insights,claude-opus-5,true,,,,,,,,")
    (r,) = data.load_results(path)
    assert r.score is None and r.k_builds is None


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("airline_rebooking,claude-opus-5,true,3,1.4,0.01,,,,,", "outside"),
        ("airline_rebooking,claude-opus-5,true,3,0.5,-0.001,,,,,", "outside"),
        ("airline_rebooking,claude-opus-5,true,3,nan,0.01,,,,,", "outside"),
        ("airline_rebooking,claude-opus-5,true,3,abc,0.01,,,,,", "not a number"),
        ("airline_rebooking,claude-opus-5,true,4,0.5,0.01,,,,,", "k_builds"),
        ("airline_rebooking,claude-opus-5,true,,0.5,0.01,,,,,", "no k_builds"),
        ("airline_rebooking,claude-opus-5,yes,3,0.5,0.01,,,,,", "opt_enabled"),
        ("airlines,claude-opus-5,true,3,0.5,0.01,,,,,", "unknown task"),
        ("airline_rebooking,opus 5,true,3,0.5,0.01,,,,,", "unknown model"),
    ],
)
def test_bad_values_are_rejected(tmp_path, row, message):
    with pytest.raises(ValueError, match=message):
        data.load_results(_write(tmp_path, row))


def test_duplicate_rows_are_rejected(tmp_path):
    row = "airline_rebooking,claude-opus-5,true,3,0.5,0.01,,,,,"
    with pytest.raises(ValueError, match="duplicate"):
        data.load_results(_write(tmp_path, row, row))


def test_grid_names_the_missing_pairs(tmp_path):
    results = data.load_results(_write(tmp_path, "airline_rebooking,claude-opus-5,true,3,0.5,0.01,,,,,"))
    with pytest.raises(ValueError, match="no row for"):
        data.grid(results, opt_enabled=True)


def _cells(scores: dict[tuple[str, str], float | None]) -> list[str]:
    """A full opt-enabled grid: every model x task at 0.1 unless `scores` says otherwise."""
    rows = []
    for m in data.MODELS:
        for t in data.TASKS:
            s = scores.get((m, t), 0.1)
            k, v = ("", "") if s is None else ("3", str(s))
            rows.append(f"{t},{m},true,{k},{v},,,,,,")
    return rows


def test_average_skips_a_task_any_model_lacks(tmp_path):
    path = _write(tmp_path, *_cells({("claude-opus-5", "crm_insights"): None,
                                     ("gpt-5.5", "crm_insights"): 1.0}))
    cells = data.grid(data.load_results(path), opt_enabled=True)
    tasks = data.shared_tasks(cells)
    assert "crm_insights" not in tasks and len(tasks) == len(data.TASKS) - 1
    # gpt-5.5's 1.0 on the dropped task must not lift its average.
    assert data.mean_score(cells, "gpt-5.5", tasks) == pytest.approx(0.1)


def test_ranking_is_best_first_and_ties_keep_the_listed_order(tmp_path):
    path = _write(tmp_path, *_cells({("kimi-k3", "airline_rebooking"): 0.9}))
    cells = data.grid(data.load_results(path), opt_enabled=True)
    ranked = data.rank_models(cells)
    assert ranked[0] == "kimi-k3"
    assert ranked[1:] == [m for m in data.MODELS if m != "kimi-k3"]


def test_tasks_rank_easiest_first_and_skip_missing_cells(tmp_path):
    # banking gets the one high score; a missing cell on it must not drag its mean down as a 0.
    path = _write(tmp_path, *_cells({("kimi-k3", "banking_support"): 0.9,
                                     ("gpt-5.5", "banking_support"): None}))
    cells = data.grid(data.load_results(path), opt_enabled=True)
    ranked = data.rank_tasks(cells)
    assert ranked[0] == "banking_support"
    assert sorted(ranked) == sorted(data.TASKS)


def test_every_task_has_a_short_label():
    assert set(data.TASKS_SHORT) == set(data.TASKS)


TRIAL_HEADER = "task,model,build_id,eval_run_id,status,elicited,task_accuracy\n"


def _trials(tmp_path: pathlib.Path, *rows: str) -> list:
    path = tmp_path / "trials.csv"
    path.write_text(TRIAL_HEADER + "".join(r + "\n" for r in rows), encoding="utf-8")
    return data.load_trials(path)


def test_committed_trial_table_loads():
    assert data.usable_trials(data.load_trials())


def test_usable_trials_drop_invalid_and_unevaluated(tmp_path):
    trials = _trials(
        tmp_path,
        "order_to_cash,claude-opus-5,b0,r0,VALID,0.5,0.5",
        "order_to_cash,claude-opus-5,b0,r1,INVALID,0.5,0.9",
        "order_to_cash,claude-opus-5,b1,,VALID,0.5,",  # build passed, eval never ran
        "order_to_cash,claude-opus-5,b2,r2,VALID*,0.2,0.1",
    )
    kept = data.usable_trials(trials)
    assert [t.eval_run_id for t in kept] == ["r0", "r2"]


def test_builds_average_their_own_trials(tmp_path):
    # b0 has three trials, b1 one: each build becomes one point.
    trials = _trials(
        tmp_path,
        "order_to_cash,claude-opus-5,b0,r0,VALID,0.6,1.0",
        "order_to_cash,claude-opus-5,b0,r1,VALID,0.5,0.7",
        "order_to_cash,claude-opus-5,b0,r2,VALID,0.4,0.4",
        "order_to_cash,claude-opus-5,b1,r3,VALID,0.2,0.0",
    )
    b0, b1 = data.elicitation_builds(data.usable_trials(trials))
    assert (b0.build_id, b0.n_trials) == ("b0", 3)
    assert b0.elicited == pytest.approx(0.5) and b0.accuracy == pytest.approx(0.7)
    assert (b1.elicited, b1.accuracy, b1.n_trials) == (0.2, 0.0, 1)


def test_breakdown_parses_pairs_and_dropped():
    got = data.parse_breakdown("task_success 0.580 safety_no_fabrication 0.520 evidence DROPPED",
                               "row 2")
    assert got == {"task_success": 0.58, "safety_no_fabrication": 0.52, "evidence": None}


@pytest.mark.parametrize("raw", ["", "   ", "0", "0.25"])
def test_breakdown_blank_or_bare_number_means_none(raw):
    assert data.parse_breakdown(raw, "row 2") is None


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("task_success", "not 'name score' pairs"),
        ("task_success 0.5 grounding", "without a score"),
        ("task_success 1.5", "outside"),
        ("task_success nan", "outside"),
        ("task_success abc", "not a number"),
        ("Task-Success 0.5", "not a metric name"),
        ("grounding 0.5 grounding 0.6", "twice"),
    ],
)
def test_bad_breakdowns_are_rejected(raw, message):
    with pytest.raises(ValueError, match=message):
        data.parse_breakdown(raw, "row 2")


def test_trial_table_without_breakdown_column_still_loads(tmp_path):
    (t,) = _trials(tmp_path, "order_to_cash,claude-opus-5,b0,r0,VALID,0.5,0.5")
    assert t.metrics is None


BUILD_HEADER = "task,model,build_id,eval_run_id,status,elicited,task_accuracy,breakdown,build_error\n"


def _outcomes(tmp_path: pathlib.Path, *rows: str) -> dict:
    path = tmp_path / "trials.csv"
    path.write_text(BUILD_HEADER + "".join(r + "\n" for r in rows), encoding="utf-8")
    return {o.build_id: o.outcome for o in data.build_outcomes(data.load_trials(path))}


def test_build_outcomes_classify_each_way_a_build_ends(tmp_path):
    got = _outcomes(
        tmp_path,
        "order_to_cash,claude-opus-5,ok,r0,VALID,0.5,0.4,,",
        # The sheet scores a failed build 0; the build error still makes it a failure.
        "order_to_cash,claude-opus-5,nosub,r1,VALID,0.5,0,,No submission",
        "order_to_cash,claude-opus-5,late,,VALID,0.5,,,2 hr build timeout",
        "order_to_cash,claude-opus-5,conf,,VALID,0.5,,,Conformance Fail",
        "order_to_cash,claude-opus-5,crash,,VALID,0.5,,,Error: Invalid value for '--port'",
        "order_to_cash,claude-opus-5,silent,,VALID,0.5,,,",  # no error, but nothing scored
        "order_to_cash,claude-opus-5,infra,r2,INVALID,0.5,0.9,,",  # every row invalid: left out
    )
    assert got == {
        "ok": data.USABLE, "nosub": data.NO_ARTIFACT, "late": data.NO_ARTIFACT,
        "conf": data.BROKEN, "crash": data.BROKEN, "silent": data.BROKEN,
    }


def test_a_build_with_one_valid_trial_counts_once(tmp_path):
    got = _outcomes(
        tmp_path,
        "order_to_cash,claude-opus-5,b0,r0,INVALID,0.5,0.9,,",
        "order_to_cash,claude-opus-5,b0,r1,VALID,0.5,0.4,,",
    )
    assert got == {"b0": data.USABLE}


TOKEN_HEADER = BUILD_HEADER.rstrip("\n") + ",build_tokens\n"


def _token_trials(tmp_path: pathlib.Path, *rows: str) -> list:
    path = tmp_path / "trials.csv"
    path.write_text(TOKEN_HEADER + "".join(r + "\n" for r in rows), encoding="utf-8")
    return data.load_trials(path)


def _full_token_rows(tokens: int) -> list[str]:
    """One valid build per model x task, each using `tokens`."""
    return [f"{t},{m},{t}_{m}_b0,{t}_{m}_r0,VALID,0.5,0.5,,,{tokens}"
            for m in data.MODELS for t in data.TASKS]


def test_build_tokens_count_each_build_once_and_include_failed_builds(tmp_path):
    rows = _full_token_rows(1000)
    rows += [
        # A second opus-5 order_to_cash build that failed: it still spent tokens and counts.
        "order_to_cash,claude-opus-5,b1,,VALID,0.5,,,No submission,3000",
        # A second trial of that build repeats its count and must not count twice.
        "order_to_cash,claude-opus-5,b1,r9,VALID,0.5,,,No submission,3000",
        # An invalid build is left out.
        "order_to_cash,claude-opus-5,b2,r8,INVALID,0.5,0.9,,,999999",
    ]
    tokens, tasks = data.mean_build_tokens(_token_trials(tmp_path, *rows))
    assert tasks == list(data.TASKS)
    # order_to_cash is (1000 + 3000) / 2 = 2000; the other 6 tasks are 1000.
    assert tokens["claude-opus-5"] == pytest.approx((2000 + 6 * 1000) / len(data.TASKS))
    assert tokens["gpt-5.5"] == pytest.approx(1000)


def test_build_tokens_that_differ_within_a_build_are_an_error(tmp_path):
    rows = _full_token_rows(1000) + [
        "order_to_cash,claude-opus-5,b1,r1,VALID,0.5,0.5,,,2000",
        "order_to_cash,claude-opus-5,b1,r2,VALID,0.5,0.5,,,2500",
    ]
    with pytest.raises(ValueError, match="different token counts"):
        data.mean_build_tokens(_token_trials(tmp_path, *rows))


def test_build_tokens_need_a_task_every_model_has(tmp_path):
    with pytest.raises(ValueError, match="no task has a build token count"):
        data.mean_build_tokens(_token_trials(tmp_path, "order_to_cash,claude-opus-5,b0,r0,VALID,0.5,0.5,,,10"))


def test_pareto_front_keeps_only_undominated_points_in_cost_order():
    points = {
        "cheap": (1.0, 0.1),
        "mid": (2.0, 0.3),
        "top": (7.0, 0.45),
        "worse_than_mid": (7.1, 0.25),  # costs more than mid and scores less
        "free_zero": (1.5, 0.0),  # beaten by cheap
    }
    assert data.pareto_front(points) == ["cheap", "mid", "top"]


def test_pareto_front_ties_keep_both_and_equal_cost_keeps_the_higher_score():
    assert data.pareto_front({"a": (1.0, 0.5), "b": (1.0, 0.5)}) == ["a", "b"]
    assert data.pareto_front({"a": (1.0, 0.5), "b": (1.0, 0.4)}) == ["a"]
    assert data.pareto_front({}) == []


def test_tiers_cover_every_model_once():
    tiered = [m for models in data.MODEL_TIERS.values() for m in models]
    assert sorted(tiered) == sorted(data.MODELS)


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("order_to_cash,claude-opus-5,b0,r0,MAYBE,0.5,0.5", "status"),
        ("order_to_cash,claude-opus-5,b0,r0,VALID,1.5,0.5", "outside"),
        ("order_to_cash,claude-opus-5,,r0,VALID,0.5,0.5", "build_id"),
    ],
)
def test_bad_trial_rows_are_rejected(tmp_path, row, message):
    with pytest.raises(ValueError, match=message):
        _trials(tmp_path, row)


def test_duplicate_eval_runs_are_rejected(tmp_path):
    row = "order_to_cash,claude-opus-5,b0,r0,VALID,0.5,0.5"
    with pytest.raises(ValueError, match="duplicate"):
        _trials(tmp_path, row, row)


def test_no_shared_task_is_an_error(tmp_path):
    path = _write(tmp_path, *_cells({("claude-opus-5", t): None for t in data.TASKS}))
    cells = data.grid(data.load_results(path), opt_enabled=True)
    with pytest.raises(ValueError, match="no task has a score for every model"):
        data.shared_tasks(cells)


# --- internal vs external: optimization steps --------------------------------------------------


@pytest.mark.parametrize(
    ("internal", "external", "sd", "group"),
    [
        (0.1, 0.005, 0.01, "false_improvement"),  # self-grade up, held-out move under the noise
        (0.1, -0.005, 0.01, "false_improvement"),
        (-0.1, 0.005, 0.01, "no_change"),  # self-grade down, nothing real happened
        (0.0, 0.0, 0.01, "no_change"),  # nothing moved at all
        (0.0, 0.05, 0.01, "missed"),  # real rise, self-grade flat
        (0.0, -0.05, 0.01, "missed"),  # real drop, self-grade flat
        (0.2, 0.05, 0.01, "agreed"),
        (-0.2, -0.05, 0.01, "agreed"),
        (0.2, -0.05, 0.01, "disagreed"),
        (0.2, 0.01, 0.01, "agreed"),  # a move equal to one sd is outside the noise
    ],
)
def test_classify_step(internal, external, sd, group):
    assert data.classify_step(internal, external, sd) == group


@pytest.mark.parametrize("args", [(float("nan"), 0.1, 0.01), (0.1, 0.1, float("inf")),
                                  (0.1, 0.1, -0.01)])
def test_classify_step_rejects_bad_numbers(args):
    with pytest.raises(ValueError):
        data.classify_step(*args)


def _counts(agreed: int, disagreed: int, missed: int, false_up: int,
            no_change: int) -> dict[str, int]:
    return {"agreed": agreed, "disagreed": disagreed, "missed": missed,
            "false_improvement": false_up, "no_change": no_change}


def test_steps_agreement_is_agreed_over_real_changes():
    s = data.Steps(_counts(11, 1, 3, 5, 4))  # the committed Opus 4.8 totals
    assert (s.total, s.real) == (24, 15)
    assert s.agreement == pytest.approx(11 / 15)  # missed changes count against it
    assert data.Steps(_counts(0, 0, 0, 3, 2)).agreement is None  # no real change at all
    assert data.Steps(_counts(0, 2, 0, 0, 0)).agreement == 0
    assert data.Steps(_counts(0, 0, 2, 0, 0)).agreement == 0  # only missed changes
    assert data.Steps().agreement is None and data.Steps().share("agreed") == 0.0


def test_steps_add():
    a = data.Steps(_counts(1, 0, 1, 2, 0), builds=1)
    b = data.Steps(_counts(3, 1, 0, 0, 4), builds=2)
    total = sum([a, b], data.Steps())
    assert total.counts == _counts(4, 1, 1, 2, 4)
    assert total.builds == 3


def _stats(root: pathlib.Path, task: str, series: dict) -> None:
    folder = root / task / "self_grade_vs_real"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "stats.json").write_text(json.dumps({"series": series}), encoding="utf-8")


def _step(internal: object, external: object) -> dict:
    return {"internal_delta": internal, "external_delta": external}


def _build(sd: object, steps: list[dict], first_last: tuple = (0.5, 1.0, 0.4, 0.5)) -> dict:
    keys = ("internal_first", "internal_last", "external_first", "external_last")
    return {"external_noise_sd": sd, "decisions": steps, **dict(zip(keys, first_last, strict=True))}


def test_load_steps_pools_builds_and_skips_empty_ones(tmp_path):
    _stats(tmp_path, "crm_insights", {
        "b0": _build(0.01, [_step(0.1, 0.05), _step(0.0, 0.05)], (0.5, 1.0, 0.4, 0.5)),
        "b1": _build(0.02, [_step(0.1, 0.001), _step(-0.1, 0.001)], (0.9, 0.8, 0.6, 0.6)),
        "b2": _build(None, []),  # no steps, so no noise needed
    })
    _stats(tmp_path, "order_to_cash", {"b0": _build(0.01, [])})
    steps = data.load_steps(tmp_path)
    assert list(steps) == ["crm_insights"]
    crm = steps["crm_insights"]
    assert crm.counts == _counts(1, 0, 1, 1, 1)
    assert crm.builds == 2
    assert crm.internal_changes == pytest.approx([0.5, -0.1])
    assert crm.external_changes == pytest.approx([0.1, 0.0])
    assert crm.mean_internal_change == pytest.approx(0.2)
    assert crm.mean_external_change == pytest.approx(0.05)


def test_step_changes_add_up_over_builds_not_tasks():
    a = data.Steps(builds=1, internal_changes=[0.3], external_changes=[0.1])
    b = data.Steps(builds=2, internal_changes=[0.0, 0.0], external_changes=[0.1, 0.1])
    total = a + b
    assert total.mean_internal_change == pytest.approx(0.1)  # (0.3 + 0 + 0) / 3, not (0.3 + 0) / 2
    assert total.mean_external_change == pytest.approx(0.1)
    assert data.Steps().mean_internal_change is None


def test_load_steps_needs_measured_noise(tmp_path):
    _stats(tmp_path, "crm_insights", {"b0": _build(None, [_step(0.1, 0.05)])})
    with pytest.raises(ValueError, match="noise was not measured"):
        data.load_steps(tmp_path)


@pytest.mark.parametrize("build", [
    _build(0.01, [_step("0.1", 0.05)]),
    _build(0.01, [_step(0.1, 0.05)], (0.5, None, 0.4, 0.5)),  # a first/final score missing
])
def test_load_steps_rejects_a_non_number(tmp_path, build):
    _stats(tmp_path, "crm_insights", {"b0": build})
    with pytest.raises(ValueError, match="not a number"):
        data.load_steps(tmp_path)


@pytest.mark.parametrize(
    ("internal", "external", "hits"),
    [
        (0.2, 0.05, {"internal_changed", "both_changed", "both_changed_same_direction",
                     "held_out_changed"}),
        (-0.2, 0.05, {"internal_changed", "both_changed", "held_out_changed"}),  # opposite ways
        (0.2, 0.005, {"internal_changed"}),  # held-out move under the noise
        (-0.1, 0.0, {"internal_changed"}),
        (0.0, -0.05, {"held_out_changed", "held_out_changed_internal_flat"}),  # missed
        (0.0, 0.0, set()),
    ],
)
def test_co_change(internal, external, hits):
    got = data.co_change(internal, external, 0.01)
    assert {k for k, v in got.items() if v} == hits


def test_co_change_rates_and_empty_cases():
    s = data.Steps(co={"internal_changed": 20, "both_changed": 12,
                       "both_changed_same_direction": 11, "held_out_changed": 15,
                       "held_out_changed_internal_flat": 3})
    assert s.held_out_follows == pytest.approx(12 / 20)
    assert s.internal_misses == pytest.approx(3 / 15)
    assert data.Steps().held_out_follows is None and data.Steps().internal_misses is None


def test_committed_consistency_totals():
    """The numbers the paper quotes, from the committed Opus 4.8 results. Slides b1 was rescored on
    2026-11-05 (its September held-out scores had lost every deck to the MIME file_type bug, #237),
    which moved one slides step from both-changed to internal-only."""
    total = sum(data.load_steps().values(), data.Steps())
    assert total.total == 24 and total.builds == 13
    assert total.co == {"internal_changed": 20, "both_changed": 11,
                        "both_changed_same_direction": 10, "held_out_changed": 14,
                        "held_out_changed_internal_flat": 3}


def test_committed_consistency_results_load():
    steps = data.load_steps()
    assert steps, "no internal-vs-external result was found"
    total = sum(steps.values(), data.Steps())
    assert 0 < total.real <= total.total
    assert 0 <= total.agreement <= 1
