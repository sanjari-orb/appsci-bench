"""Loading a task's elicitation rubric and checking it against the task's scored objectives.

Two things are pinned here. First, path resolution has to match the eval dataset loader exactly --
both pointers are repo-root-relative held-out material read by the same eval phase, and a rubric that
resolved differently from the dataset would be a trap for whoever moved a task directory. Second, a
spec that declares a rubric it does not have must fail loudly: the elicitation diagnostic reports
"no rubric" as a status, and a broken pointer degrading into that status would make a task bug look
like a task that simply has not been given a rubric yet.

The objective mapping is the cross-file rule the schema cannot check, and the reason the metric's
denominator is auditable at all -- see docs/elicitation_score_design.md.
"""
from __future__ import annotations

import pytest

from appsci_bench.eval.datatypes import EvalTypeError
from appsci_bench.eval.elicitation_rubric import (
    ElicitationRubric,
    RubricItem,
    load_rubric,
    validate_rubric,
)

_FACT_A = "The six fault_type values, spelled exactly: PRICE_DELTA, QTY_MISMATCH, TOTAL_MATH."
_FACT_B = "All four cancellation eligibility conditions, including the 24-hour window and the fare class."


def _items() -> list[dict]:
    return [
        {"item_id": "b_second_in_file", "fact": _FACT_B, "objectives": ["coverage"]},
        {"item_id": "a_first_in_file", "fact": _FACT_A,
         "objectives": ["classification_correctness"], "judge_hint": "all six, spelled as written"},
    ]


# --- loading ---------------------------------------------------------------------------------

def test_a_valid_rubric_loads_in_file_order_with_its_source(rubric_task):
    spec, repo_root = rubric_task(_items())
    rubric = load_rubric(spec, repo_root=repo_root)

    assert isinstance(rubric, ElicitationRubric)
    assert rubric.task_id == "demo_task"
    # File order, NOT sorted: sorting is the reporting layer's job, and reordering here would make
    # a rubric diff unreadable against the file it came from.
    assert [i.item_id for i in rubric.items] == ["b_second_in_file", "a_first_in_file"]
    assert rubric.items[1] == RubricItem(
        item_id="a_first_in_file", fact=_FACT_A,
        objectives=("classification_correctness",), judge_hint="all six, spelled as written")
    assert rubric.items[0].judge_hint is None
    assert rubric.source.endswith("tasks/demo_task/elicitation_rubric.yaml")
    assert len(rubric) == 2


def test_a_spec_with_no_pointer_returns_none(rubric_task):
    spec, repo_root = rubric_task(None)
    assert "elicitation_rubric" not in spec["evaluation_data"]
    assert load_rubric(spec, repo_root=repo_root) is None


def test_a_spec_with_no_evaluation_data_returns_none():
    # A rubric-less spec is a reportable status, not a crash -- the standalone scorer may be handed a
    # partial spec and still has to answer "does this task have a rubric".
    assert load_rubric({"task_id": "demo_task"}) is None
    assert load_rubric(object()) is None


def test_a_task_spec_object_is_accepted(rubric_task):
    spec, repo_root = rubric_task(_items())

    class _Spec:
        task_id = spec["task_id"]
        evaluation_data = spec["evaluation_data"]

    assert len(load_rubric(_Spec(), repo_root=repo_root).items) == 2


def test_an_absolute_pointer_is_honoured_unchanged(rubric_task, tmp_path):
    spec, repo_root = rubric_task(_items())
    absolute = repo_root / "tasks" / "demo_task" / "elicitation_rubric.yaml"
    spec["evaluation_data"]["elicitation_rubric"] = str(absolute)
    # A different repo_root must not be prepended to an already-absolute pointer.
    assert len(load_rubric(spec, repo_root=tmp_path / "elsewhere").items) == 2


# --- broken pointers -------------------------------------------------------------------------

def test_a_missing_file_raises_with_the_resolved_path(rubric_task):
    spec, repo_root = rubric_task(_items(), pointer="tasks/demo_task/not_here.yaml")
    with pytest.raises(EvalTypeError) as e:
        load_rubric(spec, repo_root=repo_root)
    assert "tasks/demo_task/not_here.yaml" in str(e.value)
    assert str(repo_root) in str(e.value)  # the resolved path, so the reader can go look


def test_a_schema_invalid_file_raises_with_the_problem_named(rubric_task):
    spec, repo_root = rubric_task([{"item_id": "bad", "fact": "taxonomy", "objectives": []}])
    with pytest.raises(EvalTypeError) as e:
        load_rubric(spec, repo_root=repo_root)
    msg = str(e.value)
    assert "invalid" in msg and str(repo_root) in msg
    # The message has to say WHAT is wrong, not just that something is: both the stub fact and the
    # empty objective list are named, each anchored to its item index.
    assert "too short" in msg or "minLength" in msg
    assert "['items', 0, 'fact']" in msg and "['items', 0, 'objectives']" in msg


