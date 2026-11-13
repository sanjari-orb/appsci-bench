"""Generic, per-task-agnostic check: every declared `scoring_kind` must match what its eval function
can actually do -- does the function reach a model call, or not?

`scoring_kind` is hand-written in a task's spec and, until this test, nothing compared it to the eval
code. That is a drift the field's own schema description admitted ("Declared by hand, NOT verified
against the eval function"), and drift is not hypothetical: order_to_cash's `fault_classification`
began life as an LLM judge and became a mechanical comparison, so its declaration had to change with
it. A caller selecting "just the judged metrics" (to re-run them, or to price a run) trusts this
field, so a stale one silently misroutes real work.

HOW THE CHECK WORKS, and why it is not source analysis. The judged objectives are mostly closures
built by a factory (`make_transcript_judge`, `make_source_*_judge`, `make_visual_*_judge`), and the
factory is imported through the helpers PACKAGE rather than the module that defines it. Reading the
source text therefore misses the call entirely -- an earlier static pass wrongly cleared twelve of
the fourteen judged objectives for exactly that reason. So this walks the RESOLVED objects instead:
from the eval function, follow every global it names and every closure cell it captured, recursing
into any function found, and ask whether a model-call primitive is among them. Factory-built
closures resolve correctly because a closure's `__globals__` is the dict of the module that DEFINED
it, where the judge helper is really bound.

The check is over-inclusive by design -- it asks whether a model call is REACHABLE, not whether a
given input reaches it. That is the right question for this field: a judged objective that bails to
`None` before calling a model (no source files, rendering unavailable) is still a judged objective.

The rules, and the asymmetry between them:
  deterministic -> must NOT reach a model call. A false "deterministic" is the costly direction: it
                   invites a caller to treat the metric as free and reproducible when it is neither.
  llm_judge     -> MUST reach a model call.
  hybrid        -> MUST reach a model call. Hybrid means "mechanical logic that calls a model on
                   some path"; one that reaches no model at all is simply deterministic. This test
                   does not try to prove the mechanical path exists -- that is a per-task fact (see
                   banking_support's retrieval_recall, which scores from env-logged retrieval when
                   the harness delivers it and judges a proxy when it does not).
  undeclared    -> skipped. Per the schema, a missing `scoring_kind` is UNDECLARED, not a default.

IF THIS TEST FAILS after you add a NEW way to call a model, add that primitive to `_MODEL_SINKS`
rather than loosening the rules -- the sinks are matched by object identity, so a rename cannot
silently empty the set, but a genuinely new entry point can.
"""
from __future__ import annotations

import importlib
import types

import pytest

from appsci_bench.common.model_config import complete
from appsci_bench.core.spec import all_task_ids, load_task_spec
from appsci_bench.eval.helpers.llm import llm_judge_call
from appsci_bench.eval.objectives import resolve_eval_function

#: Every primitive that results in a model call. `complete` is the transport itself; `llm_judge_call`
#: is the only wrapper around it in the eval path, listed too so the walk can stop one level earlier
#: (and so a judge helper that swapped transports still trips this).
_MODEL_SINKS = (llm_judge_call, complete)

#: Guard against the walk running away on a pathological object graph. Far above the deepest real
#: chain (eval function -> factory closure -> llm_judge_call -> complete). Hitting it RAISES rather
#: than returning False: an exhausted walk has not proved the absence of a model call, and quietly
#: reporting one as absent is the costly direction of this whole test.
_MAX_NODES = 20_000


