"""Tests for scripts/analysis/internal_eval_consistency/internal_vs_external.py.

Loaded by path like its siblings in this directory, since `scripts/` is a directory of runnable tools
rather than an importable package. Nothing here reaches S3, Docker or a model.

This tool joins two instruments that are easy to conflate and expensive to conflate quietly, so the
tests below are all about the same class of failure: producing a PLAUSIBLE number from the wrong pairing
rather than an error. Three are worth naming up front.

* The join key. Both sides carry a content hash AND a deploy ordinal, and the hash is the more obvious
  choice. It is wrong: the agent re-deploys programs it has already built, so one hash names two deploys
  with two different external scores. Keying on it silently drops points -- it cost one of six on the
  first real run of this analysis, and the correlation moved from -0.02 to -0.32 without anything
  failing.
* The sample limit. A record scored on a 20-case subset and one scored on the full block are different
  measurements wearing the same field name, and the mirror really does hold both for the same run.
* The empty axis. When one side never moves, the correlation is undefined, not zero. Reporting 0.0 there
  reads as "measured, no relationship" when nothing was measured at all.
"""
from __future__ import annotations

import importlib.util
import json
import math
import pathlib
import statistics
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "analysis" / "internal_eval_consistency" / "internal_vs_external.py"
_MODNAME = "_internal_vs_external_under_test"


def _load():
    """Load the tool by path, registering it in `sys.modules` first -- see the sibling loader's note."""
    spec = importlib.util.spec_from_file_location(_MODNAME, _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODNAME] = mod
    spec.loader.exec_module(mod)
    return mod


MOD = _load()


def write_external(run_dir, ordinal, score, *, content_hash="aaaa1111", limit=None, repeat=0,
                   verdict="scored", is_submission=False, passes=None):
    """One external score record, plus its per-case report when `passes` is given."""
    scores = run_dir / "checkpoint_scores"
    scores.mkdir(parents=True, exist_ok=True)
    stem = f"cap-{ordinal:03d}__r{repeat}"
    report_name = f"{stem}.report.json"
    record = {
        "capture": {"name": f"cap-{ordinal:03d}", "ordinal": ordinal, "outcome": "deployed",
                    "content_hash": content_hash, "is_submission": is_submission},
        "verdict": verdict,
        "score": score,
        "limit": limit,
        "cases_total": len(passes) if passes else 50,
        "repeat": repeat,
        "report": report_name,
    }
    (scores / f"{stem}.json").write_text(json.dumps(record))
    if passes is not None:
        (scores / report_name).write_text(json.dumps({"views": {"reward": {"passes": passes}}}))
    return record


def write_internal(run_dir, per_ordinal):
    """The agent's own score history, in the layout the mirrored run uses."""
    path = run_dir / MOD.HISTORY_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [{"iteration": o, "stage": "attempt", "headline_metric": v,
                "checkpoint": {"name": f"cap-{o:03d}", "ordinal": o, "content_hash": "aaaa1111"}}
               for o, v in sorted(per_ordinal.items())]
    path.write_text(json.dumps({"records": records}))


# --- the join ------------------------------------------------------------------------------------

def test_two_deploys_of_the_same_program_are_two_points(tmp_path):
    """A repeated content hash must not collapse. This is the bug that motivated the tool's join key.

    The agent rolls back, restoring byte-identical code, so deploys 2 and 4 here are one program with
    two external scores. Keyed by hash they become one point; keyed by ordinal they stay two, which is
    also what makes them usable as a noise measurement.
    """
    run = tmp_path / "run"
    for ordinal, score in ((1, 0.64), (2, 0.64), (3, 0.60), (4, 0.62)):
        write_external(run, ordinal, score, content_hash="dupe" if ordinal in (2, 4) else f"h{ordinal}")
    write_internal(run, {1: 0.85, 2: 0.97, 3: 0.95, 4: 0.94})

    series, skipped = MOD.collect([run], None)

    assert skipped == {}
    points = series["run"]["points"]
    assert [p["deploy"] for p in points] == [1, 2, 3, 4]
    assert [p["external"] for p in points] == [0.64, 0.64, 0.60, 0.62], \
        "both deploys of the duplicated program must keep their own external score"


