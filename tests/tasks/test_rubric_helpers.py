"""The shared rubric helpers themselves, as opposed to any one task's rubric.

A check that lives in `conftest.py` is a helper, not a test -- pytest does not collect test functions
from a conftest, so anything asserted there runs never. These are the cases about how the helpers read
a spec, which no shipped task exercises.
"""
from __future__ import annotations

import appsci_bench.core.spec as spec_mod
from tests.tasks.conftest import assert_items_feed_scored_objectives


def test_an_all_zero_weight_task_is_not_read_as_having_no_priced_objectives(monkeypatch):
    """The aggregator treats all-zero weights as an unweighted mean, so every objective still counts.

    Lives here beside the helper rather than under a task, because no shipped task is all-zero -- this
    guards the reading of `weight` itself, which is the part that will outlive any current spec.
    """
    class _Rubric:
        items = [type("I", (), {"item_id": "a", "objectives": ["x", "y"]})()]

    class _Spec:
        evaluation_data = {"objectives": [{"name": "x", "weight": 0.0}, {"name": "y", "weight": 0.0}]}

    monkeypatch.setattr(spec_mod, "load_task_spec", lambda _t: _Spec())
    assert_items_feed_scored_objectives("whatever", _Rubric())
