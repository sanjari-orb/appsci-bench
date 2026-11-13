"""Audit every task's public spec and the separate runtime inputs to a harness.

Spec-only tasks are included: generating their public context does not imply their
Docker environments have been implemented. No Docker, provider, or user-sim calls.
"""
from __future__ import annotations

import copy
import json
from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from appsci_bench.core.harness_inputs import write_harness_inputs
from appsci_bench.core.spec import (
    SCHEMA_PATH, TASKS_DIR, _AGENT_SPEC_TOP_LEVEL_KEYS, agent_spec_leaks,
    all_task_ids, build_agent_spec, resolve_spec_path, write_agent_spec,
)
from appsci_bench.eval.contract import session_api_contract
from appsci_bench.orchestrator.deployer import SERVER_ENTRY_CANDIDATES
from appsci_bench.task_runner.config import TaskConfig


def bundle_for(tmp_path: Path, spec_path: Path, **overrides):
    args = dict(
        spec_path=str(spec_path), artifact_output_dir="/work/out", artifact_port=8080,
        artifact_server_entries=SERVER_ENTRY_CANDIDATES, artifact_env_names=["OPENAI_API_KEY"],
        workspace_container="actual-build-workspace", task_env_names=[],
        model_env_names=["OPENAI_API_KEY"], model=None, build_timeout=1234,
        max_turns=None, temperature=None, optimize_cycles=None, optimization_enabled=True,
        has_data=False, has_sop=False,
    )
    args.update(overrides)
    bundle = write_harness_inputs(tmp_path / "harness", **args)
    return json.loads((bundle / "context.json").read_text()), (bundle / "prompt.md").read_text()


def test_schema_evolution_requires_an_explicit_visibility_decision():
    """A new task field must be consciously classified, not silently lost or exported."""
    schema = json.loads(SCHEMA_PATH.read_text())
    private = {"task_source_type", "source", "user_personas", "evaluation_data",
               "task_runner", "gold_solution", "owner", "co_owners"}
    public = set(_AGENT_SPEC_TOP_LEVEL_KEYS) | {"environment"}
    assert not public & private
    assert set(schema["properties"]) == public | private
    assert set(schema["properties"]["environment"]["properties"]) == {
        "resources", "access_control", "docker_compose",
    }
    assert set(schema["properties"]["environment"]["properties"]["resources"]["items"]["properties"]) == {
        "name", "type", "description", "location",
    }


@pytest.mark.parametrize("task_id", all_task_ids())
@pytest.mark.parametrize("redacted_first", [False, True], ids=["raw-spec", "mounted-spec"])
def test_all_tasks_retain_every_public_field_and_resolved_input(tmp_path, task_id, redacted_first):
    path = resolve_spec_path(task_id)
    raw = yaml.safe_load(path.read_text())
    runner = TaskConfig.from_spec(task_id, TASKS_DIR) if "task_runner" in raw else None
    task_env = (runner.resolved_agent_env(workspace_container="actual-build-workspace",
                                        tool_token="private-build-tool-token") if runner else {})
    if redacted_first:
        path = write_agent_spec(path, tmp_path / "mounted-spec.yaml")
    output_dir = (raw.get("artifacts") or {}).get("output_dir") or "/work/out"
    has_data = (TASKS_DIR / task_id / "environment/data").is_dir()
    has_sop = (TASKS_DIR / task_id / "environment/sop").is_dir()
    context, prompt = bundle_for(
        tmp_path, path, artifact_output_dir=output_dir,
        artifact_port=runner.artifact_port if runner else 8080,
        task_env_names=list(task_env), artifact_env_names=[*task_env, "OPENAI_API_KEY"],
        has_data=has_data, has_sop=has_sop,
    )
    assert context["task"] == build_agent_spec(raw)
    denies = set(raw["environment"].get("access_control", {}).get("denies", []))
    # Independent of the projection helper: catch it accidentally dropping a public field.
    for name in _AGENT_SPEC_TOP_LEVEL_KEYS:
        if name in raw:
            expected = raw[name]
            if name in {"dataset_files", "technical_docs"}:
                expected = [entry for entry in expected if entry["name"] not in denies]
            assert context["task"][name] == expected
    assert context["task"]["environment"]["resources"] == [
        {key: value for key, value in resource.items() if key != "location"}
        for resource in raw["environment"].get("resources", [])
        if resource["name"] not in denies and resource["type"] not in denies
    ]
    assert context["paths"] == {
        "task_spec": "/spec/spec.yaml", "workspace": "/work", "artifact_output": output_dir,
        "data": "/data" if has_data else None, "sop": "/sop" if has_sop else None,
    }
    assert context["environment"]["workspace_container"] == "actual-build-workspace"
    assert context["environment"]["task_env_vars"] == sorted(task_env)
    assert context["environment"]["artifact_env_vars"] == sorted({*task_env, "OPENAI_API_KEY", "PORT"})
    assert context["artifact"]["port"] == (runner.artifact_port if runner else 8080)
    assert context["artifact"]["server_entry_candidates"] == list(SERVER_ENTRY_CANDIDATES)
    assert context["artifact"]["session_api"] == session_api_contract()
    build = (raw.get("budgets") or {}).get("build") or {}
    assert context["build_controls"]["max_turns"] == build.get("turns")
    assert context["build_controls"]["max_tokens"] == build.get("tokens")
    assert context["build_controls"]["max_usd"] == build.get("usd")
    assert context["build_controls"]["task_wall_clock_s"] == build.get("wall_clock_s")
    assert context["build_controls"]["wall_clock_s"] == 1234
    assert agent_spec_leaks(context["task"]) == []
    assert "private-build-tool-token" not in json.dumps(context) + prompt
    assert "task services run in a separate workspace container" in prompt.lower()


