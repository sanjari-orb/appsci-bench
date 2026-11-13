"""The NO-USER arm inside the agent: tool registry, prompt surfaces, and config.

The invariant every test here defends is that the REGISTRY and the PROMPT agree. That pair has
already gone wrong once on this codebase (see `test_optimize_agent.py`'s notes on
`optimization_enabled`): an ablation advertised tools the registry had dropped, the agent called them,
got "Unknown tool", and adapted -- so the run measured a third thing that was neither arm, and nothing
said so. `talk_to_user` has the same two surfaces and the same failure mode, with the added wrinkle
that getting it backwards on a CONTROL run tells an agent with a working stakeholder that there is
nobody to ask.
"""
from __future__ import annotations

import pytest

from appsci_agent.applied_science_agent import AppliedScienceAgent
from appsci_agent.core.agent import AppSciAgent
from appsci_agent.core.config import AgentConfig
from appsci_agent.prompts.builder import build_system_prompt

REQUIREMENTS = "/requirements"


def _agent(**config_kwargs) -> AppSciAgent:
    return AppSciAgent(config=AgentConfig(**config_kwargs), system_prompt="stub")


def _no_user_config(**overrides) -> AgentConfig:
    kwargs = dict(user_sim_enabled=False, requirements_dir=REQUIREMENTS)
    kwargs.update(overrides)
    return AgentConfig(**kwargs)


# --- the registry -----------------------------------------------------------------------------

def test_the_stakeholder_is_there_by_default():
    assert "talk_to_user" in _agent().tools


def test_the_no_user_arm_removes_the_tool_from_the_registry():
    """Not just from the prompt. An unadvertised tool the model names anyway still EXECUTES, so a
    prompt-only ablation would leave a working line to a simulator the arm claims does not exist."""
    tools = AppSciAgent(config=_no_user_config(), system_prompt="stub").tools
    assert "talk_to_user" not in tools
    # Everything else is untouched -- this arm removes one route to information, not capability.
    assert {"mini_swe_agent", "deploy_artifact", "complete"} <= set(tools)


def test_tool_order_is_unchanged_on_a_control_run():
    """Insertion order is the order the schemas reach the model, so a reordering would change the
    control arm's request bytes for a change that is supposed to leave it alone."""
    assert list(_agent().tools) == [
        "talk_to_user", "mini_swe_agent", "deploy_artifact", "complete",
        "eval_generator", "optimize_agent",
    ]


def test_the_two_ablations_are_independent():
    tools = AppSciAgent(
        config=_no_user_config(optimization_enabled=False), system_prompt="stub"
    ).tools
    assert set(tools) == {"mini_swe_agent", "deploy_artifact", "complete"}


# --- the system prompt ------------------------------------------------------------------------

@pytest.mark.parametrize("optimization_enabled", [True, False])
def test_the_no_user_prompt_never_mentions_the_tool(optimization_enabled):
    prompt = build_system_prompt(
        task_brief="Build a thing.",
        optimization_enabled=optimization_enabled,
        user_sim_enabled=False,
        requirements_dir=REQUIREMENTS,
    )
    assert "talk_to_user" not in prompt


def test_the_no_user_prompt_names_the_mount():
    prompt = build_system_prompt(
        task_brief="Build a thing.", user_sim_enabled=False, requirements_dir=REQUIREMENTS,
    )
    assert REQUIREMENTS in prompt
    assert "provided to you in full" in prompt


def test_the_no_user_prompt_never_mentions_a_user_at_all():
    """Not "there is no user to ask" -- nothing about a user, absent or otherwise.

    This arm is not "the run where the stakeholder was taken away"; it is a run whose requirements
    arrive as files. An agent that has never been told a stakeholder was an option cannot spend a
    turn looking for one, waiting on one, or addressing one in prose, and telling it that a user
    would normally exist is information about the BENCHMARK rather than about its task. (Raised in
    review on PR #175.)
    """
    prompt = build_system_prompt(
        task_brief="Build a thing.", user_sim_enabled=False, requirements_dir=REQUIREMENTS,
        requirements_docs_dir=f"{REQUIREMENTS}/docs",
        requirements_policies_dir=f"{REQUIREMENTS}/policies",
    )
    lowered = prompt.lower()
    for word in ("stakeholder", "no user", "nobody", "talk_to_user", "ask the user"):
        assert word not in lowered, f"the no-user prompt mentions {word!r}"
    # "the user" DOES survive, twice, and correctly: `eval_generator` tells the agent to describe
    # "the user's persona/goal" for the eval it writes, which is about the ARTIFACT's own end users
    # in its own test harness -- a different person entirely from the build-time stakeholder this
    # arm removes. Pinned so a future tightening of the loop above does not delete real guidance.
    assert "the user's persona/goal" in lowered


