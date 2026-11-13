"""Load + validate a task's Objectives and resolve each to its eval-function callable.

An Objective binds a metric name to an importable eval function (design: Metrics and Objectives).
`parse_objectives` builds + validates the records; `resolve_eval_function` imports the callable and
checks its signature against the contract; `load_objectives` does both eagerly so a bad
`eval_function_path` OR a mis-shaped function fails fast -- before any artifact queries. The
RETURN-value half of the contract (score 0-1, metadata dict) is still enforced at scoring time
(scoring.py's `_validate_return`) since that can only be checked once the function actually runs.
"""
from __future__ import annotations

import importlib
import inspect
from typing import Any, Callable

from .datatypes import EvalTypeError, Objective

# The eval-function contract (design: Task-Specific Evaluation Function):
#   fn(*, data_point_id: str, conversation: list[ConversationTurn],
#      expected_output: ArtifactAgentOutput | None = None, eval_rubric: str | None = None, **kwargs)
# `scoring.py::_score_one` always calls by exactly these keywords, never positionally.
#
# `eval_rubric` here is `EvalSample.eval_rubric` -- a per-DATA-POINT NL note (see its docstring in
# datatypes.py), NOT a place for a metric's own definition/scoring criteria. A metric's rubric is
# authored once by the task designer and belongs baked into the eval function's own prompt (a
# `make_transcript_judge` `prompt_template`, or `make_source_grounding_judge`'s `metric_rubric`
# construction-time argument) -- never routed through this per-sample contract field.
_CONTRACT_KWARGS = ("data_point_id", "conversation", "expected_output", "eval_rubric")
# Not a real contract field -- probing with it isolates whether `fn` has `**kwargs` (required so
# the contract can add fields additively without breaking existing eval functions, per the design
# doc), since binding only succeeds if some parameter can absorb an unknown keyword.
_FUTURE_KWARG_PROBE = "__appsci_bench_contract_probe__"


def validate_eval_function_signature(fn: Callable, path: str) -> None:
    """Statically check that `fn` can be called the way `scoring.py` calls every eval function --
    by exactly the keywords in `_CONTRACT_KWARGS`, plus tolerance for a future additive keyword --
    without ever executing `fn`'s body. Raises EvalTypeError, quoting the underlying TypeError, if
    the real call would fail (wrong keyword names, a required parameter scoring.py never supplies,
    no `**kwargs` catch-all)."""
    sig = inspect.signature(fn)
    probe = {k: None for k in _CONTRACT_KWARGS}
    try:
        sig.bind(**probe)
    except TypeError as e:
        raise EvalTypeError(
            f"{path!r} does not match the eval-function contract "
            f"fn(*, {', '.join(_CONTRACT_KWARGS)}, **kwargs): {e}"
        ) from e
    try:
        sig.bind(**probe, **{_FUTURE_KWARG_PROBE: None})
    except TypeError as e:
        raise EvalTypeError(
            f"{path!r} must accept **kwargs so the contract can add fields additively "
            f"(design: Task-Specific Evaluation Function): {e}"
        ) from e


def parse_objectives(items: list[dict]) -> list[Objective]:
    """Build + validate Objective records from raw dicts (no import/resolution yet)."""
    if not items:
        raise EvalTypeError("a task must declare at least one objective")
    out: list[Objective] = []
    seen: set[str] = set()
    for i, raw in enumerate(items):
        try:
            name = raw["name"]
            path = raw["eval_function_path"]
            positive = raw["positive"]
            weight = raw["weight"]
        except KeyError as e:
            raise EvalTypeError(f"objective #{i} missing required field {e}") from e
        if not isinstance(name, str) or not name:
            raise EvalTypeError(f"objective #{i}: 'name' must be a non-empty string")
        if name in seen:
            raise EvalTypeError(f"duplicate objective name {name!r}")
        seen.add(name)
        if not isinstance(path, str) or not path:
            raise EvalTypeError(f"objective {name!r}: 'eval_function_path' must be a non-empty string")
        if not isinstance(positive, bool):
            raise EvalTypeError(f"objective {name!r}: 'positive' must be a boolean")
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise EvalTypeError(f"objective {name!r}: 'weight' must be a number, got {weight!r}")
        if weight < 0:
            raise EvalTypeError(f"objective {name!r}: 'weight' must be non-negative, got {weight}")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise EvalTypeError(f"objective {name!r}: 'enabled' must be a boolean")
        out.append(Objective(name=name, eval_function_path=path, positive=positive,
                              weight=float(weight), enabled=enabled))
    return out


def resolve_eval_function(path: str, *, package: str = "tasks") -> Callable:
    """Import the eval function named by `path` (relative to `package`, the tasks/ package).

    e.g. path 'order_to_cash.eval.classification_correctness' with package 'tasks' imports module
    'tasks.order_to_cash.eval' and returns its 'classification_correctness' attribute. Raises
    EvalTypeError if the module/attr is missing, the target is not callable, or its signature
    doesn't match the eval-function contract (see validate_eval_function_signature).
    """
    full = f"{package}.{path}" if package else path
    module_path, _, attr = full.rpartition(".")
    if not module_path or not attr:
        raise EvalTypeError(f"eval_function_path {path!r} must be a dotted module.attr path")
    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise EvalTypeError(f"cannot import {module_path!r} for eval_function_path {path!r}: {e}") from e
    fn = getattr(module, attr, None)
    if not callable(fn):
        raise EvalTypeError(f"{full!r} is not callable (got {type(fn).__name__})")
    validate_eval_function_signature(fn, full)
    return fn


def load_objectives(spec: Any, *, package: str = "tasks", resolve: bool = True,
                    include_disabled: bool = False) -> list[Objective]:
    """Load enabled Objectives from a TaskSpec (or dict).

    Disabled objectives are validated but neither resolved nor returned by default, so they spend no
    scoring work and produce no report rows. Weight zero alone still computes and reports a metric.
    `include_disabled=True` opts into all declared objectives; combine with `resolve=False` for
    inspection without imports. When resolve=True, selected functions are resolved eagerly.
    """
    ev = _evaluation_data(spec)
    objectives = parse_objectives(ev.get("objectives") or [])
    if not include_disabled:
        objectives = [obj for obj in objectives if obj.enabled]
    if not objectives:
        raise EvalTypeError("a task must have at least one enabled objective")
    if resolve:
        for obj in objectives:
            obj.eval_function = resolve_eval_function(obj.eval_function_path, package=package)
    return objectives


def _evaluation_data(spec: Any) -> dict:
    ev = getattr(spec, "evaluation_data", None)
    if ev is None and isinstance(spec, dict):
        ev = spec.get("evaluation_data")
    if not isinstance(ev, dict):
        raise EvalTypeError("spec has no evaluation_data block")
    return ev
