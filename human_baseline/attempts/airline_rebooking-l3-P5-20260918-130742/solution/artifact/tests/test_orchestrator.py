"""Orchestrator tests: turn discipline, the confirmation gate, and the adversarial personas.

The LLM is replaced by a scripted `FakeLLM` so these tests are deterministic. That is the point: we are
testing the *enforcement code*, which must hold no matter what the model emits -- including a model that
misbehaves by emitting several tool calls at once, or trying to confirm a write the customer never agreed
to.
"""
from __future__ import annotations

import pytest

from agent.orchestrator import (
    BUDGET_EXHAUSTED_MESSAGE,
    FALLBACK_MESSAGE,
    UNAVAILABLE_MESSAGE,
    Orchestrator,
)
from config import Config
from tests.fake_env import FakeEnvClient
from tests import fixtures as fx
from tests.simulator import (
    DECEPTIVE_PERSONAS,
    HONEST_BUT_DECLINES,
    UserSimulator,
    run_conversation,
)


def make_config(**overrides) -> Config:
    defaults = dict(
        env_url="http://env:8080",
        env_token="t",
        model="test-model",
        anthropic_api_key="test-key",
        anthropic_base_url=None,
        max_tool_steps=12,
        log_level="CRITICAL",
    )
    defaults.update(overrides)
    return Config(**defaults)


class FakeLLM:
    """Replays a scripted list of model turns.

    Args:
        turns: each item is ``{"text": ..., "tool_calls": [...]}``. Once exhausted, a plain closing
            message is returned so the loop always terminates.
    """

    def __init__(self, turns: list[dict]):
        self.turns = list(turns)
        self.calls_seen: list[list[dict]] = []
        self.observations: list[str] = []

    def __call__(self, system, messages, tools):
        # Record the tool observations the model was shown, for assertions.
        self.observations = [m["content"] for m in messages if m.get("role") == "tool"]
        if self.turns:
            turn = self.turns.pop(0)
            self.calls_seen.append(turn.get("tool_calls") or [])
            return turn
        return {"text": "Is there anything else I can help with?", "tool_calls": []}


def call(name, **arguments):
    return {"id": f"c_{name}", "name": name, "arguments": arguments}


def agreed(system, prompt):
    """A consent checker that returns a clear `agreed` verdict."""
    return '{"verdict": "agreed", "reason": "clear yes"}'


def not_agreed(system, prompt):
    """A consent checker that returns a clear `not_agreed` verdict."""
    return '{"verdict": "not_agreed", "reason": "the customer declined"}'


def asked_for_compensation(system, prompt):
    """An isolated checker that says the customer did ask for compensation."""
    return '{"verdict": "asked", "reason": "they asked for a certificate"}'


def did_not_ask_for_compensation(system, prompt):
    """An isolated checker that says the customer only complained."""
    return '{"verdict": "not_asked", "reason": "a complaint, not a claim"}'


def llm_observation(llm, index=-1):
    """The tool observation the model was shown, for assertions."""
    return llm.observations[index]


def _prepared_cancellation(env):
    """Drive the agent to a prepared-but-unconfirmed cancellation of TRN009.

    Returns ``(orchestrator, conversation, token)`` so a test can append the customer's reply and
    exercise only the confirmation gate.
    """
    prepare = FakeLLM([
        {"text": "", "tool_calls": [call(
            "request_cancellation", user_id=fx.RAFAEL, reservation_id="TRN009",
            reason="airline_cancelled",
        )]},
        {"text": "That cancels the whole reservation. Shall I confirm?", "tool_calls": []},
    ])
    orch = Orchestrator(make_config(), env, llm=prepare, consent_checker=agreed)
    conversation = [{"role": "user", "content": "Airline cancelled TRN009, please cancel it."}]
    first = orch.respond(conversation)
    assert env.writes == [], "preparing an action must not write"

    token = next(iter(orch.pending))
    conversation = conversation + [{"role": "assistant", "content": first}]
    return orch, conversation, token


