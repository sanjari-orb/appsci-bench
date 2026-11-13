"""Build-loop resilience: what the agent loop does with a turn that reaches no tool.

One truncated response used to wedge a whole build. It was recorded in history, which taught the
model to keep sending the same malformed call, and nothing counted the failures, so the run spent its
remaining turns re-sending it (order_to_cash_claude-sonnet-4-6_b1_l3_20260831_074504: 37 rejected
calls, 24 turns). These tests pin the behaviours that stop that -- a truncated turn is discarded from
history and counted toward the stuck-run breaker, with no per-occurrence reactive message (that
guidance now lives once, generically, in the system prompt) -- and the distinction the breaker
depends on: a call that never ran is not the same as work that ran and failed.
"""
from __future__ import annotations

import contextlib

import pytest

from appsci_agent.core.agent import (
    AppSciAgent, AgentState, CONTINUE_NUDGE, MAX_CONSECUTIVE_UNUSABLE_TURNS,
)
from appsci_agent.core.config import AgentConfig
from appsci_agent.core.llm_client import LLMResponse, ToolCall
from appsci_agent.tools import ToolContext, ToolResult


@pytest.fixture
def agent():
    return AppSciAgent(config=AgentConfig(api_key='k', base_url='https://example.invalid'))


def _run(agent, response, state=None):
    state = state or AgentState()
    agent._process_response(response, state, ToolContext(), [])
    return state


def _call(name='mini_swe_agent', args=None, cid='c1'):
    return ToolCall(id=cid, name=name, arguments=args if args is not None else {})


def test_a_truncated_turn_is_not_written_to_history(agent):
    """The fix that matters. Recording the partial turn is what taught the model to repeat it."""
    state = _run(agent, LLMResponse(content='I will now write the server',
                                    tool_calls=[_call()], finish_reason='length'))

    assert not any(m.get('role') == 'assistant' for m in state.messages), \
        'the cut-off assistant turn must not become an example for the model to follow'
    assert not any(m.get('role') == 'tool' for m in state.messages), 'nothing should have run'


def test_a_truncated_turn_gets_no_reactive_explanation(agent):
    """Review feedback: state the limits once in the system prompt instead of per-occurrence.

    Nothing is appended at all -- the next request just repeats the same messages, relying on the
    system prompt's general guidance rather than a message explaining this specific occurrence.
    """
    state = _run(agent, LLMResponse(tool_calls=[_call()], finish_reason='length'))

    assert state.messages == []


def test_a_truncated_turn_counts_toward_the_breaker(agent):
    state = AgentState()
    for i in range(3):
        _run(agent, LLMResponse(tool_calls=[_call()], finish_reason='length'), state)
        assert state.consecutive_unusable_turns == i + 1


def test_an_unknown_tool_name_counts_toward_the_breaker(agent):
    """Caught in review. `_execute_tool` builds this ToolResult itself, so it has to set the flag.

    Without it a model looping on a hallucinated tool name reads as progress and the breaker never
    fires -- the precise wedge it exists to stop.
    """
    state = _run(agent, LLMResponse(tool_calls=[_call(name='no_such_tool')],
                                    finish_reason='tool_calls'))

    assert state.consecutive_unusable_turns == 1
    assert any(m.get('role') == 'tool' for m in state.messages)   # the model still sees the rejection


def test_a_rejected_argument_counts_toward_the_breaker(agent):
    """A real call the tool refused before running: the b1 failure shape."""
    state = _run(agent, LLMResponse(tool_calls=[_call(name='mini_swe_agent', args={})],
                                    finish_reason='tool_calls'))

    assert state.consecutive_unusable_turns == 1


def test_talk_to_user_without_a_question_is_also_an_invalid_call(agent):
    result = agent.tools['talk_to_user'].execute({}, ToolContext())
    assert result.invalid_call is True and result.success is False