def test_a_deploy_with_no_internal_score_is_dropped_not_carried(tmp_path):
    """The submitted copy has an external score and no internal one. It cannot be paired, so it goes."""
    run = tmp_path / "run"
    write_external(run, 1, 0.64)
    write_external(run, 2, 0.62, is_submission=True)
    write_internal(run, {1: 0.85})

    series, skipped = MOD.collect([run], None)

    assert skipped == {MOD.SKIP_TOO_FEW: ["run"]}, "one pairable deploy is not a correlation"


# --- which records may be read -------------------------------------------------------------------

def test_records_at_another_limit_are_ignored_rather_than_pooled(tmp_path):
    """The mirror holds both for the same run, and pooling them would mix two case sets silently."""
    run = tmp_path / "run"
    write_external(run, 1, 0.90, limit=20, content_hash="h1")
    write_external(run, 2, 0.90, limit=20, content_hash="h2")
    write_internal(run, {1: 0.85, 2: 0.97})

    full, _ = MOD.collect([run], None)
    twenty, _ = MOD.collect([run], 20)

    assert full == {}, "full-block request must not read limit-20 records"
    assert twenty["run"]["n"] == 2


def test_an_excluded_record_is_not_read_as_a_zero(tmp_path):
    """`excluded` means our harness broke and no measurement exists. Scoring it 0.0 would invent one."""
    run = tmp_path / "run"
    write_external(run, 1, None, verdict="excluded")
    write_external(run, 2, 0.62)
    write_external(run, 3, 0.66)
    write_internal(run, {1: 0.85, 2: 0.97, 3: 0.95})

    series, _ = MOD.collect([run], None)

    assert [p["deploy"] for p in series["run"]["points"]] == [2, 3]
    assert all(p["external"] is not None for p in series["run"]["points"])


# --- the noise floor -----------------------------------------------------------------------------

def test_noise_is_measured_from_disagreeing_cases_not_the_score_gap(tmp_path):
    """Per-case flips cancel in the total, so the score difference understates the instrument.

    Here two runs of one program disagree on 4 of 8 cases while landing on the SAME score. Reading the
    score gap would report a noise floor of zero on data that is half disagreement.
    """
    run = tmp_path / "run"
    a = {"c1": True, "c2": True, "c3": False, "c4": False,
         "c5": True, "c6": True, "c7": False, "c8": False}
    b = {"c1": False, "c2": False, "c3": True, "c4": True,
         "c5": True, "c6": True, "c7": False, "c8": False}
    write_external(run, 1, 0.5, content_hash="same", passes=a)
    write_external(run, 2, 0.5, content_hash="same", passes=b)

    sd = MOD.external_noise_sd(run, None)

    assert sd is not None and sd > 0, "identical scores must not imply a zero noise floor"
    assert sd == pytest.approx((math.sqrt(4) / 8) / math.sqrt(2))


def test_noise_comes_from_repeats_when_a_capture_was_scored_more_than_once(tmp_path):
    """Re-scoring one capture IS a repeated measurement, so prefer it over the identical-program proxy.

    It also covers a task shape the proxy cannot: the proxy reads per-case pass/fail, which only exists
    for a task declaring a binary reward. order_to_cash declares none, so its reports carry no
    `views.reward` and the proxy returns None -- an unmeasured noise floor on a sweep with three repeats
    per capture that could pin it to four decimal places.
    """
    run = tmp_path / "run"
    for repeat, score in enumerate((0.9925, 0.9919, 0.9920)):
        write_external(run, 1, score, content_hash="h1", repeat=repeat)   # no per-case report at all

    sd = MOD.external_noise_sd(run, None)

    assert sd == pytest.approx(statistics.stdev([0.9925, 0.9919, 0.9920]), rel=1e-6)


