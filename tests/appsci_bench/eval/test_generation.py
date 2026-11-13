"""Unit tests: the generation step (single/multi turn, repeat, timeouts, failures)."""
from __future__ import annotations

import time

import pytest

from appsci_bench.eval.artifact_client import InProcessArtifactClient
from appsci_bench.eval.dataset import parse_dataset
from appsci_bench.eval.generation import generate
from appsci_bench.eval.datatypes import (
    TERMINATION_COMPLETE, TERMINATION_ERROR, TERMINATION_MAX_TURNS, TERMINATION_USER_STOP,
    ArtifactAgentOutput, ArtifactAgentQuery, EvalTypeError,
)
from appsci_bench.eval.artifact_agent_user_sim import ArtifactAgentUserSimulator, ScriptedArtifactAgentUserSim


def _echo(q: ArtifactAgentQuery) -> ArtifactAgentOutput:
    return ArtifactAgentOutput(text=(q.text or "").upper())


def _single_ds(n=2, repeat=1):
    samples = [{"data_point_id": f"d{i}", "single_turn_input": {"text": f"q{i}"}} for i in range(n)]
    return parse_dataset({"type": "single_turn", "repeat": repeat}, samples)


def test_single_turn_one_turn_per_sample():
    recs = generate(_single_ds(2), InProcessArtifactClient(_echo))
    assert [r.data_point_id for r in recs] == ["d0", "d1"]          # deterministic order
    assert all(len(r.conversation) == 1 and r.error is None for r in recs)
    assert recs[0].conversation[0].response.text == "Q0"


def test_repeat_yields_n_records_sorted():
    recs = generate(_single_ds(1, repeat=3), InProcessArtifactClient(_echo))
    assert [(r.data_point_id, r.repeat_index) for r in recs] == [("d0", 0), ("d0", 1), ("d0", 2)]


def test_multi_turn_drives_sim_and_stops_on_none():
    ds = parse_dataset({"type": "multi_turn"},
                       [{"data_point_id": "m", "multi_turn_input": {"user_task": "t", "user_persona": "p"}}])
    sim = ScriptedArtifactAgentUserSim(["hi", "more"])      # 2 scripted messages, then stop
    recs = generate(ds, InProcessArtifactClient(_echo), user_sim=sim)
    assert len(recs[0].conversation) == 2 and recs[0].error is None
    assert recs[0].conversation[0].query.text == "hi"


def test_multi_turn_respects_max_turns_ceiling():
    ds = parse_dataset({"type": "multi_turn", "max_turns": 2},
                       [{"data_point_id": "m", "multi_turn_input": {"user_task": "t", "user_persona": "p"}}])
    sim = ScriptedArtifactAgentUserSim(["a", "b", "c", "d"])  # script longer than the ceiling
    recs = generate(ds, InProcessArtifactClient(_echo), user_sim=sim)
    assert len(recs[0].conversation) == 2        # capped at max_turns


# --- termination_reason: why the conversation ended, for eval functions that must tell a finished
# conversation from a truncated one (tasks/airline_rebooking/eval.py gates its reward on this).
def test_termination_reason_is_user_stop_when_the_sim_ends_the_conversation():
    ds = parse_dataset({"type": "multi_turn", "max_turns": 5},
                       [{"data_point_id": "m", "multi_turn_input": {"user_task": "t", "user_persona": "p"}}])
    sim = ScriptedArtifactAgentUserSim(["hi", "more"])   # stops well inside the ceiling
    recs = generate(ds, InProcessArtifactClient(_echo), user_sim=sim)
    assert recs[0].termination_reason == TERMINATION_USER_STOP


def test_termination_reason_is_max_turns_when_the_ceiling_cuts_the_sim_off():
    ds = parse_dataset({"type": "multi_turn", "max_turns": 2},
                       [{"data_point_id": "m", "multi_turn_input": {"user_task": "t", "user_persona": "p"}}])
    sim = ScriptedArtifactAgentUserSim(["a", "b", "c", "d"])  # still talking when the ceiling hits
    recs = generate(ds, InProcessArtifactClient(_echo), user_sim=sim)
    assert recs[0].termination_reason == TERMINATION_MAX_TURNS


