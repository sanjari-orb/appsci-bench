"""Unit tests: artifact agent user sim -- turn output schema, prompt rendering, and the
session-based `ArtifactAgentUserSim` core.

No network -- `complete()` is mocked at the `appsci_bench.eval.helpers.llm` import site, mirroring
`test_llm.py`'s stubbing pattern.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from appsci_bench.eval.artifact_agent_user_sim import (
    _SYSTEM_PROMPT, PARITY_DIVERGED, PARITY_EXACT, PARITY_NEAR, TAU_BENCH_USER_SIM_MODEL,
    ArtifactAgentUserSim, ArtifactAgentUserSimulator, ScriptedArtifactAgentUserSim, _SimTurn,
    parity_status, render_sim_prompt, resolve_user_sim_config,
)
from appsci_bench.eval.datatypes import (
    ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn, File, MultiTurnInput,
)


def _sample(**kwargs) -> MultiTurnInput:
    defaults = dict(user_task="rebook my flight", user_persona="a tired business traveler")
    defaults.update(kwargs)
    return MultiTurnInput(**defaults)


def _turn(n: int) -> ConversationTurn:
    return ConversationTurn(ArtifactAgentQuery(text=f"q{n}"), ArtifactAgentOutput(text=f"r{n}"))


# --- _SimTurn ------------------------------------------------------------------------------------

def test_sim_turn_parses_valid_payload():
    turn = _SimTurn(reasoning="still going", stopped=False, message="hi there", attach_files=["a.pdf"])
    assert turn.stopped is False
    assert turn.message == "hi there"
    assert turn.attach_files == ["a.pdf"]


def test_sim_turn_rejects_missing_message_when_not_stopped():
    with pytest.raises(ValidationError):
        _SimTurn(reasoning="no message", stopped=False)


def test_sim_turn_rejects_empty_message_without_a_tool_call():
    with pytest.raises(ValidationError):
        _SimTurn(reasoning="no action", stopped=False, message="")


def test_sim_turn_allows_empty_message_during_a_tool_only_step():
    turn = _SimTurn(
        reasoning="look it up", stopped=False, message="",
        call_user_tool={"name": "lookup", "arguments": {"id": "123"}},
    )
    assert turn.message == ""
    assert turn.call_user_tool is not None


def test_sim_turn_allows_null_message_when_stopped():
    turn = _SimTurn(reasoning="all done", stopped=True, message=None)
    assert turn.message is None


def test_sim_turn_requires_message_key_even_when_stopped():
    with pytest.raises(ValidationError):
        _SimTurn(reasoning="all done", stopped=True)


def test_sim_turn_rejects_unknown_field():
    with pytest.raises(ValidationError):
        _SimTurn(reasoning="x", stopped=True, message=None, extra_field="surprise")


def test_sim_turn_attach_files_defaults_empty():
    turn = _SimTurn(reasoning="x", stopped=True, message=None)
    assert turn.attach_files == []


# --- render_sim_prompt -----------------------------------------------------------------------------

def test_render_prompt_includes_task_persona_stop_criteria():
    sample = _sample(user_stop_criteria="the rebooking is confirmed")
    rendered = render_sim_prompt(sample=sample, conversation=[])
    assert "rebook my flight" in rendered
    assert "a tired business traveler" in rendered
    assert "the rebooking is confirmed" in rendered


def test_render_prompt_defaults_stop_criteria_when_unset():
    sample = _sample()  # user_stop_criteria not passed -> dataclass default
    rendered = render_sim_prompt(sample=sample, conversation=[])
    assert "The user task has been successfully completed." in rendered


def test_render_prompt_opening_turn_states_stopped_must_be_false():
    rendered = render_sim_prompt(sample=_sample(), conversation=[])
    assert "has not started yet" in rendered
    assert "`stopped` must be `false`" in rendered


def test_render_prompt_non_opening_turn_includes_transcript():
    rendered = render_sim_prompt(sample=_sample(), conversation=[_turn(1)])
    assert "has not started yet" not in rendered
    assert "q1" in rendered and "r1" in rendered


def test_render_prompt_lists_all_sample_files_as_available():
    sample = _sample(files=[File(file_type="pdf", file_name="itinerary.pdf")])
    rendered = render_sim_prompt(sample=sample, conversation=[])
    assert "itinerary.pdf (pdf)" in rendered


def test_render_prompt_still_lists_a_file_already_sent_in_the_transcript():
    # Resending is allowed (the transcript is what shows send history, not this list) -- an
    # already-sent file must stay in the available-files list, not drop out of it.
    sent = File(file_type="pdf", file_name="itinerary.pdf")
    sample = _sample(files=[sent])
    conversation = [ConversationTurn(
        ArtifactAgentQuery(text="here", files=[sent]), ArtifactAgentOutput(text="got it"))]
    rendered = render_sim_prompt(sample=sample, conversation=conversation)
    assert "- itinerary.pdf (pdf)" in rendered                        # still in the available list
    assert "[a file was attached: itinerary.pdf (pdf)]" in rendered   # and visible in the transcript


def test_render_prompt_no_files_says_none():
    rendered = render_sim_prompt(sample=_sample(), conversation=[])
    assert "(none)" in rendered


def test_render_prompt_transcript_never_contains_file_data():
    sent = File(file_type="pdf", file_name="boarding_pass.pdf", file_data=b"%PDF-secret-bytes")
    conversation = [ConversationTurn(
        ArtifactAgentQuery(text="here", files=[sent]), ArtifactAgentOutput(text="got it"))]
    rendered = render_sim_prompt(sample=_sample(), conversation=conversation)
    assert "boarding_pass.pdf" in rendered
    assert "PDF-secret-bytes" not in rendered


def test_system_prompt_states_the_file_content_gap():
    assert "cannot see file contents" in _SYSTEM_PROMPT


def test_system_prompt_states_message_key_is_always_required():
    assert "always include" in _SYSTEM_PROMPT
    assert '"message" key' in _SYSTEM_PROMPT


# --- ArtifactAgentUserSim (sessions) ---------------------------------------------------------------

def _turn_json(*, stopped: bool, message: str | None = None, attach_files=None, reasoning="r") -> str:
    payload: dict = {"reasoning": reasoning, "stopped": stopped, "message": message}
    if attach_files is not None:
        payload["attach_files"] = attach_files
    return json.dumps(payload)


def test_sim_satisfies_protocol():
    assert isinstance(ArtifactAgentUserSim(), ArtifactAgentUserSimulator)


def test_ask_sends_a_fully_specified_schema_to_the_model():
    # Regression: a live run against a real model once omitted "reasoning" entirely and got a
    # JudgeParseError, because the old system prompt said "matching the requested schema" without
    # ever naming the actual keys. Fixed at the shared Level 0 layer (llm_judge_call now appends
    # _describe_output_schema(output_schema) to every call's system prompt) rather than by hand in
    # this module's own prompt -- this test proves that fix reaches _SimTurn's real call, not just
    # that some other module's unit test for the generic mechanism passes.
    sim = ArtifactAgentUserSim()
    sid = sim.create_session(_sample())
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value=_turn_json(stopped=False, message="hi")) as mock_complete:
        sim.ask(sid, [])
    (messages, _), _ = mock_complete.call_args
    system_content = messages[0]["content"]
    for key in ('"reasoning"', '"stopped"', '"message"', '"attach_files"'):
        assert key in system_content
    assert '"message" (string, required)' in system_content
    assert '"message" (string, optional)' not in system_content


def test_ask_forwards_its_config_to_llm_judge_call():
    from appsci_bench.common.model_config import ModelConfig

    cfg = ModelConfig(model="openai/gpt-5.5", api_key="k")
    sim = ArtifactAgentUserSim(config=cfg)
    sid = sim.create_session(_sample())
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value=_turn_json(stopped=False, message="hi")) as mock_complete:
        sim.ask(sid, [])
    (_, used_config), _ = mock_complete.call_args
    assert used_config is cfg


# --- model choice (tau-bench parity) -------------------------------------------------------------

def test_user_sim_model_is_the_one_the_published_airline_submissions_declare():
    # The authority is the leaderboard submissions, NOT tau2's `DEFAULT_LLM_USER` -- that code constant
    # is still gpt-4.1-2025-04-14 at v1.0.1, but the published runs override it, and every text airline
    # submission at tau2_bench_version 1.0.1 declares `user_simulator: "gpt-5.2"`. Matching the code
    # default would mean matching a configuration we are not measured against.
    #
    # Floating on purpose, reversing the old snapshot rule: no submission names a dated gpt-5.2
    # snapshot, so a dated pin would name something the leaderboard does not. Change this only to
    # track what the submissions declare.
    assert TAU_BENCH_USER_SIM_MODEL == "openai/gpt-5.2"


def test_resolve_user_sim_config_defaults_to_the_tau_bench_model():
    cfg = resolve_user_sim_config({})
    assert cfg.model == TAU_BENCH_USER_SIM_MODEL
    # No key/base_url pair on purpose: LiteLLM resolves provider-standard credentials itself.
    assert cfg.api_key is None and cfg.base_url is None


def test_resolve_user_sim_config_ignores_the_shared_model_namespace():
    # The whole point of the separate var: the repo-wide MODEL_* vars (Claude, used by the build-time
    # appsci-agent-user-sim and the eval judges) must not drag the eval-time sim along.
    cfg = resolve_user_sim_config({
        "MODEL_NAME": "anthropic/claude-opus-4-8",
        "MODEL_API_KEY": "gateway-jwt",
        "MODEL_BASE_URL": "https://gateway.example.com",
    })
    assert cfg.model == TAU_BENCH_USER_SIM_MODEL
    assert cfg.api_key is None and cfg.base_url is None


def test_resolve_user_sim_config_honors_the_override_var():
    cfg = resolve_user_sim_config({"ARTIFACT_USERSIM_MODEL": "anthropic/claude-opus-4-8"})
    assert cfg.model == "anthropic/claude-opus-4-8"


def test_resolve_user_sim_config_treats_a_blank_override_as_unset():
    cfg = resolve_user_sim_config({"ARTIFACT_USERSIM_MODEL": "   "})
    assert cfg.model == TAU_BENCH_USER_SIM_MODEL


def test_parity_status_grades_the_three_cases():
    # The gateway alias is NEAR-parity, not exact and not diverged: `setup_local_with_gateway.sh` must
    # name it (the gateway rejects the first-party id `gpt-5.2`), so a strict != would flag the
    # documented internal path as non-comparable on every run.
    assert parity_status(TAU_BENCH_USER_SIM_MODEL) == PARITY_EXACT
    assert parity_status("openai/openai-gpt-5.2") == PARITY_NEAR
    assert parity_status("anthropic/claude-opus-4-8") == PARITY_DIVERGED


def test_the_superseded_gpt_4_1_sim_now_counts_as_diverged():
    # The likeliest failure mode of moving this pin is a stale ARTIFACT_USERSIM_MODEL left in someone's
    # environment or on the jobs host. Neither the old first-party id nor the old gateway alias may be
    # quietly accepted as near-parity: a run on the v0.1.3-era simulator is not comparable to the
    # v1.0.1 airline column, and the divergence is invisible in the resulting numbers.
    assert parity_status("openai/gpt-4.1-2025-04-14") == PARITY_DIVERGED
    assert parity_status("openai/openai-gpt-4.1") == PARITY_DIVERGED


def test_resolve_does_not_warn_on_a_known_gateway_alias(caplog):
    # Near-parity is informational: warning here would fire on the common internal path.
    with caplog.at_level("INFO"):
        resolve_user_sim_config({"ARTIFACT_USERSIM_MODEL": "openai/openai-gpt-5.2"})
    assert "near-parity" in caplog.text
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_resolve_warns_when_the_model_diverges_from_parity(caplog):
    # Comparability to tau-bench is the only reason the model is pinned, and a divergence is
    # invisible in the resulting scores -- so it has to be loud at resolution time.
    with caplog.at_level("WARNING"):
        resolve_user_sim_config({"ARTIFACT_USERSIM_MODEL": "anthropic/claude-opus-4-8"})
    assert "not tau-bench-comparable" in caplog.text
    assert "anthropic/claude-opus-4-8" in caplog.text


def test_resolve_is_quiet_on_the_parity_model(caplog):
    with caplog.at_level("WARNING"):
        resolve_user_sim_config({})
    assert caplog.text == ""


def test_sim_exposes_its_resolved_model(monkeypatch):
    monkeypatch.delenv("ARTIFACT_USERSIM_MODEL", raising=False)
    assert ArtifactAgentUserSim().model == TAU_BENCH_USER_SIM_MODEL
    monkeypatch.setenv("ARTIFACT_USERSIM_MODEL", "anthropic/claude-opus-4-8")
    assert ArtifactAgentUserSim().model == "anthropic/claude-opus-4-8"


def test_sim_uses_the_tau_bench_model_when_no_config_is_passed(monkeypatch):
    # End-to-end through ask(): the default-constructed sim must reach `complete()` with the
    # tau-bench model even when MODEL_NAME says Claude -- i.e. it does NOT fall through to
    # llm_judge_call's own ModelConfig.from_env() fallback on the shared namespace.
    monkeypatch.setenv("MODEL_NAME", "anthropic/claude-opus-4-8")
    monkeypatch.delenv("ARTIFACT_USERSIM_MODEL", raising=False)
    sim = ArtifactAgentUserSim()
    sid = sim.create_session(_sample())
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value=_turn_json(stopped=False, message="hi")) as mock_complete:
        sim.ask(sid, [])
    (_, used_config), _ = mock_complete.call_args
    assert used_config.model == TAU_BENCH_USER_SIM_MODEL


def test_sim_honors_an_env_override_of_its_model(monkeypatch):
    monkeypatch.setenv("ARTIFACT_USERSIM_MODEL", "anthropic/claude-opus-4-8")
    sim = ArtifactAgentUserSim()
    sid = sim.create_session(_sample())
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value=_turn_json(stopped=False, message="hi")) as mock_complete:
        sim.ask(sid, [])
    (_, used_config), _ = mock_complete.call_args
    assert used_config.model == "anthropic/claude-opus-4-8"


def test_create_session_returns_fresh_id_per_call():
    sim = ArtifactAgentUserSim()
    a = sim.create_session(_sample())
    b = sim.create_session(_sample())
    assert a != b


def test_ask_on_unknown_session_raises():
    sim = ArtifactAgentUserSim()
    with pytest.raises(ValueError):
        sim.ask("does-not-exist", [])


def test_delete_session_on_unknown_session_raises():
    sim = ArtifactAgentUserSim()
    with pytest.raises(ValueError):
        sim.delete_session("does-not-exist")


def test_delete_session_twice_raises_second_time():
    sim = ArtifactAgentUserSim()
    sid = sim.create_session(_sample())
    sim.delete_session(sid)
    with pytest.raises(ValueError):
        sim.delete_session(sid)


def test_ask_after_delete_session_raises():
    sim = ArtifactAgentUserSim()
    sid = sim.create_session(_sample())
    sim.delete_session(sid)
    with pytest.raises(ValueError):
        sim.ask(sid, [])


def test_ask_opening_turn_returns_query_seeded_from_message():
    sim = ArtifactAgentUserSim()
    sid = sim.create_session(_sample())
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value=_turn_json(stopped=False, message="hi, I need to rebook")):
        query = sim.ask(sid, [])
    assert query is not None
    assert query.text == "hi, I need to rebook"


def test_ask_stop_path_returns_none():
    sim = ArtifactAgentUserSim()
    sid = sim.create_session(_sample())
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value=_turn_json(stopped=True, reasoning="criteria met")):
        query = sim.ask(sid, [_turn(1)])
    assert query is None


def test_ask_attach_files_resolves_against_sample():
    sample = _sample(files=[File(file_type="pdf", file_name="itinerary.pdf")])
    sim = ArtifactAgentUserSim()
    sid = sim.create_session(sample)

    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value=_turn_json(
                   stopped=False, message="here's my file", attach_files=["itinerary.pdf"])):
        query = sim.ask(sid, [])
    assert [f.file_name for f in query.files] == ["itinerary.pdf"]


def test_ask_attach_files_ignores_names_not_in_sample():
    sample = _sample(files=[File(file_type="pdf", file_name="itinerary.pdf")])
    sim = ArtifactAgentUserSim()
    sid = sim.create_session(sample)

    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value=_turn_json(
                   stopped=False, message="here", attach_files=["made_up.pdf"])):
        query = sim.ask(sid, [])
    assert query.files == []


def test_ask_allows_resending_a_file_already_sent_in_an_earlier_turn():
    sample = _sample(files=[File(file_type="pdf", file_name="itinerary.pdf")])
    sim = ArtifactAgentUserSim()
    sid = sim.create_session(sample)

    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value=_turn_json(
                   stopped=False, message="here's my file", attach_files=["itinerary.pdf"])):
        first = sim.ask(sid, [])
    conversation = [ConversationTurn(first, ArtifactAgentOutput(text="thanks"))]

    # The model chooses to attach the SAME file again on a later turn -- must still resolve,
    # not be silently dropped just because it was already sent once.
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value=_turn_json(
                   stopped=False, message="here it is again", attach_files=["itinerary.pdf"])):
        second = sim.ask(sid, conversation)
    assert [f.file_name for f in second.files] == ["itinerary.pdf"]


def test_never_stopping_sim_runs_to_max_turns_ceiling():
    # Mirrors generation.py's loop shape: the caller, not the sim, enforces max_turns.
    sim = ArtifactAgentUserSim()
    sid = sim.create_session(_sample())
    max_turns = 3
    conversation: list[ConversationTurn] = []
    turns_taken = 0
    with patch("appsci_bench.eval.helpers.llm.complete",
               return_value=_turn_json(stopped=False, message="still going")):
        query = sim.ask(sid, conversation)
        for _ in range(max_turns):
            if query is None:
                break
            conversation.append(_turn(len(conversation) + 1))
            turns_taken += 1
            query = sim.ask(sid, conversation)
    assert turns_taken == max_turns
    assert query is not None  # ceiling reached -- the sim itself never fired stopped=True


def test_session_isolation_under_concurrency():
    # N sessions from N samples with distinct files, driven concurrently: each session's
    # attach_files choice must only ever reference its own sample's file, never another
    # session's -- confirming each session_id maps to its own stored sample, not a shared one.
    sim = ArtifactAgentUserSim()
    samples = [_sample(files=[File(file_type="pdf", file_name=f"file_{i}.pdf")]) for i in range(8)]
    session_ids = [sim.create_session(s) for s in samples]

    def _side_effect(messages, config, **kwargs):
        prompt = messages[-1]["content"]
        match = re.search(r"- (file_\d+\.pdf) \(pdf\)", prompt)
        attach = [match.group(1)] if match else []
        return _turn_json(stopped=False, message="here", attach_files=attach)

    with patch("appsci_bench.eval.helpers.llm.complete", side_effect=_side_effect):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda sid: sim.ask(sid, []), session_ids))

    for i, query in enumerate(results):
        assert [f.file_name for f in query.files] == [f"file_{i}.pdf"]


# --- ScriptedArtifactAgentUserSim (deterministic test stub) ----------------------------------------

def test_scripted_sim_satisfies_protocol():
    assert isinstance(ScriptedArtifactAgentUserSim([]), ArtifactAgentUserSimulator)


def test_scripted_sim_replays_messages_then_stops():
    sim = ScriptedArtifactAgentUserSim(["one", "two"])
    sid = sim.create_session(_sample())
    assert sim.ask(sid, []).text == "one"
    assert sim.ask(sid, [_turn(1)]).text == "two"
    assert sim.ask(sid, [_turn(1), _turn(2)]) is None  # script exhausted -> stop


def test_scripted_sim_string_messages_coerced_to_query():
    sim = ScriptedArtifactAgentUserSim(["hello"])
    sid = sim.create_session(_sample())
    assert isinstance(sim.ask(sid, []), ArtifactAgentQuery)


def test_scripted_sim_explicit_query_messages_passthrough():
    q = ArtifactAgentQuery(text="x", json='{"a":1}')
    sim = ScriptedArtifactAgentUserSim([q])
    sid = sim.create_session(_sample())
    assert sim.ask(sid, []) is q


def test_scripted_sim_empty_script_stops_immediately():
    sim = ScriptedArtifactAgentUserSim([])
    sid = sim.create_session(_sample())
    assert sim.ask(sid, []) is None


def test_scripted_sim_sessions_independent():
    sim = ScriptedArtifactAgentUserSim(["one", "two"])
    a = sim.create_session(_sample())
    b = sim.create_session(_sample())
    assert sim.ask(a, []).text == "one"     # a's first message
    assert sim.ask(b, []).text == "one"     # b starts from its own position 0, unaffected by a


def test_scripted_sim_unknown_session_raises():
    sim = ScriptedArtifactAgentUserSim(["one"])
    with pytest.raises(ValueError):
        sim.ask("does-not-exist", [])
    with pytest.raises(ValueError):
        sim.delete_session("does-not-exist")


def test_scripted_sim_delete_then_ask_raises():
    sim = ScriptedArtifactAgentUserSim(["one"])
    sid = sim.create_session(_sample())
    sim.delete_session(sid)
    with pytest.raises(ValueError):
        sim.ask(sid, [])