def test_repeats_average_evenly_rather_than_pairwise(tmp_path):
    """Three repeats must weight 1/3 each. Folding them in pairwise gave the last one a half."""
    run = tmp_path / "run"
    for repeat, score in enumerate((0.0, 0.0, 0.9)):
        write_external(run, 1, score, content_hash="h1", repeat=repeat)

    assert MOD.read_external(run, None)[1][0] == pytest.approx(0.3)


def test_noise_is_unmeasured_when_no_program_was_scored_twice(tmp_path):
    """None, not 0.0 -- an unmeasured floor must not read as a tight one."""
    run = tmp_path / "run"
    write_external(run, 1, 0.64, content_hash="h1", passes={"c1": True})
    write_external(run, 2, 0.62, content_hash="h2", passes={"c1": False})

    assert MOD.external_noise_sd(run, None) is None


# --- correlation ---------------------------------------------------------------------------------

def test_a_flat_axis_gives_an_undefined_correlation_not_zero(tmp_path):
    """Every external score identical: nothing was measured, and 0.0 would claim otherwise."""
    run = tmp_path / "run"
    for ordinal in (1, 2, 3):
        write_external(run, ordinal, 0.64, content_hash=f"h{ordinal}")
    write_internal(run, {1: 0.85, 2: 0.90, 3: 0.97})

    series, _ = MOD.collect([run], None)

    assert math.isnan(series["run"]["pearson"])
    assert math.isnan(series["run"]["spearman"])


def test_a_perfect_ranking_is_reported_as_one(tmp_path):
    """The positive control: the tool must be able to see agreement when agreement is there."""
    run = tmp_path / "run"
    for ordinal, score in ((1, 0.50), (2, 0.60), (3, 0.70)):
        write_external(run, ordinal, score, content_hash=f"h{ordinal}")
    write_internal(run, {1: 0.10, 2: 0.50, 3: 0.90})

    series, _ = MOD.collect([run], None)

    assert series["run"]["pearson"] == pytest.approx(1.0)
    assert series["run"]["spearman"] == pytest.approx(1.0)


def test_kendall_cannot_reach_one_when_the_two_axes_tie_differently(tmp_path):
    """A property worth pinning, because it is a trap when reading the number.

    Every orderable pair here is concordant -- perfect agreement on everything the data can express --
    and tau-b is 0.816, not 1.0. Ties enter the DENOMINATOR (once per side, under a square root) rather
    than being discounted entirely, so the attainable maximum shrinks with the tie structure. A reader
    who takes tau-b's ceiling to be 1.0 will read honest agreement as partial disagreement.
    """
    internal = [0.1, 0.2, 0.3]
    external = [0.60, 0.62, 0.62]   # one of the three pairs ties

    # 2 concordant, 0 discordant, 1 pair tied on the external side: 2 / sqrt(3 * 2).
    assert MOD.kendall_tau_b(internal, external) == pytest.approx(2 / math.sqrt(6))


def test_kendall_is_undefined_when_one_axis_never_moves():
    """No pair can be ordered, so there is nothing to agree about. Not zero."""
    assert math.isnan(MOD.kendall_tau_b([0.1, 0.2, 0.3], [0.64, 0.64, 0.64]))


def test_kendall_sees_a_reversal():
    """Positive control in the other direction."""
    assert MOD.kendall_tau_b([0.1, 0.2, 0.3], [0.66, 0.62, 0.60]) == pytest.approx(-1.0)


# --- decisions -----------------------------------------------------------------------------------

def test_an_unmoved_external_score_is_a_tie_not_a_disagreement():
    """The loop can only be wrong about a direction that EXISTS.

    A change the external eval scores identically is not evidence the internal eval pointed the wrong
    way. Folding it into the failures overstated the error rate on the first real build -- 2/5 reported
    where the honest figure is 2 of 4 resolvable changes.
    """
    points = [{"deploy": 1, "internal": 0.85, "external": 0.64},
              {"deploy": 2, "internal": 0.97, "external": 0.64}]

    got = MOD.decisions(points, None)

    assert [d["verdict"] for d in got] == ["tied"]