def test_the_no_user_prompt_without_a_mount_names_no_directory():
    """A task with nothing to hand over is refused by the orchestrator, so this is a second line of
    defence -- but a prompt that renders a path-shaped blank ("your requirements are in ``") would
    send the agent looking for a directory that does not exist."""
    prompt = build_system_prompt(task_brief="x", user_sim_enabled=False, requirements_dir=None)
    prose = [line for line in prompt.splitlines() if not line.startswith(("```", "1.", "-"))]
    assert not any("``" in line for line in prose), prose
    assert "None" not in prompt
    assert "the files already mounted in your container" in prompt


@pytest.mark.parametrize("user_sim_enabled,optimization_enabled,expected", [
    (True, True, "six"), (True, False, "four"), (False, True, "five"), (False, False, "three"),
])
def test_the_prompt_counts_the_tools_it_actually_lists(
    user_sim_enabled, optimization_enabled, expected
):
    """Two independent flags make four tool surfaces. The headings count rather than spell a fixed
    number precisely so no combination can claim a tool count it does not have."""
    prompt = build_system_prompt(
        task_brief="x",
        user_sim_enabled=user_sim_enabled,
        optimization_enabled=optimization_enabled,
        requirements_dir=None if user_sim_enabled else REQUIREMENTS,
    )
    assert f"You have {expected} tools available:" in prompt
    headings = [line for line in prompt.splitlines() if line.startswith("## ")]
    numbers = [int(h.split(".")[0].removeprefix("## ")) for h in headings]
    assert numbers == list(range(1, len(numbers) + 1)), headings


@pytest.mark.parametrize("user_sim_enabled", [True, False])
@pytest.mark.parametrize("optimization_enabled", [True, False])
def test_the_prompt_never_advertises_a_tool_the_registry_lacks(
    user_sim_enabled, optimization_enabled
):
    """The regression this whole file exists for, checked across all four arms at once."""
    config = AgentConfig(
        user_sim_enabled=user_sim_enabled,
        optimization_enabled=optimization_enabled,
        requirements_dir=None if user_sim_enabled else REQUIREMENTS,
    )
    prompt = build_system_prompt(
        task_brief="x",
        user_sim_enabled=user_sim_enabled,
        optimization_enabled=optimization_enabled,
        requirements_dir=config.requirements_dir,
    )
    registry = set(AppSciAgent(config=config, system_prompt=prompt).tools)
    for name in ("talk_to_user", "mini_swe_agent", "deploy_artifact", "complete",
                 "eval_generator", "optimize_agent"):
        if name not in registry:
            assert name not in prompt, f"{name} advertised but not registered"


def test_the_production_path_passes_the_arm_through():
    """`AppliedScienceAgent.build` hands `AppSciAgent` a PRE-BUILT prompt, so the registry-derived
    default inside that class never applies on the path a real run takes. This is exactly how the
    `optimization_enabled` drift happened."""
    wrapper = AppliedScienceAgent(config=_no_user_config())
    captured = {}

    class _Recorder:
        def build_system_prompt(self, **kwargs):
            captured.update(kwargs)
            return "stub"

    class _StubAgent:
        def __init__(self, **kwargs):
            pass

        def run(self, **kwargs):
            return {"status": "completed", "turns_used": 1}

    import appsci_agent.applied_science_agent as mod

    original_builder, original_agent = mod.PromptBuilder, mod.AppSciAgent
    mod.PromptBuilder, mod.AppSciAgent = _Recorder, _StubAgent
    try:
        wrapper.build(_context())
    finally:
        mod.PromptBuilder, mod.AppSciAgent = original_builder, original_agent

    assert captured["user_sim_enabled"] is False
    assert captured["requirements_dir"] == REQUIREMENTS


