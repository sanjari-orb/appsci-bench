"""The replicate record that links a build trace to its trials, and the tracer route serving it.

A `builds: n` / `trials: k` job splits one run into 1 + k runs, each holding half the story: the build
trace has agent turns and no eval, a trial trace has an eval and no build phase. These pin the
navigation that makes that split readable, and the failure modes that would otherwise show a reader
something untrue -- a badge with no role, a link to a build that is not on this host, a corrupt file
taking the whole trace page down with it.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from appsci_bench.tracer.server import TracerServer
from shared.observability import replicate


def _client(runs_dir):
    return TestClient(TracerServer(runs_dir=runs_dir, port=0)._create_app())


# --- the record itself ------------------------------------------------------------------------

def test_a_trial_record_names_the_build_trace_it_scored(tmp_path):
    """The whole point: from a trial you can get back to the build whose artifact it scored."""
    run = tmp_path / "trial-run"
    payload = replicate.record_for(
        role=replicate.ROLE_EVAL_TRIAL, job_id="j-1", cell_id="c-trial", label="arm#b0.t1",
        build_index=0, trial_index=1, depends_on="c-build", build_trace_id="airline_20260825_1",
    )
    assert replicate.write_record(run, payload) is not None

    back = replicate.read_record(run)
    assert back["role"] == replicate.ROLE_EVAL_TRIAL
    assert back["build_trace_id"] == "airline_20260825_1"
    assert back["depends_on"] == "c-build"
    assert replicate.short_label(back) == "TRIAL b0.t1"


def test_index_zero_survives_the_round_trip(tmp_path):
    """b0 and t0 are the FIRST build and trial, not absent ones.

    A truthiness test on either index drops them, and the badge then reads "TRIAL" for b0.t0 while
    reading "TRIAL b1.t1" for its sibling -- so the one run you cannot identify is the first one.
    """
    run = tmp_path / "zero"
    replicate.write_record(run, replicate.record_for(
        role=replicate.ROLE_EVAL_TRIAL, build_index=0, trial_index=0,
    ))
    back = replicate.read_record(run)
    assert back["build_index"] == 0 and back["trial_index"] == 0
    assert replicate.short_label(back) == "TRIAL b0.t0"


def test_a_build_record_carries_no_trial_index(tmp_path):
    """`trial_index` on a build would render as "BUILD b0.t0", inventing a trial that is not there."""
    run = tmp_path / "build"
    replicate.write_record(run, replicate.record_for(
        role=replicate.ROLE_BUILD, build_index=2, trial_index=7,
    ))
    back = replicate.read_record(run)
    assert "trial_index" not in back
    assert replicate.short_label(back) == "BUILD b2"


def test_an_ordinary_run_is_deliberately_unbadged(tmp_path):
    """Every run before the replicate axes existed is `build_and_eval`.

    Badging them "BUILD+EVAL" would put a label on every historical trace to say nothing
    distinguishes it, so `short_label` returns empty and the UI renders no badge.
    """
    assert replicate.short_label({"role": replicate.ROLE_BUILD_AND_EVAL}) == ""


def test_a_record_with_no_role_is_refused_rather_than_written(tmp_path):
    """A role-less record renders as an unlabelled badge, which is worse than no badge at all."""
    run = tmp_path / "roleless"
    run.mkdir()
    assert replicate.write_record(run, {"job_id": "j-1"}) is None
    assert not (run / replicate.REPLICATE_FILENAME).exists()
    assert replicate.read_record(run) is None


def test_absent_and_corrupt_records_both_read_as_none(tmp_path):
    """A run directory is not a trusted input: it may be pulled from S3 or written by another version.

    Absent is the overwhelmingly common case and must not warn loudly; corrupt must cost the badge and
    nothing else. Neither may raise, because the tracer calls this while rendering a page.
    """
    missing = tmp_path / "no-record"
    missing.mkdir()
    assert replicate.read_record(missing) is None

    for junk in ("not json at all", "[1, 2, 3]", '"a string"', '{"role": ""}', "{}"):
        bad = tmp_path / f"bad-{abs(hash(junk))}"
        bad.mkdir()
        (bad / replicate.REPLICATE_FILENAME).write_text(junk)
        assert replicate.read_record(bad) is None, junk


def test_hostile_values_are_clamped_and_type_checked(tmp_path):
    """These values reach the tracer's UI, so an unbounded or wrong-typed one is a rendering problem.

    `True` is specifically excluded from the indices: `bool` is an `int` in Python, so an unchecked
    isinstance would let a badge render as "bTrue".
    """
    run = tmp_path / "hostile"
    run.mkdir()
    (run / replicate.REPLICATE_FILENAME).write_text(json.dumps({
        "role": "eval_trial",
        "label": "L" * 5000,
        "build_trace_id": "T" * 5000,
        "build_index": True,
        "trial_index": "three",
    }))
    back = replicate.read_record(run)
    assert len(back["label"]) == 256
    assert len(back["build_trace_id"]) == 256
    assert "build_index" not in back, "a bool index would render as bTrue"
    assert "trial_index" not in back, "a string index is not an index"
    # Still labelled, just without indices it does not have.
    assert replicate.short_label(back) == "TRIAL"


def test_writing_twice_replaces_rather_than_appends(tmp_path):
    """The worker writes once when the trace id appears and again before the S3 mirror."""
    run = tmp_path / "twice"
    first = replicate.record_for(role=replicate.ROLE_BUILD, build_index=0)
    replicate.write_record(run, first)
    replicate.write_record(run, first)
    assert replicate.read_record(run) == {"role": "build", "build_index": 0}
    # No temp files left behind by either write.
    assert [p.name for p in run.iterdir()] == [replicate.REPLICATE_FILENAME]


# --- the tracer route ------------------------------------------------------------------------

def test_the_route_reports_a_reachable_build_link(tmp_path):
    """`sibling_available` is answered server-side because only that side can see the runs dir."""
    (tmp_path / "build-tr").mkdir()
    replicate.write_record(tmp_path / "trial-tr", replicate.record_for(
        role=replicate.ROLE_EVAL_TRIAL, build_index=0, trial_index=1,
        build_trace_id="build-tr",
    ))
    body = _client(tmp_path).get("/api/replicate/trial-tr").json()
    assert body["badge"] == "TRIAL b0.t1"
    assert body["replicate"]["build_trace_id"] == "build-tr"
    assert body["sibling_available"] is True


def test_a_build_whose_run_dir_is_gone_is_not_offered_as_a_link(tmp_path):
    """A reaped build must render as text, not as a link that 404s.

    Run dirs are reaped under disk pressure while the job records live on, so a trial outliving its
    build's directory is the expected steady state on a busy host, not an edge case.
    """
    replicate.write_record(tmp_path / "trial-tr", replicate.record_for(
        role=replicate.ROLE_EVAL_TRIAL, build_index=0, trial_index=0,
        build_trace_id="reaped-build",
    ))
    body = _client(tmp_path).get("/api/replicate/trial-tr").json()
    assert body["replicate"]["build_trace_id"] == "reaped-build"
    assert body["sibling_available"] is False


def test_a_run_with_no_record_returns_null_not_an_error(tmp_path):
    """Most runs have no replicate identity; the tracer must render them exactly as before."""
    (tmp_path / "plain").mkdir()
    body = _client(tmp_path).get("/api/replicate/plain").json()
    assert body["replicate"] is None
    assert body["badge"] == ""
    assert body["sibling_available"] is False


def test_a_traversing_build_trace_id_cannot_probe_the_filesystem(tmp_path):
    """The sibling id comes out of a file, so it is untrusted input used to touch the disk.

    It is run through the same id check as a path parameter; a rejected one costs the link and
    nothing else.

    The traversal target is a directory that REALLY EXISTS just outside the runs dir. An id like
    `../../../../etc` would make this test pass without any check at all -- it resolves to a path that
    happens not to exist, so the answer would be False for the wrong reason and the assertion would
    prove nothing.
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    replicate.write_record(runs / "trial-tr", replicate.record_for(
        role=replicate.ROLE_EVAL_TRIAL, build_trace_id="../outside",
    ))
    body = TestClient(
        TracerServer(runs_dir=runs, port=0)._create_app()
    ).get("/api/replicate/trial-tr").json()
    assert (runs / ".." / "outside").is_dir(), "the traversal target must exist for this to discriminate"
    assert body["sibling_available"] is False


def test_the_dashboard_template_renders_the_badge_and_the_cross_link(tmp_path):
    """Smoke: the markup is present, so the route is not wired to a page that ignores it."""
    (tmp_path / "tr").mkdir()
    html = _client(tmp_path).get("/?trace_id=tr").text
    assert "replicate-badge" in html
    assert "loadReplicate" in html
    assert "/api/replicate/" in html
    assert "its build phase" in html
