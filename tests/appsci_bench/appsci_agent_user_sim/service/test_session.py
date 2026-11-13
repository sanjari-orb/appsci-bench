"""Unit tests for Session -- history accumulation + turn counter around a (fake) stateless sim.

This is the in-process complement to test_service.py (which exercises the same logic over HTTP).
"""
from __future__ import annotations

import json

import shared.observability.publisher as pub
from shared.observability.events import Source
from shared.observability.storage import get_source_log_path
from appsci_bench.appsci_agent_user_sim.service.session import Session


class _FakeSim:
    """Records the history + parent span handed to each answer() call; returns a marker reply."""
    def __init__(self, system_prompt: str = "SYS", reply: str | None = None) -> None:
        self.system_prompt = system_prompt
        self.seen: list[list[dict]] = []
        self.seen_parent: list[str | None] = []
        self._reply = reply

    def answer(self, question: str, history: list[dict], parent_span_id: str | None = None) -> str:
        self.seen.append([dict(t) for t in history])
        self.seen_parent.append(parent_span_id)
        return self._reply if self._reply is not None else f"re: {question}"


def test_ask_returns_message_and_increments_turn() -> None:
    s = Session(_FakeSim())
    msg1, turn1 = s.ask("q1")
    msg2, turn2 = s.ask("q2")
    assert (msg1, turn1) == ("re: q1", 1)
    assert (msg2, turn2) == ("re: q2", 2)
    assert s.turn == 2


def test_history_accumulates_with_role_flip_labels() -> None:
    sim = _FakeSim()
    s = Session(sim)
    s.ask("q1")
    s.ask("q2")
    # Each call sees the PRIOR transcript, NOT the current question (answer() appends the question
    # itself via _to_llm_messages): q1 -> []; q2 -> [q1, a1].
    assert [len(h) for h in sim.seen] == [0, 2]
    second = sim.seen[1]
    assert second[0] == {"role": "agent", "content": "q1"}
    assert second[1] == {"role": "user_sim", "content": "re: q1"}


def test_system_prompt_property_delegates_to_sim() -> None:
    s = Session(_FakeSim(system_prompt="PERSONA PROMPT"))
    assert s.system_prompt == "PERSONA PROMPT"


def test_turn_starts_at_zero() -> None:
    assert Session(_FakeSim()).turn == 0


def test_ask_forwards_parent_span_to_the_sim() -> None:
    sim = _FakeSim()
    s = Session(sim)
    s.ask("q1", parent_span_id="tool-span-xyz")
    s.ask("q2")  # default None
    assert sim.seen_parent == ["tool-span-xyz", None]


def test_no_duplicate_question_in_built_prompt(monkeypatch) -> None:
    # Regression: the agent question must appear exactly once in the prompt sent to the model. The
    # caller used to append it to history before answer(), and _to_llm_messages appends it too, so it
    # showed up as two identical consecutive user turns.
    from appsci_bench.common.model_config import ModelConfig
    from appsci_bench.appsci_agent_user_sim.core.usersim_core import AppsciAgentLLMUserSim

    captured: dict = {}

    def fake_complete(messages, config, **kwargs):
        captured["messages"] = messages
        return '{"message": "ok"}'

    monkeypatch.setattr(
        "appsci_bench.appsci_agent_user_sim.core.usersim_core.complete", fake_complete)
    sim = AppsciAgentLLMUserSim("SYS", ModelConfig(model="m", api_key="x"))
    Session(sim).ask("unique-question-42")

    users = [m for m in captured["messages"]
             if m["role"] == "user" and m["content"] == "unique-question-42"]
    assert len(users) == 1


def _reset():
    p = pub.get_publisher()
    if p is not None:
        p.close()
    pub._publisher = None


def test_usersim_events_carry_parent_span_and_full_untruncated_text(tmp_path, monkeypatch) -> None:
    # A reply longer than the 500-char event-payload cap must survive whole on the event (truncate=False),
    # and both request/response must parent under the forwarded tool span so the tracer nests the exchange.
    long_answer = "A" * 1200
    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    # Session builds its own publisher from these env vars (init_publisher(source=USERSIM)).
    monkeypatch.setenv("OBSERVABILITY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("OBSERVABILITY_TRACE_ID", "tr")
    monkeypatch.setenv("OBSERVABILITY_TASK", "t")
    try:
        Session(_FakeSim(reply=long_answer)).ask("question?", parent_span_id="tool-span-xyz")
    finally:
        _reset()

    evs = [json.loads(l) for l in
           get_source_log_path("tr", Source.USERSIM, str(tmp_path)).read_text().splitlines() if l.strip()]
    req = next(e for e in evs if e["event_type"] == "usersim.request")
    resp = next(e for e in evs if e["event_type"] == "usersim.response")

    assert req["parent_span_id"] == "tool-span-xyz"
    assert resp["parent_span_id"] == "tool-span-xyz"
    # Full answer preserved on the event (well past the 500-char default cap), not truncated to "...".
    assert resp["payload"]["answer"] == long_answer
    assert not resp["payload"]["answer"].endswith("...")


def test_usersim_events_carry_the_role_name(tmp_path, monkeypatch) -> None:
    # Session(name=role) -> the persona role rides on every usersim.* event as `name`.
    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OBSERVABILITY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("OBSERVABILITY_TRACE_ID", "tr")
    monkeypatch.setenv("OBSERVABILITY_TASK", "t")
    try:
        Session(_FakeSim(), name="Product Manager").ask("q?", parent_span_id="tool-1")
    finally:
        _reset()
    evs = [json.loads(l) for l in
           get_source_log_path("tr", Source.USERSIM, str(tmp_path)).read_text().splitlines() if l.strip()]
    assert evs and all(e["name"] == "Product Manager" for e in evs
                       if e["event_type"].startswith("usersim."))


def test_usersim_events_have_no_length_cap(tmp_path, monkeypatch) -> None:
    # The old 4,000-char slice on these two payloads is gone: they are the scored input to the
    # elicitation diagnostic, and a clipped answer reads as a fact the persona never gave. Both
    # sides must survive byte for byte, not merely "mostly".
    long_question = "Q" * 12_000
    long_answer = "A" * 12_000
    (tmp_path / "tr").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OBSERVABILITY_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("OBSERVABILITY_TRACE_ID", "tr")
    monkeypatch.setenv("OBSERVABILITY_TASK", "t")
    try:
        Session(_FakeSim(reply=long_answer)).ask(long_question)
    finally:
        _reset()

    evs = [json.loads(l) for l in
           get_source_log_path("tr", Source.USERSIM, str(tmp_path)).read_text().splitlines() if l.strip()]
    req = next(e for e in evs if e["event_type"] == "usersim.request")
    resp = next(e for e in evs if e["event_type"] == "usersim.response")

    assert req["payload"]["question"] == long_question
    assert resp["payload"]["answer"] == long_answer


def test_no_message_cap_constant_survives_anywhere() -> None:
    # A reintroduced cap would be invisible until a judge scored a truncated transcript, so guard the
    # absence of the constant itself across the package rather than just its use in session.py.
    import pathlib
    import appsci_bench

    root = pathlib.Path(appsci_bench.__file__).resolve().parent
    offenders = [str(p) for p in root.rglob("*.py") if "_MAX_MSG_CHARS" in p.read_text()]
    assert offenders == []