def test_a_sim_that_stops_on_the_last_permitted_turn_is_a_stop_not_a_ceiling_hit():
    # The off-by-one that matters: the script runs out exactly as the ceiling is reached, so the loop
    # exits by exhausting `range` -- but the sim DID end the conversation, so this is not truncation.
    ds = parse_dataset({"type": "multi_turn", "max_turns": 2},
                       [{"data_point_id": "m", "multi_turn_input": {"user_task": "t", "user_persona": "p"}}])
    sim = ScriptedArtifactAgentUserSim(["a", "b"])       # exactly max_turns messages, then stops
    recs = generate(ds, InProcessArtifactClient(_echo), user_sim=sim)
    assert len(recs[0].conversation) == 2
    assert recs[0].termination_reason == TERMINATION_USER_STOP


def test_termination_reason_is_error_when_a_query_fails():
    def _boom(q: ArtifactAgentQuery) -> ArtifactAgentOutput:
        raise RuntimeError("artifact exploded")

    ds = parse_dataset({"type": "multi_turn", "max_turns": 5},
                       [{"data_point_id": "m", "multi_turn_input": {"user_task": "t", "user_persona": "p"}}])
    recs = generate(ds, InProcessArtifactClient(_boom),
                    user_sim=ScriptedArtifactAgentUserSim(["a", "b"]))
    assert recs[0].error is not None
    assert recs[0].termination_reason == TERMINATION_ERROR


def test_single_turn_termination_is_complete():
    recs = generate(_single_ds(1), InProcessArtifactClient(_echo))
    assert recs[0].termination_reason == TERMINATION_COMPLETE


def test_a_unit_that_fails_before_its_first_turn_still_records_error_termination():
    """Regression, from a real e2e run: the eval-time user sim had no usable credentials, so the
    OPENING `ask` raised before any turn existed. That exits _run_unit via its outer `except`, so
    nothing inside the turn loop ever set a reason, and all 50 transcripts recorded
    `termination_reason: null` -- reading as "not recorded" (an unknown, scored normally) when in
    fact the unit had failed outright. The distinction has to survive a failure this early."""
    class _BrokenSim:
        def create_session(self, sample):
            return "s"

        def ask(self, session_id, conversation):
            raise RuntimeError("no credentials")

        def delete_session(self, session_id):
            pass

    ds = parse_dataset({"type": "multi_turn", "max_turns": 5},
                       [{"data_point_id": "m", "multi_turn_input": {"user_task": "t", "user_persona": "p"}}])
    recs = generate(ds, InProcessArtifactClient(_echo), user_sim=_BrokenSim())
    assert recs[0].conversation == []                     # never got a turn in
    assert recs[0].error is not None
    assert recs[0].termination_reason == TERMINATION_ERROR


def test_multi_turn_without_sim_raises():
    ds = parse_dataset({"type": "multi_turn"},
                       [{"data_point_id": "m", "multi_turn_input": {"user_task": "t", "user_persona": "p"}}])
    with pytest.raises(EvalTypeError):
        generate(ds, InProcessArtifactClient(_echo))


class _CountingSim:
    """Wraps a real sim, counting create_session/ask/delete_session calls -- for asserting the
    generation loop's session lifecycle (create/ask/delete)."""

    def __init__(self, inner: ArtifactAgentUserSimulator):
        self._inner = inner
        self.create_calls = 0
        self.ask_calls = 0
        self.delete_calls = 0

    def create_session(self, sample):
        self.create_calls += 1
        return self._inner.create_session(sample)

    def ask(self, session_id, conversation):
        self.ask_calls += 1
        return self._inner.ask(session_id, conversation)

    def delete_session(self, session_id):
        self.delete_calls += 1
        self._inner.delete_session(session_id)


