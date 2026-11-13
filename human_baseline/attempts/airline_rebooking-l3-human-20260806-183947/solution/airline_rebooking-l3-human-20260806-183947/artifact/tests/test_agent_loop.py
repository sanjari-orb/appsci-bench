"""Tests for the agent loop's mechanics, using a stub LLM and a stub environment.

No API key, no container and no network: the point is to prove the loop's structural guarantees,
which is where the two turn-shape policy rules actually live. In particular a blocked write must
never reach the environment -- asserted by checking the stub environment recorded no such call.
"""
from __future__ import annotations

import pytest

import agent as agent_module
import config
from agent import Agent
from env_client import ToolResult


# --- stubs -------------------------------------------------------------------------------------
class Block:
    """A response content block, shaped like the SDK's."""

    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


def text_block(text):
    return Block("text", text=text)


def tool_block(name, arguments, id_="tu_1"):
    return Block("tool_use", name=name, input=arguments, id=id_)


class Response:
    def __init__(self, blocks):
        self.content = blocks


class StubLLM:
    """Returns scripted responses in order; records the requests it received."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.requests = []
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        if not self._scripted:
            return Response([text_block("(out of script)")])
        return self._scripted.pop(0)


class StubEnv:
    """Records every tool call and returns canned results."""

    def __init__(self, results=None, reservation=None, user=None, statuses=None, available=None):
        self.calls = []
        self._results = results or {}
        self._reservation = reservation
        self._user = user
        self._statuses = statuses or {}
        self._available = available or {}

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        result = self._results.get(name, "ok")
        if isinstance(result, ToolResult):
            return result
        return ToolResult(True, result)

    def get_reservation(self, reservation_id):
        return self._reservation

    def get_user(self, user_id):
        return self._user

    def get_flight_status(self, flight_number, date):
        return self._statuses.get((flight_number, date))

    def find_available_flight(self, origin, destination, date, flight_number):
        return self._available.get((flight_number, date))

    def wrote(self, name):
        """Whether a given tool was ever actually invoked."""
        return any(c[0] == name for c in self.calls)


def build(scripted, env=None):
    return Agent(llm=StubLLM(scripted), env=env or StubEnv()), env


CONVO = [{"role": "user", "content": "hello"}]


# --- termination -------------------------------------------------------------------------------
def test_text_only_response_ends_the_turn():
    agent = Agent(llm=StubLLM([Response([text_block("Hello, how can I help?")])]), env=StubEnv())
    assert agent.run(CONVO) == "Hello, how can I help?"


def test_tool_call_then_text_returns_the_text():
    env = StubEnv(results={"list_all_airports": ["SFO", "SEA"]})
    agent = Agent(llm=StubLLM([
        Response([tool_block("list_all_airports", {})]),
        Response([text_block("We fly from SFO and SEA.")]),
    ]), env=env)
    assert agent.run(CONVO) == "We fly from SFO and SEA."
    assert env.calls == [("list_all_airports", {})]


def test_text_alongside_a_tool_call_is_not_shown_to_the_customer():
    """A mixed turn cannot reach the caller: only a text-only turn ends the loop."""
    env = StubEnv(results={"list_all_airports": ["SFO"]})
    agent = Agent(llm=StubLLM([
        Response([text_block("Let me look that up"), tool_block("list_all_airports", {})]),
        Response([text_block("We fly from SFO.")]),
    ]), env=env)
    reply = agent.run(CONVO)
    assert reply == "We fly from SFO."
    assert "Let me look that up" not in reply


# --- user turn cap -------------------------------------------------------------------------------
def _messages_with_user_turns(count):
    return [{"role": "user", "content": f"message {i}"} for i in range(count)]


def test_conversation_at_the_turn_cap_is_still_answered(monkeypatch):
    monkeypatch.setattr(config, "MAX_USER_TURNS", 10)
    agent = Agent(llm=StubLLM([Response([text_block("Sure, here you go.")])]), env=StubEnv())
    reply = agent.run(_messages_with_user_turns(10))
    assert reply == "Sure, here you go."


def test_conversation_past_the_turn_cap_is_refused_without_calling_the_model(monkeypatch):
    monkeypatch.setattr(config, "MAX_USER_TURNS", 10)
    llm = StubLLM([Response([text_block("Sure, here you go.")])])
    agent = Agent(llm=llm, env=StubEnv())
    reply = agent.run(_messages_with_user_turns(11))
    assert reply == agent_module.MAX_TURNS_REPLY
    assert llm.requests == []


def test_only_the_first_of_several_tool_calls_executes():
    """Policy: one tool call at a time. The extras are rejected without being executed."""
    env = StubEnv(results={"list_all_airports": ["SFO"]})
    agent = Agent(llm=StubLLM([
        Response([tool_block("list_all_airports", {}, "a"),
                  tool_block("get_user_details", {"user_id": "u"}, "b")]),
        Response([text_block("done")]),
    ]), env=env)
    agent.run(CONVO)
    assert env.calls == [("list_all_airports", {})]
    assert not env.wrote("get_user_details")


def test_rejected_extra_call_is_reported_back_to_the_model():
    env = StubEnv(results={"list_all_airports": ["SFO"]})
    llm = StubLLM([
        Response([tool_block("list_all_airports", {}, "a"),
                  tool_block("get_user_details", {"user_id": "u"}, "b")]),
        Response([text_block("done")]),
    ])
    Agent(llm=llm, env=env).run(CONVO)
    results = llm.requests[1]["messages"][-1]["content"]
    rejected = [r for r in results if r["tool_use_id"] == "b"][0]
    assert rejected["is_error"] is True
    assert "one tool call" in rejected["content"].lower()


# --- iteration cap ------------------------------------------------------------------------------
def test_iteration_cap_still_produces_a_reply(monkeypatch):
    """A model that never stops calling tools must not leave the customer with nothing."""
    monkeypatch.setattr(config, "MAX_TOOL_ITERS", 3)
    env = StubEnv(results={"list_all_airports": ["SFO"]})
    llm = StubLLM([Response([tool_block("list_all_airports", {})]) for _ in range(3)]
                  + [Response([text_block("Sorry, I could not complete that.")])])
    reply = Agent(llm=llm, env=env).run(CONVO)
    assert reply == "Sorry, I could not complete that."


def test_fallback_reply_when_closing_message_also_fails(monkeypatch):
    """If even the closing call fails, the turn still ends with a safe handoff message."""
    monkeypatch.setattr(config, "MAX_TOOL_ITERS", 1)

    class Failing(StubLLM):
        def create(self, **kw):
            if "tools" not in kw:
                raise RuntimeError("upstream unavailable")
            return Response([tool_block("list_all_airports", {})])

    reply = Agent(llm=Failing([]), env=StubEnv()).run(CONVO)
    assert reply == agent_module.FALLBACK_REPLY


# --- guard integration --------------------------------------------------------------------------
def _flown_reservation():
    return {
        "reservation_id": "R1", "user_id": "u1", "cabin": "business", "insurance": "no",
        "created_at": "2024-05-01T10:00:00", "origin": "SFO", "destination": "SEA",
        "flight_type": "one_way",
        "passengers": [{"first_name": "A", "last_name": "B", "dob": "1990-01-01"}],
        "flights": [{"flight_number": "HAT001", "date": "2024-05-10", "price": 100,
                     "origin": "SFO", "destination": "SEA"}],
        "total_baggages": 0, "nonfree_baggages": 0, "payment_history": [],
    }


def test_blocked_write_never_reaches_the_environment():
    """The core safety property: a violating write is suppressed, not merely discouraged."""
    env = StubEnv(reservation=_flown_reservation(),
                  user={"user_id": "u1", "membership": "gold", "payment_methods": {}},
                  statuses={("HAT001", "2024-05-10"): "landed"})
    llm = StubLLM([
        Response([tool_block("cancel_reservation", {"reservation_id": "R1"})]),
        Response([text_block("That trip has already flown, so I can't cancel it here.")]),
    ])
    Agent(llm=llm, env=env).run([{"role": "user", "content": "cancel R1, change of plan"}])
    assert not env.wrote("cancel_reservation")


def test_blocked_write_feedback_reaches_the_model():
    env = StubEnv(reservation=_flown_reservation(),
                  user={"user_id": "u1", "membership": "gold", "payment_methods": {}},
                  statuses={("HAT001", "2024-05-10"): "landed"})
    llm = StubLLM([
        Response([tool_block("cancel_reservation", {"reservation_id": "R1"})]),
        Response([text_block("already flown")]),
    ])
    Agent(llm=llm, env=env).run([{"role": "user", "content": "cancel R1"}])
    feedback = llm.requests[1]["messages"][-1]["content"][0]
    assert feedback["is_error"] is True
    assert "BLOCKED (FLOWN_SEGMENT)" in feedback["content"]
    assert "HUMAN AGENT" in feedback["content"]


def test_allowed_write_does_reach_the_environment():
    """The guard must not be so strict that legitimate work stops happening."""
    reservation = _flown_reservation()
    reservation["created_at"] = "2024-05-15T09:00:00"  # inside the 24h window
    env = StubEnv(reservation=reservation,
                  user={"user_id": "u1", "membership": "gold", "payment_methods": {}},
                  statuses={("HAT001", "2024-05-10"): "available"})
    llm = StubLLM([
        Response([tool_block("cancel_reservation", {"reservation_id": "R1"})]),
        Response([text_block("Cancelled.")]),
    ])
    Agent(llm=llm, env=env).run([{"role": "user", "content": "cancel R1, plans changed"}])
    assert env.wrote("cancel_reservation")


def test_build_context_stamps_route_onto_available_flights():
    """search_direct_flight never echoes origin/destination, so _find_flight must stamp them on
    from the route it actually searched -- policy.py's route checks depend on those fields."""
    env = StubEnv(available={("HAT002", "2024-05-25"): {"flight_number": "HAT002", "prices": {}}})
    ctx = Agent(llm=StubLLM([]), env=env)._build_context(
        "book_reservation",
        {"origin": "SFO", "destination": "SEA",
         "flights": [{"flight_number": "HAT002", "date": "2024-05-25"}]},
        agent_module._ConversationFacts([]),
    )
    instance = ctx.available_flights[("HAT002", "2024-05-25")]
    assert instance["origin"] == "SFO"
    assert instance["destination"] == "SEA"


