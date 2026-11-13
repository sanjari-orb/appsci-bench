"""Task Runner package for managing task environment lifecycle.

This package provides the TaskRunner class which handles:
- Spinning up Docker containers for task environments
- Bundling and transferring data into containers
- Running setup scripts to initialize databases and services
- Tearing down environments after evaluation

Example usage:
    from appsci_bench.task_runner import TaskRunner

    runner = TaskRunner(tasks_dir=Path("tasks"))
    build_env = runner.start("order_to_cash", phase="build")
    # ... the agent builds against it, then submits ...
    runner.stop(build_env)
    eval_env = runner.start("order_to_cash", phase="eval")
    # ... the evaluator scores the artifact against it ...
    runner.stop(eval_env)

See docs/task_runner.md for full design documentation.
"""

from .runner import TaskRunner
from .config import PHASE_BUILD, PHASE_EVAL, PHASES, TaskConfig, TaskEnvPhase

__all__ = ["TaskRunner", "TaskConfig", "TaskEnvPhase", "PHASE_BUILD", "PHASE_EVAL", "PHASES"]
