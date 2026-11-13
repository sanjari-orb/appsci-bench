"""Tracer S3 routes: resolving a run by id from either place, and rendering it.

``/?trace_id=<id>`` is the single entry point -- it pulls an id that isn't local yet out of the S3
mirror and then renders it through the ordinary local read path, so the URL is identical whether the
run was produced here or by someone else.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from appsci_bench.tracer import server as server_mod
from appsci_bench.tracer.server import TracerServer
from shared.observability.s3_store import (
    DownloadResult,
    RemoteTraceNotFoundError,
    S3Config,
    S3CredentialsError,
    S3Error,
)

CFG = S3Config(bucket="test-bucket", prefix="runs", region="us-east-2")


def _client(runs_dir, s3_config=CFG):
    return TestClient(
        TracerServer(runs_dir=runs_dir, port=0, s3_config=s3_config)._create_app(),
        follow_redirects=False,
    )


def _text(html: str) -> str:
    """Collapse whitespace so assertions survive the template's line wrapping."""
    return " ".join(html.split())


def _write_run(runs_dir, trace="pulled_run_20260729"):
    """Write a minimal but renderable run, as download_trace would have."""
    run = runs_dir / trace
    run.mkdir(parents=True)
    run.joinpath("events-orchestrator.jsonl").write_text(json.dumps({
        "event_id": "e1", "source": "orchestrator", "event_type": "build.start",
        "trace_id": trace, "task_id": "airline_rebooking",
        "timestamp": "2026-07-29T12:00:00+00:00", "payload": {},
    }) + "\n")
    return trace


# ---------------------------------------------------------------------------
# Pulling
# ---------------------------------------------------------------------------


def test_pull_route_downloads_and_reports_counts(tmp_path, monkeypatch):
    def fake_download(run_id, dest, cfg, force=False):
        _write_run(tmp_path, run_id)
        return DownloadResult(trace_id=run_id, dest=dest / run_id,
                              downloaded=3, skipped=1, bytes_downloaded=999)

    monkeypatch.setattr(server_mod, "download_trace", fake_download)
    r = _client(tmp_path).post("/api/s3/pull/some_run_20260729")
    assert r.status_code == 200
    body = r.json()
    assert body["trace_id"] == "some_run_20260729"
    assert (body["downloaded"], body["skipped"], body["bytes_downloaded"]) == (3, 1, 999)


def test_pull_route_404s_for_unknown_run(tmp_path, monkeypatch):
    def missing(run_id, dest, cfg, force=False):
        raise RemoteTraceNotFoundError(f"No run {run_id!r} in the mirror")

    monkeypatch.setattr(server_mod, "download_trace", missing)
    r = _client(tmp_path).post("/api/s3/pull/nope")
    assert r.status_code == 404 and "nope" in r.json()["detail"]


def test_pull_route_502s_on_credential_failure(tmp_path, monkeypatch):
    def boom(run_id, dest, cfg, force=False):
        raise S3CredentialsError("expired")

    monkeypatch.setattr(server_mod, "download_trace", boom)
    assert _client(tmp_path).post("/api/s3/pull/r1").status_code == 502


def test_pull_route_400s_when_s3_disabled(tmp_path):
    r = _client(tmp_path, s3_config=None).post("/api/s3/pull/r1")
    assert r.status_code == 400 and "not configured" in r.json()["detail"]


