"""Deterministic gold reference artifact for the airline task (design-faithful).

The `gold_solution` and the creds-free CI test subject. It implements the artifact contract
(`respond(query) -> ArtifactResponse`) and ACTS on the shared task-environment by calling the airline
**tool-API over HTTP** (AIRLINE_TOOLS_URL/AIRLINE_TOOLS_TOKEN, set by the runner) -- exactly the path
a real artifact uses. Being the gold solution, it is allowed to know each case's golden actions; it
replays them through the tools and states the required facts. Run through the eval loop it should
score ~1.0 (task_success), proving the task-env + tool-API + state-hash + oracle wiring end-to-end.

Used as `--applied-scientist gold/airline_rebooking/reference_artifact.py`: the module's top-level
build() returns an applied-scientist whose build(context) returns this artifact.
"""
from __future__ import annotations

import os

import httpx

from appsci_bench.eval.artifact import ArtifactQuery, ArtifactResponse, InProcessArtifact

TASK_ID = "airline_rebooking"


def _act_via_tool_api(actions: list) -> None:
    """Replay the golden actions through the live tool-API (writes mutate the shared task-env DB)."""
    url = os.environ.get("AIRLINE_TOOLS_URL")
    token = os.environ.get("AIRLINE_TOOLS_TOKEN")
    if not url:
        return  # no task-env bound (e.g. unit test) -> messages-only
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for a in actions or []:
        try:
            httpx.post(f"{url}/{a.get('name')}", json={"arguments": a.get("arguments") or {}},
                       headers=headers, timeout=15.0)
        except httpx.HTTPError:
            pass  # a tool precondition failure is a real outcome, not a crash


def _respond_fn(by_task: dict):
    def respond(query: ArtifactQuery) -> ArtifactResponse:
        case = by_task.get(query.intended_task, {"actions": [], "communicate_info": []})
        _act_via_tool_api(case["actions"])
        content = "\n".join(case["communicate_info"]) or "Handled per policy."
        return ArtifactResponse(output={"actions": case["actions"]},
                                messages=[{"role": "assistant", "content": content}])
    return respond


class ReferenceAppliedScientist:
    """Stands in for the AppSci agent: 'builds' the gold reference artifact for airline."""

    def build(self, context):
        # Read the airline eval oracle directly from the task's dataset. Post-#31 the dataset is a
        # DIRECTORY of per-sample dirs (eval_data/<data_point_id>/sample.json), new-format
        # `multi_turn_input` + `expected_output.json`. This gold stand-in stays self-contained and
        # keys its golden-action replay by the sample's `user_task`.
        import json
        import pathlib
        from appsci_bench.core.spec import REPO_ROOT, load_task_spec
        spec = load_task_spec(TASK_ID)
        dataset_dir = pathlib.Path(spec.dataset)
        if not dataset_dir.is_absolute():
            dataset_dir = REPO_ROOT / dataset_dir
        by_task = {}
        for sample_file in sorted(dataset_dir.glob("*/sample.json")):
            d = json.loads(sample_file.read_text())
            mti = d.get("multi_turn_input") or {}
            key = mti.get("user_task") or d.get("data_point_id") or ""
            raw = (d.get("expected_output") or {}).get("json")
            try:
                data = json.loads(raw) if raw else {}
            except (TypeError, ValueError):
                data = {}
            by_task[key] = {
                "actions": data.get("actions") or [],
                "communicate_info": data.get("communicate_info") or [],
            }
        return InProcessArtifact(_respond_fn(by_task))


def build() -> ReferenceAppliedScientist:
    """`build(context)`-factory entry point (reference solution; the benchmark's supported entrypoint
    is the Docker orchestrator, which builds via the containerized AppSci agent)."""
    return ReferenceAppliedScientist()
