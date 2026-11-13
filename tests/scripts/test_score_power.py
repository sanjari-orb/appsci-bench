"""The score^k metric (scripts/score_power.py).

Loaded by path like its siblings here. Nothing reaches S3, Docker or a model: every run is a
directory written into `tmp_path`, and the one test that exercises the mirror replaces
`subprocess.run`.

The buckets are the caller's, and that is the point these tests protect. This tool does no grouping,
no run-id parsing and no build-link resolution, so the failure mode it used to have -- attributing a
trial to the wrong build and reporting a real-looking number for a set of builds that never existed
-- is gone by construction. What is left to get wrong is the arithmetic, and whether a trial that
cannot be read is noticed rather than quietly dropped, since dropping one silently changes which
builds the number is about.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import pathlib
import statistics
import subprocess
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "score_power.py"
_MODNAME = "_score_power_under_test"

#: The airline claude-opus-4-8 arm's headline scores, one list per build, read from the run mirror.
#: Real numbers so the hand-computed expectations below are checkable against runs anyone can pull.
OPUS48_AIRLINE = [[0.58, 0.54, 0.54], [0.70, 0.62, 0.60], [0.64, 0.64, 0.64]]


def _load():
    """Load the script by path, REGISTERING it in `sys.modules` first.

    The registration is load-bearing: `@dataclass` resolves its annotations through
    `sys.modules[cls.__module__]`, so a module that is not registered fails at class-definition time
    with `'NoneType' object has no attribute '__dict__'` rather than anywhere near the cause.
    """
    spec = importlib.util.spec_from_file_location(_MODNAME, _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODNAME] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def script():
    return _load()


def write_run(root: pathlib.Path, run_id: str, cases: list[float] | None, *,
              score: float | None = None, partial: bool = False,
              scored_of: tuple[int, int] | None = None, report: bool = True) -> str:
    """One run directory shaped the way the orchestrator leaves it. Returns the run id."""
    if report:
        views: dict = {}
        if cases is not None:
            views["per_data_point"] = [{"data_point_id": f"case_{i}", "score": s}
                                       for i, s in enumerate(cases)]
        headline = score if score is not None else (
            statistics.fmean(cases) if cases else 0.0)
        payload: dict = {"final_score": headline, "partial": partial, "views": views}
        if scored_of:
            payload["cases_scored"], payload["cases_total"] = scored_of
        (root / run_id / "eval").mkdir(parents=True, exist_ok=True)
        (root / run_id / "eval" / "report.json").write_text(json.dumps(payload), encoding="utf-8")
    return run_id


def arm(root: pathlib.Path, per_build: list[list[float]], cases: int = 4) -> list[list[str]]:
    """One bucket per build, each trial scoring its headline on every example."""
    buckets = []
    for b, scores in enumerate(per_build):
        ids = [write_run(root, f"b{b}.t{t}", [s] * cases, score=s) for t, s in enumerate(scores)]
        buckets.append(ids)
    return buckets


def run_cli(script, tmp_path, buckets: list[list[str]], *extra) -> tuple[int, dict | None]:
    out = tmp_path / "out.json"
    argv = []
    for ids in buckets:
        argv += ["--build", *ids]
    code = script.main([*argv, "--runs-dir", str(tmp_path / "runs"),
                        "--cache", str(tmp_path / "cache"), "--json", str(out), *extra])
    return code, (json.loads(out.read_text()) if out.exists() else None)


# ----------------------------------------------------------------------------- the arithmetic

def test_score_is_the_mean_of_the_combination_values(script):
    """The definition, checked against an independent enumeration: for each of the 27 ways of
    keeping one trial per build, the per-example minimum averaged over the examples."""
    vectors = [[[1.0, 0.0, 1.0], [1.0, 1.0, 0.0]],
               [[0.0, 1.0, 1.0], [1.0, 1.0, 1.0]],
               [[1.0, 1.0, 1.0], [0.0, 0.0, 1.0]]]
    expected = statistics.fmean(
        statistics.fmean(min(pick[b][c] for b in range(3)) for c in range(3))
        for pick in itertools.product(*vectors))
    out = script.score_power(vectors)
    assert out["score"] == pytest.approx(expected)
    assert out["k"] == 3 and out["n_combinations"] == 8 and out["n_examples"] == 3


def test_the_minimum_is_taken_example_by_example_not_over_trial_totals(script):
    """The whole reason this works per example. Three builds each get two of four examples right,
    on DISJOINT halves: every build scores 0.5, and nothing survives all three."""
    vectors = [[[1.0, 1.0, 0.0, 0.0]], [[0.0, 0.0, 1.0, 1.0]], [[1.0, 0.0, 1.0, 0.0]]]
    assert script.score_power(vectors)["score"] == pytest.approx(0.0)


def test_the_std_is_over_the_combination_values(script):
    vectors = [[[1.0, 0.0], [0.0, 0.0]], [[1.0, 1.0], [1.0, 0.0]]]
    values = [statistics.fmean(min(a[c], b[c]) for c in range(2))
              for a, b in itertools.product(*vectors)]
    out = script.score_power(vectors)
    assert out["std"] == pytest.approx(statistics.pstdev(values))
    assert out["std"] != pytest.approx(statistics.stdev(values))  # population, not sample
    assert (out["lowest"], out["highest"]) == (pytest.approx(min(values)),
                                               pytest.approx(max(values)))


def test_the_per_example_values_average_to_the_score(script):
    """They are the decomposition of it: which examples the builds lose, not how much."""
    vectors = [[[1.0, 0.0, 0.5]], [[1.0, 1.0, 0.25]], [[0.5, 1.0, 1.0]]]
    out = script.score_power(vectors)
    assert out["per_example"] == pytest.approx([0.5, 0.0, 0.25])
    assert out["score"] == pytest.approx(statistics.fmean(out["per_example"]))


def test_every_headline_has_a_per_example_column_that_averages_to_it(script, tmp_path):
    """Three headlines, three vectors, each the decomposition of its own number. A column whose mean
    is not its headline would be the wrong reduction of the right array -- which looks entirely
    plausible in a table and is the reason this is checked rather than asserted."""
    runs = tmp_path / "runs"
    write_run(runs, "a0", [1.0, 0.0, 1.0, 0.5])
    write_run(runs, "a1", [1.0, 1.0, 0.0, 1.0])
    write_run(runs, "b0", [0.0, 1.0, 1.0, 1.0])
    write_run(runs, "b1", [1.0, 1.0, 1.0, 0.25])
    code, payload = run_cli(script, tmp_path, [["a0", "a1"], ["b0", "b1"]])
    assert code == script.EXIT_OK
    per_example = payload["per_example"]
    assert len(per_example) == 4
    assert all(set(v) == {"score", "one_draw", "worst"} for v in per_example.values())
    for column, headline in (("score", "score"), ("one_draw", "random_pick"), ("worst", "worst")):
        mean = statistics.fmean(v[column] for v in per_example.values())
        assert mean == pytest.approx(payload[headline]), column


def test_score_falls_as_more_builds_are_added(script):
    """k is not a free parameter: the worst of three draws is worse than the worst of two."""
    two = script.score_power([[[1.0, 0.0]], [[0.0, 1.0]]])
    three = script.score_power([[[1.0, 0.0]], [[0.0, 1.0]], [[1.0, 1.0]]])
    assert three["score"] <= two["score"]


def test_too_many_combinations_is_refused_rather_than_enumerated(script):
    with pytest.raises(ValueError, match="combinations"):
        script.score_power([[[1.0], [0.0]]] * 21)  # 2^21, past the cap


def test_trials_covering_different_example_counts_are_refused(script):
    """`align` pads to the union before this is called, so a mismatch here means the caller built
    the vectors itself and got them wrong -- which must not average silently."""
    with pytest.raises(ValueError, match="same examples"):
        script.score_power([[[1.0, 0.0]], [[1.0]]])


# ----------------------------------------------------------------------------- worst of all

def test_worst_is_the_per_example_minimum_over_every_trial(script):
    """No combinations and no keeping one per build: the floor the whole set reached."""
    vectors = [[[1.0, 0.0, 1.0], [1.0, 1.0, 1.0]], [[1.0, 1.0, 0.5], [0.0, 1.0, 1.0]]]
    flat = [v for trials in vectors for v in trials]
    expected = statistics.fmean(min(v[c] for v in flat) for c in range(3))
    assert script.score_power(vectors)["worst"] == pytest.approx(expected)
    assert script.score_power(vectors)["n_trials"] == 4


def test_worst_is_at_or_below_every_combination(script):
    """Taking the minimum over more trials cannot raise it, so it bounds score^k from below. If this
    ever inverted, one of the two reductions would be reading the wrong axis."""
    vectors = [[[1.0, 0.0], [0.0, 1.0]], [[1.0, 1.0], [0.5, 0.0]], [[1.0, 0.5], [1.0, 1.0]]]
    out = script.score_power(vectors)
    assert out["worst"] <= out["lowest"] <= out["score"] <= out["highest"]


def test_worst_ignores_how_the_trials_are_bucketed(script):
    """It is a statement about the trials, not about the builds: the same nine trials split three
    ways or nine ways give the same floor, while score^k does not."""
    flat = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [0.5, 1.0]]
    paired = script.score_power([[flat[0], flat[1]], [flat[2], flat[3]]])
    apart = script.score_power([[v] for v in flat])
    assert paired["worst"] == pytest.approx(apart["worst"])
    assert paired["score"] != pytest.approx(apart["score"])


def test_a_build_that_produced_nothing_drags_worst_to_zero(script, tmp_path):
    runs = tmp_path / "runs"
    write_run(runs, "a", [1.0, 1.0])
    code, payload = run_cli(script, tmp_path, [["a"], []])
    assert code == script.EXIT_OK
    assert payload["worst"] == pytest.approx(0.0)


# ----------------------------------------------------------------------------- the single draw

def test_the_draw_is_one_of_the_enumerated_combinations(script):
    """Not a recomputation and not a resample: `one draw` is one of the same combination values
    score^k averages, so it can never fall outside their range."""
    vectors = [[[1.0, 0.0], [0.0, 0.0]], [[1.0, 1.0], [1.0, 0.0]], [[0.5, 1.0], [1.0, 1.0]]]
    values = [statistics.fmean(min(pick[b][c] for b in range(3)) for c in range(2))
              for pick in itertools.product(*vectors)]
    out = script.score_power(vectors)
    assert out["random_pick"] in [pytest.approx(v) for v in values]
    assert out["lowest"] <= out["random_pick"] <= out["highest"]


def test_the_draw_matches_the_trials_it_says_it_picked(script):
    """The picked indices are reported so the number can be re-derived by hand. If they did not
    match the value, the draw would be unauditable -- which for a single-sample figure is fatal."""
    vectors = [[[1.0, 1.0], [0.0, 0.0]], [[1.0, 0.0], [0.0, 1.0]]]
    out = script.score_power(vectors)
    picked = [vectors[b][t] for b, t in enumerate(out["random_pick_trials"])]
    by_hand = statistics.fmean(min(v[c] for v in picked) for c in range(2))
    assert out["random_pick"] == pytest.approx(by_hand)


def test_the_draw_is_the_same_on_every_run_and_moves_with_the_seed(script):
    """A number people quote must not move on its own; asking for a different draw is what --seed
    is for."""
    vectors = [[[float(t)] for t in range(9)], [[float(t)] for t in range(9)]]
    first = script.score_power(vectors)
    assert script.score_power(vectors)["random_pick_trials"] == first["random_pick_trials"]
    others = {tuple(script.score_power(vectors, seed=s)["random_pick_trials"]) for s in range(20)}
    assert len(others) > 1


def test_with_one_trial_per_build_the_draw_is_the_score(script):
    """There is only one combination, so the expectation and the single draw coincide."""
    out = script.score_power([[[1.0, 0.0]], [[1.0, 1.0]]])
    assert out["random_pick"] == pytest.approx(out["score"])
    assert out["std"] == pytest.approx(0.0)


def test_the_drawn_run_ids_are_reported(script, tmp_path):
    runs = tmp_path / "runs"
    write_run(runs, "a0", [1.0, 1.0])
    write_run(runs, "a1", [0.0, 1.0])
    write_run(runs, "b0", [1.0, 0.0])
    code, payload = run_cli(script, tmp_path, [["a0", "a1"], ["b0"]])
    assert code == script.EXIT_OK
    assert payload["random_pick_run_ids"][1] == "b0"
    assert payload["random_pick_run_ids"][0] in ("a0", "a1")


def test_a_build_that_produced_nothing_is_still_drawn_from(script, tmp_path):
    """An empty bucket holds the one synthetic all-zero trial, so the draw includes it and lands at
    0 -- the same thing score^k does, on a single sample."""
    runs = tmp_path / "runs"
    write_run(runs, "a", [1.0, 1.0])
    code, payload = run_cli(script, tmp_path, [["a"], []])
    assert code == script.EXIT_OK
    assert payload["random_pick"] == pytest.approx(0.0)
    assert payload["random_pick_run_ids"] == ["a", None]


# ----------------------------------------------------------------------------- the average

def test_the_average_is_the_mean_of_the_build_means(script, tmp_path):
    """avg(avg(b0 trials), avg(b1 trials), avg(b2 trials)) -- each build counting once, whatever
    number of trials it carries."""
    buckets = [[script.Trial(f"b{b}.t{t}", "disk", s, {}) for t, s in enumerate(scores)]
               for b, scores in enumerate(OPUS48_AIRLINE)]
    assert script.build_means(buckets) == pytest.approx([0.5533333, 0.64, 0.64], abs=1e-6)
    assert script.average(buckets) == pytest.approx(0.6111111, abs=1e-6)


def test_the_across_build_spread_is_over_the_build_means(script):
    """The quantity score^k exists to charge for, shown on its own: how far the builds sit from the
    average, before any minimum is taken. A population spread, since these builds are the whole
    thing being measured rather than a sample of some larger set."""
    buckets = [[script.Trial(f"b{b}.t{t}", "disk", s, {}) for t, s in enumerate(scores)]
               for b, scores in enumerate(OPUS48_AIRLINE)]
    means = script.build_means(buckets)
    std, variance = script.across_build_spread(buckets)
    assert std == pytest.approx(statistics.pstdev(means))
    assert variance == pytest.approx(statistics.pvariance(means))
    assert std == pytest.approx(0.0409, abs=1e-4)
    assert std != pytest.approx(statistics.stdev(means))  # population, not sample


def test_builds_that_agree_have_no_across_build_spread(script):
    """Zero is a real answer: three builds that score the same leave nothing for score^k to charge,
    and the average sits where it would anyway."""
    buckets = [[script.Trial("a", "disk", 0.7, {})], [script.Trial("b", "disk", 0.7, {})]]
    assert script.across_build_spread(buckets) == (0.0, 0.0)


def test_the_across_build_spread_is_in_the_json(script, tmp_path):
    runs = tmp_path / "runs"
    write_run(runs, "a", [1.0, 1.0], score=1.0)
    write_run(runs, "b", [0.0, 0.0], score=0.0)
    code, payload = run_cli(script, tmp_path, [["a"], ["b"]])
    assert code == script.EXIT_OK
    assert payload["average"] == pytest.approx(0.5)
    assert payload["average_std_across_builds"] == pytest.approx(0.5)
    assert payload["average_variance_across_builds"] == pytest.approx(0.25)


def test_the_average_is_not_the_pooled_trial_mean(script):
    """With unequal trial counts the two part company, and the build-weighted one is the answer
    score^k is read against."""
    buckets = [[script.Trial("a", "disk", 0.9, {}), script.Trial("b", "disk", 0.9, {})],
               [script.Trial("c", "disk", 0.3, {})]]
    assert script.average(buckets) == pytest.approx(0.6)
    pooled = statistics.fmean([0.9, 0.9, 0.3])
    assert script.average(buckets) != pytest.approx(pooled)


# ----------------------------------------------------------------------------- buckets as given

def test_the_buckets_are_used_exactly_as_given(script, tmp_path):
    """No grouping, no run-id parsing, no build links. Two trials the caller puts in separate
    buckets are two builds, whatever their names say."""
    runs = tmp_path / "runs"
    write_run(runs, "b0.t0", [1.0, 1.0])
    write_run(runs, "b0.t1", [0.0, 0.0])
    code, payload = run_cli(script, tmp_path, [["b0.t0"], ["b0.t1"]])
    assert code == script.EXIT_OK
    assert payload["k"] == 2
    assert payload["score"] == pytest.approx(0.0)
    assert [b["run_ids"] for b in payload["builds"]] == [["b0.t0"], ["b0.t1"]]


def test_a_build_that_produced_nothing_is_an_empty_bucket(script, tmp_path, capsys):
    """The zero rule, and the only way to express it now: an empty bucket scores 0 on every example,
    so score^k goes to the floor while the average stays well above it."""
    runs = tmp_path / "runs"
    buckets = arm(runs, [[0.9, 0.9], [0.9]]) + [[]]
    code, payload = run_cli(script, tmp_path, buckets)
    assert code == script.EXIT_OK
    assert payload["k"] == 3
    assert payload["score"] == pytest.approx(0.0)
    assert payload["average"] == pytest.approx(0.6)  # (0.9 + 0.9 + 0) / 3
    assert payload["builds"][2] == {"run_ids": [], "scores": [], "mean": 0.0}
    assert "no trials given" in capsys.readouterr().out


def test_every_bucket_empty_scores_zero(script, tmp_path):
    code, payload = run_cli(script, tmp_path, [[], [], []])
    assert code == script.EXIT_OK
    assert payload["k"] == 3
    assert (payload["score"], payload["average"]) == (0.0, 0.0)


def test_no_build_at_all_is_refused(script, tmp_path, capsys):
    code, _ = run_cli(script, tmp_path, [])
    assert code == script.EXIT_NO_BUILD
    assert "no build given" in capsys.readouterr().err


# ----------------------------------------------------------------------------- reading runs

@pytest.mark.parametrize("kwargs,reason", [
    ({"report": False}, "no eval report"),
    ({"cases": None}, "no per-example scores"),
    ({"partial": True}, "partial"),
    ({"scored_of": (12, 50)}, "12 of 50"),
])
def test_a_trial_that_cannot_be_used_stops_the_run_by_name(script, tmp_path, capsys, monkeypatch,
                                                           kwargs, reason):
    """Dropping it instead would silently change which builds the number is about. The caller is
    told which run and why, and can drop it or empty that bucket themselves."""
    # A run with no report on disk would otherwise be looked for in the mirror; here its absence is
    # the case under test, so the fetch is a no-op and the cache stays empty.
    monkeypatch.setattr(script, "fetch_run", lambda *a, **kw: None)
    runs = tmp_path / "runs"
    cases = kwargs.pop("cases", [1.0, 1.0])
    write_run(runs, "good", [1.0, 1.0])
    write_run(runs, "bad", cases, **kwargs)
    code, payload = run_cli(script, tmp_path, [["good"], ["bad"]])
    assert code == script.EXIT_UNREADABLE
    assert payload is None
    err = capsys.readouterr().err
    assert "bad: " in err and reason in err


def test_a_run_id_that_is_not_one_never_reaches_the_mirror(script, tmp_path, capsys, monkeypatch):
    """The id is interpolated into an S3 key and handed to a subprocess, so it is checked first."""
    monkeypatch.setattr(script, "fetch_run",
                        lambda *a, **kw: pytest.fail("the mirror must not be read here"))
    code, _ = run_cli(script, tmp_path, [["../../etc/passwd"]])
    assert code == script.EXIT_UNREADABLE
    assert "not a run id" in capsys.readouterr().err


def test_a_score_of_zero_is_a_measurement_not_a_gap(script, tmp_path):
    """0.0 is a real score. An arm that fails every example is exactly what this metric is for."""
    runs = tmp_path / "runs"
    write_run(runs, "a", [0.0, 0.0], score=0.0)
    write_run(runs, "b", [0.0, 0.0], score=0.0)
    code, payload = run_cli(script, tmp_path, [["a"], ["b"]])
    assert code == script.EXIT_OK
    assert payload["score"] == 0.0 and payload["average"] == 0.0


def test_trials_covering_different_examples_are_aligned_and_flagged(script, tmp_path):
    """A trial missing an example another trial scored counts as 0 on it, rather than defining a
    smaller eval. Scoring each trial against its own list would compare builds on different work."""
    runs = tmp_path / "runs"
    write_run(runs, "wide", [1.0, 1.0, 1.0])
    write_run(runs, "narrow", [1.0, 1.0])
    code, payload = run_cli(script, tmp_path, [["wide"], ["narrow"]])
    assert code == script.EXIT_OK
    assert payload["n_examples"] == 3
    assert payload["score"] == pytest.approx(2 / 3)
    assert any("cover fewer" in w for w in payload["warnings"])


# ----------------------------------------------------------------------------- the JSON input

def test_buckets_can_be_given_as_json(script, tmp_path):
    runs = tmp_path / "runs"
    write_run(runs, "a", [1.0])
    write_run(runs, "b", [0.0])
    out = tmp_path / "out.json"
    code = script.main([json.dumps([["a"], ["b"]]), "--runs-dir", str(runs),
                        "--cache", str(tmp_path / "cache"), "--json", str(out)])
    assert code == script.EXIT_OK
    assert json.loads(out.read_text())["score"] == pytest.approx(0.0)


@pytest.mark.parametrize("text", ['{"a": 1}', "[]", '["a","b"]', "[[1,2]]", "not json"])
def test_a_malformed_json_argument_is_refused_by_shape(script, text):
    with pytest.raises(ValueError):
        script.parse_buckets(text)


def test_json_and_build_flags_together_are_refused(script, tmp_path, capsys):
    code = script.main(["[[\"a\"]]", "--build", "b", "--runs-dir", str(tmp_path)])
    assert code == script.EXIT_NO_BUILD
    assert "not both" in capsys.readouterr().err


# ----------------------------------------------------------------------------- the mirror

def _fake_aws(returncode: int, stderr: str = ""):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)
    return run


def test_a_missing_object_is_an_absence_not_a_failure(script, tmp_path, monkeypatch):
    monkeypatch.setattr(script.subprocess, "run", _fake_aws(
        1, 'fatal error: An error occurred (404) when calling the HeadObject operation: Key '
           '"runs/x/eval/report.json" does not exist'))
    script.fetch_run("x", tmp_path / "x", "b", "runs", refresh=False)
    assert not script.pending(tmp_path / "x", refresh=False)  # remembered, not re-fetched


def test_any_other_mirror_failure_stops_the_tool(script, tmp_path, monkeypatch, capsys):
    """An expired credential fails every fetch. Absorbed as missing reports it would empty the
    corpus and print a confident metric over whatever survived."""
    monkeypatch.setattr(script.subprocess, "run", _fake_aws(
        1, "An error occurred (ExpiredToken) when calling the GetObject operation"))
    code = script.main(["--build", "a", "--runs-dir", str(tmp_path / "absent"),
                        "--cache", str(tmp_path / "cache")])
    assert code == script.EXIT_MIRROR
    assert "mirror could not be read" in capsys.readouterr().err