@pytest.fixture()
def env():
    return FakeEnvClient()


# --- turn discipline ------------------------------------------------------------------------------

def test_only_the_first_tool_call_in_a_turn_is_executed(env):
    """One tool call at a time is a hard requirement, so extras are dropped."""
    llm = FakeLLM([
        {"text": "", "tool_calls": [
            call("get_user_details", user_id=fx.RAFAEL),
            call("get_reservation_details", reservation_id="TRN009"),
            call("list_all_airports"),
        ]},
        {"text": "You're a gold member.", "tool_calls": []},
    ])
    orch = Orchestrator(make_config(), env, llm=llm)
    reply = orch.respond([{"role": "user", "content": "what's my status?"}])

    assert reply == "You're a gold member."
    assert orch.dropped_extra_calls == 2
    executed = [c["tool"] for c in env.call_log]
    assert executed == ["get_user_details"], "only the first call may run"


def test_prose_is_never_shown_alongside_a_tool_call(env):
    """A turn that calls a tool must not also speak to the customer."""
    llm = FakeLLM([
        {"text": "Let me look that up for you...",
         "tool_calls": [call("get_user_details", user_id=fx.RAFAEL)]},
        {"text": "You're a gold member.", "tool_calls": []},
    ])
    orch = Orchestrator(make_config(), env, llm=llm)
    reply = orch.respond([{"role": "user", "content": "what's my status?"}])

    assert reply == "You're a gold member."
    assert "Let me look that up" not in reply


def test_step_budget_is_bounded(env):
    """A model that never stops calling tools must not loop forever."""
    llm = FakeLLM([
        {"text": "", "tool_calls": [call("list_all_airports")]} for _ in range(50)
    ])
    orch = Orchestrator(make_config(max_tool_steps=5), env, llm=llm)
    reply = orch.respond([{"role": "user", "content": "hi"}])
    assert orch.steps_taken == 5
    assert reply == BUDGET_EXHAUSTED_MESSAGE


def test_missing_llm_credential_degrades_safely(env):
    """With no key and no injected llm, the agent must not guess -- and must not write."""
    orch = Orchestrator(make_config(anthropic_api_key=None), env, llm=None)
    reply = orch.respond([{"role": "user", "content": "cancel TRN009"}])
    assert reply == UNAVAILABLE_MESSAGE
    assert env.writes == []


def test_model_exception_degrades_safely(env):
    def exploding_llm(system, messages, tools):
        raise RuntimeError("upstream 503")

    orch = Orchestrator(make_config(), env, llm=exploding_llm)
    reply = orch.respond([{"role": "user", "content": "cancel TRN009"}])
    assert reply == FALLBACK_MESSAGE
    assert env.writes == []


# --- the confirmation gate ------------------------------------------------------------------------

def test_confirm_is_refused_without_customer_agreement(env):
    """The model tries to confirm immediately; the gate must refuse because the customer never agreed."""
    llm = FakeLLM([
        {"text": "", "tool_calls": [call(
            "request_cancellation", user_id=fx.RAFAEL, reservation_id="TRN009",
            reason="airline_cancelled",
        )]},
        # Straight to confirm, with no customer yes in the transcript.
        {"text": "", "tool_calls": [call("confirm_pending_action", token="PLACEHOLDER")]},
        {"text": "Shall I go ahead and cancel it?", "tool_calls": []},
    ])
    orch = Orchestrator(make_config(), env, llm=llm, consent_checker=not_agreed)
    reply = orch.respond([
        {"role": "user", "content": "The airline cancelled my flight on TRN009."}
    ])

    assert env.writes == [], "no write may happen without the customer's yes"
    assert "go ahead" in reply.lower()


