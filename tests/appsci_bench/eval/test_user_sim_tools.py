"""Seam D: the eval-time user simulator calling the CUSTOMER's own tools.

Some domains put the deciding action on the customer's side -- they submit their own application and
the artifact can only advise. 49 of banking's 97 cases have a golden action whose requestor is the
user, so a simulator that can only talk could never reach the golden state there, however good the
artifact's advice was: the case would score 0 for a reason with nothing to do with the artifact.

This file pins both halves of that:

  * the capability works -- a granted tool is executed through the injected executor, and the sim
    remembers it acted (a tool call never appears in the transcript, so without that memory it would
    re-submit the same application every turn);
  * the capability is INERT when unused -- a sample with no `user_tools` renders the exact prompt it
    rendered before, gets the unmodified system prompt, and never touches an executor. This file is
    shared with airline, order_to_cash and slides_generator, so "changes nothing for them" is a
    property worth testing, not assuming.

Creds-free: the LLM transport is stubbed at the import site, the executor is a fake.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from appsci_bench.eval.artifact_agent_user_sim import (
    _SYSTEM_PROMPT,
    _TOOL_INSTRUCTIONS,
    ArtifactAgentUserSim,
    render_sim_prompt,
)
from appsci_bench.eval.datatypes import MultiTurnInput, UserTool


def _sample(**over) -> MultiTurnInput:
    base = dict(user_task="Get a no-fee cashback card.", user_persona="You are Sarah Bosch.")
    base.update(over)
    # Bare names are accepted so the many call sites below stay about the behaviour they test rather
    # than about tool signatures; `UserTool.from_dict` is the same coercion the dataset loader applies.
    if base.get("user_tools"):
        base["user_tools"] = [UserTool.from_dict(t) for t in base["user_tools"]]
    return MultiTurnInput(**base)


def _turn_json(**over) -> str:
    """A model response for one sim turn, as the structured-output transport would return it."""
    payload = {"reasoning": "thinking", "stopped": False, "message": "ok, done"}
    payload.update(over)
    return json.dumps(payload)


class _FakeExecutor:
    """Records calls; returns a canned result or raises when told to."""

    def __init__(self, result="application submitted", error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        return self.result


def test_tool_only_actions_can_continue_past_sixteen_until_shared_budget_is_exhausted():
    from appsci_bench.common.model_config import ModelConfig
    from appsci_bench.eval.step_budget import LocalStepBudget, MaxStepsReached

    executor = _FakeExecutor()
    sim = ArtifactAgentUserSim(config=ModelConfig(model="fake/model"), tool_executor=executor)
    session = sim.create_session(_sample(user_tools=["apply_for_credit_card"]))
    budget = LocalStepBudget(200)
    action = _turn_json(message="", call_user_tool={"name": "apply_for_credit_card", "arguments": {}})
    with patch("appsci_bench.eval.helpers.llm.complete", side_effect=[action] * 17 + [_turn_json()]):
        assert sim.ask(session, [], step_budget=budget).text == "ok, done"
    assert len(executor.calls) == 17
    assert budget.snapshot()["steps"] == 34
    # The next customer turn uses the SAME budget, not another tool-only allowance.
    budget.consume(165)
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=action) as complete:
        with pytest.raises(MaxStepsReached):
            sim.ask(session, [], step_budget=budget)
    assert complete.call_count == 1
    assert len(executor.calls) == 18
    assert budget.snapshot()["steps"] == 201
    sim.delete_session(session)
    with pytest.raises(ValueError, match="unknown or deleted session_id"):
        sim.ask(session, [], step_budget=budget)


def _sim_with(turn_json: str, executor=None) -> tuple[ArtifactAgentUserSim, object]:
    """A sim whose single model call returns `turn_json`. Patches the transport, not the sim."""
    from appsci_bench.common.model_config import ModelConfig
    sim = ArtifactAgentUserSim(config=ModelConfig(model="fake/model"), tool_executor=executor)
    return sim, patch("appsci_bench.eval.helpers.llm.complete", return_value=turn_json)


def test_tool_calls_have_no_implicit_step_limit_without_a_shared_budget():
    executor = _FakeExecutor()
    sim, _ = _sim_with(_turn_json(), executor)
    session = sim.create_session(_sample(user_tools=["apply_for_credit_card"]))
    action = _turn_json(message="", call_user_tool={"name": "apply_for_credit_card", "arguments": {}})
    with patch("appsci_bench.eval.helpers.llm.complete", side_effect=[action] * 101 + [_turn_json()]):
        assert sim.ask(session, [], step_budget=None).text == "ok, done"
    assert len(executor.calls) == 101  # More than 200 request/result steps, with no configured ceiling.
    sim.delete_session(session)


# --------------------------------------------------------------------------- inert when unused

def test_prompt_is_unchanged_for_a_sample_without_user_tools() -> None:
    """The whole tools block is conditional, so a task that does not model user actions is untouched."""
    prompt = render_sim_prompt(sample=_sample(), conversation=[])

    assert "your own tools" not in prompt.lower()
    assert "tools you have already used" not in prompt.lower()


def test_used_tools_are_not_rendered_without_granted_tools() -> None:
    """Even if a history somehow existed, no grant means no block -- one condition guards both."""
    prompt = render_sim_prompt(
        sample=_sample(), conversation=[],
        used_tools=[{"name": "x", "arguments": {}, "outcome": "ok"}],
    )
    assert "already used" not in prompt.lower()


def test_system_prompt_gains_tool_instructions_only_when_tools_are_granted() -> None:
    """`llm_judge_call` appends its own schema instructions, so compare on the tool block itself
    rather than on the whole system prompt."""
    sim, transport = _sim_with(_turn_json())
    with transport as complete:
        session = sim.create_session(_sample())          # no user_tools
        sim.ask(session, [])
        assert _TOOL_INSTRUCTIONS not in complete.call_args.args[0][0]["content"]

    sim2, transport2 = _sim_with(_turn_json())
    with transport2 as complete2:
        session2 = sim2.create_session(_sample(user_tools=["apply_for_credit_card"]))
        sim2.ask(session2, [])
        assert _TOOL_INSTRUCTIONS in complete2.call_args.args[0][0]["content"]


def test_executor_is_never_called_when_the_model_asks_for_no_tool() -> None:
    executor = _FakeExecutor()
    sim, transport = _sim_with(_turn_json(), executor)
    with transport:
        session = sim.create_session(_sample(user_tools=["apply_for_credit_card"]))
        query = sim.ask(session, [])

    assert executor.calls == []
    assert query is not None and query.text == "ok, done"


# --------------------------------------------------------------------------- the capability

def test_granted_tool_is_executed_and_the_message_still_reaches_the_artifact() -> None:
    """Acting and speaking happen in ONE turn: the customer submits, then says so."""
    executor = _FakeExecutor()
    call = {"name": "apply_for_credit_card", "arguments": {"card_type": "Gold Rewards Card"}}
    sim, transport = _sim_with(_turn_json(call_user_tool=call), executor)

    with transport:
        session = sim.create_session(_sample(user_tools=["apply_for_credit_card"]))
        query = sim.ask(session, [])

    assert executor.calls == [("apply_for_credit_card", {"card_type": "Gold Rewards Card"})]
    assert query is not None and query.text == "ok, done"


def test_tool_only_step_executes_then_speaks_with_result_and_coerces_json_string() -> None:
    """Regression for banking cases such as tau2_banking_31.

    GPT-5.2 emits an empty message while it calls ``call_discoverable_user_tool`` and naturally
    represents that wrapper's JSON-string ``arguments`` parameter as a nested object. The harness
    must execute the action, expose its result to the next simulator step, and send only the later
    spoken message to the artifact.
    """
    from appsci_bench.common.model_config import ModelConfig

    executor = _FakeExecutor(result="last four digits: 5320")
    sample = _sample(user_tools=[{
        "name": "call_discoverable_user_tool",
        "description": "Call a tool that was given to you by the agent.",
        "parameters": {
            "type": "object",
            "properties": {
                "discoverable_tool_name": {"type": "string"},
                "arguments": {"type": "string"},
            },
            "required": ["discoverable_tool_name"],
        },
    }])
    tool_only = _turn_json(
        message="",
        call_user_tool={
            "name": "call_discoverable_user_tool",
            "arguments": {
                "discoverable_tool_name": "get_card_last_4_digits",
                "arguments": {"credit_card_account_id": "cc_890389b165_silver"},
            },
        },
    )
    spoken = _turn_json(message="The last four digits are 5320.")
    sim = ArtifactAgentUserSim(
        config=ModelConfig(model="fake/model"), tool_executor=executor)

    with patch(
        "appsci_bench.eval.helpers.llm.complete", side_effect=[tool_only, spoken],
    ) as complete:
        session = sim.create_session(sample)
        query = sim.ask(session, [])

    assert len(executor.calls) == 1
    name, arguments = executor.calls[0]
    assert name == "call_discoverable_user_tool"
    assert arguments["discoverable_tool_name"] == "get_card_last_4_digits"
    assert json.loads(arguments["arguments"]) == {
        "credit_card_account_id": "cc_890389b165_silver"}
    assert query is not None and query.text == "The last four digits are 5320."
    assert complete.call_count == 2
    assert "last four digits: 5320" in complete.call_args.args[0][-1]["content"]


def test_the_sim_remembers_it_acted() -> None:
    """A tool call is not something said to the artifact, so it is absent from the transcript. Without
    this memory the sim would re-submit the same application on every subsequent turn."""
    executor = _FakeExecutor()
    call = {"name": "apply_for_credit_card", "arguments": {"card_type": "Gold"}}
    sim, transport = _sim_with(_turn_json(call_user_tool=call), executor)

    with transport:
        session = sim.create_session(_sample(user_tools=["apply_for_credit_card"]))
        sim.ask(session, [])
        history = sim._tool_calls[session]

    assert len(history) == 1
    assert history[0]["name"] == "apply_for_credit_card"
    assert history[0]["outcome"].startswith("ok:")

    rendered = render_sim_prompt(
        sample=_sample(user_tools=["apply_for_credit_card"]), conversation=[], used_tools=history)
    assert "already used" in rendered.lower()
    assert "apply_for_credit_card" in rendered


def test_a_tool_the_case_does_not_grant_is_refused() -> None:
    """The model must not invent capabilities, and must not borrow the artifact's."""
    executor = _FakeExecutor()
    call = {"name": "change_user_email", "arguments": {}}       # an AGENT tool, not the customer's
    sim, transport = _sim_with(_turn_json(call_user_tool=call), executor)

    with transport:
        session = sim.create_session(_sample(user_tools=["apply_for_credit_card"]))
        sim.ask(session, [])
        history = sim._tool_calls[session]

    assert executor.calls == [], "an ungranted tool must never reach the environment"
    assert history[0]["outcome"].startswith("refused:")


