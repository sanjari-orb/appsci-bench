"""Standing up parallel eval stacks: what it refuses, what it names, and what it tears down.

Two things are being protected here. The first is that width 1 starts NOTHING -- every existing path
(the eval-only CLI's lock and orphan detection, teardown, the tracer) addresses the task environment
by its historic name, so a default run has to keep meaning what it meant. The second is that a width
this task cannot honestly serve is REFUSED before anything starts, because the alternative is a run
that scores every case against one store and reports a plausible number.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import appsci_bench.orchestrator.eval_lanes as el
from appsci_bench.eval.lanes import EvalLane
from appsci_bench.orchestrator.eval_lanes import LaneSetupError, eval_lane_pool
from appsci_bench.task_runner.config import TaskConfig


def _config(*, agent_env=None, lane_token_var="TOK"):
    """A TaskConfig with the shape a replicable task has: a templated address and a token var."""
    return TaskConfig(
        task_id="t",
        docker_compose=Path("tasks/t/environment/docker-compose.yml"),
        network_name="bench-network",
        workspace_name="t-workspace",
        environments={"build": None, "eval": None},
        agent_env=agent_env if agent_env is not None else [
            {"name": "T_TOOLS_URL", "value": "http://{workspace_container}:8080/tools"},
            {"name": "T_TOOLS_TOKEN", "value": "{tool_token}"},
        ],
        env_admin_token_var="T_ADMIN",
        lane_token_var=lane_token_var,
    )


_TASK_ENV = {"workspace_container": "t-workspace-1", "env_admin_token_var": "T_ADMIN",
             "phase": "eval"}


def _make_lane(client, env_dict, label):
    return EvalLane(client=client, setup_state=lambda s: None,
                    read_final_state=lambda s: {}, label=label)


def _make_lane_no_hooks(client, env_dict, label):
    return EvalLane(client=client, label=label)


# --- width 1 changes nothing ----------------------------------------------------------------------


def test_width_one_starts_nothing():
    """No TaskRunner, no deploy. The caller's environment and artifact are the whole pool."""
    with patch.object(el, "TaskRunner") as runner, \
         patch.object(el, "HttpArtifactClient", return_value=MagicMock()):
        with eval_lane_pool(
            task_config=None, tasks_dir=None, width=1,
            endpoint_url="http://localhost:1/respond", task_env=_TASK_ENV,
            artifact_dir=None, make_lane=_make_lane,
        ) as pool:
            assert pool.width == 1
            assert pool.max_workers == 1          # env hooks => one case at a time, as before
    runner.assert_not_called()


def test_width_one_without_env_hooks_leaves_concurrency_alone():
    with patch.object(el, "HttpArtifactClient", return_value=MagicMock()):
        with eval_lane_pool(
            task_config=None, tasks_dir=None, width=1,
            endpoint_url="http://localhost:1/respond", task_env=None,
            artifact_dir=None, make_lane=_make_lane_no_hooks,
        ) as pool:
            assert pool.max_workers is None


# --- refusals, all before anything is started -----------------------------------------------------


def _expect_refusal(match, **overrides):
    kwargs = dict(
        task_config=_config(), tasks_dir=Path("tasks"), width=3,
        endpoint_url="http://localhost:1/respond", task_env=_TASK_ENV,
        artifact_dir=Path("/artifact"), make_lane=_make_lane,
    )
    kwargs.update(overrides)
    with patch.object(el, "TaskRunner") as runner, \
         patch.object(el, "HttpArtifactClient", return_value=MagicMock()):
        with pytest.raises(LaneSetupError, match=match):
            with eval_lane_pool(**kwargs):
                pass
    runner.assert_not_called()


def test_refuses_a_task_whose_environment_address_is_literal():
    """The silent failure this exists to prevent: every copy's artifact would be handed copy 0's
    address and they would all transact on one store."""
    literal = _config(agent_env=[{"name": "T_TOOLS_URL", "value": "http://t-workspace-1:8080/tools"}])
    _expect_refusal("pins the environment address literally", task_config=literal)