def test_agreement_and_disagreement_are_read_from_the_signs():
    points = [{"deploy": 1, "internal": 0.90, "external": 0.60},
              {"deploy": 2, "internal": 0.95, "external": 0.66},   # both up   -> agree
              {"deploy": 3, "internal": 0.99, "external": 0.62},   # up / down -> disagree
              {"deploy": 4, "internal": 0.80, "external": 0.58}]   # both down -> agree

    got = MOD.decisions(points, None)

    assert [d["verdict"] for d in got] == ["agree", "disagree", "agree"]
    assert [d["cycle"] for d in got] == ["1->2", "2->3", "3->4"]


def test_a_change_smaller_than_the_noise_is_flagged_but_still_counted():
    """Dropping unresolvable changes would select on the outcome; hiding them would mislead."""
    points = [{"deploy": 1, "internal": 0.90, "external": 0.60},
              {"deploy": 2, "internal": 0.95, "external": 0.61}]

    got = MOD.decisions(points, 0.035)

    assert got[0]["verdict"] == "agree", "still counted"
    assert got[0]["within_noise"] is True, "and still flagged"


def test_decisions_need_consecutive_deploys():
    """A gap is not a change the loop made, so no decision is inferred across it."""
    points = [{"deploy": 1, "internal": 0.90, "external": 0.60},
              {"deploy": 4, "internal": 0.95, "external": 0.66}]

    assert MOD.decisions(points, None) == []


# --- regret --------------------------------------------------------------------------------------

def test_regret_compares_the_submitted_version_with_the_best_one_built():
    external = {1: (0.64, "h1", False), 2: (0.66, "h2", False), 3: (0.62, "h3", True)}

    got = MOD.regret(external)

    assert got["best_deploy"] == 2 and got["best_score"] == 0.66
    assert got["submitted_deploy"] == 3 and got["submitted_score"] == 0.62
    assert got["regret"] == pytest.approx(0.04)


def test_regret_is_none_when_no_capture_is_flagged_as_the_submission():
    """Assuming the last deploy was submitted is wrong exactly when the loop rolled back."""
    external = {1: (0.64, "h1", False), 2: (0.66, "h2", False)}

    assert MOD.regret(external) is None


def test_regret_is_zero_when_the_loop_submitted_its_best():
    external = {1: (0.60, "h1", False), 2: (0.66, "h2", True)}

    assert MOD.regret(external)["regret"] == pytest.approx(0.0)


def test_shipping_a_copy_of_the_best_program_is_exactly_zero_regret():
    """The submission is usually a COPY of an earlier deploy, so deploy-wise regret measures noise.

    Deploy 2 and the submission are the same program and differ only by re-scoring. Comparing them as
    separate candidates reported +0.007 on real data where the true answer is zero -- the loop shipped
    precisely the program it picked.
    """
    external = {1: (0.60, "h1", False), 2: (0.7338, "best", False), 3: (0.7271, "best", True)}

    got = MOD.regret(external)

    assert got["regret"] == 0.0, "shipping the chosen program cannot cost anything"
    assert got["submitted_is_best_program"] is True


def test_regret_averages_every_measurement_of_a_program():
    """A program deployed repeatedly is one candidate, and all its scorings inform its value."""
    external = {1: (0.60, "shipped", False), 2: (0.70, "other", False),
                3: (0.64, "shipped", True)}

    got = MOD.regret(external)

    assert got["submitted_score"] == pytest.approx(0.62), "mean of the shipped program's two scorings"
    assert got["regret"] == pytest.approx(0.08)


def test_a_redeployed_program_is_one_point_not_several(tmp_path):
    """Deploys of one program are repeated measures. Correlating over them lets it vote twice.

    Here program A is deployed three times and B once. Over deploys, A takes part in three of the four
    pairs and dominates the coefficient; over programs there is exactly one A-versus-B comparison, which
    is the only comparison the data actually contains. On a real build this inflation HID the result:
    cci b0's six deploys of three programs reported +0.36 where the programs rank +1.00.
    """
    run = tmp_path / "run"
    for ordinal, (content_hash, ext) in enumerate(
            [("a", 0.60), ("b", 0.70), ("a", 0.61), ("a", 0.59)], start=1):
        write_external(run, ordinal, ext, content_hash=content_hash)
    write_internal(run, {1: 0.90, 2: 0.95, 3: 0.90, 4: 0.90})

    series, _ = MOD.collect([run], None)
    s = series["run"]

    assert s["n"] == 2, "two programs, however many times they were deployed"
    assert s["n_deploys"] == 4
    assert [p["program"] for p in s["programs"]] == ["A", "B"]
    assert s["programs"][0]["deploys"] == [1, 3, 4]
    assert s["programs"][0]["external"] == pytest.approx((0.60 + 0.61 + 0.59) / 3)