def test_confirm_succeeds_after_a_real_agreement(env):
    """With a genuine yes as the customer's latest message, the write goes through."""
    prepare = FakeLLM([
        {"text": "", "tool_calls": [call(
            "request_cancellation", user_id=fx.RAFAEL, reservation_id="TRN009",
            reason="airline_cancelled",
        )]},
        {"text": "That cancels the whole reservation. Shall I confirm?", "tool_calls": []},
    ])
    orch = Orchestrator(make_config(), env, llm=prepare, consent_checker=agreed)
    conversation = [{"role": "user", "content": "Airline cancelled TRN009, please cancel it."}]
    first = orch.respond(conversation)
    assert env.writes == []

    token = next(iter(orch.pending))
    conversation += [
        {"role": "assistant", "content": first},
        {"role": "user", "content": "Yes, go ahead."},
    ]
    orch._llm = FakeLLM([
        {"text": "", "tool_calls": [call("confirm_pending_action", token=token)]},
        {"text": "Done -- that's cancelled.", "tool_calls": []},
    ])
    second = orch.respond(conversation)

    assert env.write_tools_called == ["cancel_reservation"]
    assert "cancelled" in second.lower()


def test_customer_saying_no_prevents_the_write(env):
    prepare = FakeLLM([
        {"text": "", "tool_calls": [call(
            "request_cancellation", user_id=fx.RAFAEL, reservation_id="TRN009",
            reason="airline_cancelled",
        )]},
        {"text": "Shall I confirm?", "tool_calls": []},
    ])
    orch = Orchestrator(make_config(), env, llm=prepare, consent_checker=not_agreed)
    conversation = [{"role": "user", "content": "cancel TRN009, airline cancelled it"}]
    first = orch.respond(conversation)
    token = next(iter(orch.pending))

    conversation += [
        {"role": "assistant", "content": first},
        {"role": "user", "content": "No, don't do that."},
    ]
    orch._llm = FakeLLM([
        {"text": "", "tool_calls": [call("confirm_pending_action", token=token)]},
        {"text": "Understood, I've left it as it is.", "tool_calls": []},
    ])
    orch.respond(conversation)
    assert env.writes == [], "a refusal must never produce a write"


def test_confirm_with_an_unknown_token_is_refused(env):
    llm = FakeLLM([
        {"text": "", "tool_calls": [call("confirm_pending_action", token="nope")]},
        {"text": "Let me check that again.", "tool_calls": []},
    ])
    orch = Orchestrator(make_config(), env, llm=llm, consent_checker=agreed)
    orch.respond([{"role": "user", "content": "yes do it"}])
    assert env.writes == []


# --- verification is visible to the model ---------------------------------------------------------

def test_reservation_read_exposes_real_flight_statuses(env):
    """The model is handed the true statuses, so it cannot repeat a false claim unchallenged."""
    llm = FakeLLM([
        {"text": "", "tool_calls": [call("get_reservation_details", reservation_id="TRN004")]},
        {"text": "That flight has already been flown.", "tool_calls": []},
    ])
    orch = Orchestrator(make_config(), env, llm=llm)
    orch.respond([{"role": "user", "content": "tell me about TRN004"}])

    observation = llm.observations[-1]
    assert "landed" in observation
    assert "already_flown" in observation


# --- the adversarial personas ---------------------------------------------------------------------

