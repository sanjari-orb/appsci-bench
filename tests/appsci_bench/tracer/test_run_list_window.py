"""The run list is a window, not the whole history -- and everything outside it stays reachable.

Rendering a row is not free: each one costs a merge of that run's per-source logs and a parse of
the merged log. The page therefore chooses its rows from directory mtimes first (no parsing) and
reads only those. These tests pin the two halves of that bargain: the window really does bound the
work, and a run outside it is still findable by search, by widening the window, or by id.
"""
from __future__ import annotations

import json
import os
import time

from fastapi.testclient import TestClient

from appsci_bench.tracer import server as server_mod
from appsci_bench.tracer.server import (
    RUN_LIST_MAX_ROWS,
    RUN_LIST_MIN_ROWS,
    TracerServer,
    parse_days,
    select_run_window,
)
from shared.observability import replicate, storage
from shared.observability.s3_store import S3Config

CFG = S3Config(bucket="test-bucket", prefix="runs", region="us-east-2")

DAY = 86400.0


def _client(runs_dir, s3_config=None):
    return TestClient(
        TracerServer(runs_dir=runs_dir, port=0, s3_config=s3_config)._create_app(),
        follow_redirects=False,
    )


def _text(html: str) -> str:
    return " ".join(html.split())


def _fill_window(runs_dir, n=RUN_LIST_MIN_ROWS):
    """Enough recent runs that the minimum-rows floor is not what decides the listing.

    The floor exists so a quiet host still shows its last runs; below it the window is inactive by
    design, so any test about what the window HIDES has to clear it first.
    """
    return [_write_run(runs_dir, f"filler_run_{i:03d}", age_days=0.01 * i) for i in range(n)]


def _age(run_dir, days: float):
    """Backdate a run dir and everything in it -- the window reads the newest of the two."""
    when = time.time() - days * DAY
    for path in list(run_dir.iterdir()) + [run_dir]:
        os.utime(path, (when, when))


def _write_run(runs_dir, trace, *, age_days: float = 0.0):
    """A renderable run whose files are aged to place it in or out of the window."""
    run = runs_dir / trace
    run.mkdir(parents=True)
    log = run / "events-orchestrator.jsonl"
    log.write_text(json.dumps({
        "event_id": f"{trace}-e1", "source": "orchestrator", "event_type": "build.start",
        "trace_id": trace, "task_id": "airline_rebooking",
        "timestamp": "2026-07-29T12:00:00+00:00", "payload": {},
    }) + "\n")
    if age_days:
        _age(run, age_days)
    return trace


# ---------------------------------------------------------------------------
# Choosing the window, from ids and mtimes alone
# ---------------------------------------------------------------------------


def test_the_window_keeps_recent_runs_and_drops_older_ones():
    now = 1_000_000.0
    fresh = [(f"fresh_{i:03d}", now - 3600 - i) for i in range(RUN_LIST_MIN_ROWS)]
    rows = fresh + [("stale", now - 5 * DAY)]

    picked = select_run_window(rows, days=2, query="", now=now)

    assert picked["ids"] == [run_id for run_id, _ in fresh]
    assert (picked["shown"], picked["total"]) == (RUN_LIST_MIN_ROWS, RUN_LIST_MIN_ROWS + 1)
    assert picked["floored"] is False


def test_an_empty_window_falls_back_to_the_newest_runs_and_says_so():
    """A box idle for a week must not render as an empty table -- that reads like data loss."""
    now = 1_000_000.0
    rows = [(f"old_{i}", now - (30 + i) * DAY) for i in range(3)]

    picked = select_run_window(rows, days=2, query="", now=now)

    assert picked["ids"] == ["old_0", "old_1", "old_2"]
    assert picked["floored"] is True


def test_the_floor_never_reaches_past_the_newest_few():
    now = 1_000_000.0
    rows = [(f"old_{i:03d}", now - (30 + i) * DAY) for i in range(RUN_LIST_MIN_ROWS * 3)]

    picked = select_run_window(rows, days=2, query="", now=now)

    assert len(picked["ids"]) == RUN_LIST_MIN_ROWS
    assert picked["ids"][0] == "old_000"       # newest first, and only the newest