def test_a_duplicate_item_id_raises_through_the_loader(rubric_task):
    spec, repo_root = rubric_task([
        {"item_id": "same", "fact": _FACT_A, "objectives": ["coverage"]},
        {"item_id": "same", "fact": _FACT_B, "objectives": ["coverage"]},
    ])
    with pytest.raises(EvalTypeError, match="duplicate item_id"):
        load_rubric(spec, repo_root=repo_root)


def test_unparseable_yaml_raises(rubric_task):
    spec, repo_root = rubric_task(None, raw_rubric_text="task_id: demo\nitems: [oops\n")
    with pytest.raises(EvalTypeError, match="not readable YAML"):
        load_rubric(spec, repo_root=repo_root)


def test_a_rubric_naming_a_different_task_raises(rubric_task):
    # Rubrics are per-task and not comparable across tasks, so a copy-pasted rubric that kept its
    # source task's id would otherwise score one task against another's answer key.
    spec, repo_root = rubric_task(_items(), rubric_task_id="some_other_task")
    with pytest.raises(EvalTypeError, match="some_other_task"):
        load_rubric(spec, repo_root=repo_root)


# --- the objective mapping -------------------------------------------------------------------

def _rubric(*objective_lists) -> ElicitationRubric:
    return ElicitationRubric(
        task_id="demo_task",
        items=tuple(RubricItem(item_id=f"item_{i}", fact=_FACT_A, objectives=tuple(objs))
                    for i, objs in enumerate(objective_lists)),
        source="in-memory")


def test_validate_rubric_is_clean_when_every_objective_exists():
    rubric = _rubric(["classification_correctness"], ["coverage", "classification_correctness"])
    assert validate_rubric(rubric, ["classification_correctness", "coverage"]) == []


def test_validate_rubric_names_the_item_and_the_unknown_objective():
    rubric = _rubric(["not_a_real_objective"])
    (problem,) = validate_rubric(rubric, ["classification_correctness", "coverage"])
    assert "item_0" in problem
    assert "not_a_real_objective" in problem
    assert "classification_correctness" in problem  # the known set, so the fix is obvious


def test_one_good_and_one_bad_reference_is_exactly_one_problem():
    rubric = _rubric(["coverage", "not_a_real_objective"])
    (problem,) = validate_rubric(rubric, ["coverage"])
    # One line for the item, listing only the objective that is actually unknown.
    named = problem.split("does not score")[0]
    assert "not_a_real_objective" in named and "coverage" not in named


def test_one_problem_per_offending_item():
    rubric = _rubric(["nope_one"], ["coverage"], ["nope_two"])
    problems = validate_rubric(rubric, ["coverage"])
    assert len(problems) == 2
    assert "item_0" in problems[0] and "item_2" in problems[1]


def test_objectives_may_be_given_as_the_spec_dicts_or_as_names(rubric_task):
    spec, repo_root = rubric_task(_items())
    rubric = load_rubric(spec, repo_root=repo_root)
    # Both callers exist: a task test has the names, the eval phase has evaluation_data's objectives.
    assert validate_rubric(rubric, spec["evaluation_data"]["objectives"]) == []
    assert validate_rubric(rubric, ["classification_correctness", "coverage"]) == []


def test_a_task_with_no_objectives_flags_every_item():
    rubric = _rubric(["coverage"], ["classification_correctness"])
    assert len(validate_rubric(rubric, [])) == 2


def test_objectives_may_also_be_resolved_objective_records():
    """The shape the eval phase actually holds. It was missing here at first, and the consequence was
    not a crash but a scored diagnostic silently reported as `unmeasurable` -- which is why all three
    shapes are pinned rather than just the two a task test would use."""
    from appsci_bench.eval.datatypes import Objective

    objectives = [Objective(name=n, eval_function_path=f"x.{n}", positive=True, weight=1.0)
                  for n in ("coverage", "classification_correctness")]
    assert validate_rubric(_rubric(["coverage"]), objectives) == []
    (problem,) = validate_rubric(_rubric(["nope"]), objectives)
    assert "nope" in problem and "coverage" in problem      # the known set is still listed


def test_an_objective_of_an_unexpected_shape_is_ignored_rather_than_crashing():
    # A None or a number in the list is a caller bug, not a reason to lose the diagnostic.
    problems = validate_rubric(_rubric(["coverage"]), ["coverage", None, 7, object()])
    assert problems == []