def test_refuses_a_token_placeholder_with_nowhere_to_write_it():
    _expect_refusal("lane_token_var", task_config=_config(lane_token_var=None))


def test_refuses_a_run_with_no_task_environment():
    _expect_refusal("running without a task environment", task_env=None)


def test_refuses_a_run_with_no_artifact_source():
    _expect_refusal("needs the artifact source directory", artifact_dir=None)


def test_refuses_when_the_caller_supplied_no_spec():
    _expect_refusal("needs the task's spec", task_config=None)


# --- the happy path, and its teardown --------------------------------------------------------------


def _fake_runner(start_results):
    runner = MagicMock()
    runner.start.side_effect = start_results
    runner.teardown_descriptor.side_effect = (
        lambda task_id, lane=0: {"workspace_container": f"t-l{lane}-workspace-1", "lane": lane}
    )
    return runner


def _started(lane):
    return {"workspace_container": f"t-l{lane}-workspace-1", "reused_existing_container": False}


def test_extra_lanes_get_their_own_environment_artifact_and_token():
    runner = _fake_runner([_started(1), _started(2)])
    deploys: list[dict] = []

    def fake_deploy(cfg, artifact_dir, **kw):
        deploys.append(kw)
        return f"http://localhost:900{len(deploys)}/respond", kw["container_name"], None

    with patch.object(el, "TaskRunner", return_value=runner), \
         patch.object(el, "HttpArtifactClient", return_value=MagicMock()), \
         patch("appsci_bench.orchestrator.run.deploy_artifact_dir", fake_deploy), \
         patch.object(el.subprocess, "run") as docker:
        with eval_lane_pool(
            task_config=_config(), tasks_dir=Path("tasks"), width=3,
            endpoint_url="http://localhost:1/respond", task_env=_TASK_ENV,
            artifact_dir=Path("/artifact"), make_lane=_make_lane,
        ) as pool:
            assert pool.width == 3
            assert pool.max_workers == 3
            assert [lane.label for lane in pool.lanes] == ["lane-0", "lane-1", "lane-2"]

    # Each extra environment came up under its own compose project, with its own token.
    assert [c.kwargs["lane"] for c in runner.start.call_args_list] == [1, 2]
    tokens = [c.kwargs["env_overlay"]["TOK"] for c in runner.start.call_args_list]
    assert len(set(tokens)) == 2, "two copies shared a tool token"

    # Each extra artifact is its own container, pointed at its own environment, and built ONCE --
    # every copy runs the tag lane 0 already built rather than racing to rebuild it.
    assert [d["container_name"] for d in deploys] == ["appsci-artifact-t-l1", "appsci-artifact-t-l2"]
    assert [d["workspace_container"] for d in deploys] == ["t-l1-workspace-1", "t-l2-workspace-1"]
    assert all(d["build"] is False for d in deploys)
    assert {d["image_name"] for d in deploys} == {"appsci-artifact-t:latest"}

    # Everything this call created is gone, and only what it created: the two extra environments and
    # the two extra artifact containers. Lane 0's names appear nowhere in the teardown.
    assert runner.stop.call_count == 2
    removed = [c.args[0] for c in docker.call_args_list if c.args[0][:3] == ["docker", "rm", "-f"]]
    assert removed == [["docker", "rm", "-f", "appsci-artifact-t-l1"],
                       ["docker", "rm", "-f", "appsci-artifact-t-l2"]]


# --- orphans left by a run that did not shut down cleanly ------------------------------------------


