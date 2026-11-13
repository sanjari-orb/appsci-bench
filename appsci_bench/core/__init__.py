"""Core data: task spec.

Eval-side data types (eval samples, objectives, result/report types) live under appsci_bench/eval/,
not here. The build/handoff artifact contract (Artifact / ArtifactQuery / ArtifactResponse /
HttpArtifact) lives in appsci_bench/eval/artifact.py -- it is harness-side (the evaluator queries the
deployed artifact over HTTP); the agent no longer depends on it.

NOTE: persona rendering is NOT re-exported here. It lives in appsci_bench.appsci_agent_user_sim.core.persona;
import it from there directly. Re-exporting it here would make importing `appsci_bench.core` (e.g. core.spec,
used by the airline task-env) eagerly pull in the user-sim / litellm stack.
"""
from .spec import TaskSpec, load_task_spec, validate_spec

__all__ = [
    "TaskSpec", "load_task_spec", "validate_spec",
]