@pytest.mark.parametrize("persona", DECEPTIVE_PERSONAS, ids=lambda p: p.name)
def test_deceptive_personas_never_obtain_a_write(env, persona):
    """A customer who lies and then pushes hard must not extract a mutation.

    The agent here is a deliberately over-agreeable model: it simply tries to do whatever the customer
    last asked, including confirming. The policy and confirmation gates are what must stop it -- which is
    exactly the failure mode the stakeholder was worried about.

    The consent checker is pinned to `agreed` on purpose, so the *policy* layer alone has to hold.
    These personas are stopped by ineligibility, not by a missing yes, and a permissive consent
    check must not be able to turn any of them into a write.
    """
    simulator = UserSimulator(persona)

    def over_agreeable_agent(messages):
        # Always tries the most damaging action for this persona, every turn.
        attempts = {
            "lying_about_cancellation": call(
                "request_compensation", user_id=fx.OWEN, reservation_id="TRN003",
                situation="cancelled",
            ),
            "lying_about_membership": call(
                "request_add_baggage", user_id=fx.PRIYA, reservation_id="TRN006",
                new_total_bags=3,
            ),
            "lying_about_insurance": call(
                "request_cancellation", user_id=fx.QUINN, reservation_id="TRN007",
                reason="other", reason_detail="I'm sick",
            ),
        }
        llm = FakeLLM([
            {"text": "", "tool_calls": [attempts[persona.name]]},
            {"text": "I'm not able to do that, I'm afraid.", "tool_calls": []},
        ])
        return Orchestrator(
            make_config(), env, llm=llm, consent_checker=agreed
        ).respond(messages)

    transcript = run_conversation(simulator, over_agreeable_agent, max_turns=8)

    assert env.writes == [], (
        f"{persona.name} obtained a write: {env.write_tools_called}. Expected: {persona.expectation}"
    )
    assert len(transcript) >= 2


def test_lying_about_membership_still_charges_the_real_rate(env):
    """Priya claims gold; she is regular. The charge must follow the record, not the claim.

    TRN006 is basic economy with 1 passenger, so a regular member gets 0 free bags. She already has
    1 bag, so going to 3 bills the 2 *added* bags: $100. A gold member would have got 2 free and paid
    nothing, so the price still proves which membership was used.
    """
    orch = Orchestrator(make_config(), env, llm=FakeLLM([]))
    result = orch.actions.request_add_baggage(
        fx.PRIYA, "TRN006", new_total_bags=3, payment_id="credit_card_50000006"
    )
    assert result.facts["charge"] == 100, result.message
    assert "regular" in result.message


def test_a_gold_member_would_pay_nothing_for_the_same_bags(env):
    """The contrast that makes the test above meaningful: gold gets 2 free bags on basic economy."""
    orch = Orchestrator(make_config(), env, llm=FakeLLM([]))
    result = orch.actions.request_add_baggage(
        fx.RAFAEL, "TRN010", new_total_bags=2, payment_id="credit_card_50000009"
    )
    assert result.facts["charge"] == 0, result.message


def test_existing_bags_are_not_re_charged(env):
    """Adding one bag bills one bag, even when the bags already held are beyond the free allowance.

    TRN006 (regular, basic economy, 0 free) already carries 1 chargeable bag. Going 1 -> 2 must cost
    $50, not $100: the stored `nonfree_baggages` is 0 on every reservation in the store, so billing
    from it re-charged the customer for the bag they already had.
    """
    orch = Orchestrator(make_config(), env, llm=FakeLLM([]))
    result = orch.actions.request_add_baggage(
        fx.PRIYA, "TRN006", new_total_bags=2, payment_id="credit_card_50000006"
    )
    assert result.facts["charge"] == 50, result.message
    # The record still stores the true total chargeable count, not the incremental one.
    assert result.pending.arguments["nonfree_baggages"] == 2


def test_honest_customer_who_declines_gets_no_write(env):
    """Eligible request, but the customer says no at the confirmation step."""
    simulator = UserSimulator(HONEST_BUT_DECLINES)
    orch = Orchestrator(make_config(), env, llm=FakeLLM([]), consent_checker=not_agreed)

    def agent(messages):
        token = next(iter(orch.pending), None)
        turns = (
            [{"text": "", "tool_calls": [call("confirm_pending_action", token=token)]},
             {"text": "Understood, nothing changed.", "tool_calls": []}]
            if token
            else [{"text": "", "tool_calls": [call(
                "request_cancellation", user_id=fx.RAFAEL, reservation_id="TRN009",
                reason="airline_cancelled",
            )]},
             {"text": "Shall I go ahead?", "tool_calls": []}]
        )
        orch._llm = FakeLLM(turns)
        return orch.respond(messages)

    run_conversation(simulator, agent, max_turns=6)
    assert env.writes == [], "the customer declined, so nothing may be written"


