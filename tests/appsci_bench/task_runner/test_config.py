"""Spec-plumbing tests for TaskConfig: the artifact deploy contract, and the per-phase environments.

`task_runner.artifact_container` / `artifact_port` are what the host-side deployer uses to name and
publish the artifact container (deterministic teardown, section 5 of docs/artifact_design.md). These
tests prove they parse with sensible defaults so every existing docker-env task keeps working without
a spec edit, and that an explicit declaration is read through.

`task_runner.environments` declares the task environment once per RUN PHASE -- the build env the agent
explores and the eval env the artifact is scored against (docs/task_env_split.md). The tests below
pin the two failure modes that would silently defeat the split: a phase that reads through as "no
environment" when the author simply forgot to declare it, and a typo'd phase name doing the same.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from appsci_bench.core.spec import all_task_ids
from appsci_bench.task_runner.config import TaskConfig


def _write_spec(tasks_dir: Path, task_id: str, task_runner: dict) -> None:
    """Write a minimal but valid spec.yaml with the given task_runner block."""
    task_dir = tasks_dir / task_id
    task_dir.mkdir(parents=True)
    spec = {
        "task_id": task_id,
        "name": "t",
        "description": "d",
        "task_source_type": "synthetic",
        "environment": {
            "docker_compose": "env/docker-compose.yml",
        },
        "task_runner": task_runner,
    }
    (task_dir / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))


_PHASE = {
    "bundle": [{"src": "environment/build/fixture/", "dest": "/fixture/"}],
    "setup_script": "/scripts/setup.sh",
}
_BASE_RUNNER = {
    "network_name": "bench-network",
    "workspace_name": "widget-workspace",
    "environments": {"build": _PHASE, "eval": _PHASE},
}


def _tasks_with_task_runner() -> list[str]:
    """Only docker-env tasks carry a task_runner block; persona-template tasks don't."""
    out = []
    for t in all_task_ids():
        data = yaml.safe_load((Path("tasks") / t / "spec.yaml").read_text())
        if "task_runner" in data:
            out.append(t)
    return out


def test_defaults_when_not_declared(tmp_path):
    # A task_runner block WITHOUT the new fields -> deterministic default name + port 8080.
    _write_spec(tmp_path, "widget_task", dict(_BASE_RUNNER))
    config = TaskConfig.from_spec("widget_task", tmp_path)
    assert config.artifact_container == "appsci-artifact-widget_task"
    assert config.artifact_port == 8080


def test_explicit_declaration_is_read_through(tmp_path):
    _write_spec(
        tmp_path,
        "airline_rebooking",
        {**_BASE_RUNNER,
         "artifact_container": "appsci-artifact-airline_rebooking",
         "artifact_port": 9090},
    )
    config = TaskConfig.from_spec("airline_rebooking", tmp_path)
    assert config.artifact_container == "appsci-artifact-airline_rebooking"
    assert config.artifact_port == 9090


def test_empty_declared_name_falls_back_to_default(tmp_path):
    # An explicitly-empty artifact_container still defaults off the task id (guards a blank in YAML).
    _write_spec(tmp_path, "blank_task", {**_BASE_RUNNER, "artifact_container": ""})
    config = TaskConfig.from_spec("blank_task", tmp_path)
    assert config.artifact_container == "appsci-artifact-blank_task"


@pytest.mark.parametrize("task_id", _tasks_with_task_runner())
def test_every_docker_env_task_spec_still_parses(task_id):
    # The new optional fields must not break parsing of any real docker-env spec: each still yields a
    # usable (non-empty) container name and a positive port.
    config = TaskConfig.from_spec(task_id, Path("tasks"))
    assert config.artifact_container
    assert isinstance(config.artifact_port, int) and config.artifact_port > 0


# --- per-phase environments -------------------------------------------------

def test_both_phases_parse(tmp_path):
    _write_spec(tmp_path, "widget_task", dict(_BASE_RUNNER))
    config = TaskConfig.from_spec("widget_task", tmp_path)

    for phase in ("build", "eval"):
        env = config.phase(phase)
        assert env is not None
        assert env.bundle == [{"src": "environment/build/fixture/", "dest": "/fixture/"}]
        assert str(env.setup_script) == "/scripts/setup.sh"


def test_eval_none_means_no_environment(tmp_path):
    """A task whose eval phase genuinely needs none says so, and reads back as None."""
    _write_spec(tmp_path, "widget_task", {
        **_BASE_RUNNER, "environments": {"build": _PHASE, "eval": "none"},
    })
    config = TaskConfig.from_spec("widget_task", tmp_path)
    assert config.phase("build") is not None
    assert config.phase("eval") is None


def test_a_missing_phase_is_an_error_not_a_silent_none(tmp_path):
    """The failure this guards: an omitted eval block reading through as "needs no environment".

    That would skip the eval data load entirely and score the artifact against an empty store -- a
    whole run of zeros with nothing to explain them. An author who decided writes 'none'; an author
    who forgot gets an error.
    """
    _write_spec(tmp_path, "widget_task", {
        **_BASE_RUNNER, "environments": {"build": _PHASE},
    })
    with pytest.raises(KeyError, match="environments.eval is missing"):
        TaskConfig.from_spec("widget_task", tmp_path)


def test_a_missing_environments_block_names_the_migration(tmp_path):
    _write_spec(tmp_path, "widget_task", {
        "network_name": "bench-network", "workspace_name": "widget-workspace",
    })
    with pytest.raises(KeyError, match="task_env_split"):
        TaskConfig.from_spec("widget_task", tmp_path)


def test_a_phase_missing_its_setup_script_is_an_error(tmp_path):
    _write_spec(tmp_path, "widget_task", {
        **_BASE_RUNNER,
        "environments": {"build": {"bundle": []}, "eval": _PHASE},
    })
    with pytest.raises(KeyError, match="setup_script"):
        TaskConfig.from_spec("widget_task", tmp_path)


def test_an_unknown_phase_name_raises_rather_than_reading_as_no_environment(tmp_path):
    """A typo must not read through as "this phase has no environment" -- same silent-zeros failure."""
    _write_spec(tmp_path, "widget_task", dict(_BASE_RUNNER))
    config = TaskConfig.from_spec("widget_task", tmp_path)
    with pytest.raises(ValueError, match="unknown run phase"):
        config.phase("evaluation")


@pytest.mark.parametrize("task_id", _tasks_with_task_runner())
def test_every_docker_env_task_declares_both_phases(task_id):
    """Every real task must have been migrated: both phases declared, build always a real env."""
    config = TaskConfig.from_spec(task_id, Path("tasks"))
    assert config.phase("build") is not None, f"{task_id} has no build environment"
    assert "eval" in config.environments, f"{task_id} does not declare its eval phase"
