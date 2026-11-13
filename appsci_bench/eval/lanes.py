"""Eval lanes: the independent (task-env + artifact) stacks generation draws from.

A task whose eval cases mutate a shared environment used to run its whole dataset one case at a time.
That was never a property of the benchmark -- a case needs a CLEAN environment, not the SAME one -- it
was a property of there being exactly one environment. A lane is one such environment plus the
artifact bound to it, and a pool of W lanes is what lets W cases run at once without any of them
seeing another's writes.

Two shapes, and the difference is the whole module:

  * EXCLUSIVE (`LanePool.exclusive`) -- W lanes, each held by at most one case at a time, for the
    whole of that case (setup -> conversation -> final-state read). Built for tasks that set
    `evaluation_data.reset_env_per_sample`, where the per-case hooks reset and hash a whole store.
  * SHARED (`LanePool.shared`) -- one lane with no env hooks, handed to every worker at once, which is
    what every other task has always done. `max_workers` is None here, meaning "the caller's own
    sizing rule still applies"; nothing about those tasks changes.

The invariant worth stating, because violating it is a silently wrong score rather than an error: a
case must hold ONE lane for its ENTIRE life. Not two cases to a lane (they would reset each other's
store mid-conversation), and not one case across two lanes (a multi-turn artifact is allowed to keep
state of its own between turns -- see `artifact_client.HttpArtifactClient`).
"""
from __future__ import annotations

import contextlib
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Iterator

from .artifact_client import ArtifactAgentClient
from .datatypes import EvalSample
from .step_budget import StepBudget


class NoAvailableLanes(RuntimeError):
    """Every lane was retired; pending cases cannot be evaluated."""


@dataclass(frozen=True)
class EvalLane:
    """One (task-env, artifact) stack: what to query, and how to prepare/read its environment.

    `setup_state` / `read_final_state` are the same per-case hooks `generation.generate` has always
    taken, bound here to THIS lane's environment rather than to a single global one. Both are None for
    a lane with no environment behind it.

    `user_sim` overrides the simulator the generation loop was given, for this lane only. It exists
    for one reason: on a task whose simulated customer can call the environment's own tools, the
    executor behind those calls is bound to ONE environment -- so a lane's sim has to be bound to that
    lane's. None (the common case, and every task whose customer only talks) means the shared one.

    `label` names the lane in logs ("lane-0", "lane-1", ...). It exists so a case that failed can be
    traced back to the stack it ran on, which is the first question asked when one lane's cases all
    fail and the others' do not.

    `step_budget_factory` starts a per-case budget after environment reset. `cancel_query` stops
    an in-flight artifact before lane reuse; closing an HTTP connection alone cannot stop its tools.
    """

    client: ArtifactAgentClient
    setup_state: Callable[[EvalSample], None] | None = None
    read_final_state: Callable[[EvalSample], dict] | None = None
    user_sim: object | None = None
    label: str = "lane-0"
    step_budget_factory: Callable[[int], StepBudget] | None = None
    cancel_query: Callable[[], None] | None = None


class LanePool:
    """The lanes a generation run may draw from, and how many cases may be in flight.

    Construct through :meth:`shared` or :meth:`exclusive` rather than directly -- the two differ in
    whether `acquire()` blocks, and picking the wrong one silently is exactly the failure this
    module exists to prevent.
    """

    def __init__(self, lanes: list[EvalLane], *, exclusive: bool):
        if not lanes:
            raise ValueError("a LanePool needs at least one lane")
        if not exclusive and len(lanes) > 1:
            raise ValueError(
                "a shared pool holds exactly one lane; several lanes with no exclusion would let two "
                "cases mutate one environment at the same time"
            )
        self._lanes = list(lanes)
        self._exclusive = exclusive
        self._condition = threading.Condition()
        self._retired: dict[int, str] = {}
        self._free = deque(lanes) if exclusive else None

    @classmethod
    def shared(cls, lane: EvalLane) -> "LanePool":
        """One lane, no exclusion -- the shape every task without per-case env state has always run
        with. The single client is used concurrently by every worker, as it is today.

        Refuses a lane carrying env hooks: sharing those would run several cases through one reset and
        one whole-store hash, which is the exact mis-scoring lanes exist to prevent.
        """
        if lane.setup_state is not None or lane.read_final_state is not None:
            raise ValueError(
                "a shared lane must have no per-case environment hooks; several cases would then "
                "reset and hash one store at the same time. Use LanePool.exclusive."
            )
        return cls([lane], exclusive=False)

    @classmethod
    def exclusive(cls, lanes: list[EvalLane]) -> "LanePool":
        """W independent stacks, one case at a time in each."""
        return cls(lanes, exclusive=True)

    @property
    def lanes(self) -> list[EvalLane]:
        return list(self._lanes)

    @property
    def width(self) -> int:
        """How many stacks back this pool. 1 for a shared pool."""
        return len(self._lanes)

    @property
    def has_env_hooks(self) -> bool:
        """Whether any lane prepares/reads environment state per case."""
        return any(
            lane.setup_state is not None or lane.read_final_state is not None
            for lane in self._lanes
        )

    @property
    def max_workers(self) -> int | None:
        """The ceiling this pool imposes on in-flight cases, or None for "no ceiling of my own".

        An exclusive pool cannot serve more cases than it has lanes, so its width IS the ceiling. A
        shared pool imposes nothing and leaves the caller's existing sizing rule in charge -- which is
        how a task with no environment keeps the concurrency it has today.
        """
        return len(self._lanes) if self._exclusive else None

    @property
    def retired_lanes(self) -> dict[str, str]:
        with self._condition:
            return {lane.label: self._retired[id(lane)] for lane in self._lanes
                    if id(lane) in self._retired}

    def retire(self, lane: EvalLane, reason: str) -> None:
        """Permanently exclude an unsafe lane and wake waiters if no capacity remains."""
        with self._condition:
            self._retired[id(lane)] = reason
            if self._free is not None:
                self._free = deque(item for item in self._free if item is not lane)
            self._condition.notify_all()

    @contextlib.contextmanager
    def acquire(self) -> Iterator[EvalLane]:
        """Hold one lane for the duration of the block.

        On an exclusive pool this blocks until a lane is free. Ordinary case errors return the
        lane; failed recovery retires it explicitly. If all lanes are retired, wake pending cases
        with NoAvailableLanes rather than waiting forever.
        """
        if self._free is None:
            if self.retired_lanes:
                raise NoAvailableLanes("all eval lanes were retired after failed artifact recovery")
            yield self._lanes[0]
            return
        with self._condition:
            while not self._free:
                if len(self._retired) == len(self._lanes):
                    raise NoAvailableLanes("all eval lanes were retired after failed artifact recovery")
                self._condition.wait()
            lane = self._free.popleft()
        try:
            yield lane
        finally:
            with self._condition:
                if id(lane) not in self._retired:
                    self._free.append(lane)
                self._condition.notify_all()
