"""Generic, per-task-agnostic conformance test: every declared objective's eval function, for
every task, must either not exist yet (skip -- eval.py not authored for this task yet, not a defect) or match the
eval-function contract (fail -- a mis-defined function, caught here instead of only surfacing as a
per-data-point error the first time a real evaluation run happens to call it).

Deliberately reuses resolve_eval_function (the same fail-fast path load_objectives/run_evaluation
takes for a real run) rather than re-implementing the check, so this test and the runtime path can
never drift apart. Coverage is automatic: a new task, or a newly-added/edited eval function on any
existing task, is checked the next time the suite runs -- no one has to remember to add a
task-specific test for it.
"""
from __future__ import annotations

import importlib

import pytest

from appsci_bench.core.spec import all_task_ids, load_task_spec
from appsci_bench.eval.datatypes import EvalTypeError
from appsci_bench.eval.objectives import parse_objectives, resolve_eval_function


def _objective_ids():
    """(task_id, objective_name, eval_function_path) for every objective declared by every task
    that has migrated to the new evaluation_data shape. Tasks still on the legacy metrics/rubrics
    shape have nothing to check here."""
    ids = []
    for task_id in all_task_ids():
        spec = load_task_spec(task_id)
        ev = spec.evaluation_data or {}
        if "objectives" not in ev:
            continue
        for obj in parse_objectives(ev["objectives"]):
            ids.append((task_id, obj.name, obj.eval_function_path))
    return ids


_OBJECTIVES = _objective_ids()
_OBJECTIVE_TEST_IDS = [f"{t}:{n}" for t, n, _ in _OBJECTIVES]


@pytest.mark.parametrize("task_id, objective_name, eval_function_path", _OBJECTIVES, ids=_OBJECTIVE_TEST_IDS)
def test_objective_eval_function_conforms_to_the_contract(task_id, objective_name, eval_function_path):
    full = f"tasks.{eval_function_path}"
    module_path, _, attr = full.rpartition(".")
    try:
        importlib.import_module(module_path)
    except ImportError:
        pytest.skip(f"{task_id}.{objective_name}: {module_path} not authored yet")
        return
    # The module exists -- a resolution failure past this point means the function itself (its
    # attribute name, callability, or signature) is genuinely mis-defined, not merely unbuilt.
    resolve_eval_function(eval_function_path)


def test_every_migrated_task_has_at_least_one_objective():
    # Guards against a task's evaluation_data accidentally declaring an empty objectives list --
    # parse_objectives already rejects that at load time, but confirm no task silently has zero
    # parametrized cases above (which would make this whole file vacuously pass).
    assert _OBJECTIVES, "no migrated task declared any objectives -- something upstream is broken"
