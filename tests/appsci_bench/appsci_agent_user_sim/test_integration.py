"""Happy-path integration test: the appsci agent user sim against a REAL task spec + persona.

Unlike the unit tests (which use a MockTaskSpec), this drives the full wiring end to end:
  load_task_context(real spec.yaml) -> AppsciAgentLLMUserSim.from_persona(real persona.yaml) -> multi-turn
  answer() with accumulating history.
The LLM call is mocked (deterministic, no network), so this asserts the *wiring/happy path*, not
behavioral quality (that's the later regulation suites).

Parametrized over a real task's personas; defaults to order_to_cash (the fully fleshed task).
"""
from __future__ import annotations

import json
import pathlib
from unittest.mock import patch

import pytest

from appsci_bench.common.model_config import ModelConfig
from appsci_bench.appsci_agent_user_sim.core.task_context import load_task_context
from appsci_bench.appsci_agent_user_sim.core.usersim_core import ROLE_AGENT, ROLE_SIM, AppsciAgentLLMUserSim

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# (task_id, persona_file) pairs to exercise. order_to_cash is the only fully fleshed task today;
# add more here as tasks get real personas -- the test body is task-agnostic.
TASK_PERSONAS = [
    ("order_to_cash", "l1.yaml"),
    ("order_to_cash", "l2.yaml"),
    ("order_to_cash", "l3.yaml"),
]

_CFG = ModelConfig(model="anthropic/claude-opus-4-8", api_key="no-token")


def _spec_path(task_id: str) -> pathlib.Path:
    return REPO_ROOT / "tasks" / task_id / "spec.yaml"


def _persona_path(task_id: str, persona_file: str) -> pathlib.Path:
    return REPO_ROOT / "tasks" / task_id / "user_personas" / persona_file


@pytest.mark.parametrize("task_id,persona_file", TASK_PERSONAS)
class TestSimAgainstRealSpec:
    """Drive AppsciAgentLLMUserSim against a real spec+persona with a mocked model."""

    def test_from_persona_builds_with_persona_content(self, task_id, persona_file):
        task = load_task_context(_spec_path(task_id))
        sim = AppsciAgentLLMUserSim.from_persona(task, _persona_path(task_id, persona_file), _CFG)
        # The rendered system prompt should carry the universal contract + the task description.
        assert "Your Behavioral Contract" in sim._system_prompt
        assert task.description.strip()[:20] in sim._system_prompt

    def test_single_answer_happy_path(self, task_id, persona_file):
        task = load_task_context(_spec_path(task_id))
        sim = AppsciAgentLLMUserSim.from_persona(task, _persona_path(task_id, persona_file), _CFG)
        with patch("appsci_bench.appsci_agent_user_sim.core.usersim_core.complete",
                   return_value='{"message": "Here is what I can tell you."}'):
            out = sim.answer("What does success look like?", [])
        assert out == "Here is what I can tell you."

    def test_multi_turn_history_flows(self, task_id, persona_file):
        """A 3-turn conversation: history accumulates and the role flip reaches the model each turn."""
        task = load_task_context(_spec_path(task_id))
        sim = AppsciAgentLLMUserSim.from_persona(task, _persona_path(task_id, persona_file), _CFG)

        history: list[dict] = []
        replies = iter([
            '{"message": "answer one"}',
            '{"message": "answer two"}',
            '{"message": "answer three"}',
        ])
        sent_message_lists = []

        def fake_complete(messages, config, **kwargs):
            sent_message_lists.append(messages)
            return next(replies)

        questions = ["q1", "q2", "q3"]
        with patch("appsci_bench.appsci_agent_user_sim.core.usersim_core.complete", side_effect=fake_complete):
            for q in questions:
                reply = sim.answer(q, history)
                history.append({"role": ROLE_AGENT, "content": q})
                history.append({"role": ROLE_SIM, "content": reply})

        # Each reply parsed + non-empty.
        assert [h["content"] for h in history if h["role"] == ROLE_SIM] == [
            "answer one", "answer two", "answer three",
        ]
        # The model saw a growing message list: turn 1 = system+user(q1); turn 3 = system + 5 prior.
        assert len(sent_message_lists[0]) == 2                      # system, user(q1)
        assert len(sent_message_lists[2]) == 6                      # system + (q1,a1,q2,a2) + user(q3)
        # Role flip: first is system, last is the new agent question as 'user'.
        for msgs in sent_message_lists:
            assert msgs[0]["role"] == "system"
            assert msgs[-1]["role"] == "user"
        # Replayed sim turns are 'assistant', replayed agent turns are 'user' (the flip).
        third = sent_message_lists[2]
        assert third[1]["role"] == "user" and third[1]["content"] == "q1"
        # Replayed sim turn is re-wrapped as the {"message": ...} JSON the persona asks for, so the
        # model keeps emitting JSON on later turns (guards the turn-2 prose-drift 500).
        assert third[2]["role"] == "assistant"
        assert json.loads(third[2]["content"]) == {"message": "answer one"}
