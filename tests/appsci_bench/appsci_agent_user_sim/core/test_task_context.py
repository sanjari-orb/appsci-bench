"""Tests for load_task_context -- the service's decoupled task loader (reads spec.yaml fields)."""
from __future__ import annotations

import pathlib

from appsci_bench.appsci_agent_user_sim.core.task_context import TaskContext, load_task_context

REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]


def test_loads_real_order_to_cash_spec() -> None:
    tc = load_task_context(REPO_ROOT / "tasks" / "order_to_cash" / "spec.yaml")
    assert isinstance(tc, TaskContext)
    assert tc.description.strip()                      # non-empty customer ask
    assert isinstance(tc.environment, dict)
    # order_to_cash declares structured resources (used by known_resources resolution).
    assert tc.environment.get("resources")


def test_missing_optional_fields_default_cleanly(tmp_path: pathlib.Path) -> None:
    spec = tmp_path / "spec.yaml"
    spec.write_text("description: just a description\n")  # only one field present
    tc = load_task_context(spec)
    assert tc.description == "just a description"
    assert tc.business_goals == []
    assert tc.dataset_files == []
    assert tc.technical_docs == []
    assert tc.interface_design == ""
    assert tc.environment == {}


def test_empty_spec_yields_all_defaults(tmp_path: pathlib.Path) -> None:
    spec = tmp_path / "spec.yaml"
    spec.write_text("")  # empty file -> yaml.safe_load returns None -> handled
    tc = load_task_context(spec)
    assert tc.description == ""
    assert tc.environment == {}


def test_satisfies_task_context_like_protocol() -> None:
    # The frozen TaskContext must structurally satisfy persona.TaskContextLike (runtime_checkable).
    from appsci_bench.appsci_agent_user_sim.core.persona import TaskContextLike
    tc = load_task_context(REPO_ROOT / "tasks" / "order_to_cash" / "spec.yaml")
    assert isinstance(tc, TaskContextLike)
