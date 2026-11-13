"""Tests for the AppsciAgentLLMUserSim simulator core. No network -- complete() is mocked."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from appsci_bench.common.model_config import ModelConfig
from appsci_bench.appsci_agent_user_sim.core.usersim_core import (
    ROLE_AGENT,
    ROLE_SIM,
    AppsciAgentLLMUserSim,
    AppsciAgentUserSimError,
    _parse_message,
    _to_llm_messages,
)

_CFG = ModelConfig(model="anthropic/claude-opus-4-8", api_key="no-token")


class TestParseMessage:
    def test_clean_json(self) -> None:
        assert _parse_message('{"message": "hello"}') == "hello"

    def test_code_fenced_json(self) -> None:
        raw = '```json\n{"message": "fenced reply"}\n```'
        assert _parse_message(raw) == "fenced reply"

    def test_json_embedded_in_prose(self) -> None:
        raw = 'Sure, here you go: {"message": "embedded"} -- hope that helps!'
        assert _parse_message(raw) == "embedded"

    def test_message_with_braces_and_quotes(self) -> None:
        # message content itself contains braces/escaped quotes; brace-matching must stay string-aware.
        raw = '{"message": "use {curly} and say \\"hi\\""}'
        assert _parse_message(raw) == 'use {curly} and say "hi"'

    def test_non_string_message_rejected(self) -> None:
        # pydantic must reject a non-string message rather than coerce it.
        with pytest.raises(AppsciAgentUserSimError):
            _parse_message('{"message": 123}')

    def test_extra_field_rejected(self) -> None:
        # should_stop (or any extra field) is forbidden -> the sim cannot smuggle a stop signal.
        with pytest.raises(AppsciAgentUserSimError):
            _parse_message('{"message": "hi", "should_stop": true}')

    def test_garbled_raises(self) -> None:
        with pytest.raises(AppsciAgentUserSimError, match="could not parse"):
            _parse_message("not json at all")

    def test_no_object_raises(self) -> None:
        with pytest.raises(AppsciAgentUserSimError):
            _parse_message("[1, 2, 3]")


class TestRoleFlip:
    def test_message_construction_and_flip(self) -> None:
        history = [
            {"role": ROLE_AGENT, "content": "q1"},
            {"role": ROLE_SIM, "content": "a1"},
        ]
        msgs = _to_llm_messages("PERSONA", history, "q2")
        assert msgs == [
            {"role": "system", "content": "PERSONA"},
            {"role": "user", "content": "q1"},        # agent -> user
            # sim -> assistant, re-wrapped in the {"message": ...} JSON the persona asks for so the
            # model keeps emitting JSON on later turns (see _to_llm_messages docstring).
            {"role": "assistant", "content": '{"message": "a1"}'},
            {"role": "user", "content": "q2"},        # new question -> user
        ]

    def test_sim_turns_rewrapped_as_json(self) -> None:
        # The replayed sim turn must be valid JSON that round-trips back to the original prose --
        # this is the regression guard for the turn-2 prose-drift 500 (special chars must escape).
        history = [{"role": ROLE_SIM, "content": 'he said "hi" {ok}\nline2'}]
        msgs = _to_llm_messages("P", history, "q")
        assert json.loads(msgs[1]["content"]) == {"message": 'he said "hi" {ok}\nline2'}

    def test_unknown_role_raises(self) -> None:
        with pytest.raises(AppsciAgentUserSimError, match="unknown role"):
            _to_llm_messages("P", [{"role": "system", "content": "x"}], "q")


class TestAnswer:
    def test_answer_builds_messages_and_returns_parsed(self) -> None:
        sim = AppsciAgentLLMUserSim("SYSTEM PROMPT", _CFG)
        history = [
            {"role": ROLE_AGENT, "content": "earlier q"},
            {"role": ROLE_SIM, "content": "earlier a"},
        ]
        with patch("appsci_bench.appsci_agent_user_sim.core.usersim_core.complete",
                   return_value='{"message": "the answer"}') as mock:
            out = sim.answer("new q", history)

        assert out == "the answer"
        sent_messages = mock.call_args.args[0]
        assert sent_messages[0] == {"role": "system", "content": "SYSTEM PROMPT"}
        assert sent_messages[-1] == {"role": "user", "content": "new q"}
        # role flip applied to replayed history
        assert sent_messages[1]["role"] == "user" and sent_messages[2]["role"] == "assistant"
        # json_object hint requested
        assert mock.call_args.kwargs.get("json_object") is True

    def test_answer_propagates_parse_error(self) -> None:
        sim = AppsciAgentLLMUserSim("P", _CFG)
        with patch("appsci_bench.appsci_agent_user_sim.core.usersim_core.complete", return_value="garbage"):
            with pytest.raises(AppsciAgentUserSimError):
                sim.answer("q", [])


class TestAnswerRetriesProseReplies:
    """A prose reply must cost one retry, not the whole exchange.

    `json_object=True` is a hint the gateway this runs behind accepts and then ignores, so a persona
    model answering in markdown instead of `{"message": ...}` is a live, probabilistic failure: it
    stalled a 40-minute airline build, because the service turned the parse failure into a 500 and the
    agent's `talk_to_user` surfaced it as a bare "Server error".
    """

    _PROSE = "A few of these are off. Let me correct:\n\n**1. Cabin:** basic economy can be changed..."

    def test_a_prose_reply_is_retried_and_the_retry_is_used(self) -> None:
        sim = AppsciAgentLLMUserSim("SYSTEM", _CFG)
        with patch(
            "appsci_bench.appsci_agent_user_sim.core.usersim_core.complete",
            side_effect=[self._PROSE, '{"message": "the real answer"}'],
        ) as mock:
            out = sim.answer("what are the change rules?", [])

        assert out == "the real answer"
        assert mock.call_count == 2

        # The retry must carry the original conversation, the prose the model actually sent, and a
        # corrective -- not a bare re-ask, which would just resample the same drift.
        retry_messages = mock.call_args_list[1].args[0]
        assert retry_messages[0]["role"] == "system"
        assert any(m.get("content") == self._PROSE for m in retry_messages), (
            "the retry must show the model its own unparseable reply"
        )
        assert "not valid JSON" in retry_messages[-1]["content"]
        assert mock.call_args_list[1].kwargs.get("json_object") is True

    def test_a_prose_reply_that_stays_prose_still_fails_loud(self) -> None:
        """No default-filling. An invented persona turn would put words in the stakeholder's mouth,
        and the elicitation metric is scored on what the persona actually said."""
        sim = AppsciAgentLLMUserSim("SYSTEM", _CFG)
        with patch(
            "appsci_bench.appsci_agent_user_sim.core.usersim_core.complete",
            side_effect=[self._PROSE, "still not json"],
        ) as mock:
            with pytest.raises(AppsciAgentUserSimError, match="retried once"):
                sim.answer("q", [])

        assert mock.call_count == 2

    def test_a_parseable_reply_costs_no_extra_call(self) -> None:
        """The retry must not become a tax on the normal path -- every exchange pays for the sim."""
        sim = AppsciAgentLLMUserSim("SYSTEM", _CFG)
        with patch(
            "appsci_bench.appsci_agent_user_sim.core.usersim_core.complete",
            return_value='{"message": "fine"}',
        ) as mock:
            assert sim.answer("q", []) == "fine"

        assert mock.call_count == 1

    def test_the_offending_output_is_logged(self, caplog) -> None:
        """The reason a build stalled has to exist somewhere on disk. Before this it lived only in the
        500's `detail`, which the caller discards."""
        sim = AppsciAgentLLMUserSim("SYSTEM", _CFG)
        with caplog.at_level("WARNING"), patch(
            "appsci_bench.appsci_agent_user_sim.core.usersim_core.complete",
            side_effect=[self._PROSE, '{"message": "ok"}'],
        ):
            sim.answer("q", [])

        assert any("Cabin" in r.getMessage() for r in caplog.records), (
            "the unparseable output itself must be logged, not just the fact of failure"
        )


