"""Grouping the run list so a build and its trials read as one expandable entry.

The failure these guard against is losing runs from the list. A replicate sweep puts 1 + k runs on
this page per build; grouping them wrongly either merges two different builds into one row or drops
trials whose parent is missing, and in both cases a run the operator paid for stops being visible.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from appsci_bench.tracer.server import TracerServer
from shared.observability import eval_report, replicate


def _trace(tid):
    return {"trace_id": tid, "start_time": "t0", "end_time": "t1", "event_count": 1}


def _build(job, cell, index):
    return replicate.record_for(role=replicate.ROLE_BUILD, job_id=job, cell_id=cell,
                                build_index=index)


def _trial(job, cell, build_cell, b, t, build_trace=None):
    return replicate.record_for(role=replicate.ROLE_EVAL_TRIAL, job_id=job, cell_id=cell,
                                depends_on=build_cell, build_index=b, trial_index=t,
                                build_trace_id=build_trace)


def test_a_build_and_its_trials_become_one_entry():
    rows = [
        (_trace("b0"), _build("j", "cb0", 0)),
        (_trace("t00"), _trial("j", "ct00", "cb0", 0, 0)),
        (_trace("t01"), _trial("j", "ct01", "cb0", 0, 1)),
    ]
    out = replicate.group_runs(rows)
    assert len(out) == 1
    g = out[0]
    assert g["kind"] == "group" and g["build"]["trace_id"] == "b0"
    assert [t["trace"]["trace_id"] for t in g["trials"]] == ["t00", "t01"]


def test_two_submissions_of_one_arm_do_not_merge():
    """`cell_id` is job-independent, so both submissions' b0 share one.

    Keyed on the cell alone, both builds and all four trials collapse into a single row that claims
    one build had four trials -- the same ambiguity that doubled k in the pass^k estimator before it
    was job-scoped.
    """
    rows = [
        (_trace("j1-b0"), _build("j1", "cb0", 0)),
        (_trace("j1-t0"), _trial("j1", "ct0", "cb0", 0, 0)),
        (_trace("j2-b0"), _build("j2", "cb0", 0)),
        (_trace("j2-t0"), _trial("j2", "ct0", "cb0", 0, 0)),
    ]
    out = replicate.group_runs(rows)
    assert len(out) == 2, "two submissions are two builds, not one build with two trials"
    assert {g["job_id"] for g in out} == {"j1", "j2"}
    assert all(len(g["trials"]) == 1 for g in out)


def test_trials_whose_build_was_reaped_still_appear():
    """Run dirs are reaped under disk pressure while job records live on.

    If a parentless group were dropped, those trials would vanish from the only page that lists runs
    -- the operator would conclude they never ran.
    """
    out = replicate.group_runs([
        (_trace("t00"), _trial("j", "ct00", "gone", 0, 0)),
        (_trace("t01"), _trial("j", "ct01", "gone", 0, 1)),
    ])
    assert len(out) == 1
    assert out[0]["build"] is None
    assert len(out[0]["trials"]) == 2
    assert out[0]["build_index"] == 0, "the index a trial reports is better than none"


def test_ordinary_runs_are_untouched_and_keep_their_position():
    """Nearly every run on this page predates the replicate axes.

    Order is the caller's (newest first); a group takes the slot of its first-seen member so an
    unrelated run cannot be reordered around it.
    """
    out = replicate.group_runs([
        (_trace("plain-1"), None),
        (_trace("b0"), _build("j", "cb0", 0)),
        (_trace("plain-2"), {"role": replicate.ROLE_BUILD_AND_EVAL}),
        (_trace("t00"), _trial("j", "ct00", "cb0", 0, 0)),
    ])
    kinds = [(e["kind"], e["trace"]["trace_id"] if e["kind"] == "run" else "group") for e in out]
    assert kinds == [("run", "plain-1"), ("group", "group"), ("run", "plain-2")]
    # The trial joined the group that was already open, rather than starting a second one.
    assert len(out[1]["trials"]) == 1


def test_a_trial_that_names_no_build_stays_an_ordinary_row():
    """Grouping it would mean inventing a parent it never named."""
    out = replicate.group_runs([
        (_trace("orphan"), replicate.record_for(role=replicate.ROLE_EVAL_TRIAL, trial_index=0)),
    ])
    assert out == [{"kind": "run", "trace": _trace("orphan")}]


def test_trials_sort_by_index_not_by_arrival():
    """The flat list is ordered by time, and t1 can finish before t0 after a retry."""
    out = replicate.group_runs([
        (_trace("t02"), _trial("j", "c2", "cb", 0, 2)),
        (_trace("t00"), _trial("j", "c0", "cb", 0, 0)),
        (_trace("t01"), _trial("j", "c1", "cb", 0, 1)),
    ])
    assert [t["trace"]["trace_id"] for t in out[0]["trials"]] == ["t00", "t01", "t02"]


# --- rendered -------------------------------------------------------------------------------

def test_the_run_list_renders_the_group_with_every_trial_reachable(tmp_path):
    """End to end through the real template: one parent row, and a row per trial."""
    for tid in ("run_build", "run_t0", "run_t1"):
        (tmp_path / tid).mkdir()
        (tmp_path / tid / "events.jsonl").write_text(
            '{"id": 1, "trace_id": "' + tid + '", "event_type": "x", "source": "s", '
            '"timestamp": "2026-08-25T00:00:00Z", "payload": {}}\n'
        )
    replicate.write_record(tmp_path / "run_build", _build("j", "cb", 0))
    replicate.write_record(tmp_path / "run_t0",
                           _trial("j", "c0", "cb", 0, 0, build_trace="run_build"))
    replicate.write_record(tmp_path / "run_t1",
                           _trial("j", "c1", "cb", 0, 1, build_trace="run_build"))

    html = TestClient(TracerServer(runs_dir=tmp_path, port=0)._create_app()).get("/").text
    # The rendered button, not the string: the JS below the table also names this class in its
    # selectors, so a bare substring count would be 3 no matter how many groups there are.
    assert html.count('class="replicate-toggle"') == 1, "one expandable parent for the build"
    assert html.count('class="replicate-child"') == 2, "a row per trial"
    assert "BUILD b0" in html and "TRIAL b0.t0" in html and "TRIAL b0.t1" in html
    # Every run is still individually openable -- grouping must not cost a way in.
    for tid in ("run_build", "run_t0", "run_t1"):
        assert f"?trace_id={tid}" in html


def test_every_eval_trial_is_linked_on_the_parent_row_itself(tmp_path):
    """Grouping must not be the reason an eval report cannot be found.

    Collapsed-by-default grouped the runs neatly and left every eval invisible on arrival -- worse
    than the flat list it replaced, because this page is the only index of runs there is. Two
    independent guarantees, so neither alone has to carry it: the group renders EXPANDED, and the
    trials are linked on the parent row whether it is open or shut.
    """
    for tid in ("run_build", "run_t0", "run_t1"):
        (tmp_path / tid).mkdir()
        (tmp_path / tid / "events.jsonl").write_text(
            '{"id": 1, "trace_id": "' + tid + '", "event_type": "x", "source": "s", '
            '"timestamp": "2026-08-25T00:00:00Z", "payload": {}}\n'
        )
    replicate.write_record(tmp_path / "run_build", _build("j", "cb", 0))
    replicate.write_record(tmp_path / "run_t0",
                           _trial("j", "c0", "cb", 0, 0, build_trace="run_build"))
    replicate.write_record(tmp_path / "run_t1",
                           _trial("j", "c1", "cb", 0, 1, build_trace="run_build"))

    html = TestClient(TracerServer(runs_dir=tmp_path, port=0)._create_app()).get("/").text

    # 1. Open on arrival.
    assert 'aria-expanded="true"' in html, "a group must not arrive collapsed"
    assert 'aria-expanded="false"' not in html

    # 2. Reachable from the parent row regardless, so a collapsed group is still navigable.
    parent = html[html.index('class="replicate-parent"'):]
    parent = parent[: parent.index("</tr>")]
    assert "eval t0" in parent and "eval t1" in parent
    for tid in ("run_t0", "run_t1"):
        assert f"?trace_id={tid}" in parent, f"{tid} must be linked on the parent row"


def test_the_collapsed_state_is_applied_by_script_not_by_markup(tmp_path):
    """This page works with JavaScript off, so a trial must not be hidden by the HTML itself.

    Server-rendered `hidden` would make every trial unreachable for a reader without JS -- the run
    list is the only index of runs there is.
    """
    (tmp_path / "run_t0").mkdir()
    (tmp_path / "run_t0" / "events.jsonl").write_text(
        '{"id": 1, "trace_id": "run_t0", "event_type": "x", "source": "s", '
        '"timestamp": "2026-08-25T00:00:00Z", "payload": {}}\n'
    )
    replicate.write_record(tmp_path / "run_t0", _trial("j", "c0", "cb", 0, 0))
    html = TestClient(TracerServer(runs_dir=tmp_path, port=0)._create_app()).get("/").text
    child = html[html.index('class="replicate-child"'):]
    child = child[: child.index("</tr>")]
    assert " hidden" not in child, "collapsing belongs to the script, so no-JS still lists the run"
    assert "initReplicateGroups" in html


# --- accuracy + latency on the list ------------------------------------------------------------

def _seed_run(root, tid, *, score=None, per_metric=None, errored=0,
              start="00:00:00", end="00:00:30"):
    d = root / tid
    d.mkdir(parents=True, exist_ok=True)
    (d / "events.jsonl").write_text(
        '{"id": 1, "trace_id": "%s", "event_type": "x", "source": "s", '
        '"timestamp": "2026-08-25T%sZ", "payload": {}}\n'
        '{"id": 2, "trace_id": "%s", "event_type": "x", "source": "s", '
        '"timestamp": "2026-08-25T%sZ", "payload": {}}\n' % (tid, start, tid, end)
    )
    if score is not None or per_metric:
        (d / "eval").mkdir(exist_ok=True)
        (d / "eval" / "report.json").write_text(json.dumps({
            "task_id": "airline", "final_score": score, "warnings": [],
            "results": [{"data_point_id": f"d{i}", "error": "boom" if i < errored else None}
                        for i in range(3)],
            "views": {
                "per_metric": [{"objective": k, "score": v}
                               for k, v in (per_metric or {}).items()],
                "per_data_point": [{"data_point_id": "d0", "score": score}],
            },
        }))
    return d


def test_a_trial_shows_its_score_and_duration(tmp_path):
    """The two things a run list is scanned for.

    Without them the grouped view said only that runs existed -- which is what prompted this: the
    scores were nowhere on the page.
    """
    _seed_run(tmp_path, "t0", score=0.58, per_metric={"task_success": 0.58},
              start="09:33:00", end="09:35:00")
    replicate.write_record(tmp_path / "t0", _trial("j", "c0", "cb", 0, 0))
    html = TestClient(TracerServer(runs_dir=tmp_path, port=0)._create_app()).get("/").text
    assert "0.580" in html
    assert "2m00s" in html, "latency must render as a duration, not raw seconds"
    assert "task_succes" in html, "the per-objective score belongs beside the headline"


def test_a_group_row_carries_its_trials_scores_not_the_builds():
    """A build produces an artifact and scores nothing.

    A group row showing only the build's own numbers shows a dash -- exactly how the scores went
    missing once the trials were nested underneath it.
    """
    agg = eval_report.aggregate([
        {"final_score": 1.0, "n_errored": 0},
        {"final_score": 0.5, "n_errored": 0},
    ])
    assert agg["mean"] == 0.75 and agg["min"] == 0.5 and agg["max"] == 1.0
    assert agg["n_scored"] == 2 and agg["n_total"] == 2


def test_a_mean_over_fewer_trials_than_the_group_has_is_countable():
    """A mean over 1 of 4 trials is a different claim from a mean over 4.

    Both counts are reported so the renderer can mark it; averaging whatever happened to be scored
    without saying so is how a partly-failed group looks like a complete measurement.
    """
    agg = eval_report.aggregate([{"final_score": 1.0, "n_errored": 0}, None, None, None])
    assert agg["n_scored"] == 1 and agg["n_total"] == 4
    assert agg["mean"] == 1.0, "the mean is over what was actually scored"


def test_an_unscored_run_is_not_rendered_as_zero(tmp_path):
    """None means "not measured"; 0.0 means "measured, and it failed".

    Collapsing them puts a plausible-looking total failure in the table for a run that was never
    scored -- and a BUILD run is never scored, by design.
    """
    assert eval_report.aggregate([None, None])["mean"] is None
    _seed_run(tmp_path, "b0")          # no eval report at all
    replicate.write_record(tmp_path / "b0", _build("j", "cb", 0))
    html = TestClient(TracerServer(runs_dir=tmp_path, port=0)._create_app()).get("/").text
    assert "0.000" not in html, "an unscored run must not render as a zero score"


def test_a_corrupt_report_costs_the_score_and_not_the_page(tmp_path):
    """This file lives in a run dir the benchmark subject writes, so the list must still render.

    The nesting depth is the shape that raises `RecursionError` rather than `JSONDecodeError`, which
    is the case that once killed a whole drain.
    """
    d = _seed_run(tmp_path, "t0")
    (d / "eval").mkdir(exist_ok=True)
    (d / "eval" / "report.json").write_text("[" * 100000 + "]" * 100000)
    r = TestClient(TracerServer(runs_dir=tmp_path, port=0)._create_app()).get("/")
    assert r.status_code == 200
    assert "t0" in r.text


def test_the_report_is_parsed_once_per_stat(tmp_path):
    """The list reloads every 10s, and a finished report never changes.

    Without the stat-keyed cache this page re-parses every report on every reload -- 1.26 MB of JSON
    every 10 seconds on the current jobs host, growing with the run count.
    """
    _seed_run(tmp_path, "t0", score=0.5, per_metric={"task_success": 0.5})
    calls: list[str] = []
    real = eval_report.read_report

    def counting(run_dir):
        calls.append(str(run_dir))
        return real(run_dir)

    eval_report.read_report = counting
    try:
        eval_report._cache.clear()
        for _ in range(5):
            eval_report.cached_headline(tmp_path / "t0")
    finally:
        eval_report.read_report = real
        eval_report._cache.clear()
    assert len(calls) == 1, f"parsed {len(calls)} times for an unchanged report"


def test_the_cache_survives_a_full_scan_of_more_runs_than_it_was_sized_for():
    """The caller scans EVERY run per render, which defeats a small bounded cache completely.

    Measured on the shipped version at cap 512 with 700 runs: 700 parses per render, every render, so
    the docstring's "one parse per run" was false above the cap. A bounded cache under a full
    sequential scan larger than itself has a 0% hit rate whatever it evicts -- what it drops is what
    the next render asks for first.
    """
    import pathlib

    parsed: list[str] = []
    real = eval_report.read_report
    orig_stat = pathlib.Path.stat

    class _Stat:
        st_mtime, st_size = 1.0, 10

    eval_report.read_report = lambda d: (parsed.append(str(d)), {"final_score": 1.0})[1]
    pathlib.Path.stat = lambda self, **k: _Stat()
    try:
        eval_report._cache.clear()
        runs = [pathlib.Path(f"/runs/r{i:04d}") for i in range(700)]
        for _ in range(2):
            for r in runs:
                eval_report.cached_headline(r)
        # 700 for the first render, then warm: anything near 1400 means the cache is not working.
        assert len(parsed) <= 800, f"{len(parsed)} parses over two renders of 700 runs"
    finally:
        pathlib.Path.stat = orig_stat
        eval_report.read_report = real
        eval_report._cache.clear()


def test_a_build_record_missing_its_cell_id_is_not_silently_dropped():
    """Two such records collapsed to the key ("", "") and the earlier one VANISHED from the list.

    The build slot is assigned unconditionally, so the second record overwrote the first -- not
    degraded to an ungrouped row, gone. Unreachable from the worker (it always writes a cell id) but
    reachable from the S3-pulled or foreign-version input this module is documented to expect, and
    losing a run from the only index of runs is the worst outcome available here.
    """
    out = replicate.group_runs([
        ({"trace_id": "first"}, {"role": replicate.ROLE_BUILD}),
        ({"trace_id": "second"}, {"role": replicate.ROLE_BUILD}),
    ])
    ids = {e["trace"]["trace_id"] for e in out if e["kind"] == "run"}
    ids |= {e["build"]["trace_id"] for e in out if e["kind"] == "group" and e.get("build")}
    assert ids == {"first", "second"}, f"a run disappeared: {ids}"


def test_a_naive_aware_timestamp_pair_does_not_take_the_whole_page_down():
    """`_event_span_seconds` runs per run while building the list, outside any per-run guard.

    A run whose stamps disagree about tzinfo parses fine and then raises `TypeError` on the
    subtraction, which would 500 the entire run list rather than costing that one run its duration.
    """
    from appsci_bench.tracer.server import _event_span_seconds

    assert _event_span_seconds("2026-01-01T00:00:00", "2026-01-01T00:00:05+00:00") is None
    assert _event_span_seconds("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00") is None
    assert _event_span_seconds("2026-01-01T00:00:00Z", "2026-01-01T00:00:05Z") == 5.0


def test_the_run_list_stylesheet_carries_a_cache_buster(tmp_path):
    """Without `?v=`, a stylesheet change reaches this page only after a manual cache clear -- and
    this is the page the replicate badge and grouping styles are on."""
    (tmp_path / "r0").mkdir()
    html = TestClient(TracerServer(runs_dir=tmp_path, port=0)._create_app()).get("/").text
    assert "styles.css?v=" in html, "the run list must bust its stylesheet cache like the dashboard"