def test_build_context_stamps_reversed_route_for_the_return_leg():
    """A round trip's return leg is only found by reversing origin/destination in the search."""
    class DirectionalEnv(StubEnv):
        def find_available_flight(self, origin, destination, date, flight_number):
            if (origin, destination) == ("SEA", "SFO"):
                return {"flight_number": flight_number, "prices": {}}
            return None

    ctx = Agent(llm=StubLLM([]), env=DirectionalEnv())._build_context(
        "book_reservation",
        {"origin": "SFO", "destination": "SEA",
         "flights": [{"flight_number": "HAT003", "date": "2024-05-30"}]},
        agent_module._ConversationFacts([]),
    )
    instance = ctx.available_flights[("HAT003", "2024-05-30")]
    assert instance["origin"] == "SEA"
    assert instance["destination"] == "SFO"


def test_repeated_violations_stop_the_turn(monkeypatch):
    """A model that keeps retrying a blocked call is cut off rather than left to loop."""
    monkeypatch.setattr(config, "MAX_REPEATED_VIOLATIONS", 2)
    env = StubEnv(reservation=_flown_reservation(),
                  user={"user_id": "u1", "membership": "gold", "payment_methods": {}},
                  statuses={("HAT001", "2024-05-10"): "landed"})
    llm = StubLLM([
        Response([tool_block("cancel_reservation", {"reservation_id": "R1"})]),
        Response([tool_block("cancel_reservation", {"reservation_id": "R1"})]),
        Response([text_block("I can't cancel that; transferring you.")]),
    ])
    reply = Agent(llm=llm, env=env).run([{"role": "user", "content": "cancel R1"}])
    assert reply == "I can't cancel that; transferring you."
    assert not env.wrote("cancel_reservation")
    # The closing request must withdraw the tools, so the model has to answer in text.
    assert "tools" not in llm.requests[-1]