class TestRunConversation:
    def test_respects_max_turns(self) -> None:
        sim = AppsciAgentLLMUserSim("P", _CFG)
        # agent always has another question -> only max_turns bounds the loop.
        agent_fn = lambda _sim_msg: "another question"
        with patch("appsci_bench.appsci_agent_user_sim.core.usersim_core.complete",
                   return_value='{"message": "reply"}'):
            transcript = sim.run_conversation(agent_fn, max_turns=3)
        # 3 exchanges -> 6 turns (agent + sim each).
        assert len(transcript) == 6
        assert sum(1 for t in transcript if t["role"] == ROLE_AGENT) == 3
        assert sum(1 for t in transcript if t["role"] == ROLE_SIM) == 3

    def test_stops_when_agent_done(self) -> None:
        sim = AppsciAgentLLMUserSim("P", _CFG)
        calls = {"n": 0}

        def agent_fn(_sim_msg):
            calls["n"] += 1
            return "q" if calls["n"] == 1 else None  # done after the first exchange

        with patch("appsci_bench.appsci_agent_user_sim.core.usersim_core.complete",
                   return_value='{"message": "reply"}'):
            transcript = sim.run_conversation(agent_fn, max_turns=10)
        assert len(transcript) == 2  # one exchange, then agent signaled done

    def test_first_agent_call_gets_none(self) -> None:
        sim = AppsciAgentLLMUserSim("P", _CFG)
        seen = []

        def agent_fn(sim_msg):
            seen.append(sim_msg)
            return None if len(seen) > 1 else "q"

        with patch("appsci_bench.appsci_agent_user_sim.core.usersim_core.complete",
                   return_value='{"message": "reply"}'):
            sim.run_conversation(agent_fn, max_turns=5)
        assert seen[0] is None          # first turn: no prior sim message
        assert seen[1] == "reply"       # second turn: sees the sim's last reply

    def test_agent_fn_retried_then_succeeds(self) -> None:
        sim = AppsciAgentLLMUserSim("P", _CFG)
        attempts = {"n": 0}

        def flaky_agent(_sim_msg):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("transient agent failure")
            return None if attempts["n"] > 2 else "q"

        with patch("appsci_bench.appsci_agent_user_sim.core.usersim_core.complete",
                   return_value='{"message": "reply"}'):
            transcript = sim.run_conversation(flaky_agent, max_turns=5, agent_retries=2)
        assert len(transcript) == 2  # recovered from the first-call failure, did one exchange

    def test_agent_fn_exhausts_retries_raises(self) -> None:
        sim = AppsciAgentLLMUserSim("P", _CFG)

        def always_fails(_sim_msg):
            raise RuntimeError("agent down")

        with patch("appsci_bench.appsci_agent_user_sim.core.usersim_core.complete",
                   return_value='{"message": "reply"}'):
            with pytest.raises(RuntimeError, match="agent down"):
                sim.run_conversation(always_fails, max_turns=5, agent_retries=1)
