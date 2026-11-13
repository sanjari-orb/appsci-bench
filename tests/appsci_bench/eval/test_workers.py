"""The eval phase's one worker pool: its sizing rule and its ownership rules.

Two independent fan-outs run in the eval phase -- scoring every (data point, objective) pair, and
judging every item of an elicitation rubric. Each used to size its own pool, so a run's real
concurrency was the sum of two budgets neither side could see. These tests pin that the sizing rule
did not change in the move, and the ownership property the elicitation diagnostic depends on: a pool
handed to a function comes back still usable.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from appsci_bench.eval.workers import DEFAULT_MAX_WORKERS, pool_size, worker_pool


# The expression scoring.py used inline before this module existed. Kept literally, as the reference
# the new rule is compared against -- if these ever disagree, the move changed behaviour.
def _old_rule(n_items: int, cpu_count: int) -> int:
    return min(16, max(1, min(n_items, cpu_count or 2)))


@pytest.mark.parametrize("n_items", [0, 1, 2, 3, 7, 15, 16, 17, 40, 500])
@pytest.mark.parametrize("cpu_count", [1, 2, 4, 8, 10, 16, 64])
def test_sizing_matches_the_rule_scoring_used_before(n_items, cpu_count):
    with patch.object(os, "cpu_count", return_value=cpu_count):
        assert pool_size(n_items) == _old_rule(n_items, cpu_count)


def test_sizing_stays_between_one_and_the_ceiling():
    for n in (0, 1, 1000):
        for cpus in (1, 4, 128, None):
            with patch.object(os, "cpu_count", return_value=cpus):
                assert 1 <= pool_size(n) <= DEFAULT_MAX_WORKERS


def test_no_work_still_gives_a_usable_pool_of_one():
    # A caller may open the pool before knowing whether there is anything to submit; zero must not
    # become a ThreadPoolExecutor(0), which raises.
    assert pool_size(0) == 1
    with worker_pool(0) as pool:
        assert pool.submit(lambda: 7).result() == 7


def test_an_unavailable_cpu_count_falls_back_to_two():
    with patch.object(os, "cpu_count", return_value=None):
        assert pool_size(100) == 2


def test_an_explicit_max_workers_overrides_the_rule_outright():
    with patch.object(os, "cpu_count", return_value=64):
        assert pool_size(100, 3) == 3
        assert pool_size(1, 9) == 9           # even above the work count
        assert pool_size(100, 99) == 99       # even above the ceiling, as before


def test_the_pool_is_shut_down_on_a_normal_exit():
    with worker_pool(4) as pool:
        assert pool.submit(lambda: 1).result() == 1
    with pytest.raises(RuntimeError):
        pool.submit(lambda: 1)


def test_the_pool_is_shut_down_when_the_block_raises():
    # A raising eval phase must not leave workers behind holding model calls open.
    with pytest.raises(ValueError), worker_pool(4) as pool:
        raise ValueError("boom")
    with pytest.raises(RuntimeError):
        pool.submit(lambda: 1)


def test_the_pool_is_sized_by_the_rule():
    with patch.object(os, "cpu_count", return_value=8):
        with worker_pool(3) as pool:
            assert pool._max_workers == 3
        with worker_pool(100) as pool:
            assert pool._max_workers == 8


# --- the ownership property the elicitation diagnostic rests on ------------------------------

def test_an_injected_pool_is_not_shut_down_by_score_records():
    """The whole point of sharing: scoring must hand the pool back usable, because the elicitation
    diagnostic fans out into the same one immediately afterwards."""
    from appsci_bench.eval.datatypes import Dataset, EvalSample, GenerationRecord, Objective

    sample = EvalSample(data_point_id="dp1", single_turn_input="q")
    dataset = Dataset(type="single_turn", data=[sample])
    obj = Objective(name="m", eval_function_path="x.y", positive=True, weight=1.0,
                    eval_function=lambda **kw: (1.0, {}))
    record = GenerationRecord(data_point_id="dp1", repeat_index=0, conversation=[])

    from appsci_bench.eval.scoring import score_records
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        results = score_records([record], dataset, [obj], pool=pool)
        assert [r.score for r in results] == [1.0]
        assert pool.submit(lambda: "still alive").result() == "still alive"
    finally:
        pool.shutdown(wait=True)


def test_an_injected_pool_gives_identical_results_to_an_owned_one():
    from appsci_bench.eval.datatypes import Dataset, EvalSample, GenerationRecord, Objective
    from appsci_bench.eval.scoring import score_records

    samples = [EvalSample(data_point_id=f"dp{i}", single_turn_input="q") for i in range(4)]
    dataset = Dataset(type="single_turn", data=samples)
    objs = [Objective(name=n, eval_function_path="x.y", positive=True, weight=1.0,
                      eval_function=lambda *, data_point_id, **kw: (len(data_point_id) / 10, {}))
            for n in ("alpha", "beta")]
    records = [GenerationRecord(data_point_id=s.data_point_id, repeat_index=0, conversation=[])
               for s in samples]

    without = score_records(records, dataset, objs)
    pool = ThreadPoolExecutor(max_workers=3)
    try:
        with_pool = score_records(records, dataset, objs, pool=pool)
    finally:
        pool.shutdown(wait=True)
    assert [(r.data_point_id, r.objective, r.score, r.error) for r in without] == \
           [(r.data_point_id, r.objective, r.score, r.error) for r in with_pool]