def test_eval_generator_without_a_handoff_file_is_also_an_invalid_call(agent, tmp_path):
    """Caught in review. A precondition rejection is nothing having run, same as a bad argument."""
    result = agent.tools['eval_generator'].execute({}, ToolContext(artifact_dir=str(tmp_path)))
    assert result.invalid_call is True and result.success is False


def test_optimize_agent_without_an_eval_is_also_an_invalid_call(agent, tmp_path):
    result = agent.tools['optimize_agent'].execute({}, ToolContext(artifact_dir=str(tmp_path)))
    assert result.invalid_call is True and result.success is False


def test_optimize_agent_without_a_deployed_candidate_is_also_an_invalid_call(agent, tmp_path):
    eval_dir = tmp_path / 'eval'
    eval_dir.mkdir()
    for name in ('run.py', 'eval_data.json', 'README.md'):
        (eval_dir / name).write_text('{}')
    context = ToolContext(artifact_dir=str(tmp_path), workspace_dir=str(tmp_path),
                           eval_ready=True, artifact_endpoint=None)
    result = agent.tools['optimize_agent'].execute({}, context)
    assert result.invalid_call is True and result.success is False


def test_work_that_ran_and_failed_does_not_count(agent, monkeypatch):
    """The distinction the breaker rests on.

    A deploy that fails four times while the agent fixes its Dockerfile is ordinary iteration and such
    runs recover. Counting it would abort builds that were making progress.
    """
    monkeypatch.setattr(agent.tools['deploy_artifact'], 'execute',
                        lambda params, context: ToolResult(
                            success=False, output={}, error='image build failed'))
    state = AgentState()
    for _ in range(MAX_CONSECUTIVE_UNUSABLE_TURNS + 2):
        _run(agent, LLMResponse(tool_calls=[_call(name='deploy_artifact')],
                                finish_reason='tool_calls'), state)

    assert state.consecutive_unusable_turns == 0


def test_a_tool_that_raises_does_not_count(agent, monkeypatch):
    """Same reasoning: it ran, so the agent has a real error to work from."""
    def boom(params, context):
        raise RuntimeError('exploded')

    monkeypatch.setattr(agent.tools['deploy_artifact'], 'execute', boom)
    state = _run(agent, LLMResponse(tool_calls=[_call(name='deploy_artifact')],
                                    finish_reason='tool_calls'))

    assert state.consecutive_unusable_turns == 0


def test_one_good_turn_resets_the_streak(agent, monkeypatch):
    monkeypatch.setattr(agent.tools['deploy_artifact'], 'execute',
                        lambda params, context: ToolResult(success=True, output={'ok': True}))
    state = AgentState()
    for _ in range(MAX_CONSECUTIVE_UNUSABLE_TURNS - 1):
        _run(agent, LLMResponse(tool_calls=[_call()], finish_reason='length'), state)
    assert state.consecutive_unusable_turns == MAX_CONSECUTIVE_UNUSABLE_TURNS - 1

    _run(agent, LLMResponse(tool_calls=[_call(name='deploy_artifact')],
                            finish_reason='tool_calls'), state)
    assert state.consecutive_unusable_turns == 0, 'recovery must clear the streak, not just pause it'


def test_the_breaker_trips_well_before_the_turn_ceiling(agent):
    """The point of the breaker: end a wedged run with budget left, not after consuming it."""
    assert MAX_CONSECUTIVE_UNUSABLE_TURNS < AgentConfig().max_turns / 2


# --- Provider refusals and empty turns ----------------------------------------------------------------
#
# crm_insights_claude-opus-5_b2_l3_20260920_175834 ended on its 40-turn ceiling after 7 minutes: 29
# turns in a row came back finish_reason=content_filter, 26 of them with no content and no tool call.
# Those went to the no-tool-call branch, which only appended a nudge and never touched the breaker, so
# the run spent every remaining turn being refused and was recorded as an ordinary no-submission.