def test_a_failing_tool_is_recorded_not_raised() -> None:
    """A rejected customer action is a graded outcome the conversation carries on around."""
    executor = _FakeExecutor(error=RuntimeError("income too low"))
    call = {"name": "apply_for_credit_card", "arguments": {}}
    sim, transport = _sim_with(_turn_json(call_user_tool=call), executor)

    with transport:
        session = sim.create_session(_sample(user_tools=["apply_for_credit_card"]))
        query = sim.ask(session, [])
        history = sim._tool_calls[session]

    assert query is not None, "a failed user action must not abort the case"
    assert "income too low" in history[0]["outcome"]


def test_a_granted_tool_with_no_executor_is_flagged_not_silently_dropped(caplog) -> None:
    """If a run forgets the executor, the case becomes unwinnable -- that must be visible in the log,
    not an invisible zero."""
    call = {"name": "apply_for_credit_card", "arguments": {}}
    sim, transport = _sim_with(_turn_json(call_user_tool=call), None)

    with transport, caplog.at_level("WARNING"):
        session = sim.create_session(_sample(user_tools=["apply_for_credit_card"]))
        sim.ask(session, [])
        history = sim._tool_calls[session]

    assert history[0]["outcome"].startswith("unavailable:")
    assert "no tool executor" in caplog.text


