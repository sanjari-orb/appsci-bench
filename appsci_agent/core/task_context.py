"""Task context passed from the benchmark to the agent.

This defines what information the agent receives at the start of a run.
Notably, user_personas is NOT included — the agent should not know
which persona it's communicating with.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TaskContext:
    """What the benchmark hands the agent at the start of a run."""

    # Core task info
    description: str  # the customer ask
    business_goals: list[str] = field(default_factory=list)  # high-level objectives

    # Available resources (filtered by access control)
    environment: dict = field(default_factory=dict)  # Docker sandbox + resources
    dataset_files: list[dict] = field(default_factory=list)  # available data files
    technical_docs: list[dict] = field(default_factory=list)  # technical documentation

    # Artifact requirements
    interface_design: str | None = None  # expected interface description
    artifacts: dict = field(default_factory=dict)  # expected artifacts + output_dir

    # Constraints
    budgets: dict = field(default_factory=dict)  # turn/token/cost/time limits
