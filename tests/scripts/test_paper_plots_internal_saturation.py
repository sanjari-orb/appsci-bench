"""Tests for scripts/paper_plots/compute_internal_saturation.py.

Loaded by path, since `scripts/` is not a package. Nothing here reaches S3: the one function that
shells out to `aws` is only exercised for its input check.
"""

from __future__ import annotations

import csv
import importlib.util
import pathlib
import sys

import pytest

_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "paper_plots"
sys.path.insert(0, str(_DIR))
_spec = importlib.util.spec_from_file_location("paper_plots_internal_saturation",
                                               _DIR / "compute_internal_saturation.py")
sat = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sat
_spec.loader.exec_module(sat)

NEVER = sat.NEVER


def _build(scores, minutes=None, *, model="claude-opus-4-8", task="order_to_cash", cases=5):
    minutes = tuple(minutes) if minutes is not None else tuple(float(i) for i in range(len(scores)))
    return sat.Build(model, task, "b", tuple(scores), minutes, tuple([cases] * len(scores)))


def _results(*score_lists, **kw):
    """Through the per-build CSV and back, the path the tables actually take."""
    return [sat.BuildResult(model=r["model"], task=r["task"], n_rounds=r["n_rounds"],
                            saturated_at=None if r["saturated_at_round"] == NEVER else r["saturated_at_round"],
                            plateaued_at=None if r["plateaued_at_round"] == NEVER else r["plateaued_at_round"],
                            minutes_to_saturate=float(r["minutes_to_saturate"]) if r["minutes_to_saturate"] else None,
                            suite_size=float(r["median_suite_size"]))
            for r in sat.per_build_rows([_build(s, **kw) for s in score_lists])]


# --- saturation --------------------------------------------------------------------------------------

def test_saturation_is_the_first_round_at_or_above_the_threshold():
    """Not the best round, and not the last: the eval has no headroom from the first one on."""
    assert sat.saturated_at((0.5, 0.99, 0.8, 1.0)) == 2


def test_just_below_the_threshold_is_never_saturated():
    assert sat.saturated_at((0.5, 0.98996)) == NEVER


# --- plateaus ----------------------------------------------------------------------------------------

def test_going_flat_is_the_final_best_round_when_nothing_after_it_beats_it_by_the_margin():
    """Round 2 sets the best at 0.80; 0.805 and 0.809 both miss it by less than the loop's 0.01."""
    assert sat.plateaued_at((0.5, 0.80, 0.805, 0.809)) == 2


def test_a_gain_of_exactly_the_margin_counts_as_progress():
    """The loop keeps a change at `score >= beat + 0.01`, so the plateau rule must agree at the boundary."""
    assert sat.plateaued_at((0.50, 0.51, 0.52, 0.53)) == NEVER


def test_one_round_without_a_gain_before_the_build_ends_is_flat():
    """"Never increases again" is judged to the end of the build, so a single later round is enough."""
    assert sat.plateaued_at((0.5, 0.7, 0.6)) == 2


def test_a_build_whose_best_is_its_last_round_was_still_improving():
    assert sat.plateaued_at((0.5, 0.9)) == NEVER
    assert sat.plateaued_at((0.5,)) == NEVER


def test_a_stall_that_later_recovers_is_not_a_plateau():
    """Two failed rounds are not enough on their own: the score rose again, so it never went flat."""
    assert sat.plateaued_at((0.5, 0.8, 0.7, 0.7, 0.9)) == NEVER


def test_a_first_round_the_loop_never_beats_plateaus_at_round_1():
    """The Opus 4.8 banking shape: every change scored below the baseline."""
    assert sat.plateaued_at((0.933, 0.900, 0.861, 0.828)) == 1


def test_a_regression_does_not_reset_the_best():
    """Falling back and recovering to the old best is not progress; only beating the best is."""
    assert sat.plateaued_at((0.8, 0.5, 0.8)) == 1


def test_saturating_is_plateauing_even_if_the_score_edges_up_afterwards():
    """0.99 -> 1.0 is a gain of 0.01, so the build is not flat, but at 0.99 its eval had no room left."""
    assert sat.plateaued_at((0.99, 1.0)) == 1


def test_a_single_saturated_round_is_a_plateau():
    assert sat.plateaued_at((1.0,)) == 1


def test_the_earlier_of_saturating_and_going_flat_wins():
    """Flat from round 1 (0.99 misses 0.985 + 0.01), though it only saturates at round 3."""
    assert sat.plateaued_at((0.985, 0.9, 0.99)) == 1


def test_every_saturated_build_in_the_committed_data_is_plateaued():
    rows = sat.per_build_rows(sat.load_rounds())

    for r in rows:
        if r["saturated_at_round"] != NEVER:
            assert r["plateaued_at_round"] != NEVER, r["build_id"]
            assert r["plateaued_at_round"] <= r["saturated_at_round"], r["build_id"]


# --- the per-build CSV and reading it back -----------------------------------------------------------