def test_a_blocked_turn_counts_toward_the_breaker(agent):
    state = AgentState()
    for i in range(3):
        _run(agent, LLMResponse(finish_reason='content_filter'), state)
        assert state.consecutive_unusable_turns == i + 1
    assert state.content_filtered_turns == 3


def test_an_empty_turn_counts_toward_the_breaker(agent):
    """No content and no tool call carries nothing to act on, whatever the finish_reason."""
    state = _run(agent, LLMResponse(finish_reason='stop'))
    assert state.consecutive_unusable_turns == 1
    assert state.content_filtered_turns == 0


def test_a_text_only_turn_still_does_not_count(agent):
    """The model said something; the nudge answers it. Only empty or blocked turns are unusable."""
    state = _run(agent, LLMResponse(content='Let me think about the schema first.',
                                    finish_reason='stop'))
    assert state.consecutive_unusable_turns == 0
    assert state.messages[0] == {'role': 'assistant', 'content': 'Let me think about the schema first.'}


def test_a_blocked_turn_with_a_tool_call_runs_nothing(agent, monkeypatch):
    """Three of the 29 refusals still carried a tool call. A blocked response is cut off where the
    block landed, so its call is not trusted any more than a truncated one is."""
    ran = []
    monkeypatch.setattr(agent.tools['deploy_artifact'], 'execute',
                        lambda params, context: ran.append(params) or ToolResult(success=True, output={}))
    state = _run(agent, LLMResponse(content='partial', tool_calls=[_call(name='deploy_artifact')],
                                    finish_reason='content_filter'))

    assert ran == []
    assert state.consecutive_unusable_turns == 1
    assert not any(m.get('role') in ('assistant', 'tool') for m in state.messages), \
        'the blocked exchange must not be replayed on later requests'


def test_the_nudge_names_an_abnormal_finish_reason(agent):
    state = _run(agent, LLMResponse(finish_reason='content_filter'))
    assert 'finish_reason=content_filter' in state.messages[-1]['content']


def test_the_nudge_is_unchanged_for_a_normal_turn(agent):
    state = _run(agent, LLMResponse(content='thinking', finish_reason='stop'))
    assert state.messages[-1] == {'role': 'user', 'content': CONTINUE_NUDGE}


class _RefusingClient:
    """Every call comes back the way the CRM run's did after turn 11: blocked and empty."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        return LLMResponse(finish_reason='content_filter', usage={'total_tokens': 10})


class _RecordingPublisher:
    def __init__(self):
        self.events = []

    def emit(self, event_type, payload=None, **kwargs):
        self.events.append((str(event_type), payload or {}))
        return f'span-{len(self.events)}'


def test_a_run_of_refusals_stops_at_the_breaker_and_says_why(tmp_path, monkeypatch):
    """The CRM run end to end: it must stop after the breaker's streak, not on the turn ceiling, and
    its record must name the refusals."""
    agent = AppSciAgent(config=AgentConfig(api_key='k', base_url='https://example.invalid',
                                           workspace_dir=str(tmp_path)))
    agent.llm_client = _RefusingClient()
    publisher = _RecordingPublisher()
    agent._obs_publisher = publisher
    # The full model-I/O capture is not under test and writes to the trace store; keep it out.
    monkeypatch.setattr('appsci_agent.core.agent.model_call', lambda *a, **k: contextlib.nullcontext(None))

    result = agent.run(task_description='build it', max_turns=40)

    assert result['stop_reason'] == 'stuck_content_filter'
    assert result['turns_used'] == MAX_CONSECUTIVE_UNUSABLE_TURNS
    assert agent.llm_client.calls == MAX_CONSECUTIVE_UNUSABLE_TURNS
    end = [p for t, p in publisher.events if t == 'agent.run_end']
    assert len(end) == 1
    assert end[0]['stop_reason'] == 'stuck_content_filter'
    assert end[0]['content_filtered_turns'] == MAX_CONSECUTIVE_UNUSABLE_TURNS