@pytest.mark.parametrize(
    "reply, expected_writes",
    [
        ('{"verdict": "agreed", "reason": "yes with a scope caveat"}', ["cancel_reservation"]),
        ('{"verdict": "not_agreed", "reason": "paused to ask a question"}', []),
        ('{"verdict": "unclear", "reason": "ambiguous"}', []),
    ],
)
def test_only_an_agreed_verdict_permits_the_write(env, reply, expected_writes):
    """`agreed` is the only verdict that opens the gate; anything else writes nothing."""
    orch, conversation, token = _prepared_cancellation(env)
    conversation += [{"role": "user", "content": "Yes, go ahead -- but don't change the passengers."}]

    orch._consent_checker = lambda system, prompt: reply
    orch._llm = FakeLLM([
        {"text": "", "tool_calls": [call("confirm_pending_action", token=token)]},
        {"text": "All set.", "tool_calls": []},
    ])
    orch.respond(conversation)

    assert env.write_tools_called == expected_writes


def test_confirmation_survives_a_trailing_scope_caveat(env):
    """Regression for tau2_airline_18: a constraint after "yes" is not a refusal.

    The old gate matched a refusal keyword anywhere in the message, so "Yes -- go ahead ... Don't
    touch the flights" was read as a no. The agent had already priced five downgrades correctly and
    the customer had agreed; the gate refused a confirmation that was genuinely given and the loop
    drained into the fallback, losing all five writes and scoring 0.0.
    """
    orch, conversation, token = _prepared_cancellation(env)
    conversation += [{
        "role": "user",
        "content": "Yes -- go ahead and cancel it now. Don't change the passengers.",
    }]

    seen = {}

    def consent_checker(system, prompt):
        seen["prompt"] = prompt
        return '{"verdict": "agreed", "reason": "authorised with a scope constraint"}'

    orch._consent_checker = consent_checker
    orch._llm = FakeLLM([
        {"text": "", "tool_calls": [call("confirm_pending_action", token=token)]},
        {"text": "Done -- that's cancelled.", "tool_calls": []},
    ])
    reply = orch.respond(conversation)

    assert env.write_tools_called == ["cancel_reservation"]
    assert reply == "Done -- that's cancelled."
    # The check must see the customer's words verbatim, so it can spot the caveat for what it is.
    assert "Don't change the passengers" in seen["prompt"]


def test_consent_check_is_isolated_from_the_agent_model(env):
    """The gate must not be judged by the model that wants to pass through it.

    Otherwise the model's intent to call `confirm_pending_action` becomes its own evidence of
    consent -- exactly the "confirm on the customer's behalf" failure the gate exists to stop.
    """
    orch, conversation, token = _prepared_cancellation(env)
    conversation += [{"role": "user", "content": "Yes, go ahead."}]

    agent_llm = FakeLLM([
        {"text": "", "tool_calls": [call("confirm_pending_action", token=token)]},
        {"text": "Done.", "tool_calls": []},
    ])
    consent_prompts = []

    def consent_checker(system, prompt):
        consent_prompts.append(prompt)
        return '{"verdict": "agreed", "reason": "clear yes"}'

    orch._llm = agent_llm
    orch._consent_checker = consent_checker
    orch.respond(conversation)

    assert len(consent_prompts) == 1, "the gate must run its own separate check"
    prompt = consent_prompts[0]
    # No transcript, no tool names, no hint that a confirmation was requested.
    assert "confirm_pending_action" not in prompt
    assert "request_cancellation" not in prompt
    assert "Airline cancelled TRN009" not in prompt


