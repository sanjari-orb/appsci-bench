"""Simulation budgets stop on steps, including tools and the final stop message."""
from unittest.mock import patch

import pytest

from appsci_bench.common.model_config import ModelConfig
from appsci_bench.eval.artifact_agent_user_sim import (
    ArtifactAgentUserSim, ScriptedArtifactAgentUserSim, _SimTurn,
)
from appsci_bench.eval.artifact_client import InProcessArtifactClient
from appsci_bench.eval.dataset import parse_dataset
from appsci_bench.eval.datatypes import ArtifactAgentOutput, GenerationRecord
from appsci_bench.eval.generation import generate
from appsci_bench.eval.step_budget import LocalStepBudget, MaxStepsReached, check_budget
from appsci_bench.eval.store import TranscriptStore


def dataset(max_steps):
    return parse_dataset({"type": "multi_turn", "max_steps": max_steps}, [{
        "data_point_id": "case", "multi_turn_input": {"user_task": "t", "user_persona": "p"},
    }])


def test_step_configuration_replaces_the_default_exchange_limit():
    ds = dataset(200)
    assert ds.max_turns is None
    assert ds.max_steps == 200


@pytest.mark.parametrize("config,turns,reason,steps", [
    ({"max_turns": 250}, 250, "max_turns", None),
    ({"max_turns": 250, "max_steps": None}, 250, "max_turns", None),
    ({"max_steps": 50}, 25, "max_steps", {"steps": 50, "max_steps": 50}),
    ({"max_steps": 500}, 250, "max_steps", {"steps": 500, "max_steps": 500}),
])
def test_real_simulator_uses_only_the_configured_conversation_limit(config, turns, reason, steps):
    ds = parse_dataset({"type": "multi_turn", **config}, [{
        "data_point_id": "case", "multi_turn_input": {"user_task": "t", "user_persona": "p"},
    }])
    sim = ArtifactAgentUserSim(config=ModelConfig(model="fake/model"))
    turn = _SimTurn(reasoning="continue", stopped=False, message="next")
    with patch("appsci_bench.eval.artifact_agent_user_sim.llm_judge_call", return_value=turn):
        rec, = generate(ds, InProcessArtifactClient(lambda q: ArtifactAgentOutput(text="ok")),
                        user_sim=sim, query_timeout_s=None)
    assert rec.error is None
    assert len(rec.conversation) == turns
    assert rec.termination_reason == reason
    assert rec.simulation_steps == steps


@pytest.mark.parametrize("extra", [
    {"max_steps": 0}, {"max_steps": True}, {"max_steps": "200"},
    {"max_steps": 200, "max_turns": 20}, {"max_steps": 200, "type": "single_turn"},
])
def test_rejects_invalid_or_ambiguous_budget(extra):
    with pytest.raises(ValueError):
        parse_dataset({"type": "multi_turn", **extra}, [])


def test_tool_result_finishes_even_when_call_reaches_ceiling():
    budget = LocalStepBudget(4)
    budget.consume(3)
    budget.consume(2)
    assert budget.snapshot() == {"steps": 5, "max_steps": 4}
    with pytest.raises(MaxStepsReached):
        check_budget(budget)
    with pytest.raises(MaxStepsReached):
        budget.consume()
    assert budget.snapshot()["steps"] == 5


@pytest.mark.parametrize("limit,reason,turns,steps", [
    (200, "user_stop", 25, 51),  # exceeds the old 20-exchange limit
    (50, "max_steps", 25, 50),  # do not make an extra simulator call at the ceiling
    (51, "max_steps", 25, 51),  # upstream ceiling overrides a stop on the same step
    (1, "max_steps", 0, 1),
])
def test_speakers_share_one_budget(limit, reason, turns, steps, tmp_path):
    store = TranscriptStore(tmp_path)
    rec, = generate(dataset(limit), InProcessArtifactClient(lambda q: ArtifactAgentOutput(text="ok")),
                    user_sim=ScriptedArtifactAgentUserSim(["next"] * 25), store=store)
    assert rec.termination_reason == reason
    assert rec.error is None
    assert len(rec.conversation) == turns
    assert rec.simulation_steps == {"steps": steps, "max_steps": limit}
    assert store.load(store.list_conversations()[0])["simulation_steps"] == rec.simulation_steps
    assert GenerationRecord.from_dict(rec.to_dict()).simulation_steps == rec.simulation_steps


def test_case_budget_is_fresh_for_each_repeat():
    ds = dataset(4)
    ds.repeat = 2
    recs = generate(ds, InProcessArtifactClient(lambda q: ArtifactAgentOutput(text="ok")),
                    user_sim=ScriptedArtifactAgentUserSim(["next"] * 5))
    assert [r.simulation_steps["steps"] for r in recs] == [4, 4]
    assert all(r.termination_reason == "max_steps" and r.error is None for r in recs)
