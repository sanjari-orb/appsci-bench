"""One bounded worker pool for the whole eval phase.

The eval phase has two fan-outs after generation: scoring every (data point, objective) pair, and judging
every item of a task's elicitation rubric. Left to size a pool each, a run's real concurrency would be the
sum of two budgets that neither side could see, so they share one -- opened by `eval_system` and sized on
whichever fan-out is larger, since scoring scales with the dataset and the diagnostic with the rubric.

"The" sizing rule, but not the only copy of it: `generation.py` still carries the same expression inline
for its own pool. That is deliberate (see the last paragraph), and it is worth knowing rather than
discovering -- a change to the rule here does not reach generation.

**Never submit into the shared pool from inside one of its own workers.** A pool of N threads whose
workers block waiting on work they themselves submitted deadlocks as soon as N tasks are in flight.
Both fan-outs are driven from the eval phase's own thread, so nothing does this today -- the rule is
here because a future nested submission would be a hang, not an error, and hangs are expensive to
diagnose.

The generation phase keeps its own pool on purpose. It is network-bound, it finishes before scoring
begins, and folding it in would widen the blast radius for no gain.
"""
from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

# Ceiling regardless of machine or workload. Every worker can hold an in-flight model call, so this is
# a politeness limit on the gateway as much as a local resource limit.
DEFAULT_MAX_WORKERS = 16


def pool_size(n_items: int, max_workers: int | None = None) -> int:
    """How many workers to run for `n_items` pieces of work; at least 1, never above the ceiling.

    This is the rule the scoring phase has always used, lifted here unchanged so moving to a shared
    pool changes no behaviour. An explicit `max_workers` overrides it outright, as it did before.

    Sizing to the work rather than to the machine matters because the items are model calls, not CPU
    work: a two-item rubric has no use for sixteen threads.
    """
    if max_workers is not None:
        return max_workers
    return min(DEFAULT_MAX_WORKERS, max(1, min(n_items, os.cpu_count() or 2)))


@contextlib.contextmanager
def worker_pool(n_items: int, max_workers: int | None = None) -> Iterator[ThreadPoolExecutor]:
    """A pool sized by :func:`pool_size`, shut down on the way out -- including on an exception.

    Shutdown waits for in-flight work, so a caller that leaves this block early still leaves no
    threads behind holding a model call open.
    """
    pool = ThreadPoolExecutor(max_workers=pool_size(n_items, max_workers))
    try:
        yield pool
    finally:
        pool.shutdown(wait=True)