def test_restoring_the_incumbent_is_not_a_decision(tmp_path):
    """Rejecting a change means redeploying the previous source, which is not a change to judge.

    Deploy 3 puts program A back after B was rejected. Both deltas are zero by construction, so counting
    it would pad the denominator with a non-event the loop never proposed.
    """
    run = tmp_path / "run"
    for ordinal, (content_hash, ext) in enumerate(
            [("a", 0.60), ("b", 0.70), ("a", 0.60)], start=1):
        write_external(run, ordinal, ext, content_hash=content_hash)
    write_internal(run, {1: 0.90, 2: 0.95, 3: 0.90})

    got = MOD.decisions(
        [{"deploy": o, "internal": i, "external": e, "hash": h}
         for o, i, e, h in [(1, .90, .60, "a"), (2, .95, .70, "b"), (3, .90, .60, "a")]], None)

    assert [d["cycle"] for d in got] == ["1->2"], "only the real change is judged"


# --- deploy lineage ------------------------------------------------------------------------------

def test_a_rolled_back_program_keeps_its_original_letter(tmp_path):
    """Deploys are not a ladder. A repeated program must read as a repeat, not as a new candidate.

    This build is: write A, try B, revert to A, try C, revert to A, ship A. Six deploys, three
    programs, two abandoned experiments -- and one of the abandoned ones may well be the best thing it
    ever built, which is invisible if every deploy looks like a fresh candidate.
    """
    run = tmp_path / "run"
    lineage = [(1, "aaa"), (2, "bbb"), (3, "aaa"), (4, "ccc"), (5, "aaa")]
    for ordinal, content_hash in lineage:
        write_external(run, ordinal, 0.60 + ordinal / 100, content_hash=content_hash)
    write_external(run, 6, 0.64, content_hash="aaa", is_submission=True)
    write_internal(run, {o: 0.9 + o / 1000 for o, _h in lineage})

    series, _ = MOD.collect([run], None)
    every = series["run"]["external_all"]

    assert [e["program"] for e in every] == ["A", "B", "A", "C", "A", "A"]
    assert series["run"]["programs_built"] == 3
    assert series["run"]["shipped_program"] == "A"
    assert series["run"]["programs_abandoned"] == ["B", "C"]


def test_every_scored_deploy_is_plotted_even_without_an_internal_score(tmp_path):
    """The external score exists for all of them; only the internal side has gaps.

    The submitted copy never carries an internal score, and the agent's history skips deploys too --
    one real build had three of nine missing. Plotting only the paired intersection deleted deploys
    that genuinely happened and left the x-axis with holes nothing explained.
    """
    run = tmp_path / "run"
    for ordinal in (1, 2, 3, 4):
        write_external(run, ordinal, 0.5 + ordinal / 100, content_hash=f"h{ordinal}")
    write_internal(run, {1: 0.90, 3: 0.95})          # deploys 2 and 4 have no internal score

    series, _ = MOD.collect([run], None)

    assert [e["deploy"] for e in series["run"]["external_all"]] == [1, 2, 3, 4], "all of them drawn"
    assert [e["has_internal"] for e in series["run"]["external_all"]] == [True, False, True, False]
    assert [p["deploy"] for p in series["run"]["points"]] == [1, 3], "but only pairs are correlated"


# --- skip census ---------------------------------------------------------------------------------

