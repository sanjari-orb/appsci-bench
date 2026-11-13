"""File-level validation of an elicitation rubric (schema/elicitation_rubric.schema.json).

A rubric is answer-key material: the facts the agent was supposed to extract from the build-time
customer simulator, with the true answers written out. These tests pin the rules that can be checked
by looking at one file, and they are deliberately split by which layer owns each rule --

  - the schema owns shape, the 50-item cap, the `fact` length floor, the `item_id` character set, and
    closedness (a typo'd key must fail, not vanish);
  - the loader owns `item_id` uniqueness, because JSON Schema has no keyword for "unique by property".

Everything else in the six authoring rules -- criticality, phrasing an item so no agent-facing file
could satisfy it, and writing it from the source rather than from a run -- is human review. No schema can check them, and pretending otherwise would be
worse than leaving them to the reviewer. See docs/elicitation_score_design.md.
"""
from __future__ import annotations

import json
import pathlib

import jsonschema
import pytest

from appsci_bench.eval.elicitation_rubric import (
    MAX_ITEMS,
    RUBRIC_SCHEMA_PATH,
    validate_rubric_document,
)

SCHEMA = json.loads(RUBRIC_SCHEMA_PATH.read_text())

# A fact long enough to clear the minLength floor, which exists so a bare label cannot pass as a fact.
_FACT = "The six fault_type values, spelled exactly: PRICE_DELTA, QTY_MISMATCH, TOTAL_MATH."


def _item(**overrides) -> dict:
    base = {"item_id": "fault_taxonomy", "fact": _FACT, "objectives": ["classification_correctness"]}
    base.update(overrides)
    return base


def _rubric(**overrides) -> dict:
    base = {"task_id": "order_to_cash", "items": [_item()]}
    base.update(overrides)
    return base


def _schema_errors(doc: dict) -> list[str]:
    return [e.message for e in jsonschema.Draft202012Validator(SCHEMA).iter_errors(doc)]


# --- accepted shapes -------------------------------------------------------------------------

def test_minimal_rubric_validates():
    assert _schema_errors(_rubric()) == []
    assert validate_rubric_document(_rubric()) == []


def test_judge_hint_is_optional_and_accepted():
    hint = "all six values, spelled as written; a paraphrase of the categories is not this item"
    assert _schema_errors(_rubric(items=[_item(judge_hint=hint)])) == []


def test_an_item_may_feed_several_objectives():
    assert _schema_errors(_rubric(items=[_item(objectives=["a_metric", "b_metric"])])) == []


def test_schema_path_exists_and_is_the_2020_12_dialect():
    assert pathlib.Path(RUBRIC_SCHEMA_PATH).is_file()
    assert SCHEMA["$schema"].endswith("2020-12/schema")


# --- the cap ---------------------------------------------------------------------------------

def test_item_cap_accepts_the_limit_and_rejects_one_more():
    at_cap = [_item(item_id=f"item_{i}") for i in range(MAX_ITEMS)]
    assert _schema_errors(_rubric(items=at_cap)) == []
    assert _schema_errors(_rubric(items=at_cap + [_item(item_id="one_too_many")]))


def test_max_items_constant_matches_the_schema():
    # Two sources of truth for the cap would drift; the constant exists so task tests need not
    # hardcode 50, so pin it to the schema rather than to a literal.
    assert SCHEMA["properties"]["items"]["maxItems"] == MAX_ITEMS


def test_an_empty_item_list_is_rejected():
    assert _schema_errors(_rubric(items=[]))


# --- rejected shapes ------------------------------------------------------------------------

def test_duplicate_item_id_is_rejected():
    # Owned by the loader, not the schema: a per-item verdict is tracked by id across runs, so two
    # items sharing one id would silently overwrite each other in every report.
    doc = _rubric(items=[_item(item_id="same"), _item(item_id="same", fact=_FACT + " Also DUPLICATE.")])
    problems = validate_rubric_document(doc)
    assert any("duplicate item_id" in p and "same" in p for p in problems)


def test_duplicate_item_id_is_reported_once_per_id():
    doc = _rubric(items=[_item(item_id="same"), _item(item_id="same"), _item(item_id="same")])
    assert len([p for p in validate_rubric_document(doc) if "duplicate item_id" in p]) == 1


def test_empty_objectives_is_rejected():
    # An item no objective scores cannot have been critical -- a perfect artifact is perfect as
    # measured -- so the mapping is required, not advisory.
    assert _schema_errors(_rubric(items=[_item(objectives=[])]))


def test_repeated_objective_in_one_item_is_rejected():
    assert _schema_errors(_rubric(items=[_item(objectives=["same_metric", "same_metric"])]))


def test_a_one_word_fact_is_rejected():
    # The judge decides whether the TRUTH arrived, so the truth has to be in the item. A label is not
    # a fact; the length floor is the mechanical part of that rule.
    assert _schema_errors(_rubric(items=[_item(fact="taxonomy")]))


@pytest.mark.parametrize("typo", ["item_ids", "objective", "facts", "judge_hints"])
def test_a_typod_item_key_is_rejected_not_ignored(typo):
    item = _item()
    item[typo] = item.pop({"item_ids": "item_id", "objective": "objectives",
                           "facts": "fact", "judge_hints": "judge_hint"}[typo], "x")
    assert _schema_errors(_rubric(items=[item]))


def test_a_typod_top_level_key_is_rejected():
    assert _schema_errors({"task": "order_to_cash", "items": [_item()]})
    assert _schema_errors(_rubric(item=[_item()]))


@pytest.mark.parametrize("bad_id", ["fault-taxonomy", "FaultTaxonomy", "fault taxonomy", "fault.id", ""])
def test_item_id_character_set_is_enforced(bad_id):
    # item_id is used as a dict key in reports and as a filename-safe label, so it stays lowercase
    # snake_case rather than anything a shell or a JSON path would have to quote.
    assert _schema_errors(_rubric(items=[_item(item_id=bad_id)]))


@pytest.mark.parametrize("doc", [
    {"items": [_item()]},                                      # no task_id
    {"task_id": "order_to_cash"},                              # no items
    {"task_id": "", "items": [_item()]},                       # empty task_id
    {"task_id": "order_to_cash", "items": _item()},            # items not a list
    {"task_id": "order_to_cash", "items": ["a string"]},       # item not an object
])
def test_structurally_broken_documents_are_rejected(doc):
    assert _schema_errors(doc)


def test_validate_rubric_document_survives_a_document_the_schema_rejected():
    # It reports both layers' problems in one list, so it must not blow up on a shape the schema has
    # already condemned.
    for doc in ({"items": "not a list"}, {}, None, [], {"items": [None, 3]}):
        assert isinstance(validate_rubric_document(doc), list)