def test_a_tool_call_on_the_stopping_turn_still_executes() -> None:
    """The customer's last act may be the deciding one -- submitting, then hanging up."""
    executor = _FakeExecutor()
    call = {"name": "apply_for_credit_card", "arguments": {"card_type": "Gold"}}
    sim, transport = _sim_with(_turn_json(stopped=True, message=None, call_user_tool=call), executor)

    with transport:
        session = sim.create_session(_sample(user_tools=["apply_for_credit_card"]))
        query = sim.ask(session, [])

    assert query is None, "stopped means the conversation ends"
    assert executor.calls == [("apply_for_credit_card", {"card_type": "Gold"})]


# --------------------------------------------------------------------------- session hygiene

def test_tool_history_is_per_session() -> None:
    """Concurrent eval workers each drive their own conversation; histories must not bleed."""
    executor = _FakeExecutor()
    call = {"name": "apply_for_credit_card", "arguments": {}}
    sim, transport = _sim_with(_turn_json(call_user_tool=call), executor)

    with transport:
        a = sim.create_session(_sample(user_tools=["apply_for_credit_card"]))
        b = sim.create_session(_sample(user_tools=["apply_for_credit_card"]))
        sim.ask(a, [])

        assert len(sim._tool_calls[a]) == 1
        assert sim._tool_calls[b] == []