def _reaches_model_call(fn) -> bool:
    """True if `fn` can reach a model-call primitive.

    Walks the real object graph, not the source. Each item on the stack is paired with the namespace
    its names resolve against -- a function brings its own `__globals__` (the module that DEFINED it,
    which is what makes factory-built closures resolve correctly), and a nested code object inherits
    its parent's. For each item: check whether it IS a sink, resolve the names it references, read
    every closure cell it captured, and descend into any nested def/lambda. Non-functions are dead
    ends -- a referenced module object or constant cannot itself call a model.

    Nested code objects are walked directly rather than rebuilt into functions: one with free
    variables cannot be rebuilt without its parent's closure, and those cells are already pushed
    separately by the branch above it.
    """
    seen: set[int] = set()
    # `seen` keys on id(), so every object it records must outlive the walk -- otherwise a collected
    # object's id could be reused and a distinct object wrongly skipped as already-visited. Everything
    # here is reachable from live module globals, but pin them anyway: the memo's correctness should
    # not depend on that staying true.
    keepalive: list[object] = []
    stack: list[tuple[object, dict]] = [(fn, getattr(fn, "__globals__", {}))]
    while stack:
        if len(seen) >= _MAX_NODES:
            raise RuntimeError(
                f"reachability walk from {getattr(fn, '__qualname__', fn)!r} exceeded "
                f"{_MAX_NODES} nodes without terminating; raise _MAX_NODES or narrow the walk "
                f"rather than letting it report 'no model call' on an unfinished search"
            )
        obj, namespace = stack.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        keepalive.append(obj)
        if any(obj is sink for sink in _MODEL_SINKS):
            return True
        if isinstance(obj, types.CodeType):
            code = obj
        else:
            code = getattr(obj, "__code__", None)
            if code is None:
                continue
            namespace = getattr(obj, "__globals__", namespace)
            for cell in getattr(obj, "__closure__", None) or ():
                try:
                    stack.append((cell.cell_contents, namespace))
                except ValueError:
                    continue  # an empty cell (recursive definition not yet bound)
        for name in code.co_names:
            if name in namespace:
                stack.append((namespace[name], namespace))
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                stack.append((const, namespace))
    return False


def _declared_objectives():
    """(task_id, objective_name, eval_function_path, scoring_kind) for every objective that declares
    a scoring_kind. An objective without one is UNDECLARED and has nothing to verify."""
    out = []
    for task_id in all_task_ids():
        ev = load_task_spec(task_id).evaluation_data or {}
        for raw in ev.get("objectives") or []:
            kind = raw.get("scoring_kind")
            if kind:
                out.append((task_id, raw["name"], raw["eval_function_path"], kind))
    return out


_DECLARED = _declared_objectives()
_TEST_IDS = [f"{t}:{n}:{k}" for t, n, _, k in _DECLARED]


@pytest.mark.parametrize("task_id, objective_name, eval_function_path, scoring_kind",
                         _DECLARED, ids=_TEST_IDS)
def test_declared_scoring_kind_matches_the_eval_function(task_id, objective_name,
                                                         eval_function_path, scoring_kind):
    module_path = f"tasks.{eval_function_path}".rpartition(".")[0]
    try:
        importlib.import_module(module_path)
    except ImportError:
        # Unfinished implementation, not a defect -- same policy as
        # test_eval_function_contracts.py. The declaration starts being checked the moment the
        # module lands, with no one having to remember to enable it.
        pytest.skip(f"{task_id}.{objective_name}: {module_path} not authored yet")
        return

    fn = resolve_eval_function(eval_function_path)
    reaches = _reaches_model_call(fn)

    if scoring_kind == "deterministic":
        assert not reaches, (
            f"{task_id}.{objective_name} is declared scoring_kind: deterministic but its eval "
            f"function can reach a model call. Either it is really 'llm_judge'/'hybrid', or a "
            f"judge crept into a metric that is supposed to be free and reproducible."
        )
    else:  # llm_judge / hybrid -- the schema's enum admits nothing else
        assert reaches, (
            f"{task_id}.{objective_name} is declared scoring_kind: {scoring_kind} but its eval "
            f"function reaches no model call. If the judge was replaced by mechanical logic "
            f"(as order_to_cash's fault_classification once was), the declaration must become "
            f"'deterministic'."
        )


def test_the_walk_can_actually_find_a_model_call():
    """Guard the guard. Every assertion above rests on `_reaches_model_call`; if the walk silently
    stopped finding anything, the deterministic cases would all pass vacuously and the judged ones
    would fail loudly -- but a subtler regression could weaken it without that signal. Pin both
    directions against known objects rather than task code, so this stays true as tasks change."""
    from appsci_bench.eval.helpers import make_transcript_judge, text_similarity
    assert _reaches_model_call(make_transcript_judge("{{ transcript }}")), \
        "the walk no longer finds llm_judge_call inside a factory-built transcript judge"
    assert not _reaches_model_call(text_similarity), \
        "the walk now reports a model call for a purely mechanical helper"


def test_some_objective_declares_a_scoring_kind():
    """Confirm the parametrization is non-empty, so this file cannot pass vacuously."""
    assert _DECLARED, "no task declared any scoring_kind -- something upstream is broken"