def _docker_ps(names):
    """Fake `docker ps` returning `names`, and a no-op for every other docker call."""
    def run(argv, **kw):
        if argv[:3] == ["docker", "ps", "-a"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(names) + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def test_orphaned_lanes_are_reaped_before_anything_starts():
    """The sweep case. The queue worker SIGKILLs a cell that overruns its grace period, which runs
    none of the teardown, and the worker does no docker cleanup between cells -- so without this every
    hard-killed cell leaves its extra environments running for the life of the box."""
    runner = MagicMock()
    runner.teardown_descriptor.side_effect = lambda task_id, lane=0: {"lane": lane}
    with patch.object(el.subprocess, "run",
                      side_effect=_docker_ps(["t-l1-workspace-1", "t-l3-workspace-1"])) as docker:
        reaped = el.reap_orphan_lanes(runner, _config())

    assert reaped == [1, 3]
    assert [c.kwargs["lane"] for c in runner.teardown_descriptor.call_args_list] == [1, 3]
    assert runner.stop.call_count == 2
    # The artifact container is not part of the compose project, so it needs removing separately.
    removed = [c.args[0] for c in docker.call_args_list if c.args[0][:3] == ["docker", "rm", "-f"]]
    assert removed == [["docker", "rm", "-f", "appsci-artifact-t-l1"],
                       ["docker", "rm", "-f", "appsci-artifact-t-l3"]]


def test_reaping_enumerates_the_host_not_the_requested_width():
    """A run at width 2 must still clear what a previous width-8 run left behind, so leftovers cannot
    accumulate across a sweep whose arms use different widths."""
    runner = MagicMock()
    runner.teardown_descriptor.side_effect = lambda task_id, lane=0: {"lane": lane}
    with patch.object(el.subprocess, "run",
                      side_effect=_docker_ps(["t-l5-workspace-1", "t-l7-workspace-1"])):
        assert el.reap_orphan_lanes(runner, _config()) == [5, 7]


def test_reaping_ignores_containers_that_are_not_this_tasks_lanes():
    """Lane 0 especially: it is the environment the caller brought up and may be an operator's own."""
    runner = MagicMock()
    with patch.object(el.subprocess, "run", side_effect=_docker_ps(
            ["t-workspace-1", "other-l1-workspace-1", "t-l1-db-1", "appsci-artifact-t"])):
        assert el.reap_orphan_lanes(runner, _config()) == []
    runner.stop.assert_not_called()


def test_a_failure_to_reap_one_orphan_does_not_block_the_run():
    """Failing to remove someone else's leftover is not a reason to refuse to score."""
    runner = MagicMock()
    runner.teardown_descriptor.side_effect = lambda task_id, lane=0: {"lane": lane}
    runner.stop.side_effect = [RuntimeError("docker is busy"), None]
    with patch.object(el.subprocess, "run",
                      side_effect=_docker_ps(["t-l1-workspace-1", "t-l2-workspace-1"])):
        assert el.reap_orphan_lanes(runner, _config()) == [2]   # lane 1 failed, lane 2 still removed


def test_a_lane_that_adopts_an_existing_container_is_refused():
    """compose attaching to a container that was already there means that copy holds whatever the last
    run left in it -- a poisoned store, which would score as a bad artifact."""
    runner = _fake_runner([{"workspace_container": "t-l1-workspace-1",
                            "reused_existing_container": True}])
    with patch.object(el, "TaskRunner", return_value=runner), \
         patch.object(el, "HttpArtifactClient", return_value=MagicMock()):
        with pytest.raises(LaneSetupError, match="adopted an existing container"):
            with eval_lane_pool(
                task_config=_config(), tasks_dir=Path("tasks"), width=2,
                endpoint_url="http://localhost:1/respond", task_env=_TASK_ENV,
                artifact_dir=Path("/artifact"), make_lane=_make_lane,
            ):
                pass
    runner.stop.assert_called_once()     # the descriptor recorded before the start still tears down


def test_a_failed_deploy_tears_down_the_environment_it_had_already_started():
    runner = _fake_runner([_started(1)])

    def fake_deploy(cfg, artifact_dir, **kw):
        return None, kw["container_name"], "image not found"

    with patch.object(el, "TaskRunner", return_value=runner), \
         patch.object(el, "HttpArtifactClient", return_value=MagicMock()), \
         patch("appsci_bench.orchestrator.run.deploy_artifact_dir", fake_deploy):
        with pytest.raises(LaneSetupError, match="artifact deploy failed"):
            with eval_lane_pool(
                task_config=_config(), tasks_dir=Path("tasks"), width=2,
                endpoint_url="http://localhost:1/respond", task_env=_TASK_ENV,
                artifact_dir=Path("/artifact"), make_lane=_make_lane,
            ):
                pass
    runner.stop.assert_called_once()