def test_multi_turn_session_lifecycle_create_once_ask_repeatedly_delete_once():
    ds = parse_dataset({"type": "multi_turn"},
                       [{"data_point_id": "m", "multi_turn_input": {"user_task": "t", "user_persona": "p"}}])
    sim = _CountingSim(ScriptedArtifactAgentUserSim(["hi", "more"]))
    generate(ds, InProcessArtifactClient(_echo), user_sim=sim)
    assert sim.create_calls == 1
    assert sim.ask_calls == 3          # "hi", "more", then the stop-signaling None
    assert sim.delete_calls == 1


class _RaisingAfterNSim:
    """A sim whose `ask` raises after `n_ok` successful turns -- for proving `delete_session`
    fires from `generation.py`'s `finally`, not just on the normal stop path."""

    def __init__(self, n_ok: int):
        self.create_calls = 0
        self.delete_calls = 0
        self._n_ok = n_ok
        self._count = 0

    def create_session(self, sample):
        self.create_calls += 1
        return "sid"

    def ask(self, session_id, conversation):
        if self._count >= self._n_ok:
            raise RuntimeError("sim exploded")
        self._count += 1
        return ArtifactAgentQuery(text=f"turn{self._count}")

    def delete_session(self, session_id):
        self.delete_calls += 1


def test_multi_turn_session_deleted_even_when_ask_raises_mid_loop():
    ds = parse_dataset({"type": "multi_turn"},
                       [{"data_point_id": "m", "multi_turn_input": {"user_task": "t", "user_persona": "p"}}])
    sim = _RaisingAfterNSim(n_ok=1)
    recs = generate(ds, InProcessArtifactClient(_echo), user_sim=sim)
    assert recs[0].error is not None and "sim exploded" in recs[0].error
    assert sim.create_calls == 1
    assert sim.delete_calls == 1       # cleaned up despite the mid-loop exception


def test_raising_client_records_error_not_crash():
    def boom(q):
        raise RuntimeError("artifact down")
    recs = generate(_single_ds(1), InProcessArtifactClient(boom))
    assert recs[0].error is not None and "artifact down" in recs[0].error
    assert len(recs[0].conversation) == 1        # the failed turn is recorded


def test_slow_client_times_out_fast():
    def slow(q):
        time.sleep(2.0)
        return ArtifactAgentOutput(text="late")
    start = time.monotonic()
    recs = generate(_single_ds(1), InProcessArtifactClient(slow), query_timeout_s=0.3)
    elapsed = time.monotonic() - start
    assert recs[0].error is not None and "timed out" in recs[0].error
    assert elapsed < 1.5                          # did not wait the full 2s


def test_store_integration_writes_one_json_per_unit(tmp_path):
    from appsci_bench.eval.store import TranscriptStore
    store = TranscriptStore(tmp_path)
    generate(_single_ds(2, repeat=2), InProcessArtifactClient(_echo), store=store)
    assert len(store.list_conversations()) == 4   # 2 samples x 2 repeats


def test_file_path_sourced_file_is_materialized_before_the_client_sees_it(tmp_path):
    p = tmp_path / "attachment.txt"
    p.write_bytes(b"attachment bytes")
    samples = [{"data_point_id": "d0", "single_turn_input": {
        "text": "q0", "files": [{"file_type": "txt", "file_name": "attachment.txt",
                                  "file_path": str(p)}]}}]
    ds = parse_dataset({"type": "single_turn"}, samples)
    seen = {}

    def capture(q: ArtifactAgentQuery) -> ArtifactAgentOutput:
        seen["file_data"] = q.files[0].file_data
        return ArtifactAgentOutput(text="ok")

    generate(ds, InProcessArtifactClient(capture))
    assert seen["file_data"] == b"attachment bytes"