def _context():
    from appsci_agent.core import TaskContext

    return TaskContext(description="Build a thing.")


# --- the task message -------------------------------------------------------------------------

@pytest.mark.parametrize("optimization_enabled", [True, False])
def test_the_no_user_task_message_points_at_the_mount(optimization_enabled):
    """The task message is a second prompt surface with its own copy of "ask the user", and it is
    rendered on every run -- an arm that fixed only the system prompt would still tell the agent to
    call a tool it does not have."""
    agent = AppSciAgent(config=AgentConfig(), system_prompt="stub")
    message = agent._format_task_message(
        "Build a thing.", environment=None,
        optimization_enabled=optimization_enabled,
        user_sim_enabled=False, requirements_dir=REQUIREMENTS,
    )
    assert "talk_to_user" not in message
    assert REQUIREMENTS in message
    assert "provided to you in full" in message
    # Same rule as the system prompt: this surface names no user either, absent or otherwise.
    lowered = message.lower()
    for word in ("stakeholder", "no user", "nobody", "ask the user"):
        assert word not in lowered, f"the no-user task message mentions {word!r}"


def test_the_control_task_message_still_asks_the_user():
    agent = AppSciAgent(config=AgentConfig(), system_prompt="stub")
    message = agent._format_task_message("Build a thing.", environment=None)
    assert "talk_to_user" in message


def test_the_no_user_task_message_without_a_mount_names_no_directory():
    agent = AppSciAgent(config=AgentConfig(), system_prompt="stub")
    message = agent._format_task_message(
        "Build a thing.", environment=None, user_sim_enabled=False, requirements_dir=None,
    )
    assert "``" not in message and "None" not in message


# --- config -----------------------------------------------------------------------------------

def test_a_stakeholder_alongside_the_documents_is_refused():
    with pytest.raises(ValueError, match="requirements_dir is set"):
        AgentConfig(requirements_dir=REQUIREMENTS)


@pytest.mark.parametrize("raw,expected", [
    ("0", False), ("false", False), ("no", False), ("off", False), ("FALSE", False),
    ("1", True), ("true", True), ("yes", True), ("on", True),
])
def test_the_env_var_reads_every_boolean_spelling(monkeypatch, raw, expected):
    monkeypatch.setenv("AGENT_USER_SIM_ENABLED", raw)
    if not expected:
        monkeypatch.setenv("APPSCI_REQUIREMENTS_DIR", REQUIREMENTS)
    assert AgentConfig.from_env().user_sim_enabled is expected


def test_an_unparseable_env_var_is_an_error_not_a_guess(monkeypatch):
    """`AGENT_USER_SIM_ENABLED=nope` read as truthy would run the control arm while the sweep record
    claimed the ablation, and the score would still look usable."""
    monkeypatch.setenv("AGENT_USER_SIM_ENABLED", "nope")
    with pytest.raises(ValueError, match="AGENT_USER_SIM_ENABLED"):
        AgentConfig.from_env()


def test_an_unset_env_var_leaves_the_control_arm(monkeypatch):
    monkeypatch.delenv("AGENT_USER_SIM_ENABLED", raising=False)
    monkeypatch.delenv("APPSCI_REQUIREMENTS_DIR", raising=False)
    assert AgentConfig.from_env().user_sim_enabled is True


def test_the_mount_path_comes_from_the_environment(monkeypatch):
    """Carried as a value rather than hardcoded in the agent, so the path the prompt names is the
    path the orchestrator actually bound."""
    monkeypatch.setenv("AGENT_USER_SIM_ENABLED", "0")
    monkeypatch.setenv("APPSCI_REQUIREMENTS_DIR", "/somewhere/else")
    assert AgentConfig.from_env().requirements_dir == "/somewhere/else"


# --- the container entrypoint -------------------------------------------------------------------
#
# The tests above all build an `AppliedScienceAgent` directly, which is the layer BELOW the thing
# docker actually runs. `appsci_agent/run.py:main()` is the entrypoint, and it used to require the
# stakeholder's address unconditionally -- so in this arm the container exited 1 before the agent was
# ever constructed and the orchestrator reported the run `crashed`. Every test above passed the whole
# time, because none of them ran `main()`.

