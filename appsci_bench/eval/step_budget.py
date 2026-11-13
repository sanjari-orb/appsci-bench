"""One simulation-step budget shared by both speakers and their environment tools.

A text message (including the customer's stop message) costs one step. A tool request and
its result cost two. As in tau2, an already-issued tool request gets its result before the
limit is checked, so a tool pair starting at limit - 1 finishes at limit + 1.
"""
from __future__ import annotations

import threading
from typing import Protocol


class MaxStepsReached(Exception):
    """Normal simulation termination, not an artifact or simulator error."""


class LaneCleanupError(RuntimeError):
    """Artifact recovery failed; retire this lane before its environment can be reused."""


class StepBudgetUnavailable(RuntimeError):
    """Infrastructure failure; this case attempt cannot receive a task score."""

    def __init__(self, message, *, unsafe_to_reuse_lane=False):
        super().__init__(message)
        # A timed-out admin POST may still apply later inside the environment. Retire that lane;
        # restarting only the artifact does not cancel an outstanding environment admin process.
        self.unsafe_to_reuse_lane = unsafe_to_reuse_lane


class StepBudget(Protocol):
    tools_counted_externally: bool

    def consume(self, steps: int = 1) -> None: ...
    def snapshot(self) -> dict: ...


def check_budget(budget: StepBudget) -> None:
    state = budget.snapshot()
    if state["steps"] >= state["max_steps"] and not state.get("pending_environment", False):
        raise MaxStepsReached


class LocalStepBudget:
    """For in-process clients/tests; environment calls must explicitly consume tool steps."""

    tools_counted_externally = False

    def __init__(self, max_steps: int):
        self.max_steps = max_steps
        self.steps = 0
        self._lock = threading.Lock()

    def consume(self, steps: int = 1) -> None:
        with self._lock:
            if self.steps >= self.max_steps:
                raise MaxStepsReached
            self.steps += steps

    def snapshot(self) -> dict:
        with self._lock:
            return {"steps": self.steps, "max_steps": self.max_steps}