def test_multi_turn_file_path_sourced_file_is_materialized_before_the_sim_can_attach_it(tmp_path):
    # The multi_turn sibling of the single_turn test above: _run_multi_turn's own
    # materialize_files(mti) call (generation.py) must resolve a file_path-sourced
    # multi_turn_input.files entry into real bytes BEFORE the sim's ask() resolves attach_files
    # against it -- otherwise the artifact would receive a dangling file_path, not the file.
    import json
    from unittest.mock import patch

    from appsci_bench.eval.artifact_agent_user_sim import ArtifactAgentUserSim

    p = tmp_path / "boarding_pass.pdf"
    p.write_bytes(b"boarding pass bytes")
    samples = [{"data_point_id": "m", "multi_turn_input": {
        "user_task": "t", "user_persona": "p",
        "files": [{"file_type": "pdf", "file_name": "boarding_pass.pdf", "file_path": str(p)}]}}]
    ds = parse_dataset({"type": "multi_turn"}, samples)
    seen = {}

    def capture(q: ArtifactAgentQuery) -> ArtifactAgentOutput:
        if q.files:
            seen["file_data"] = q.files[0].file_data
        return ArtifactAgentOutput(text="ok")

    replies = iter([
        json.dumps({"reasoning": "r", "stopped": False, "message": "here",
                   "attach_files": ["boarding_pass.pdf"]}),
        json.dumps({"reasoning": "r2", "stopped": True}),
    ])
    with patch("appsci_bench.eval.helpers.llm.complete", side_effect=lambda *a, **k: next(replies)):
        generate(ds, InProcessArtifactClient(capture), user_sim=ArtifactAgentUserSim())
    assert seen["file_data"] == b"boarding pass bytes"


# --- per-sample env-reset seam (setup_state / read_final_state) --------------------------------

def test_setup_state_runs_before_conversation_and_read_final_state_after():
    events = []

    def client_fn(q: ArtifactAgentQuery) -> ArtifactAgentOutput:
        events.append(("query", q.text))
        return ArtifactAgentOutput(text="ok")

    def setup_state(sample):
        events.append(("setup", sample.data_point_id))

    def read_final_state(sample):
        events.append(("read", sample.data_point_id))
        return {"env_state_hash": f"hash-{sample.data_point_id}"}

    recs = generate(_single_ds(1), InProcessArtifactClient(client_fn),
                    setup_state=setup_state, read_final_state=read_final_state)
    # order per unit: setup -> query -> read
    assert events == [("setup", "d0"), ("query", "q0"), ("read", "d0")]
    assert recs[0].final_state == {"env_state_hash": "hash-d0"}


def test_hooks_force_sequential_execution():
    # If units ran concurrently, the shared `inside` flag would be observed True by another unit.
    inside = {"n": 0}
    overlap = {"seen": False}

    def setup_state(sample):
        inside["n"] += 1
        if inside["n"] > 1:
            overlap["seen"] = True

    def client_fn(q):
        return ArtifactAgentOutput(text="ok")

    def read_final_state(sample):
        inside["n"] -= 1
        return {"env_state_hash": "h"}

    generate(_single_ds(6), InProcessArtifactClient(client_fn),
             setup_state=setup_state, read_final_state=read_final_state)
    assert overlap["seen"] is False        # never two units in flight at once


def test_read_final_state_failure_is_recorded_not_crash():
    def boom_read(sample):
        raise RuntimeError("state read failed")
    recs = generate(_single_ds(1), InProcessArtifactClient(_echo), read_final_state=boom_read)
    assert recs[0].error is not None and "state read failed" in recs[0].error
    assert recs[0].final_state is None


def test_read_final_state_skipped_after_conversation_error_and_original_error_preserved():
    # A conversation-phase failure (the artifact query raises) must (a) skip read_final_state entirely
    # and (b) NOT be overwritten by it -- the more-informative conversation error is what surfaces.
    calls = {"read": 0}

    def boom_client(q):
        raise RuntimeError("artifact down")

    def read_final_state(sample):
        calls["read"] += 1
        return {"env_state_hash": "h"}

    recs = generate(_single_ds(1), InProcessArtifactClient(boom_client),
                    read_final_state=read_final_state)
    assert calls["read"] == 0                              # not called once the conversation failed
    assert recs[0].error is not None and "artifact down" in recs[0].error   # original error preserved
    assert recs[0].final_state is None