def test_never_is_written_as_minus_one_and_read_back_as_absent(tmp_path):
    path = tmp_path / "per_build.csv"
    sat.write_per_build(sat.per_build_rows([_build([0.2, 0.3])]), path)

    with path.open(newline="", encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    (result,) = sat.load_per_build(path)

    assert (row["saturated_at_round"], row["plateaued_at_round"]) == ("-1", "-1")
    assert (result.saturated_at, result.plateaued_at) == (None, None)


@pytest.mark.parametrize("field, value, message", [
    ("saturated_at_round", "0", "neither a round nor -1"),
    ("plateaued_at_round", "-3", "neither a round nor -1"),
    ("saturated_at_round", "9", "past the build's"),
])
def test_an_impossible_round_in_the_per_build_csv_stops_the_load(tmp_path, field, value, message):
    path = tmp_path / "per_build.csv"
    row = sat.per_build_rows([_build([0.5, 1.0])])[0]
    row[field] = value
    sat.write_per_build([row], path)

    with pytest.raises(ValueError, match=message):
        sat.load_per_build(path)


# --- per group ---------------------------------------------------------------------------------------

def test_already_saturated_at_round_1_is_counted_separately():
    """Its own eval scored it at ceiling before any change: the headline sign of an easy exam."""
    g = sat.summarize(_results([1.0, 1.0], [0.5, 0.99]))

    assert g.at_round_1 == 1
    assert g.mean_rounds == 1.5


def test_each_round_average_covers_only_the_builds_that_reached_it():
    """Saturates at 2 (so plateaus at 2); flat from 1; still rising at its last round."""
    g = sat.summarize(_results([0.5, 1.0], [0.5, 0.4, 0.3, 0.2], [0.2, 0.4, 0.6]))

    assert (g.builds, g.saturated, g.plateaued) == (3, 1, 2)
    assert g.mean_rounds == 2, "only the saturated build"
    assert g.mean_plateau_round == 1.5, "only the two plateaued builds"
    assert g.rounds_per_build == 3, "rounds per build still averages over every build"


def test_a_group_where_nothing_saturated_leaves_the_round_figures_blank_not_zero():
    g = sat.summarize(_results([0.4, 0.5]))

    assert g.saturated == 0
    assert (g.mean_rounds, g.median_rounds, g.mean_minutes) == (None, None, None)


def test_groups_follow_the_paper_order_and_end_with_all():
    rows = sat.groups(_results([1.0], model="kimi-k3") + _results([1.0], model="claude-opus-5"))

    assert [m for grouping, m, _, _ in rows if grouping == "model"] == ["claude-opus-5", "kimi-k3"]
    assert rows[-1][0] == "all" and rows[-1][3].builds == 2


# --- the build set -----------------------------------------------------------------------------------

def _trial(build_id, status, model="claude-opus-4-8", task="order_to_cash"):
    return sat.Trial(task=task, model=model, build_id=build_id, eval_run_id=f"{build_id}.t", status=status,
                     elicited=None, accuracy=None)


def test_each_build_counts_once_however_many_trials_it_has():
    trials = [_trial("b1", "VALID"), _trial("b1", "VALID"), _trial("b1", "VALID*")]

    assert sat.valid_builds(trials) == [("claude-opus-4-8", "order_to_cash", "b1")]


def test_a_build_is_kept_when_any_trial_is_valid_and_dropped_when_none_are():
    """The same rule data.build_outcomes applies, so these builds are the paper's builds."""
    trials = [_trial("b1", "INVALID"), _trial("b1", "VALID"), _trial("b2", "INVALID")]

    assert [b for _, _, b in sat.valid_builds(trials)] == ["b1"]


# --- reading history and the rounds table ------------------------------------------------------------

def _rounds_csv(tmp_path, *rows):
    path = tmp_path / "rounds.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=sat.ROUNDS_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _row(scores="0.5;1.0", minutes="0.0;2.0", cases="5;5", n=2, model="claude-opus-4-8"):
    return {"model": model, "task": "order_to_cash", "build_id": "b", "n_rounds": n,
            "scores": scores, "minutes": minutes, "cases": cases}


def test_a_record_with_no_headline_score_is_not_a_round():
    history = {"records": [{"headline_metric": 0.5, "timestamp": 0, "cases": [1, 2]},
                           {"headline_metric": None, "timestamp": 30},
                           {"headline_metric": 1.0, "timestamp": 120, "cases": [1, 2]}]}

    scores, minutes, cases = sat.rounds_from_history(history)

    assert scores == [0.5, 1.0]
    assert minutes == [0, 2]
    assert cases == [2, 2]


def test_a_missing_timestamp_blanks_minutes_rather_than_misaligning_them():
    history = {"records": [{"headline_metric": 0.5, "timestamp": 0}, {"headline_metric": 1.0}]}

    assert sat.rounds_from_history(history)[1] == []


def test_a_build_with_no_rounds_is_listed_but_not_loaded(tmp_path):
    path = _rounds_csv(tmp_path, _row(), _row(scores="", minutes="", cases="", n=0))

    assert len(sat.load_rounds(path)) == 1


@pytest.mark.parametrize("override, message", [
    ({"n": 3}, "n_rounds 3 but 2 scores"),
    ({"minutes": "0.0"}, "1 minutes for 2 rounds"),
    ({"scores": "0.5;1.5"}, "outside"),
    ({"scores": "0.5;abc"}, "bad scores"),
    ({"model": "not-a-model"}, "unknown model"),
])
def test_a_malformed_rounds_row_stops_the_load(tmp_path, override, message):
    path = _rounds_csv(tmp_path, _row(**override))

    with pytest.raises(ValueError, match=message):
        sat.load_rounds(path)


def test_a_build_id_that_is_not_run_shaped_is_refused_before_any_aws_call():
    with pytest.raises(ValueError, match="does not look like a run id"):
        sat.fetch_history("../other-bucket/key", profile=None)


# --- the committed data ------------------------------------------------------------------------------

def test_committed_rounds_table_covers_every_valid_build():
    """A stale rounds table would silently drop builds added to the sheet since the last --fetch."""
    listed = set()
    with sat.ROUNDS_CSV.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            listed.add((row["model"], row["task"], row["build_id"]))

    assert listed == set(sat.valid_builds(sat.load_trials()))
    assert sat.load_rounds(), "the committed table must load"