def test_pull_route_forwards_force_flag(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_download(run_id, dest, cfg, force=False):
        seen["force"] = force
        return DownloadResult(trace_id=run_id, dest=dest / run_id)

    monkeypatch.setattr(server_mod, "download_trace", fake_download)
    c = _client(tmp_path)
    c.post("/api/s3/pull/r1")
    assert seen["force"] is False
    c.post("/api/s3/pull/r1?force=true")
    assert seen["force"] is True


# ---------------------------------------------------------------------------
# The central entry point: /?trace_id= resolves local OR mirrored runs
# ---------------------------------------------------------------------------


def test_index_pulls_an_unknown_id_from_the_mirror_and_renders_it(tmp_path, monkeypatch):
    """The whole point: one URL loads a run without the reader knowing where it came from."""
    pulled = []

    def fake_download(run_id, dest, cfg, force=False):
        pulled.append(run_id)
        _write_run(tmp_path, run_id)
        return DownloadResult(trace_id=run_id, dest=dest / run_id, downloaded=1)

    monkeypatch.setattr(server_mod, "download_trace", fake_download)
    r = _client(tmp_path).get("/?trace_id=only_in_s3_20260729")
    assert r.status_code == 200
    assert pulled == ["only_in_s3_20260729"]
    assert "only_in_s3_20260729" in r.text


def test_index_does_not_touch_s3_for_an_already_local_run(tmp_path, monkeypatch):
    trace = _write_run(tmp_path)

    def boom(*a, **k):
        raise AssertionError("should not consult S3 for a local run")

    monkeypatch.setattr(server_mod, "download_trace", boom)
    assert _client(tmp_path).get(f"/?trace_id={trace}").status_code == 200


def test_index_404s_when_run_is_in_neither_place(tmp_path, monkeypatch):
    def missing(run_id, dest, cfg, force=False):
        raise RemoteTraceNotFoundError("not in mirror")

    monkeypatch.setattr(server_mod, "download_trace", missing)
    r = _client(tmp_path).get("/?trace_id=nowhere")
    assert r.status_code == 404 and "nowhere" in r.json()["detail"]


def test_index_surfaces_credential_error_rather_than_404(tmp_path, monkeypatch):
    """"Run doesn't exist" and "your session lapsed" need different fixes -- don't conflate them."""
    def boom(run_id, dest, cfg, force=False):
        raise S3CredentialsError("run ./scripts/setup_s3_sso.sh")

    monkeypatch.setattr(server_mod, "download_trace", boom)
    r = _client(tmp_path).get("/?trace_id=some_run")
    assert r.status_code == 502
    assert "setup_s3_sso.sh" in r.json()["detail"]


def test_index_renders_local_run_even_when_s3_is_broken(tmp_path, monkeypatch):
    """A lapsed session must not hide a run that is already on disk."""
    trace = _write_run(tmp_path)

    def boom(run_id, dest, cfg, force=False):
        raise S3CredentialsError("expired")

    monkeypatch.setattr(server_mod, "download_trace", boom)
    assert _client(tmp_path).get(f"/?trace_id={trace}").status_code == 200


def test_index_404s_for_unknown_run_with_s3_disabled(tmp_path):
    assert _client(tmp_path, s3_config=None).get("/?trace_id=nope").status_code == 404


# ---------------------------------------------------------------------------
# The actual payoff: a pulled run renders through the existing local path
# ---------------------------------------------------------------------------


def test_pulled_run_renders_through_the_normal_local_read_path(tmp_path, monkeypatch):
    def fake_download(run_id, dest, cfg, force=False):
        _write_run(tmp_path, run_id)
        return DownloadResult(trace_id=run_id, dest=dest / run_id, downloaded=1)

    monkeypatch.setattr(server_mod, "download_trace", fake_download)
    c = _client(tmp_path)
    trace = "shared_run_20260729"
    assert c.post(f"/api/s3/pull/{trace}").status_code == 200

    # No S3 involvement from here on: these are the pre-existing local routes.
    dash = c.get(f"/?trace_id={trace}")
    assert dash.status_code == 200 and trace in dash.text

    events = c.get(f"/api/events/{trace}").json()
    assert events["count"] == 1
    assert events["events"][0]["event_type"] == "build.start"

    # ...and it shows up in the local run list like any other run.
    assert trace in c.get("/").text


def test_run_list_search_box_is_always_present_and_notes_s3_state(tmp_path):
    # One central box either way; only the hint below it changes.
    with_s3 = _client(tmp_path).get("/").text
    assert 'name="trace_id"' in with_s3
    assert "s3://test-bucket/runs/" in with_s3

    without_s3 = _client(tmp_path, s3_config=None).get("/").text
    assert 'name="trace_id"' in without_s3
    assert "S3 mirroring is disabled" in without_s3


def _mirror(monkeypatch, *, mirrored: set[str], complete: set[str]):
    """Stand in for the two calls the run list makes about the mirror.

    They are separate on purpose: one delimited LIST says which runs are in the bucket at all, and
    a HEAD per DISPLAYED run says whether its upload finished. Patching them apart is what lets a
    test assert the second one is only asked about the rows on screen.
    """
    monkeypatch.setattr(server_mod, "list_mirrored_run_ids", lambda cfg: set(mirrored))
    monkeypatch.setattr(
        server_mod, "manifest_present", lambda ids, cfg: {i for i in ids if i in complete}
    )


# ---------------------------------------------------------------------------
# Run list: is each run shared, or local only?
# A local-only run is fully usable -- it just isn't shareable yet -- so the page
# says so and names the upload command rather than failing or staying silent.
# ---------------------------------------------------------------------------


def test_run_list_marks_a_mirrored_run_as_shared(tmp_path, monkeypatch):
    trace = _write_run(tmp_path)
    _mirror(monkeypatch, mirrored={trace}, complete={trace})

    html = _client(tmp_path).get("/").text
    assert "Shared" in html
    assert "Local only" not in html
    assert "upload_runs_to_s3.py" not in html   # nothing to do


def test_run_list_flags_a_local_only_run_with_the_upload_command(tmp_path, monkeypatch):
    """The ask: tell the user this run is local and give them the command."""
    trace = _write_run(tmp_path)
    _mirror(monkeypatch, mirrored=set(), complete=set())

    html = _client(tmp_path).get("/").text
    assert "Local only" in html
    assert "is on this machine only" in _text(html)
    assert f"python scripts/upload_runs_to_s3.py {trace}" in html
    # ...and how to fix the most likely reason the upload was skipped.
    assert "aws sso login" in html


def test_run_list_lists_every_local_only_run_in_one_command(tmp_path, monkeypatch):
    a = _write_run(tmp_path, "run_a_20260730")
    b = _write_run(tmp_path, "run_b_20260730")
    _mirror(monkeypatch, mirrored=set(), complete=set())

    html = _client(tmp_path).get("/").text
    assert "2 runs are on this machine only" in _text(html)
    cmd = f"python scripts/upload_runs_to_s3.py {a} {b}"
    alt = f"python scripts/upload_runs_to_s3.py {b} {a}"
    assert cmd in html or alt in html


def test_run_list_does_not_claim_local_only_when_s3_is_unreachable(tmp_path, monkeypatch):
    """Unknown must not be rendered as "not shared" -- that would be a false alarm."""
    _write_run(tmp_path)

    def boom(cfg):
        raise S3CredentialsError("run ./scripts/setup_s3_sso.sh")

    monkeypatch.setattr(server_mod, "list_mirrored_run_ids", boom)
    html = _client(tmp_path).get("/").text
    assert "Mirror status unknown" in html
    assert "setup_s3_sso.sh" in html
    assert "Local only" not in html
    assert "Shared" not in html


def test_run_list_explains_when_s3_is_disabled(tmp_path):
    _write_run(tmp_path)
    html = _client(tmp_path, s3_config=None).get("/").text
    assert "S3 mirroring is off for this tracer" in html
    assert "Local only" not in html


def test_run_list_never_500s_when_the_mirror_check_fails(tmp_path, monkeypatch):
    _write_run(tmp_path)

    def boom(cfg):
        raise S3Error("bucket exploded")

    monkeypatch.setattr(server_mod, "list_mirrored_run_ids", boom)
    r = _client(tmp_path).get("/")
    assert r.status_code == 200      # the local run list is the important part of this page


def test_run_list_falls_back_to_bare_command_for_many_local_only_runs(tmp_path, monkeypatch):
    """Listing dozens of ids would be unreadable; the bare command is equivalent."""
    for i in range(6):
        _write_run(tmp_path, f"run_{i}_20260730")
    _mirror(monkeypatch, mirrored=set(), complete=set())

    html = _client(tmp_path).get("/").text
    assert "6 runs are on this machine only" in _text(html)
    assert "<pre class=\"mirror-cmd\">python scripts/upload_runs_to_s3.py</pre>" in html
    assert "run_0_20260730 run_1_20260730" not in html      # not enumerated
    assert "already\n                   mirrored are skipped" in html or "already" in html


def test_run_list_distinguishes_a_partial_upload_from_shared(tmp_path, monkeypatch):
    """An upload that died before its manifest must not claim to be fully shared."""
    trace = _write_run(tmp_path)
    _mirror(monkeypatch, mirrored={trace}, complete=set())   # present, but no manifest

    html = _client(tmp_path).get("/").text
    assert "Partial" in html
    assert "Shared" not in html
    assert "uploaded only partially" in _text(html)
    # Re-running the upload is the fix, and it is safe.
    assert "python scripts/upload_runs_to_s3.py" in html
    # It is not "local only" -- that would send the wrong message about what happened.
    assert "on this machine only" not in _text(html)


def test_malformed_run_id_is_a_400_not_a_502(tmp_path, monkeypatch):
    """A caller mistake should not be reported as an upstream failure."""
    def unused(*a, **k):
        raise AssertionError("should not reach S3 for an invalid id")

    monkeypatch.setattr(server_mod, "download_trace", unused)
    # `..` would be normalized away by the HTTP layer before routing, so use an id that reaches
    # the handler intact but cannot name a run.
    r = _client(tmp_path).post("/api/s3/pull/bad%20id")
    assert r.status_code == 400, r.text
    assert "Invalid run id" in r.json()["detail"]


def test_surrounding_whitespace_in_a_run_id_is_normalized_not_rejected(tmp_path, monkeypatch):
    """`abc` and `abc ` must take the same lock, since they write the same directory."""
    seen = []

    def fake_download(run_id, dest, cfg, force=False):
        seen.append(run_id)
        _write_run(tmp_path, run_id)
        return DownloadResult(trace_id=run_id, dest=dest / run_id, downloaded=1)

    monkeypatch.setattr(server_mod, "download_trace", fake_download)
    r = _client(tmp_path).post("/api/s3/pull/spaced_run_20260730%20")
    assert r.status_code == 200, r.text
    assert seen == ["spaced_run_20260730"]
