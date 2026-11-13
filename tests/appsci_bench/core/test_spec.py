"""Task-spec schema tests.

Guards `_TEMPLATE/spec.yaml` -- the reference new-task authors copy -- against schema regressions,
and does the same for each task's real spec.yaml: the 3 tasks with real eval code get a full
dataset+objectives end-to-end load test on top of schema validation; the remaining tasks
(spec-shape-migrated only, no eval.py yet) get at least a schema-validates test so a spec regression
doesn't go unnoticed before their eval code lands.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from appsci_bench.core.spec import TASKS_DIR, validate_spec
from appsci_bench.eval.dataset import load_dataset
from appsci_bench.eval.objectives import load_objectives

_TEMPLATE_SPEC = TASKS_DIR / "_TEMPLATE" / "spec.yaml"


def _load(path):
    return yaml.safe_load(path.read_text())


def test_template_spec_validates_against_the_schema():
    errs = validate_spec(_load(_TEMPLATE_SPEC), strict=True)
    assert errs == []


def test_template_eval_data_is_a_directory_of_per_sample_subdirectories():
    dataset_dir = TASKS_DIR.parent / _load(_TEMPLATE_SPEC)["evaluation_data"]["dataset"].replace(
        "<task_id>", "_TEMPLATE")
    assert dataset_dir.is_dir()
    sample_dirs = [p for p in dataset_dir.iterdir() if p.is_dir()]
    assert sample_dirs, "expected at least one per-sample subdirectory"
    for d in sample_dirs:
        assert (d / "sample.json").is_file()


def test_order_to_cash_spec_validates_against_the_schema():
    spec_path = TASKS_DIR / "order_to_cash" / "spec.yaml"
    errs = validate_spec(_load(spec_path), strict=True)
    assert errs == []


def test_order_to_cash_dataset_and_objectives_load_end_to_end():
    spec = _load(TASKS_DIR / "order_to_cash" / "spec.yaml")
    dataset = load_dataset(spec)
    assert dataset.type == "single_turn"
    # 60 = the generator's full run. Ten of these were previously carved out and mounted for the
    # agent as labeled examples at /data/examples.jsonl; they are held out and scored now, so the
    # agent gets no labeled data (see eval_data/README.md).
    assert len(dataset.data) == 60
    assert len({s.data_point_id for s in dataset.data}) == 60  # no duplicate ids

    objectives = load_objectives(spec)
    assert {o.name for o in objectives} == {
        "flag_accuracy", "flag_recall", "fault_classification", "reason_quality",
        "safety_no_fabrication", "safety_safe_by_construction"}
    assert all(o.eval_function is not None for o in objectives)


def test_slides_generator_spec_validates_against_the_schema():
    spec_path = TASKS_DIR / "slides_generator" / "spec.yaml"
    errs = validate_spec(_load(spec_path), strict=True)
    assert errs == []


def test_slides_generator_objectives_all_resolve():
    spec = _load(TASKS_DIR / "slides_generator" / "spec.yaml")
    objectives = load_objectives(spec)
    # Asserting the name SET rather than a bare count: a count alone passes if one objective is
    # swapped for another, and this task's objective set changed once already (figure_relevance and
    # layout_color_quality removed, outcome_coverage renamed to prompt_satisfaction).
    assert {o.name for o in objectives} == {
        "functional_success", "output_format_compliance", "constraint_satisfaction",
        "groundedness", "expectations_satisfaction", "content_coverage", "prompt_satisfaction",
        "structure_clarity", "content_narrative", "template_adherence", "layout_quality",
        "safety_safe_by_construction"}
    assert all(o.eval_function is not None for o in objectives)


def test_slides_generator_product_samples_load_end_to_end():
    # Positive end-to-end load. The 5 product-slidesgen-* samples are vendored in the repo via Git
    # LFS (see eval_data/README.md), so this normally just runs; the skip only guards a checkout
    # where the LFS objects weren't pulled. eval_data/ also holds 5 generated-sample-* samples
    # (synthetic data, see GENERATION_SPEC.md). Confirms all 10 samples parse, materialize their
    # attachments, and carry a presentation deck in expected_output.json.
    spec = _load(TASKS_DIR / "slides_generator" / "spec.yaml")
    try:
        dataset = load_dataset(spec)
    except Exception as e:                                  # LFS objects not pulled in this checkout
        pytest.skip(f"product sample files not present ({e.__class__.__name__}); run "
                    "git lfs pull to enable this test")

    assert {s.data_point_id for s in dataset.data} == (
        {f"product-slidesgen-0{i}" for i in range(1, 6)}
        | {f"generated-sample-0{i}" for i in range(5)})
    for s in dataset.data:
        for f in (s.single_turn_input.files if s.single_turn_input else []):
            f.materialize()
            assert f.file_data                              # real attachment bytes, not empty
        assert json.loads(s.expected_output.json)["presentation"]["slides"]  # gold reference deck


def test_slides_generator_synthetic_sample_loads_end_to_end(tmp_path):
    # Fully offline regression test for this task's CURRENT sample.json shape -- unlike
    # test_slides_generator_product_samples_load_end_to_end above, this builds its own sample and
    # never skips: it doesn't depend on the LFS-vendored product binaries, only on the dataset
    # schema itself (single_turn_input files + expected_output.json's presentation/user_constraints
    # shape + a gold.pptx in expected_output.files). Catches a load_dataset/File.materialize()
    # regression for this task even in a checkout where LFS objects weren't pulled.
    sample_dir = tmp_path / "synthetic-slidesgen-01"
    sample_dir.mkdir()
    (sample_dir / "financials.csv").write_bytes(b"metric,value\nrevenue,100")
    (sample_dir / "template.pptx").write_bytes(b"fake template pptx bytes")
    (sample_dir / "gold.pptx").write_bytes(b"fake gold pptx bytes")

    expected_output_json = json.dumps({
        "presentation": {"slides": [{"slideNumber": 1, "layout": "title_only", "elements": []}]},
        "user_constraints": {"expected_slide_count_range": [1, 3]},
    })
    sample = {
        "data_point_id": "synthetic-slidesgen-01",
        "single_turn_input": {
            "text": "Turn the attached financials into a deck.",
            "files": [
                {"file_type": "csv", "file_name": "financials.csv", "file_path": "financials.csv"},
                {"file_type": "pptx", "file_name": "template.pptx", "file_path": "template.pptx"},
            ],
        },
        "expected_output": {
            "json": expected_output_json,
            "files": [{"file_type": "pptx", "file_name": "gold.pptx", "file_path": "gold.pptx"}],
        },
    }
    (sample_dir / "sample.json").write_text(json.dumps(sample))

    spec = {"evaluation_data": {"type": "single_turn", "dataset": str(tmp_path)}}
    dataset = load_dataset(spec)

    assert [s.data_point_id for s in dataset.data] == ["synthetic-slidesgen-01"]
    s = dataset.data[0]
    for f in s.single_turn_input.files:
        f.materialize()
        assert f.file_data
    for f in s.expected_output.files:
        f.materialize()
        assert f.file_data
    parsed = json.loads(s.expected_output.json)
    assert parsed["presentation"]["slides"]
    assert parsed["user_constraints"]["expected_slide_count_range"] == [1, 3]


def test_airline_rebooking_spec_validates_against_the_schema():
    spec_path = TASKS_DIR / "airline_rebooking" / "spec.yaml"
    errs = validate_spec(_load(spec_path), strict=True)
    assert errs == []


def test_airline_rebooking_dataset_and_objectives_load_end_to_end():
    spec = _load(TASKS_DIR / "airline_rebooking" / "spec.yaml")
    dataset = load_dataset(spec)
    assert dataset.type == "multi_turn"
    assert len(dataset.data) == 50
    assert len({s.data_point_id for s in dataset.data}) == 50  # no duplicate ids
    assert all(s.multi_turn_input is not None for s in dataset.data)

    objectives = load_objectives(spec)
    assert {o.name for o in objectives} == {
        "task_success", "policy_compliance",
        "safety_no_fabrication", "safety_safe_by_construction"}
    assert all(o.eval_function is not None for o in objectives)


def test_customer_call_insights_spec_validates_against_the_schema():
    spec_path = TASKS_DIR / "customer_call_insights" / "spec.yaml"
    errs = validate_spec(_load(spec_path), strict=True)
    assert errs == []


def test_customer_call_insights_dataset_and_objectives_load_end_to_end():
    spec = _load(TASKS_DIR / "customer_call_insights" / "spec.yaml")
    dataset = load_dataset(spec)
    assert dataset.type == "single_turn"
    assert len({s.data_point_id for s in dataset.data}) == len(dataset.data)  # no duplicate ids
    assert all(s.single_turn_input is not None for s in dataset.data)
    # Every sample attaches its call transcript by file_path; the loader resolves (but does not read)
    # it, so a renamed or missing transcript fails here rather than mid-eval.
    for s in dataset.data:
        assert s.single_turn_input.files, f"{s.data_point_id}: no transcript attached"
        for f in s.single_turn_input.files:
            f.materialize()
            assert f.file_data

    # At least one sample must have all three list labels empty -- that is the anti-fabrication case,
    # and the only place the deterministic empty-vs-empty path is exercised end to end.
    oracles = [json.loads(s.expected_output.json) for s in dataset.data]
    assert any(not o["next_steps"] and not o["risks"] and not o["commitments"] for o in oracles), (
        "dataset needs at least one all-empty-label sample (see eval_data/README.md)")

    objectives = load_objectives(spec)
    assert {o.name for o in objectives} == {
        "next_steps_f1", "risks_f1", "commitments_f1", "sentiment_accuracy",
        "safety_no_fabrication", "safety_safe_by_construction"}
    assert all(o.eval_function is not None for o in objectives)
    assert sum(o.weight for o in objectives) == pytest.approx(1.0)


def test_banking_support_scores_only_on_task_success():
    # Default evaluation skips the disabled diagnostics, while the two always-on safety axes
    # (report-only, weight 0.0 -- see tasks/banking_support/eval_safety.py) still resolve, same as
    # every other task. An explicit opt-in additionally resolves the disabled diagnostics too.
    spec = _load(TASKS_DIR / "banking_support" / "spec.yaml")
    objectives = load_objectives(spec)
    assert [o.name for o in objectives] == [
        "task_success", "safety_no_fabrication", "safety_safe_by_construction"]
    assert callable(objectives[0].eval_function)
    objectives = load_objectives(spec, include_disabled=True)
    assert {o.name for o in objectives} == {
        "task_success", "groundedness", "retrieval_recall",
        "verification_compliance", "no_fabrication",
        "safety_no_fabrication", "safety_safe_by_construction",
    }
    assert all(o.eval_function is not None for o in objectives)
    weights = {o.name: o.weight for o in objectives}
    assert weights["task_success"] == pytest.approx(1.0)
    assert sum(o.weight for o in objectives) == pytest.approx(1.0)
    assert all(
        w == pytest.approx(0.0) for name, w in weights.items() if name != "task_success"
    ), f"only task_success may carry weight on banking; got {weights}"


# --- shape-only-migrated tasks: schema validity only (no eval.py/eval_data yet) -------------------
# These tasks got their spec.yaml moved to the new objectives shape but have no eval_function
# implementations or dataset on disk yet, so load_dataset/load_objectives would fail for reasons
# unrelated to spec correctness. A schema-validates test still guards against a spec regression
# (e.g. someone reintroducing `metrics`/`rubrics`) before their eval code lands.
@pytest.mark.parametrize("task_id", [
    "crm_insights", "compliance_review",
])
def test_shape_only_task_spec_validates_against_the_schema(task_id):
    spec_path = TASKS_DIR / task_id / "spec.yaml"
    errs = validate_spec(_load(spec_path), strict=True)
    assert errs == []


# --- the carve block: a coverage waiver, and nothing else -----------------------------------------
# Category coverage is the one carve invariant a strict carve-out can be unable to satisfy, so a task
# may record a decision to accept the gap (scripts/carve_guard.py reads it). The schema is what stops
# that becoming a way to switch off the other two: it accepts a coverage waiver with a substantive
# reason AND the specific enumerations it accepts, and rejects everything else -- including any attempt
# to name disjointness or closure.
#
# Both halves of a waiver are required by the schema, and each catches a different way of writing a
# waiver that does not mean anything: a list with no reason is undocumented, and a reason with no list
# excuses whatever coverage happens to be missing for as long as the block survives.

_GOOD_WAIVER = {"coverage_waiver": {
    "reason": ("Strict carve, no synthesized records: withholding every record an eval case touches, "
               "closed over the reference graph, leaves 8 of 768 records."),
    "enumerations": ["credit_card_accounts.status", "users.membership_level"]}}


def _with_carve(task_id: str, carve: dict) -> dict:
    spec = _load(TASKS_DIR / task_id / "spec.yaml")
    spec["task_runner"]["carve"] = carve
    return spec


def test_a_coverage_waiver_with_a_substantive_reason_validates():
    assert validate_spec(_with_carve("banking_support", _GOOD_WAIVER), strict=True) == []


@pytest.mark.parametrize("carve, why", [
    ({"coverage_waiver": {}}, "no reason at all"),
    ({"coverage_waiver": {"reason": "n/a"}}, "a reason too short to be one"),
    ({"coverage_waiver": {"reason": "x" * 60, "approved": True}}, "an undeclared extra key"),
    # A reason with no list is the shape this schema most needs to reject: it reads like a waiver and
    # would excuse every coverage gap the task ever grows, including one nobody meant to accept.
    ({"coverage_waiver": {"reason": "x" * 60}}, "a reason naming no enumerations"),
    ({"coverage_waiver": {"reason": "x" * 60, "enumerations": []}}, "an empty list of enumerations"),
    ({"coverage_waiver": {"enumerations": ["users.tier"]}}, "a list with no reason"),
    # `collection.field` is the form the guard reports a gap in, and the only form it matches. A bare
    # field name does not identify a gap -- two collections can both have a `status`.
    ({"coverage_waiver": {"reason": "x" * 60, "enumerations": ["tier"]}}, "a bare field name"),
    ({"coverage_waiver": {"reason": "x" * 60, "enumerations": ["users."]}}, "a collection and no field"),
    ({"coverage_waiver": {"reason": "x" * 60, "enumerations": ["users.tier", "users.tier"]}},
     "the same enumeration twice"),
    # The point of the whole design: the threat bar and the schema-integrity bar are not waivable, and
    # a spec cannot express the wish. If either name ever validates here, the waiver has become a
    # general off switch.
    ({"closure_waiver": {"reason": "x" * 60}}, "an attempt to waive referential closure"),
    ({"disjointness_waiver": {"reason": "x" * 60}}, "an attempt to waive disjointness"),
])
def test_the_carve_block_rejects_everything_but_a_coverage_waiver(carve, why):
    assert validate_spec(_with_carve("banking_support", carve), strict=True), f"accepted {why}"


def test_banking_is_the_only_task_that_declares_a_carve_waiver():
    """A waiver is a recorded decision, so a SECOND one appearing unnoticed is the failure mode.

    This test used to pin that no task declared one at all. Banking's carve is the case the vocabulary was
    built for and now declares the first: 15 of its 17 collections are empty in its build share, so no
    choice of records covers one of every category. That is a decision somebody made and wrote down, which
    is why it is named here rather than counted.

    If this starts failing because another task declared one, read the reason that task wrote and decide
    whether you agree with it -- a waiver is cheap to add and expensive to notice later. What the checker
    accepts is tested above; what it MEASURES on banking's real data, including that the list names exactly
    the gaps and no more, is in tests/appsci_bench/core/test_carve_guards.py.
    """
    waived = {t: (_load(TASKS_DIR / t / "spec.yaml").get("task_runner") or {}).get("carve")
              for t in sorted(p.name for p in TASKS_DIR.iterdir()
                              if p.is_dir() and (p / "spec.yaml").exists())}
    assert {t for t, c in waived.items() if c} == {"banking_support"}, \
        f"the set of tasks declaring a carve waiver changed: {sorted(t for t, c in waived.items() if c)}"

    waiver = waived["banking_support"]["coverage_waiver"]
    assert validate_spec(_load(TASKS_DIR / "banking_support" / "spec.yaml"), strict=True) == []
    # a reason long enough to be one, and a list long enough to be doing work rather than decorating
    assert len(waiver["reason"]) > 200, "banking's waiver reason is too short to be a recorded decision"
    assert len(waiver["enumerations"]) == 8, (
        f"banking names {len(waiver['enumerations'])} enumerations, not the 8 its build store leaves "
        f"uncovered. It was 18 before the store gained authored synthetic records, which filled every "
        f"empty collection; the 8 left are the ones only the eval customers' own identities could cover")
    assert waiver["enumerations"] == sorted(waiver["enumerations"]), (
        "keep the list sorted, so a regenerated waiver diffs minimally against the one it replaces")
    # the whole point of the list: no wildcard, no bare collection name
    assert all(e.count(".") == 1 and not e.endswith(("*", ".")) for e in waiver["enumerations"]), \
        "every entry must name one collection.field -- a wildcard would absorb a future gap unnoticed"
