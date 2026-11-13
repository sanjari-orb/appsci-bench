"""Unit tests for objective parsing + eval-function resolution (fail-fast)."""
from __future__ import annotations

import importlib

import pytest

from appsci_bench.eval.objectives import (
    load_objectives, parse_objectives, resolve_eval_function, validate_eval_function_signature,
)
from appsci_bench.eval.datatypes import EvalTypeError

_PKG = "tests.fixtures"  # eval_task.eval_fns lives here
_OK = {"name": "m1", "eval_function_path": "eval_task.eval_fns.always_one",
       "positive": True, "weight": 1.0}


def test_parse_ok():
    objs = parse_objectives([_OK])
    assert objs[0].name == "m1" and objs[0].positive is True and objs[0].weight == 1.0
    assert objs[0].enabled is True


def test_parse_rejects_empty():
    with pytest.raises(EvalTypeError):
        parse_objectives([])


@pytest.mark.parametrize("bad", [
    {"name": "m", "eval_function_path": "p", "positive": True},                  # missing weight
    {"name": "m", "eval_function_path": "p", "positive": True, "weight": True},  # bool weight
    {"name": "m", "eval_function_path": "p", "positive": "yes", "weight": 1.0},  # non-bool positive
    {"name": "", "eval_function_path": "p", "positive": True, "weight": 1.0},    # empty name
    {"name": "m", "eval_function_path": "p", "positive": True, "weight": -0.5},  # negative weight
])
def test_parse_rejects_bad_fields(bad):
    with pytest.raises(EvalTypeError):
        parse_objectives([bad])


def test_parse_rejects_negative_weight_with_clear_message():
    bad = {"name": "m", "eval_function_path": "p", "positive": True, "weight": -1.0}
    with pytest.raises(EvalTypeError, match="non-negative"):
        parse_objectives([bad])


def test_parse_allows_zero_weight():
    # zero is valid (aggregator falls back to an unweighted mean if all weights are 0)
    objs = parse_objectives([{**_OK, "weight": 0}])
    assert objs[0].weight == 0.0


def test_parse_rejects_duplicate_names():
    with pytest.raises(EvalTypeError):
        parse_objectives([_OK, dict(_OK)])


def test_objective_json_schema_rejects_negative_weight():
    # The standalone objective schema enforces weight >= 0 (the inline copy in task_spec.schema.json
    # carries the same constraint; both are hand-synced since the spec validator can't $ref cross-file).
    jsonschema = pytest.importorskip("jsonschema")
    import json
    from appsci_bench.core.spec import REPO_ROOT
    schema = json.loads((REPO_ROOT / "schema" / "objective.schema.json").read_text())
    v = jsonschema.Draft202012Validator(schema)
    assert v.is_valid(_OK)
    assert v.is_valid({**_OK, "weight": 0})
    assert not v.is_valid({**_OK, "weight": -0.1})


def test_resolve_ok():
    fn = resolve_eval_function("eval_task.eval_fns.always_one", package=_PKG)
    assert fn(data_point_id="d", conversation=[])[0] == 1.0


@pytest.mark.parametrize("path", [
    "eval_task.eval_fns.does_not_exist",   # bad attr
    "eval_task.nope.always_one",           # bad module
    "eval_task.eval_fns.NOT_CALLABLE",     # not callable
    "noseparator",                         # not dotted
])
def test_resolve_fails(path):
    with pytest.raises(EvalTypeError):
        resolve_eval_function(path, package=_PKG)


def test_load_objectives_eager_resolution_attaches_fn():
    spec = {"evaluation_data": {"objectives": [_OK]}}
    objs = load_objectives(spec, package=_PKG)
    assert callable(objs[0].eval_function)
    assert objs[0].eval_function(data_point_id="d", conversation=[])[0] == 1.0


def test_load_objectives_fails_fast_on_bad_path():
    spec = {"evaluation_data": {"objectives": [
        {"name": "m", "eval_function_path": "eval_task.eval_fns.missing", "positive": True, "weight": 1.0}]}}
    with pytest.raises(EvalTypeError):
        load_objectives(spec, package=_PKG)


def test_disabled_objective_is_not_resolved_unless_explicitly_included():
    spec = {"evaluation_data": {"objectives": [_OK, {
        **_OK, "name": "disabled", "enabled": False,
        "eval_function_path": "missing_module.missing_function",
    }]}}
    assert [o.name for o in load_objectives(spec, package=_PKG)] == ["m1"]
    declared = load_objectives(spec, resolve=False, include_disabled=True)
    assert [o.enabled for o in declared] == [True, False]
    with pytest.raises(EvalTypeError, match="cannot import"):
        load_objectives(spec, package=_PKG, include_disabled=True)


def test_all_disabled_fails_before_evaluation():
    spec = {"evaluation_data": {"objectives": [{**_OK, "enabled": False}]}}
    with pytest.raises(EvalTypeError, match="at least one enabled objective"):
        load_objectives(spec, package=_PKG)


@pytest.mark.parametrize("enabled", ["false", 0, 1, None])
def test_enabled_must_be_a_boolean_in_parser_and_both_schemas(enabled):
    import json
    import jsonschema
    from appsci_bench.core.spec import REPO_ROOT

    with pytest.raises(EvalTypeError, match="'enabled' must be a boolean"):
        parse_objectives([{**_OK, "enabled": enabled}])
    standalone = json.loads((REPO_ROOT / "schema/objective.schema.json").read_text())
    task = json.loads((REPO_ROOT / "schema/task_spec.schema.json").read_text())
    inline = task["properties"]["evaluation_data"]["properties"]["objectives"]["items"]
    for schema in (standalone, inline):
        validator = jsonschema.Draft202012Validator(schema)
        assert validator.is_valid({**_OK, "enabled": False})
        assert validator.is_valid({**_OK, "enabled": True})
        assert not validator.is_valid({**_OK, "enabled": enabled})


# --- signature validation (fail-fast on a mis-shaped eval function, not just a bad path) ---------

def test_validate_signature_accepts_a_conforming_function():
    fn = resolve_eval_function("eval_task.eval_fns.always_one", package=_PKG)
    validate_eval_function_signature(fn, "eval_task.eval_fns.always_one")  # no raise


@pytest.mark.parametrize("attr", [
    "missing_kwargs_catchall",           # no **kwargs -- fails the forward-compat probe
    "requires_an_extra_positional_arg",  # required param scoring.py never supplies
    "wrong_keyword_names",               # doesn't accept the contract's keywords at all
])
def test_resolve_fails_fast_on_a_mis_shaped_function(attr):
    # resolve_eval_function (called at spec-load time, before any run) must reject these -- not
    # defer to scoring.py's per-call TypeError, which would only surface once per real data point.
    with pytest.raises(EvalTypeError):
        resolve_eval_function(f"eval_task.eval_fns.{attr}", package=_PKG)


def test_validate_signature_error_names_the_missing_kwargs_support():
    fn = importlib.import_module("tests.fixtures.eval_task.eval_fns").missing_kwargs_catchall
    with pytest.raises(EvalTypeError, match="kwargs"):
        validate_eval_function_signature(fn, "eval_task.eval_fns.missing_kwargs_catchall")