def test_deleting_a_session_drops_its_tool_history() -> None:
    sim, transport = _sim_with(_turn_json())
    with transport:
        session = sim.create_session(_sample(user_tools=["apply_for_credit_card"]))
    sim.delete_session(session)

    assert session not in sim._tool_calls
    with pytest.raises(ValueError):
        sim.delete_session(session)


# --------------------------------------------------------------------------- the data carries through

def test_multi_turn_input_round_trips_user_tools() -> None:
    sample = _sample(user_tools=["apply_for_credit_card", "submit_referral"])
    assert MultiTurnInput.from_dict(sample.to_dict()).user_tools == sample.user_tools


def test_multi_turn_input_defaults_to_no_tools() -> None:
    """Every existing task's samples omit the field entirely."""
    parsed = MultiTurnInput.from_dict({"user_task": "t", "user_persona": "p"})
    assert parsed.user_tools == []


# --- tool SIGNATURES reach the prompt ------------------------------------------------------------
# The simulator used to be handed bare tool names while its instructions forbade inventing argument
# values, so it could only act on tools that take none. Measured on
# banking_support_b1.t0_l3_20260910_031639: 3 of 3 on the zero-argument tool, 1 of 6 on the two-argument
# one whose values are spoken aloud, 0 of 19 on the three-argument one. A case whose golden outcome
# needs a customer-side action then failed as though the artifact had given bad advice.
#
# The signature rides in the SAMPLE, not in a task-level pointer resolved per run: every other per-case
# fact about this simulator already lives there, and `task_runner` also starts the build phase's
# environment, which has no customer simulator to configure.

def test_the_prompt_states_each_granted_tools_arguments():
    sample = _sample(user_tools=[{
        "name": "apply_for_credit_card",
        "description": "Apply for a credit card.",
        "parameters": {"properties": {"card_type": {}, "annual_income": {}, "referral": {}},
                       "required": ["card_type", "annual_income"]},
    }])
    out = render_sim_prompt(sample=sample, conversation=[])
    assert "required arguments: card_type, annual_income" in out
    assert "optional arguments: referral" in out
    assert "Apply for a credit card." in out


def test_a_tool_taking_no_arguments_says_so_rather_than_going_silent():
    """The distinction the simulator has to act on: 'no arguments' and 'arguments I was not told' look
    identical when only a name is rendered, and it is the second that used to stall it."""
    sample = _sample(user_tools=[{
        "name": "request_human_agent_transfer", "description": "Ask for a human.",
        "parameters": {"properties": {}, "required": []},
    }])
    out = render_sim_prompt(sample=sample, conversation=[])
    assert "takes no arguments" in out


def test_a_tool_declared_as_a_bare_name_still_renders():
    """Degrades rather than crashing: a hand-authored dataset that gives no signature behaves exactly
    as every dataset did before signatures existed."""
    sample = _sample(user_tools=["mystery_tool"])
    out = render_sim_prompt(sample=sample, conversation=[])
    assert "- mystery_tool" in out


def test_a_signature_survives_the_sample_round_trip():
    """The signature is DATA now, so the loader has to carry it -- a round trip that dropped it would
    put the simulator back on bare names without failing anything else."""
    sample = _sample(user_tools=[{
        "name": "submit_referral", "description": "Refer someone.",
        "parameters": {"properties": {"user_id": {}, "account_type": {}},
                       "required": ["user_id", "account_type"]},
    }])
    parsed = MultiTurnInput.from_dict(sample.to_dict())
    assert parsed.user_tools == sample.user_tools
    assert "required arguments: user_id, account_type" in render_sim_prompt(
        sample=parsed, conversation=[])


def test_a_bare_name_round_trips_as_a_name():
    """to_dict omits the empty description/parameters rather than writing nulls, so a dataset that
    declared a bare name keeps declaring one."""
    assert MultiTurnInput.from_dict(_sample(user_tools=["t"]).to_dict()).user_tools == [UserTool("t")]
    assert _sample(user_tools=["t"]).to_dict()["user_tools"] == [{"name": "t"}]