def test_each_missing_half_is_named_rather_than_silently_absent(tmp_path):
    """A run that contributes nothing must say WHICH half it was missing, not vanish from the output."""
    no_external = tmp_path / "no_external"
    write_internal(no_external, {1: 0.85, 2: 0.97})
    (no_external / "checkpoint_scores").mkdir(parents=True, exist_ok=True)

    no_internal = tmp_path / "no_internal"
    write_external(no_internal, 1, 0.64)
    write_external(no_internal, 2, 0.62)

    disjoint = tmp_path / "disjoint"
    write_external(disjoint, 1, 0.64)
    write_external(disjoint, 2, 0.62)
    write_internal(disjoint, {7: 0.85, 8: 0.97})

    _, skipped = MOD.collect([no_external, no_internal, disjoint], None)

    assert skipped[MOD.SKIP_NO_EXTERNAL] == ["no_external"]
    assert skipped[MOD.SKIP_NO_INTERNAL] == ["no_internal"]
    assert skipped[MOD.SKIP_NO_OVERLAP] == ["disjoint"]


# --- what gets published -------------------------------------------------------------------------

def test_published_stats_carry_no_internal_bookkeeping_keys(tmp_path):
    """`stats.json` is a data artefact others read. The tool's own scratch keys must not leak into it."""
    run = tmp_path / "run"
    for ordinal, score in ((1, 0.64), (2, 0.60), (3, 0.66)):
        write_external(run, ordinal, score, content_hash=f"h{ordinal}")
    write_internal(run, {1: 0.85, 2: 0.97, 3: 0.95})
    out = tmp_path / "out"

    assert MOD.main(["--run-dir", str(run), "--out", str(out)]) == 0

    generation = next(out.iterdir())
    stats = json.loads((generation / "stats.json").read_text())
    entry = stats["series"]["run"]
    assert not [k for k in entry if k.startswith("_")], f"leaked bookkeeping keys: {sorted(entry)}"
    assert entry["n"] == 3

    rows = (generation / "points.csv").read_text().strip().splitlines()
    assert rows[0].startswith("run_id,build,deploy,content_hash")
    assert len(rows) == 4


def test_unique_build_indices_keep_their_bare_labels():
    """Outputs for runs that never collided must not change: `cases__b1.png` stays `cases__b1.png`."""
    ids = ["airline_rebooking_claude-opus-4-8_b0_l3_20260910_214941",
           "airline_rebooking_claude-opus-4-8_b1_l3_20260911_023444"]

    assert MOD.assign_labels(ids) == {ids[0]: "b0", ids[1]: "b1"}


def test_a_shared_build_index_is_disambiguated_by_launch_date():
    """Two launches can both be `b0`; left alone, the second overwrites the first's per-case figure."""
    a = "banking_support_claude-opus-4-8_b0_l3_20260920_174913"
    b = "banking_support_claude-opus-4-8_b0_l3_20260921_075749"
    c = "banking_support_claude-opus-4-8_b1_l3_20260921_085532"

    assert MOD.assign_labels([a, b, c]) == {a: "b0-0920", b: "b0-0921", c: "b1"}


def test_same_day_launches_fall_back_to_the_full_stamp():
    a = "banking_support_claude-opus-4-8_b0_l3_20260921_075749"
    b = "banking_support_claude-opus-4-8_b0_l3_20260921_174913"

    labels = MOD.assign_labels([a, b])

    assert labels == {a: "b0-20260921_075749", b: "b0-20260921_174913"}


def test_disambiguated_runs_get_distinct_output_files(tmp_path, monkeypatch):
    """The failure this guards is silent: one figure where there should be two."""
    a = "banking_support_claude-opus-4-8_b0_l3_20260920_174913"
    b = "banking_support_claude-opus-4-8_b0_l3_20260921_075749"
    monkeypatch.setattr(MOD, "_LABELS", MOD.assign_labels([a, b]))

    assert MOD.short_label(a) != MOD.short_label(b)


def test_nothing_to_read_is_an_error_exit_not_an_empty_success(tmp_path):
    """An empty fetch root means the caller's data is not where they think, which must not exit 0."""
    assert MOD.main(["--run-dir", str(tmp_path / "absent"), "--out", str(tmp_path / "out")]) == 1