def test_consent_check_failure_fails_closed_with_a_way_forward(env):
    """If the check cannot run, consent is unknown: no write, but no silent refusal either."""
    orch, conversation, token = _prepared_cancellation(env)
    conversation += [{"role": "user", "content": "Yes, go ahead."}]

    def exploding_checker(system, prompt):
        raise TimeoutError("classifier unreachable")

    orch._consent_checker = exploding_checker
    orch._llm = FakeLLM([
        {"text": "", "tool_calls": [call("confirm_pending_action", token=token)]},
        {"text": "Sorry -- could you confirm you'd like me to go ahead?", "tool_calls": []},
    ])
    reply = orch.respond(conversation)

    assert env.writes == [], "an unverifiable confirmation must never write"
    observation = llm_observation(orch._llm)
    assert "could not be verified" in observation
    assert "confirm" in reply.lower()


def test_unparseable_consent_reply_fails_closed(env):
    """A reply with no usable verdict is treated as unknown, not as agreement."""
    orch, conversation, token = _prepared_cancellation(env)
    conversation += [{"role": "user", "content": "Yes, go ahead."}]

    orch._consent_checker = lambda system, prompt: "I think probably yes?"
    orch._llm = FakeLLM([
        {"text": "", "tool_calls": [call("confirm_pending_action", token=token)]},
        {"text": "Could you confirm that for me?", "tool_calls": []},
    ])
    orch.respond(conversation)

    assert env.writes == []


def test_consent_check_is_skipped_when_nothing_was_said(env):
    """An empty last message needs no API call to know it is not agreement."""
    orch, conversation, token = _prepared_cancellation(env)
    conversation += [{"role": "user", "content": ""}]

    calls = []
    orch._consent_checker = lambda system, prompt: calls.append(prompt) or '{"verdict": "agreed"}'
    orch._llm = FakeLLM([
        {"text": "", "tool_calls": [call("confirm_pending_action", token=token)]},
        {"text": "Shall I go ahead?", "tool_calls": []},
    ])
    orch.respond(conversation)

    assert calls == [], "no need to spend a call on an empty message"
    assert env.writes == []


# --- honest, distinguishable failure reporting ----------------------------------------------------

def test_the_operational_failures_are_distinguishable(env):
    """No-credential, internal-error and budget-exhausted must not share one string.

    Collapsing them into a single "I'm not able to help" is what made a crash read as over-refusal
    in the eval report -- hiding the bug and making the policy layer look wrong.
    """
    no_credential = Orchestrator(make_config(anthropic_api_key=None), env, llm=None).respond(
        [{"role": "user", "content": "hi"}]
    )

    def exploding_llm(system, messages, tools):
        raise RuntimeError("upstream 503")

    model_error = Orchestrator(make_config(), env, llm=exploding_llm).respond(
        [{"role": "user", "content": "hi"}]
    )

    budget = Orchestrator(
        make_config(max_tool_steps=3),
        env,
        llm=FakeLLM([{"text": "", "tool_calls": [call("list_all_airports")]} for _ in range(10)]),
    ).respond([{"role": "user", "content": "hi"}])

    assert len({no_credential, model_error, budget}) == 3, "each failure needs its own message"
    for message in (no_credential, model_error, budget):
        assert "haven't made any changes" in message or "colleague" in message


def test_unexpected_tool_error_becomes_a_recoverable_observation(env):
    """An internal bug must not surface to the customer as a refusal.

    Only ToolRejection/ToolCallError/TypeError were handled, so a KeyError from the policy or domain
    layer escaped `_dispatch`, passed the loop's try (which wraps only the model call), and left
    `respond()` as the generic fallback -- indistinguishable from a deliberate handover.
    """
    def exploding_read(reservation_id):
        raise KeyError("cabin")

    env.get_reservation_details = exploding_read

    llm = FakeLLM([
        {"text": "", "tool_calls": [call("get_reservation_details", reservation_id="TRN009")]},
        {"text": "Let me try that another way.", "tool_calls": []},
    ])
    orch = Orchestrator(make_config(), env, llm=llm)
    reply = orch.respond([{"role": "user", "content": "tell me about TRN009"}])

    assert reply == "Let me try that another way.", "the turn must survive an internal error"
    observation = llm.observations[-1]
    assert "internal" in observation
    assert env.writes == []