@pytest.mark.parametrize("has_data,has_sop", [(False, False), (True, False), (False, True), (True, True)])
def test_runtime_mounts_overrides_and_environment_scopes(tmp_path, monkeypatch, has_data, has_sop):
    monkeypatch.setenv("OPENAI_API_KEY", "private-model-credential")
    monkeypatch.setenv("CUSTOM_HARNESS_TOKEN", "private-harness-credential")
    monkeypatch.setenv("EVAL_ADMIN_TOKEN", "private-evaluator-credential")
    context, prompt = bundle_for(
        tmp_path, resolve_spec_path("order_to_cash"), artifact_output_dir="/deliverable",
        artifact_port=9090, has_data=has_data, has_sop=has_sop,
        model="chosen-model", max_turns=0, temperature=0, optimize_cycles=0,
        task_env_names=["CUSTOM_API_URL", "CUSTOM_API_TOKEN"],
        harness_env_names=("CUSTOM_HARNESS_TOKEN",),
        artifact_env_names=["CUSTOM_API_URL", "CUSTOM_API_TOKEN", "OPENAI_API_KEY"],
    )
    assert context["paths"]["artifact_output"] == "/deliverable"
    assert context["paths"]["data"] == ("/data" if has_data else None)
    assert context["paths"]["sop"] == ("/sop" if has_sop else None)
    assert context["build_controls"]["model"] == "chosen-model"
    assert context["build_controls"]["max_turns"] == 0
    assert context["build_controls"]["temperature"] == 0
    assert context["build_controls"]["optimize_cycles"] == 0
    assert context["artifact"]["required_files"] == ["Dockerfile"]
    assert context["artifact"]["bind_host"] == "0.0.0.0"
    assert context["artifact"]["port_env"] == "PORT"
    assert context["environment"]["harness_env_vars"] == ["CUSTOM_HARNESS_TOKEN"]
    assert context["environment"]["artifact_env_vars"] == [
        "CUSTOM_API_TOKEN", "CUSTOM_API_URL", "OPENAI_API_KEY", "PORT",
    ]
    assert "set to 9090 when deployed" in prompt
    for entry in SERVER_ENTRY_CANDIDATES:
        assert entry in prompt
    for forbidden in ("private-model-credential", "private-harness-credential",
                      "private-evaluator-credential", "EVAL_ADMIN_TOKEN"):
        assert forbidden not in json.dumps(context) + prompt


def test_complete_task_shape_matches_reference_context_and_enforces_denies(tmp_path):
    from appsci_agent.core.task_context import TaskContext
    from appsci_agent.run import load_task_context_from_spec

    raw = {
        "task_id": "example", "name": "Example", "description": "Build a service.",
        "business_goals": ["Answer questions", "Produce a report"],
        "dataset_files": [{"name": "samples", "description": "Public sample records"},
                          {"name": "restricted", "description": "DENIED_DATA_SENTINEL"}],
        "technical_docs": [{"name": "api", "description": "Public API documentation"},
                           {"name": "restricted", "description": "DENIED_DOC_SENTINEL"}],
        "interface_design": "Accept text, structured input, and attachments.",
        "artifacts": {"output_dir": "/deliverable", "expected": [
            {"id": "agent", "kind": "agent", "primary": True},
            {"id": "report", "kind": "output", "path": "report.pdf", "required": True},
            {"id": "notes", "kind": "documentation", "required": False},
        ]},
        "budgets": {"build": {"turns": 7, "tokens": 123, "usd": 0, "wall_clock_s": 900},
                    "latency_slo_ms": 2000},
        "environment": {"docker_compose": "private-compose.yml",
                        "access_control": {"denies": ["restricted", "live_traces"]},
                        "resources": [
                            {"name": "public", "type": "api", "location": "private-host-path",
                             "description": "Public service"},
                            {"name": "restricted", "type": "database", "description": "DENIED_DB_SENTINEL"},
                            {"name": "traces", "type": "live_traces", "description": "DENIED_TRACE_SENTINEL"},
                        ]},
        "task_runner": {"env_admin_token_var": "PRIVATE_ADMIN_SENTINEL"},
        "evaluation_data": {"dataset": "PRIVATE_EVAL_SENTINEL"},
        "gold_solution": {"path": "PRIVATE_GOLD_SENTINEL"},
        "user_personas": ["PRIVATE_PERSONA_SENTINEL"],
        "source": {"notes": "PRIVATE_SOURCE_SENTINEL"},
        "future_private_field": "PRIVATE_FUTURE_SENTINEL",
    }
    before = copy.deepcopy(raw)
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(raw))
    # Even the mounted spec must apply the deny filter; otherwise it bypasses context.json.
    mounted = write_agent_spec(path, tmp_path / "public-spec.yaml")
    context, prompt = bundle_for(tmp_path, mounted, artifact_output_dir="/deliverable")
    reference = load_task_context_from_spec(str(path))
    for descriptor in fields(TaskContext):
        if descriptor.name != "environment":
            assert context["task"][descriptor.name] == getattr(reference, descriptor.name)
    assert context["task"]["environment"]["resources"] == [
        {key: value for key, value in r.items() if key != "location"}
        for r in reference.environment["resources"]
    ]
    assert context["task"] == yaml.safe_load(mounted.read_text()) == build_agent_spec(raw)
    assert raw == before
    public_text = json.dumps(context) + prompt + mounted.read_text()
    for forbidden in ("DENIED_", "PRIVATE_", "private-compose.yml", "private-host-path"):
        assert forbidden not in public_text
