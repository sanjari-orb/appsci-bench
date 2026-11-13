"""Happy-path integration test: the artifact agent user sim against a REAL task dataset sample.

Unlike the unit tests (`test_artifact_agent_user_sim.py`, synthetic `MultiTurnInput` fixtures),
this drives the full wiring end to end:
  load_task_spec(real spec.yaml) -> load_dataset -> ArtifactAgentUserSim.create_session(real sample)
  -> repeated ask() against an in-process stub artifact reply -> stopped=True -> delete_session().
The LLM call is mocked (deterministic, no network), so this asserts the *wiring/happy path*, not
behavioral quality (does the sim stay in character, stop at a reasonable point, etc. -- that needs
a real model and its own opt-in suite, not built here).

Parametrized over real airline_rebooking samples -- the only task with a real multi_turn dataset
today.
"""
from __future__ import annotations

import dataclasses
import json
from unittest.mock import patch

import pytest

from appsci_bench.core.spec import load_task_spec
from appsci_bench.eval.artifact_agent_user_sim import ArtifactAgentUserSim
from appsci_bench.eval.dataset import load_dataset
from appsci_bench.eval.datatypes import ArtifactAgentOutput, ConversationTurn, File, MultiTurnInput

DATA_POINTS = ["tau2_airline_0", "tau2_airline_5", "tau2_airline_20"]


def _real_sample(data_point_id: str) -> MultiTurnInput:
    spec = load_task_spec("airline_rebooking")
    dataset = load_dataset(spec)
    sample = next(s for s in dataset.data if s.data_point_id == data_point_id)
    assert sample.multi_turn_input is not None  # every airline_rebooking sample is multi_turn
    return sample.multi_turn_input


@pytest.mark.parametrize("data_point_id", DATA_POINTS)
class TestSimAgainstRealDataset:
    """Drive ArtifactAgentUserSim against a real sample with a mocked model."""

    def test_drives_a_full_conversation_to_a_stop(self, data_point_id):
        mti = _real_sample(data_point_id)
        assert mti.user_task and mti.user_persona  # both real content, no placeholder

        sim = ArtifactAgentUserSim()
        sid = sim.create_session(mti)
        conversation: list[ConversationTurn] = []
        replies = iter([
            json.dumps({"reasoning": "opening", "stopped": False, "message": "turn one"}),
            json.dumps({"reasoning": "still going", "stopped": False, "message": "turn two"}),
            json.dumps({"reasoning": "criteria met", "stopped": True, "message": None}),
        ])
        try:
            with patch("appsci_bench.eval.helpers.llm.complete",
                      side_effect=lambda *_args, **_kwargs: next(replies)):
                query = sim.ask(sid, conversation)
                for _ in range(10):  # generation.py's real ceiling; 10 is plenty for this script
                    if query is None:
                        break
                    conversation.append(ConversationTurn(query, ArtifactAgentOutput(text="ack")))
                    query = sim.ask(sid, conversation)
        finally:
            sim.delete_session(sid)

        # Accumulating, coherent transcript: exactly the two real turns, in order, before the stop.
        assert query is None
        assert [t.query.text for t in conversation] == ["turn one", "turn two"]
        assert [t.response.text for t in conversation] == ["ack", "ack"]

    def test_ask_after_delete_session_raises(self, data_point_id):
        mti = _real_sample(data_point_id)
        sim = ArtifactAgentUserSim()
        sid = sim.create_session(mti)
        sim.delete_session(sid)
        with pytest.raises(ValueError):
            sim.ask(sid, [])


def test_files_attach_and_never_leak_across_sessions():
    # airline_rebooking's real samples carry no files, so this attaches a synthetic file to two
    # real samples' otherwise-real task/persona content -- the one thing the parametrized
    # happy-path test above can't exercise with real data alone.
    mti_a = dataclasses.replace(
        _real_sample("tau2_airline_0"), files=[File(file_type="pdf", file_name="a_only.pdf")])
    mti_b = dataclasses.replace(
        _real_sample("tau2_airline_5"), files=[File(file_type="pdf", file_name="b_only.pdf")])

    sim = ArtifactAgentUserSim()
    sid_a = sim.create_session(mti_a)
    sid_b = sim.create_session(mti_b)
    try:
        with patch("appsci_bench.eval.helpers.llm.complete",
                  return_value=json.dumps({"reasoning": "attaching", "stopped": False,
                                           "message": "here", "attach_files": ["a_only.pdf"]})):
            query_a = sim.ask(sid_a, [])
        with patch("appsci_bench.eval.helpers.llm.complete",
                  return_value=json.dumps({"reasoning": "attaching", "stopped": False,
                                           "message": "here", "attach_files": ["a_only.pdf"]})):
            # session b's model call names A's file -- must NOT resolve, since b's sample never had it.
            query_b = sim.ask(sid_b, [])
    finally:
        sim.delete_session(sid_a)
        sim.delete_session(sid_b)

    assert query_a is not None and query_b is not None
    assert [f.file_name for f in query_a.files] == ["a_only.pdf"]
    assert query_b.files == []  # b's sample has no file named "a_only.pdf" -- correctly filtered out