def test_reservation_read_tells_the_model_it_is_not_a_verdict(env):
    """A read must not look like a decision.

    Regression: against a live model, reading TRN004 and seeing `already_flown` was enough for it to
    write its own refusal, so `request_cancellation` was never called and the flown-flight TRANSFER never
    happened. The observation now carries an explicit reminder that only the request_* tools decide.
    """
    llm = FakeLLM([
        {"text": "", "tool_calls": [call("get_reservation_details", reservation_id="TRN004")]},
        {"text": "Let me check whether that can be cancelled.", "tool_calls": []},
    ])
    orch = Orchestrator(make_config(), env, llm=llm)
    orch.respond([{"role": "user", "content": "cancel TRN004"}])

    observation = llm.observations[-1]
    assert "not a decision" in observation
    assert "request_" in observation


def test_flown_cancellation_reaches_the_transfer_tool(env):
    """The end-to-end path for the canonical transfer, driven through the intent tool."""
    llm = FakeLLM([
        {"text": "", "tool_calls": [call(
            "request_cancellation", user_id=fx.OWEN, reservation_id="TRN004",
            reason="change_of_plan",
        )]},
        {"text": "Part of that trip has already flown, so I'm passing you to a colleague.",
         "tool_calls": []},
    ])
    orch = Orchestrator(make_config(), env, llm=llm)
    orch.respond([{"role": "user", "content": "cancel TRN004, my plans changed"}])

    assert env.transfer_summaries, "the flown-flight case must reach transfer_to_human_agents"
    assert env.writes == [], "a transfer must never also write"


# --- tool schema / handler agreement --------------------------------------------------------------

def test_every_tool_schema_covers_its_handlers_required_arguments():
    """A required Python parameter missing from the schema makes the tool uncallable.

    `request_compensation` shipped exactly this way: `user_asked` was required by the handler but
    absent from the schema, so every call raised TypeError and compensation was unreachable -- both
    for fraudulent claims (which looked "correctly denied") and for legitimate ones. The model can
    only send what the schema advertises, so the two must agree.

    `user_asked` is the deliberate exception: it is supplied by the orchestrator from an isolated
    check, never by the model, so it must NOT appear in the schema.
    """
    import inspect

    from actions import ActionService
    from agent.prompts import TOOL_SCHEMAS

    injected = {"request_compensation": {"user_asked"}}
    problems = []
    for schema in TOOL_SCHEMAS:
        name = schema["name"]
        handler = getattr(ActionService, name, None)
        if handler is None:
            continue
        advertised = set(schema["input_schema"].get("properties", {}))
        advertised |= injected.get(name, set())
        for param_name, param in inspect.signature(handler).parameters.items():
            if param_name == "self":
                continue
            if param.default is inspect.Parameter.empty and param_name not in advertised:
                problems.append(f"{name}: required parameter {param_name!r} is not in the schema")
        for extra in advertised - set(inspect.signature(handler).parameters) - injected.get(name, set()):
            problems.append(f"{name}: schema advertises {extra!r}, which the handler does not accept")
    assert not problems, "; ".join(problems)


def test_compensation_is_callable_with_only_the_schemas_fields(env):
    """The regression itself: the model's best-possible call must not fail as a bad call."""
    llm = FakeLLM([
        {"text": "", "tool_calls": [call(
            "request_compensation", user_id=fx.RAFAEL, reservation_id="TRN009",
            situation="cancelled",
        )]},
        {"text": "You're eligible for a $100 certificate. Shall I send it?", "tool_calls": []},
    ])
    orch = Orchestrator(
        make_config(), env, llm=llm, consent_checker=asked_for_compensation
    )
    orch.respond([{"role": "user", "content": "My flight was cancelled -- I want compensation."}])

    observation = llm.observations[-1]
    assert "bad_call" not in observation, observation
    assert "Missing or unexpected argument" not in observation, observation
    assert "100" in observation, observation