CONTAINER_SPEC = """
task_id: demo
description: a demo task
business_goals: ["ship it"]
environment:
  resources: []
artifacts:
  output_dir: /work/out
"""


class _AgentReached(BaseException):
    """Raised in place of constructing the real agent, to prove `main()` got that far.

    `BaseException`, not `Exception`, on purpose: `main()` wraps the agent construction in a broad
    `except Exception` that logs and returns 1, so an ordinary sentinel would be swallowed and every
    test here would read "returned 1" whether it reached the agent or died at the env checks -- which
    is precisely the distinction these tests exist to make.
    """


def _entrypoint_env(monkeypatch, tmp_path, *, arm: str) -> None:
    """The environment the container manager builds, for one arm, and nothing left over.

    The `delenv`s matter: an operator's own `APPSCI_AGENT_*` and `AGENT_*` leak into a local pytest
    run, and one stray `AGENT_USER_SIM_ENABLED` would silently flip which arm is under test.
    """
    spec = tmp_path / "spec.yaml"
    spec.write_text(CONTAINER_SPEC)
    for name in ("APPSCI_USERSIM_URL", "APPSCI_USERSIM_TOKEN", "AGENT_USER_SIM_ENABLED",
                 "APPSCI_REQUIREMENTS_DIR", "AGENT_MODEL", "AGENT_MAX_TURNS",
                 "AGENT_OPTIMIZE_CYCLES", "AGENT_OPTIMIZATION_ENABLED", "AGENT_TEMPERATURE",
                 "ORCHESTRATOR_SUBMIT_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TASK_WORKSPACE", str(tmp_path / "work"))
    monkeypatch.setenv("TASK_SPEC_PATH", str(spec))
    if arm == "no_user":
        # Exactly what `agent_container.py` injects for this arm: the flag and the mount, and
        # deliberately NEITHER user-sim variable.
        monkeypatch.setenv("AGENT_USER_SIM_ENABLED", "0")
        monkeypatch.setenv("APPSCI_REQUIREMENTS_DIR", REQUIREMENTS)
    else:
        monkeypatch.setenv("APPSCI_USERSIM_URL", "http://usersim:8300")
        monkeypatch.setenv("APPSCI_USERSIM_TOKEN", "t0ken")


def _stop_at_agent(monkeypatch):
    import appsci_agent.run as run_mod

    def _boom(*a, **kw):
        raise _AgentReached()

    monkeypatch.setattr(run_mod, "AppliedScienceAgent", _boom)


def test_the_container_starts_in_the_no_user_arm_without_a_stakeholder_address(monkeypatch, tmp_path):
    """The blocking one. Requiring APPSCI_USERSIM_URL/TOKEN killed the container at line 1 of the
    run: this arm never injects them, because pointing `TalkToUserTool` at an empty address would be
    worse than not setting them. `main()` must reach the agent anyway."""
    import appsci_agent.run as run_mod

    _entrypoint_env(monkeypatch, tmp_path, arm="no_user")
    _stop_at_agent(monkeypatch)

    # Not SystemExit: reaching the agent at all is the whole assertion.
    with pytest.raises(_AgentReached):
        run_mod.main()


