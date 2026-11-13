"""The coding sub-agent is told its step budget, in the prompt mini-swe actually renders.

mini-swe ends a sub-task with `LimitsExceeded` once `step_limit` model calls have been made, and a
limit exit discards the trajectory: the caller gets no submission and learns nothing from the work.
Nothing in the prompt said the budget existed, so a sub-agent had no reason to prefer submitting over
one more round of exploring.

These tests render the shipped config the way mini-swe does -- Jinja with `StrictUndefined`, against
the variables `DefaultAgent.get_template_vars()` actually supplies. That strictness is the reason to
test it at all: a template naming a variable mini-swe does not provide raises on EVERY step, so a typo
here would not degrade the sub-agent, it would stop it dead.
"""
from __future__ import annotations

from jinja2 import StrictUndefined, Template
from minisweagent.agents.default import DefaultAgent

from appsci_agent.tools.mini_swe_agent import load_appsci_config


class _StubModel:
    def get_template_vars(self) -> dict:
        return {"model_name": "stub"}

    def format_message(self, **kwargs) -> dict:
        return kwargs


class _StubEnv:
    def get_template_vars(self) -> dict:
        return {"system": "Linux", "release": "6.0", "machine": "arm64", "cwd": "/work"}


def _vars(step_limit: int = 50) -> dict:
    """The variable set mini-swe renders its templates against, for a given effective step limit.

    Built through `DefaultAgent` rather than assembled by hand, so a change to what mini-swe supplies
    shows up here instead of being mirrored by a fixture that agrees with an old version of it.
    """
    config = dict(load_appsci_config()["agent"])
    # What `mini_swe_agent.py` does before constructing the agent: the effective limit comes from
    # `AgentConfig.mini_swe_max_turns`, so the yaml's own value is never what a run uses.
    config["step_limit"] = step_limit
    return DefaultAgent(_StubModel(), _StubEnv(), **config).get_template_vars()


def _render(template: str, **extra) -> str:
    return Template(template, undefined=StrictUndefined).render(**_vars(), **extra)


def test_the_system_prompt_states_the_step_budget():
    rendered = _render(load_appsci_config()["agent"]["system_template"])
    assert "at most 50 steps" in rendered
    # The consequence is the part that should change behaviour: finishing early beats one more look
    # around, because a limit exit returns nothing at all. (Matched on one line of the block -- the
    # surrounding sentences wrap, and a substring spanning a wrap would never match.)
    assert "Running out is a total loss," in rendered


def test_the_budget_shown_is_the_one_the_run_will_enforce():
    """A hard-coded number in the prompt would go stale the moment the limit moved."""
    assert "at most 12 steps" in Template(
        load_appsci_config()["agent"]["system_template"], undefined=StrictUndefined,
    ).render(**_vars(step_limit=12))


def test_every_shipped_template_renders_under_strict_undefined():
    """The whole config, not just the part this change touched: one undefined name breaks every step."""
    config = load_appsci_config()
    _render(config["agent"]["system_template"])
    _render(config["agent"]["instance_template"], task="explore the environment")

    class _Output(dict):
        """Mini-swe passes an object the template reads attributes off."""

        def __getattr__(self, name):
            try:
                return self[name]
            except KeyError as e:
                raise AttributeError(name) from e

    observation = config["model"]["observation_template"]
    # Both branches: the short-output path and the elision path, which reads different fields.
    _render(observation, output=_Output(returncode=0, output="ok", exception_info=None))
    _render(observation, output=_Output(returncode=1, output="x" * 20_000, exception_info=None))
    _render(config["model"]["format_error_template"], error="boom", finish_reason="length")
