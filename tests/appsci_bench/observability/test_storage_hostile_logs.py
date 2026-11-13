"""Hostile / corrupt JSONL lines in a run's observability logs.

Separate from `test_storage.py`, which covers the round-trip behaviour, because these are all one
property: a line the reader cannot parse costs that line, never the listing. Every file read here
lives inside a run directory, and the benchmark subject writes inside run directories -- so a line
can be hostile as well as merely truncated, and the tracer's run list is a page an operator loads
while a run is failing.
"""
from __future__ import annotations

import json

from shared.observability import storage

#: One line of valid JSON, ~195 KB, nested deeply enough to exhaust the parser's stack. `json.loads`
#: recurses per nesting level and raises `RecursionError` -- a `RuntimeError` subclass, so
#: `except json.JSONDecodeError` does not catch it, and neither would `except ValueError`.
DEEPLY_NESTED_JSON = "[" * 100_000 + "]" * 100_000

#: 4,400 digits. Over CPython's ~4,300-digit parse limit, so `json.loads` refuses the line with a
#: BARE `ValueError`; `json.JSONDecodeError` is a *subclass*, so only the wider clause catches both.
OVERLONG_INT_LITERAL = "9" * 4_400


def _seed_trace(runs_dir, trace_id, first_line):
    trace = runs_dir / trace_id
    trace.mkdir(parents=True, exist_ok=True)
    (trace / "events-appsci_agent.jsonl").write_text(first_line + "\n", encoding="utf-8")
    return trace


def _honest_line(event_id, ts="2026-08-11T00:00:00"):
    return json.dumps({"event_id": event_id, "source": "appsci_agent", "timestamp": ts})


def test_one_unparseable_run_does_not_break_the_listing_of_every_other_run(tmp_path):
    """Reproduced as a persistent HTTP 500 on `GET /tracer/` -- the run list, not one run's page.

    Two things had to line up. `_iter_log_lines` caught only `json.JSONDecodeError`, so a
    `RecursionError` from a deeply nested line escaped it; and `list_traces` called
    `consolidate_trace` *outside* its per-trace `try/except`, so the escape was not contained to the
    run that caused it. The listing then 500-ed on every request and stayed that way until someone
    deleted the run directory by hand.

    So the assertion is about the healthy run: it is still listed. Tolerating the bad line is not
    enough on its own -- the bad run has to be the only thing lost.
    """
    runs = tmp_path / "runs"
    _seed_trace(runs, "healthy-trace", _honest_line("ok-1"))
    _seed_trace(runs, "poisoned-trace", DEEPLY_NESTED_JSON)

    traces = storage.list_traces(runs)  # no RecursionError

    assert [t["trace_id"] for t in traces] == ["healthy-trace"]
    assert traces[0]["event_count"] == 1


def test_an_overlong_integer_literal_in_a_source_log_costs_only_that_line(tmp_path):
    """The bare-`ValueError` sibling of the nesting case, through the same narrow `except`.

    A single number over CPython's parse limit is refused with a plain `ValueError`, which
    `except json.JSONDecodeError` does not catch either. Written first so the property is that the
    iterator keeps going: the honest event behind it is still read and still counted.
    """
    runs = tmp_path / "runs"
    trace = runs / "mixed-trace"
    trace.mkdir(parents=True)
    (trace / "events-appsci_agent.jsonl").write_text(
        '{"event_id": ' + OVERLONG_INT_LITERAL + "}\n" + _honest_line("ok-1") + "\n",
        encoding="utf-8",
    )

    traces = storage.list_traces(runs)  # no ValueError

    assert [t["trace_id"] for t in traces] == ["mixed-trace"]
    assert traces[0]["event_count"] == 1


def test_a_poisoned_consolidated_log_does_not_stop_new_events_being_merged(tmp_path):
    """`consolidate_trace` reads the consolidated log to build its dedup set, so it reads it too.

    That read is the first thing it does with the file, before any new event is appended, so an
    unparseable line there used to abort the merge for the whole run rather than just being skipped.
    """
    trace = tmp_path / "runs" / "t1"
    trace.mkdir(parents=True)
    (trace / "events.jsonl").write_text(DEEPLY_NESTED_JSON + "\n", encoding="utf-8")
    (trace / "events-appsci_agent.jsonl").write_text(_honest_line("ok-1") + "\n", encoding="utf-8")

    consolidated = storage.consolidate_trace(trace)  # no RecursionError

    assert consolidated is not None
    # The unreadable line is skipped rather than deduped against, so the new event is appended.
    assert [e["event_id"] for e in storage.read_events(consolidated)] == ["ok-1"]


def test_consolidation_failing_for_one_run_is_contained_to_that_run(tmp_path):
    """Where the merge is called from matters independently of what the merge tolerates.

    `list_traces` used to call `consolidate_trace` *above* its per-trace `try/except`, so any
    exception the merge did not itself handle took down the listing of every run rather than one. The
    parse widening above closes the parse hole, but not this one: `_iter_log_lines` still only
    tolerates a *missing* file, so any other `OSError` on a run's logs -- an unreadable file, an
    inaccessible bind mount, an `ENOTDIR` -- still escapes the merge.

    Provoked here with a directory sitting where a per-source log should be, because that is the one
    such failure that reproduces deterministically on any machine and as any user. The property under
    test is the containment, not the specific errno.
    """
    runs = tmp_path / "runs"
    _seed_trace(runs, "healthy-trace", _honest_line("ok-1"))
    broken = runs / "unreadable-trace"
    broken.mkdir(parents=True)
    (broken / "events-appsci_agent.jsonl").mkdir()

    traces = storage.list_traces(runs)  # no IsADirectoryError

    assert [t["trace_id"] for t in traces] == ["healthy-trace"]