def test_the_control_arm_still_refuses_to_start_without_one(monkeypatch, tmp_path):
    """Kept strict on purpose. A control run that reached the agent with no stakeholder address
    would fail later, one turn at a time, looking like an agent that could not elicit anything."""
    import appsci_agent.run as run_mod

    _entrypoint_env(monkeypatch, tmp_path, arm="control")
    monkeypatch.delenv("APPSCI_USERSIM_URL")
    _stop_at_agent(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        run_mod.main()
    assert exc.value.code == 1


def test_the_control_arm_still_refuses_without_a_token(monkeypatch, tmp_path):
    import appsci_agent.run as run_mod

    _entrypoint_env(monkeypatch, tmp_path, arm="control")
    monkeypatch.delenv("APPSCI_USERSIM_TOKEN")
    _stop_at_agent(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        run_mod.main()
    assert exc.value.code == 1


def test_the_control_arm_reaches_the_agent_when_both_are_present(monkeypatch, tmp_path):
    import appsci_agent.run as run_mod

    _entrypoint_env(monkeypatch, tmp_path, arm="control")
    _stop_at_agent(monkeypatch)

    with pytest.raises(_AgentReached):
        run_mod.main()


def test_the_entrypoint_hands_the_agent_the_arm_it_was_given(monkeypatch, tmp_path):
    """Not just "it started" -- it has to start in the RIGHT arm. `main()` builds the config before
    the environment checks now, so this pins that the reordering did not change what it builds."""
    import appsci_agent.run as run_mod

    seen = {}

    def _capture(config):
        seen["config"] = config
        raise _AgentReached()

    _entrypoint_env(monkeypatch, tmp_path, arm="no_user")
    monkeypatch.setattr(run_mod, "AppliedScienceAgent", lambda config: _capture(config))

    with pytest.raises(_AgentReached):
        run_mod.main()
    assert seen["config"].user_sim_enabled is False
    assert seen["config"].requirements_dir == REQUIREMENTS


# --- the two halves of the mount ----------------------------------------------------------------

_DOCS = f"{REQUIREMENTS}/docs"
_POLICIES = f"{REQUIREMENTS}/policies"


def test_the_prompt_names_both_halves_and_says_what_each_holds():
    """The agent has to tell an interface contract from a decision rule. Naming only the parent puts
    two kinds of material in one undifferentiated pile and invites the failure the policy mount
    exists to fix: reading the output contract, believing that is the requirement set, and inventing
    the thresholds."""
    prompt = build_system_prompt(
        task_brief="Build a thing.", user_sim_enabled=False, requirements_dir=REQUIREMENTS,
        requirements_docs_dir=_DOCS, requirements_policies_dir=_POLICIES,
    )
    assert _DOCS in prompt and _POLICIES in prompt
    # Each half is described in terms of the question it answers, not just listed. Matched against
    # the unwrapped text: these sentences are hard-wrapped in the template and a line-based assertion
    # would break on a reflow that changed nothing.
    flat = " ".join(prompt.split())
    assert f"`{_DOCS}`** — the interface" in flat
    assert "what a correct reply looks like" in flat
    assert f"`{_POLICIES}`** — the operating policy" in flat
    assert "decision rules, thresholds and materiality lines" in flat
    assert "Neither half is sufficient alone" in flat


@pytest.mark.parametrize(
    "docs_dir, policies_dir, named, unnamed",
    [
        (_DOCS, None, _DOCS, _POLICIES),
        (None, _POLICIES, _POLICIES, _DOCS),
    ],
)
def test_the_prompt_names_only_the_half_that_was_mounted(docs_dir, policies_dir, named, unnamed):
    """A task can have documents and no extracts (order_to_cash today) or extracts and no documents.
    Naming a directory that was not bound sends the agent to an empty `ls` -- and an agent that finds
    nothing where the rules were promised may conclude there are no rules."""
    prompt = build_system_prompt(
        task_brief="x", user_sim_enabled=False, requirements_dir=REQUIREMENTS,
        requirements_docs_dir=docs_dir, requirements_policies_dir=policies_dir,
    )
    assert named in prompt
    assert unnamed not in prompt
    # With one half there is no "both" claim to make.
    assert "Neither half is sufficient alone" not in prompt


def test_the_control_prompt_names_neither_half():
    """Same rule as the parent mount: on a run with a stakeholder these paths do not exist, and a
    prompt that mentions them would describe a container the agent is not in."""
    prompt = build_system_prompt(
        task_brief="x", user_sim_enabled=True,
    )
    assert _DOCS not in prompt and _POLICIES not in prompt


def test_the_task_message_points_at_the_policies_when_there_are_some():
    """The task message is short and read last; the clause that earns its space is the one naming
    where the decision rules are, because that is the half an agent skips."""
    from appsci_agent.core.agent import _requirements_instruction

    with_policies = _requirements_instruction(False, REQUIREMENTS, _POLICIES)
    without = _requirements_instruction(False, REQUIREMENTS, None)
    assert _POLICIES in with_policies
    assert "decision rules" in with_policies
    assert _POLICIES not in without
    assert "None" not in without
