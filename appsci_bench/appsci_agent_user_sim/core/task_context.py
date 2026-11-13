"""A standalone task view for the appsci-agent-user-sim service.

The simulator needs a few plain fields from the task spec to render the persona (see
``persona.TaskContextLike``). The benchmark's ``TaskSpec`` provides them, but the service must not
import ``appsci_bench`` -- so this loads those fields straight from the task ``spec.yaml`` into a
lightweight ``TaskContext`` that satisfies ``TaskContextLike`` structurally.

Note: this intentionally does NOT validate the spec against the JSON schema (that's the benchmark's
job, done before a run). It only reads the persona-rendering fields.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

import yaml


@dataclass(frozen=True)
class TaskContext:
    """The persona-rendering slice of a task (satisfies persona.TaskContextLike)."""
    description: str = ""
    business_goals: list[str] = field(default_factory=list)
    dataset_files: list[dict] = field(default_factory=list)
    technical_docs: list[dict] = field(default_factory=list)
    interface_design: str = ""
    environment: dict = field(default_factory=dict)


def load_task_context(spec_path: str | pathlib.Path) -> TaskContext:
    """Read the persona-rendering fields from a task spec.yaml into a TaskContext (no schema check)."""
    data = yaml.safe_load(pathlib.Path(spec_path).read_text()) or {}
    return TaskContext(
        description=data.get("description", ""),
        business_goals=list(data.get("business_goals", [])),
        dataset_files=list(data.get("dataset_files", [])),
        technical_docs=list(data.get("technical_docs", [])),
        interface_design=data.get("interface_design", ""),
        environment=dict(data.get("environment", {})),
    )
