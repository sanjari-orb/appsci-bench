"""Reading a run's log twice for an unchanged run is the cost the tracer's run list was paying.

Both caches here are keyed on the file's (size, mtime) rather than on a TTL: an append-only log
that has stopped growing is immutable, and one that is still being written changes its stat on
every event. The tests that matter are the invalidation ones -- a cache that never re-reads a live
run would make the dashboard stop updating, which is worse than the slow page it replaced.
"""
from __future__ import annotations

import json
import os
import time

from shared.observability import storage


def _event(eid: str, ts: str = "2026-07-29T12:00:00+00:00") -> str:
    return json.dumps({
        "event_id": eid, "source": "orchestrator", "event_type": "build.start",
        "trace_id": "t", "timestamp": ts, "payload": {},
    }) + "\n"


def _run(tmp_path, name="run_20260729_120000", events=("e1",)):
    run = tmp_path / name
    run.mkdir(parents=True)
    (run / "events-orchestrator.jsonl").write_text("".join(_event(e) for e in events))
    return run


def _count_parses(monkeypatch) -> list[str]:
    """Record every log file the parser opens."""
    seen: list[str] = []
    real = storage._iter_log_lines

    def spy(path):
        seen.append(str(path))
        return real(path)

    monkeypatch.setattr(storage, "_iter_log_lines", spy)
    return seen


def _clear_memos():
    storage._consolidated.clear()
    storage._summaries.clear()


# ---------------------------------------------------------------------------
# consolidate_trace
# ---------------------------------------------------------------------------


def test_an_unchanged_run_is_not_re_merged(tmp_path, monkeypatch):
    run = _run(tmp_path)
    _clear_memos()
    storage.consolidate_trace(run)   # first pass: merges
    storage.consolidate_trace(run)   # second: settles the memo (the merge moved the target's stat)

    parsed = _count_parses(monkeypatch)
    storage.consolidate_trace(run)

    assert parsed == []


def test_a_new_event_is_still_picked_up(tmp_path):
    """The live-run case. A memo that missed this would freeze the dashboard mid-run."""
    run = _run(tmp_path)
    _clear_memos()
    storage.consolidate_trace(run)
    storage.consolidate_trace(run)

    with open(run / "events-orchestrator.jsonl", "a", encoding="utf-8") as f:
        f.write(_event("e2", "2026-07-29T12:01:00+00:00"))
    storage.consolidate_trace(run)

    merged = (run / "events.jsonl").read_text().strip().splitlines()
    assert len(merged) == 2


def test_a_new_source_log_is_still_picked_up(tmp_path):
    """A second writer (the agent, the user-sim) appearing after the first merge."""
    run = _run(tmp_path)
    _clear_memos()
    storage.consolidate_trace(run)
    storage.consolidate_trace(run)

    (run / "events-appsci_agent.jsonl").write_text(_event("a1", "2026-07-29T12:02:00+00:00"))
    storage.consolidate_trace(run)

    assert len((run / "events.jsonl").read_text().strip().splitlines()) == 2


def test_a_deleted_consolidated_log_is_rebuilt(tmp_path):
    run = _run(tmp_path)
    _clear_memos()
    storage.consolidate_trace(run)
    storage.consolidate_trace(run)

    (run / "events.jsonl").unlink()
    storage.consolidate_trace(run)

    assert (run / "events.jsonl").exists()


def test_two_runs_do_not_share_a_memo_entry(tmp_path):
    a = _run(tmp_path, "run_a_20260729")
    b = _run(tmp_path, "run_b_20260729", events=("b1", "b2"))
    _clear_memos()

    storage.consolidate_trace(a)
    storage.consolidate_trace(b)

    assert len((a / "events.jsonl").read_text().strip().splitlines()) == 1
    assert len((b / "events.jsonl").read_text().strip().splitlines()) == 2


def test_the_memo_is_bounded(tmp_path, monkeypatch):
    """It is a cache, not a leak: a long-lived tracer sees every run the host ever writes."""
    monkeypatch.setattr(storage, "_MEMO_MAX", 4)
    _clear_memos()

    for i in range(12):
        storage.consolidate_trace(_run(tmp_path, f"run_{i:03d}_20260729"))

    assert len(storage._consolidated) <= 4


# ---------------------------------------------------------------------------
# cached_trace_summary
# ---------------------------------------------------------------------------


def test_a_summary_is_parsed_once_per_stat(tmp_path, monkeypatch):
    run = _run(tmp_path)
    events = storage.consolidate_trace(run)
    _clear_memos()

    parsed = _count_parses(monkeypatch)
    first = storage.cached_trace_summary(events, run.name)
    second = storage.cached_trace_summary(events, run.name)

    assert first == second
    assert len(parsed) == 1


