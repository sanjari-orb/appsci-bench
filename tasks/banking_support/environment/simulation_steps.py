"""Evaluator-owned step accounting, separate from the banking DB and its reward hash."""
from contextlib import contextmanager
from threading import RLock


class StepLimitReached(Exception):
    pass


class SimulationSteps:
    def __init__(self):
        self.lock = RLock()
        self.tool_lock = RLock()
        self.max_steps = None  # build exploration is not an eval conversation
        self.steps = 0
        self.pending_environment = False
        self.epoch = 0

    def reset(self, max_steps=None):
        with self.tool_lock, self.lock:
            self.max_steps = max_steps
            self.steps = 0
            self.epoch += 1

    def snapshot(self):
        with self.lock:
            return {"steps": self.steps, "max_steps": self.max_steps,
                    "pending_environment": self.pending_environment}

    def _check(self):
        if self.max_steps is not None and self.steps >= self.max_steps:
            raise StepLimitReached

    def consume(self, steps):
        with self.lock:
            self._check()
            self.steps += steps

    @contextmanager
    def tool(self):
        with self.lock:
            epoch = self.epoch
        # Tools serialize, but status polling remains nonblocking during slow environment work.
        # A queued request from the previous case must not run against a newly reset DB.
        with self.tool_lock:
            with self.lock:
                if epoch != self.epoch:
                    raise StepLimitReached
                self._check()
                if self.max_steps is not None:
                    self.steps += 1
                    self.pending_environment = True
            try:
                yield
            finally:
                with self.lock:
                    if self.max_steps is not None:
                        self.steps += 1  # failed tool responses count too
                        self.pending_environment = False
