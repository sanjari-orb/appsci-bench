"""The live event stream must hand each event to the page exactly once.

Regression: the stream tracked "where to resume from" by taking the id of the last event in each
batch. Batches come back in timestamp order while an id is a line number in append order, and the
two disagree whenever one writer's log is picked up after another's but holds older events -- which
is routine, since the agent, the user sim and the evaluator each append to their own file and the
reader merges them on a poll. The resume marker therefore walked backwards and the next tick re-sent
everything above it, twice a second, for as long as the tab stayed open. The page appended those
events again and rebuilt its whole timeline each time: the "tracer refreshes every couple of seconds
and flickers" report.

The stream is driven directly rather than over a test client: its loop ends only when the client
hangs up, and an HTTP test client cannot hang up on an ASGI app it is itself hosting -- the request
never returns. Taking the response's own iterator lets the test stop after a fixed number of poll
ticks, which is the thing under test anyway.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from starlette.requests import Request

from appsci_bench.tracer.server import TracerServer
from shared.observability import storage

STREAM_PATH = "/api/events/{trace_id}/stream"


def _event(source: str, second: int, event_id: str) -> dict:
    return {
        "event_id": event_id,
        "trace_id": "tr",
        "source": source,
        "event_type": "agent.step",
        "timestamp": f"2026-08-31T00:00:{second:02d}",
        "span_id": event_id,
        "payload": {},
    }


def _seed_interleaved(tmp_path, trace: str = "tr") -> str:
    """A trace whose merged log has ids running against its timestamps.

    Consolidated in two passes on purpose: the agent's events are merged first, then the user sim's
    file appears holding events that are OLDER. Ids are positions in the append-only merged log, so
    the user sim's land above the agent's while their timestamps fall between them -- exactly what a
    poll-driven merge of several writers produces.
    """
    d = tmp_path / trace
    d.mkdir(parents=True)
    d.joinpath("events-agent.jsonl").write_text(
        "".join(json.dumps(_event("agent", s, f"a{s:02d}")) + "\n" for s in (5, 10))
    )
    storage.consolidate_trace(d)
    d.joinpath("events-usersim.jsonl").write_text(
        "".join(json.dumps(_event("usersim", s, f"b{s:02d}")) + "\n" for s in (6, 7))
    )
    storage.consolidate_trace(d)
    return trace


async def _never() -> dict:
    """A receive channel with nothing on it: the client stays connected."""
    await asyncio.sleep(3600)
    return {"type": "http.disconnect"}


async def _stream_ticks(runs_dir, trace: str, ticks: int) -> list[dict]:
    """Run the stream for `ticks` poll cycles and return the events it sent."""
    app = TracerServer(runs_dir=runs_dir, port=0)._create_app()
    endpoint = next(r.endpoint for r in app.routes if getattr(r, "path", None) == STREAM_PATH)
    request = Request(
        {"type": "http", "method": "GET", "path": f"/api/events/{trace}/stream", "headers": []},
        _never,
    )

    response = await endpoint(request, trace_id=trace, since_id=0)
    sent: list[dict] = []
    seen_ticks = 0
    try:
        async for chunk in response.body_iterator:
            if chunk.startswith("data: "):
                sent.append(json.loads(chunk[len("data: "):]))
            elif chunk.startswith(": keepalive"):
                seen_ticks += 1
                if seen_ticks >= ticks:
                    break
    finally:
        await response.body_iterator.aclose()
    return sent


def test_read_path_can_hand_back_a_batch_whose_ids_descend(tmp_path):
    """The precondition the stream has to survive. If this stops holding, the test below stops
    testing anything."""
    trace = _seed_interleaved(tmp_path)
    ids = [e["id"] for e in storage.read_events(tmp_path / trace / "events.jsonl")]
    assert ids != sorted(ids), "expected timestamp order to disagree with id order"


@pytest.mark.parametrize("ticks", [3, 6])
def test_stream_sends_each_event_once_when_ids_run_against_timestamps(tmp_path, ticks):
    """Every event exactly once, and silence thereafter however long the tab stays open."""
    trace = _seed_interleaved(tmp_path)

    sent = asyncio.run(_stream_ticks(tmp_path, trace, ticks))

    assert sorted(e["event_id"] for e in sent) == ["a05", "a10", "b06", "b07"]