def test_all_history_is_still_capped():
    """`?days=0` must not be a one-click route back to the page this window exists to fix."""
    now = 1_000_000.0
    rows = [(f"r{i:04d}", now - i) for i in range(RUN_LIST_MAX_ROWS + 40)]

    picked = select_run_window(rows, days=0, query="", now=now)

    assert len(picked["ids"]) == RUN_LIST_MAX_ROWS
    assert picked["truncated"] is True
    assert picked["showing_all"] is False


def test_search_ignores_the_window_entirely():
    """The reason to search by id is that the run is old, so windowing the search first is useless."""
    now = 1_000_000.0
    rows = [("airline_fresh", now), ("o2c_ancient", now - 400 * DAY)]

    picked = select_run_window(rows, days=2, query="o2c", now=now)

    assert picked["ids"] == ["o2c_ancient"]
    assert picked["matched"] == 1


def test_search_is_case_insensitive_and_matches_a_fragment():
    now = 1_000_000.0
    rows = [("airline_rebooking_claude-haiku-4-5_20260902", now - 90 * DAY)]

    assert select_run_window(rows, days=2, query="HAIKU", now=now)["ids"] == [rows[0][0]]


def test_a_search_matching_nothing_is_empty_not_a_fallback_to_recent():
    """Falling back to the newest runs here would answer a question nobody asked."""
    now = 1_000_000.0
    rows = [("airline_fresh", now)]

    picked = select_run_window(rows, days=2, query="nonesuch", now=now)

    assert picked["ids"] == []
    assert picked["floored"] is False


def test_a_junk_window_value_is_the_default_not_an_error():
    assert parse_days("banana") == parse_days(None)
    assert parse_days("-5") == parse_days(None)
    assert parse_days("all") == 0
    assert parse_days("7") == 7
    assert parse_days("99999") == 3650      # clamped, not multiplied into an absurd cutoff


# ---------------------------------------------------------------------------
# The rendered page
# ---------------------------------------------------------------------------


def test_the_page_lists_recent_runs_and_hides_older_ones(tmp_path):
    filler = _fill_window(tmp_path)
    _write_run(tmp_path, "ancient_run_20260101", age_days=40)

    html = _client(tmp_path).get("/").text

    assert filler[0] in html
    assert "ancient_run_20260101" not in html
    assert f"{len(filler)} of {len(filler) + 1} local runs" in _text(html)


def test_widening_the_window_brings_the_older_run_back(tmp_path):
    _fill_window(tmp_path)
    _write_run(tmp_path, "ancient_run_20260101", age_days=40)

    html = _client(tmp_path).get("/?days=0").text

    assert "ancient_run_20260101" in html
    assert f"All {RUN_LIST_MIN_ROWS + 1} local runs" in _text(html)


def test_searching_finds_a_run_the_window_hid(tmp_path):
    filler = _fill_window(tmp_path)
    _write_run(tmp_path, "ancient_run_20260101", age_days=40)

    html = _client(tmp_path).get("/?q=ancient").text

    assert "ancient_run_20260101" in html
    assert filler[0] not in html


def test_a_search_with_no_match_says_it_searched_everything(tmp_path):
    """Otherwise "not found" is indistinguishable from "not in the last two days"."""
    _write_run(tmp_path, "fresh_run_20260902")

    html = _client(tmp_path).get("/?q=zzz").text

    assert "No run id matches" in _text(html)
    assert "1 local run were searched" in _text(html) or "1 local run" in _text(html)


def test_the_page_says_what_it_is_not_showing(tmp_path):
    _fill_window(tmp_path)
    _write_run(tmp_path, "ancient_run_20260101", age_days=40)

    text = _text(_client(tmp_path).get("/").text)

    assert "Older ones are still here" in text


def test_a_hidden_run_still_opens_by_id(tmp_path):
    """The window is a listing choice; it must not make a run unreachable."""
    _write_run(tmp_path, "ancient_run_20260101", age_days=40)

    r = _client(tmp_path).get("/?trace_id=ancient_run_20260101")

    assert r.status_code == 200
    assert "ancient_run_20260101" in r.text


def test_a_junk_days_value_does_not_500(tmp_path):
    _write_run(tmp_path, "fresh_run_20260902")

    assert _client(tmp_path).get("/?days=../../etc").status_code == 200


def test_an_absurdly_long_search_is_truncated_not_rejected(tmp_path):
    _write_run(tmp_path, "fresh_run_20260902")

    r = _client(tmp_path).get("/?q=" + "a" * 5000)

    assert r.status_code == 200