def test_read_tools_are_not_guarded():
    """Reads must flow freely; the agent needs them to establish the facts."""
    env = StubEnv(results={"get_reservation_details": {"reservation_id": "R1"}})
    llm = StubLLM([
        Response([tool_block("get_reservation_details", {"reservation_id": "R1"})]),
        Response([text_block("Here are the details.")]),
    ])
    Agent(llm=llm, env=env).run(CONVO)
    assert env.wrote("get_reservation_details")


# --- environment errors -------------------------------------------------------------------------
def test_tool_error_is_surfaced_to_the_model_not_raised():
    """A 400 from the environment is information to relay, not an exception."""
    env = StubEnv(results={"get_reservation_details":
                           ToolResult(False, "Reservation ZZZ not found")})
    llm = StubLLM([
        Response([tool_block("get_reservation_details", {"reservation_id": "ZZZ"})]),
        Response([text_block("I couldn't find that reservation.")]),
    ])
    reply = Agent(llm=llm, env=env).run(CONVO)
    assert reply == "I couldn't find that reservation."
    result = llm.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert "not found" in result["content"]


# --- conversational facts -----------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("I am ill and cannot fly", "health"),
    ("there is a storm", "weather"),
    ("my plans changed", "change_of_plan"),
    ("the airline cancelled my flight", "airline_cancelled"),
    ("just checking in", None),
])
def test_cancellation_reason_is_read_from_the_transcript(text, expected):
    facts = agent_module._ConversationFacts([{"role": "user", "content": text}])
    assert facts.cancellation_reason == expected


