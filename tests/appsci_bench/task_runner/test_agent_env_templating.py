"""`agent_env` placeholders, and the refusal that keeps a mis-templated task from running wide.

A task environment can be copied so eval cases stop queueing behind one store. Two facts differ
between the copies -- the container the tools are on, and the token that opens them -- so a spec that
spells either out literally hands every copy's artifact copy 0's address. Nothing errors when that
happens: the cases run, they all transact on one store, and the score is wrong and plausible. Hence a
refusal rather than a warning.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from appsci_bench.core.spec import load_task_spec
from appsci_bench.task_runner.config import TaskConfig

TASKS = Path("tasks")


def _config(agent_env, *, lane_token_var=None):
    return TaskConfig(
        task_id="t",
        docker_compose=Path("tasks/t/environment/docker-compose.yml"),
        network_name="bench-network",
        workspace_name="t-workspace",
        environments={"build": None, "eval": None},
        agent_env=agent_env,
        lane_token_var=lane_token_var,
    )


def test_placeholders_are_filled_in_per_environment():
    cfg = _config([
        {"name": "URL", "value": "http://{workspace_container}:8080/tools"},
        {"name": "TOKEN", "value": "{tool_token}"},
        {"name": "PLAIN", "value": "unchanged"},
    ])
    assert cfg.resolved_agent_env(workspace_container="t-l2-workspace-1", tool_token="tok-2") == {
        "URL": "http://t-l2-workspace-1:8080/tools",
        "TOKEN": "tok-2",
        "PLAIN": "unchanged",
    }


def test_an_unknown_placeholder_is_refused_not_passed_through():
    """An unsubstituted `{...}` reaches the artifact as a literal and surfaces much later as an
    unexplained connection error, so it fails here instead."""
    cfg = _config([{"name": "URL", "value": "http://{workspace}:8080"}])
    with pytest.raises(KeyError, match="unknown placeholder"):
        cfg.resolved_agent_env(workspace_container="t-workspace-1")


def test_a_token_placeholder_with_no_token_is_refused():
    cfg = _config([{"name": "TOKEN", "value": "{tool_token}"}])
    with pytest.raises(ValueError, match="tool_token"):
        cfg.resolved_agent_env(workspace_container="t-workspace-1")


def test_the_default_workspace_container_matches_what_the_runner_derives():
    assert _config([]).default_workspace_container == "t-workspace-1"


# --- the refusal ----------------------------------------------------------------------------------


def test_a_literal_environment_address_blocks_running_several_copies():
    cfg = _config([{"name": "URL", "value": "http://t-workspace-1:8080/tools"}])
    blockers = cfg.parallel_env_blockers()
    assert len(blockers) == 1
    assert "{workspace_container}" in blockers[0]


def test_a_token_placeholder_needs_somewhere_to_write_the_token():
    cfg = _config([
        {"name": "URL", "value": "http://{workspace_container}:8080/tools"},
        {"name": "TOKEN", "value": "{tool_token}"},
    ])
    assert any("lane_token_var" in b for b in cfg.parallel_env_blockers())
    assert _config(cfg.agent_env, lane_token_var="TOK").parallel_env_blockers() == []


def test_a_task_with_no_agent_env_is_not_blocked():
    """Nothing to mis-template. Such a task is refused for a different reason (no environment at all),
    which is the lane pool's business, not this check's."""
    assert _config([]).parallel_env_blockers() == []


# --- the real specs -------------------------------------------------------------------------------


@pytest.mark.parametrize("task_id", ["airline_rebooking", "banking_support"])
def test_the_per_sample_reset_tasks_can_actually_run_several_copies(task_id):
    """These two are the whole point of the feature: they reset their environment per case, so with
    one environment they run strictly one case at a time. A regression that re-literalises either
    spec would leave the flag accepted and silently inert."""
    spec = load_task_spec(TASKS / task_id / "spec.yaml")
    assert spec.evaluation_data.get("reset_env_per_sample") is True
    cfg = TaskConfig.from_spec(task_id, TASKS)
    assert cfg.parallel_env_blockers() == []
    resolved = cfg.resolved_agent_env(workspace_container="x-l1-workspace-1", tool_token="tok")
    assert any("x-l1-workspace-1" in v for v in resolved.values())
    assert "tok" in resolved.values()