# ---------------------------------------------------------------------------
# What the window is for: bounding the work
# ---------------------------------------------------------------------------


def test_only_the_displayed_runs_are_read_from_disk(tmp_path, monkeypatch):
    """The whole point. Reading every run to show a window of it is the cost being removed."""
    filler = _fill_window(tmp_path)
    _write_run(tmp_path, "ancient_run_20260101", age_days=40)

    consolidated: list[str] = []
    real = storage.consolidate_trace
    monkeypatch.setattr(
        storage, "consolidate_trace",
        lambda d: (consolidated.append(str(d)), real(d))[1],
    )

    _client(tmp_path).get("/")

    assert any(filler[0] in d for d in consolidated)
    assert not any("ancient_run_20260101" in d for d in consolidated)


def test_the_mirror_is_checked_with_one_listing_plus_the_shown_rows(tmp_path, monkeypatch):
    """Not one LIST over every object in the bucket: that cost 33 round-trips on the real mirror."""
    filler = _fill_window(tmp_path)
    _write_run(tmp_path, "ancient_run_20260101", age_days=40)

    listings: list[str] = []
    probed: list[list[str]] = []

    def fake_list(cfg):
        listings.append(cfg.bucket)
        return set(filler) | {"ancient_run_20260101"}

    def fake_probe(ids, cfg):
        ids = list(ids)
        probed.append(ids)
        return set(ids)

    monkeypatch.setattr(server_mod, "list_mirrored_run_ids", fake_list)
    monkeypatch.setattr(server_mod, "manifest_present", fake_probe)

    html = _client(tmp_path, s3_config=CFG).get("/").text

    assert listings == ["test-bucket"]                     # exactly one, whatever the run count
    assert len(probed) == 1
    assert sorted(probed[0]) == sorted(filler)             # only the rows on screen
    assert "ancient_run_20260101" not in probed[0]
    assert "Shared" in html


def test_a_shown_trials_build_is_pulled_into_the_window(tmp_path):
    """A build is older than its trials, so any useful window cuts exactly between them.

    Left out, the group renders with "build run dir not on this host" -- which is what a REAPED
    build looks like, on a build that is sitting right there on disk.
    """
    _fill_window(tmp_path)
    build = _write_run(tmp_path, "build_run_20260101")
    trial = _write_run(tmp_path, "trial_run_20260902")
    replicate.write_record(tmp_path / build, replicate.record_for(
        role=replicate.ROLE_BUILD, job_id="j", cell_id="cb0", build_index=0,
    ))
    replicate.write_record(tmp_path / trial, replicate.record_for(
        role=replicate.ROLE_EVAL_TRIAL, job_id="j", cell_id="ct0", depends_on="cb0",
        build_index=0, trial_index=0, build_trace_id=build,
    ))
    _age(tmp_path / build, 40)      # after the record: writing it would reset the mtime

    html = _client(tmp_path).get("/").text

    assert build in html
    assert "build run dir not on this host" not in html
    assert "1 older build" in _text(html)


def test_a_genuinely_reaped_build_is_not_invented(tmp_path):
    """The pull-in only reaches runs that exist; a deleted build still reads as deleted."""
    _fill_window(tmp_path)
    trial = _write_run(tmp_path, "trial_run_20260902")
    replicate.write_record(tmp_path / trial, replicate.record_for(
        role=replicate.ROLE_EVAL_TRIAL, job_id="j", cell_id="ct0", depends_on="cb0",
        build_index=0, trial_index=0, build_trace_id="build_that_was_reaped_20260101",
    ))

    html = _client(tmp_path).get("/").text

    assert "build run dir not on this host" in html
    assert "older build" not in _text(html)


def test_the_search_term_is_escaped_where_it_is_echoed_back(tmp_path):
    """It is reflected into the page twice (the input's value and the scope line)."""
    _write_run(tmp_path, "fresh_run_20260902")

    html = _client(tmp_path).get('/?q="><script>alert(1)</script>').text

    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;" in html


def test_the_auto_refresh_keeps_the_chosen_window(tmp_path):
    """A tick that re-fetched a bare `/` would silently reset the view every 10 seconds."""
    _write_run(tmp_path, "fresh_run_20260902")

    html = _client(tmp_path).get("/?days=30").text

    assert "location.pathname + location.search" in html