# --- the "never offer compensation proactively" gate ----------------------------------------------

def test_compensation_is_refused_when_the_customer_only_complained(env):
    """The gate is decided from the customer's words, not from the model wanting to compensate."""
    llm = FakeLLM([
        {"text": "", "tool_calls": [call(
            "request_compensation", user_id=fx.RAFAEL, reservation_id="TRN009",
            situation="cancelled",
        )]},
        {"text": "I've cancelled that for you.", "tool_calls": []},
    ])
    orch = Orchestrator(
        make_config(), env, llm=llm, consent_checker=did_not_ask_for_compensation
    )
    orch.respond([{"role": "user", "content": "The airline cancelled my flight and I'm furious."}])

    assert "only considered when a customer asks" in llm.observations[-1]
    assert env.writes == [], "no certificate may be prepared or sent"


def test_a_model_claiming_the_customer_asked_cannot_force_compensation(env):
    """`user_asked` supplied by the model is discarded: its own intent is not evidence."""
    llm = FakeLLM([
        {"text": "", "tool_calls": [call(
            "request_compensation", user_id=fx.RAFAEL, reservation_id="TRN009",
            situation="cancelled", user_asked=True,
        )]},
        {"text": "Nothing further.", "tool_calls": []},
    ])
    orch = Orchestrator(
        make_config(), env, llm=llm, consent_checker=did_not_ask_for_compensation
    )
    orch.respond([{"role": "user", "content": "This is unacceptable, I'm a loyal customer."}])

    assert "only considered when a customer asks" in llm.observations[-1]
    assert env.writes == []


def test_compensation_fails_closed_when_the_request_check_is_unavailable(env):
    """An unavailable check must not become an offer."""
    def broken(system, prompt):
        raise RuntimeError("no model")

    llm = FakeLLM([
        {"text": "", "tool_calls": [call(
            "request_compensation", user_id=fx.RAFAEL, reservation_id="TRN009",
            situation="cancelled",
        )]},
        {"text": "Let me help with the rest.", "tool_calls": []},
    ])
    orch = Orchestrator(make_config(), env, llm=llm, consent_checker=broken)
    orch.respond([{"role": "user", "content": "I want compensation for the cancellation."}])

    assert "could not verify" in llm.observations[-1]
    assert env.writes == []


def test_an_earlier_compensation_request_still_counts_after_a_plain_yes(env):
    """A request stands for the rest of the conversation.

    The customer asks once, then answers an ordinary follow-up with "yes, go ahead". Judging only that
    latest turn withdrew an offer that had already been correctly made, so the whole customer side is
    judged and the verdict latches.
    """
    seen_prompts = []

    def checker(system, prompt):
        seen_prompts.append(prompt)
        # Mirrors the real classifier: the ask is visible in the transcript, so it says "asked".
        asked = "certificate" in prompt or "compensation I" in prompt
        verdict = "asked" if asked else "not_asked"
        return '{"verdict": "%s", "reason": "judged over the transcript"}' % verdict

    llm = FakeLLM([
        {"text": "", "tool_calls": [call(
            "request_compensation", user_id=fx.RAFAEL, reservation_id="TRN009",
            situation="cancelled",
        )]},
        {"text": "A $100 certificate is available. Shall I send it?", "tool_calls": []},
    ])
    orch = Orchestrator(make_config(), env, llm=llm, consent_checker=checker)
    conversation = [
        {"role": "user", "content": "TRN009 was cancelled -- I want the travel certificate I'm owed."},
        {"role": "assistant", "content": "A $100 certificate is available. Shall I send it?"},
        {"role": "user", "content": "Yes please, go ahead and send it."},
    ]
    orch.respond(conversation)

    assert "100" in llm.observations[-1], llm.observations[-1]
    assert "only considered when a customer asks" not in llm.observations[-1]
    # The check saw the whole customer side, not just the latest "yes".
    assert "certificate I'm owed" in seen_prompts[0]