@pytest.mark.parametrize("text,expected", [
    ("my flight was cancelled, any compensation?", "cancelled_flight"),
    ("the flight was delayed, I want a certificate", "delayed_flight"),
    ("the crew was rude, I want compensation", "other"),
    ("my flight was cancelled, please rebook me", None),
])
def test_complaint_requires_the_customer_to_ask(text, expected):
    """Compensation is never proactive, so a complaint only counts when they ask for something."""
    facts = agent_module._ConversationFacts([{"role": "user", "content": text}])
    assert facts.complaint == expected


def test_reservation_resolved_starts_false_and_is_set_by_an_observed_write():
    """The delay certificate depends on evidence of the change, not on an intention to make it."""
    reservation = _flown_reservation()
    reservation["created_at"] = "2024-05-15T09:00:00"
    env = StubEnv(reservation=reservation,
                  user={"user_id": "u1", "membership": "gold", "payment_methods": {}},
                  statuses={("HAT001", "2024-05-10"): "available"})
    facts = agent_module._ConversationFacts([{"role": "user", "content": "cancel it"}])
    assert facts.reservation_resolved is False
    agent = Agent(llm=StubLLM([Response([text_block("x")])]), env=env)
    agent._handle_tool_call("cancel_reservation", {"reservation_id": "R1"}, facts)
    assert facts.reservation_resolved is True


# --- message translation -------------------------------------------------------------------------
def test_translation_drops_system_turns_and_keeps_order():
    out = agent_module._to_anthropic_messages([
        {"role": "system", "content": "ignored"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "help"},
    ])
    assert out == [{"role": "user", "content": "hi"},
                   {"role": "assistant", "content": "hello"},
                   {"role": "user", "content": "help"}]


def test_translation_ensures_a_leading_user_turn():
    """Anthropic requires the conversation to start with a user message."""
    out = agent_module._to_anthropic_messages([{"role": "assistant", "content": "hello"}])
    assert out[0]["role"] == "user"


def test_translation_skips_empty_content():
    out = agent_module._to_anthropic_messages([{"role": "user", "content": ""},
                                               {"role": "user", "content": "real"}])
    assert out == [{"role": "user", "content": "real"}]