def test_a_grown_log_is_re_summarised(tmp_path):
    run = _run(tmp_path)
    events = storage.consolidate_trace(run)
    _clear_memos()
    assert storage.cached_trace_summary(events, run.name)["event_count"] == 1

    with open(events, "a", encoding="utf-8") as f:
        f.write(_event("e2", "2026-07-29T12:05:00+00:00"))

    summary = storage.cached_trace_summary(events, run.name)
    assert summary["event_count"] == 2
    assert summary["end_time"] == "2026-07-29T12:05:00+00:00"


def test_a_rewrite_of_the_same_length_is_still_noticed(tmp_path):
    """Size alone would miss this; the mtime half of the key is why it does not."""
    run = _run(tmp_path)
    events = storage.consolidate_trace(run)
    _clear_memos()
    assert storage.cached_trace_summary(events, run.name)["start_time"] == "2026-07-29T12:00:00+00:00"

    time.sleep(0.01)
    events.write_text(_event("e1", "2026-07-29T23:59:59+00:00"))   # same byte count, new content

    assert storage.cached_trace_summary(events, run.name)["start_time"] == "2026-07-29T23:59:59+00:00"


def test_a_missing_log_summarises_as_empty_rather_than_raising(tmp_path):
    _clear_memos()
    summary = storage.cached_trace_summary(tmp_path / "gone" / "events.jsonl", "gone")

    assert summary["event_count"] == 0
    assert summary["start_time"] is None


# ---------------------------------------------------------------------------
# run_dir_mtimes: the ordering key that costs no parsing
# ---------------------------------------------------------------------------


def test_run_dirs_come_back_newest_first(tmp_path):
    old = _run(tmp_path, "old_run_20260101")
    new = _run(tmp_path, "new_run_20260902")
    stamp = time.time() - 30 * 86400
    for path in (old / "events-orchestrator.jsonl", old):
        os.utime(path, (stamp, stamp))

    rows = storage.run_dir_mtimes(tmp_path)

    assert [name for name, _ in rows] == [new.name, old.name]


def test_a_runs_mtime_follows_its_newest_file_not_its_directory(tmp_path):
    """A dir's own mtime stops moving once its files exist, so a long run would date from its first."""
    run = _run(tmp_path)
    old = time.time() - 10 * 86400
    os.utime(run, (old, old))
    fresh = time.time()
    os.utime(run / "events-orchestrator.jsonl", (fresh, fresh))

    (_, mtime), = storage.run_dir_mtimes(tmp_path)

    assert mtime >= fresh - 1


def test_directories_that_are_not_runs_are_left_out(tmp_path):
    _run(tmp_path, "real_run_20260902")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "notes.txt").write_text("hi")

    assert [name for name, _ in storage.run_dir_mtimes(tmp_path)] == ["real_run_20260902"]


def test_a_run_dir_holding_only_an_eval_still_counts(tmp_path):
    """Its logs were reaped or it was pulled report-first; it is still a run."""
    run = tmp_path / "eval_only_20260902"
    (run / "eval").mkdir(parents=True)

    assert [name for name, _ in storage.run_dir_mtimes(tmp_path)] == ["eval_only_20260902"]


def test_a_missing_runs_dir_is_empty_not_an_error(tmp_path):
    assert storage.run_dir_mtimes(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# list_traces(only=...)
# ---------------------------------------------------------------------------


def test_only_restricts_the_scan_to_the_named_runs(tmp_path, monkeypatch):
    _run(tmp_path, "wanted_20260902")
    _run(tmp_path, "unwanted_20260101")
    _clear_memos()

    parsed = _count_parses(monkeypatch)
    traces = storage.list_traces(tmp_path, only=["wanted_20260902"])

    assert [t["trace_id"] for t in traces] == ["wanted_20260902"]
    assert not any("unwanted" in path for path in parsed)


def test_only_ignores_an_id_that_is_not_there(tmp_path):
    _run(tmp_path, "wanted_20260902")

    traces = storage.list_traces(tmp_path, only=["wanted_20260902", "gone_20260101"])

    assert [t["trace_id"] for t in traces] == ["wanted_20260902"]


def test_only_refuses_to_walk_out_of_the_runs_dir(tmp_path):
    """These ids arrive in a query string."""
    outside = tmp_path / "outside"
    _run(outside, "secret_20260101")
    runs = tmp_path / "runs"
    _run(runs, "wanted_20260902")

    traces = storage.list_traces(runs, only=["../outside/secret_20260101", "..", "wanted_20260902"])

    assert [t["trace_id"] for t in traces] == ["wanted_20260902"]


def test_an_empty_only_lists_nothing_rather_than_everything(tmp_path):
    """`only=[]` is "no runs selected", not "no filter" -- the difference is a full scan."""
    _run(tmp_path, "wanted_20260902")

    assert storage.list_traces(tmp_path, only=[]) == []
