"""The template's elicitation-rubric stub.

A stub that does not validate is worse than no stub: it is the first thing a new task designer copies, so
whatever shape it has becomes the shape of the next rubric. These tests hold it to the schema and to the
two things the examples exist to demonstrate -- an atomic item and a bundled one that states its own
completeness.

The template is deliberately NOT a loadable task (its spec.yaml carries `<placeholder>` values), so this
checks the file directly rather than going through `load_rubric`.
"""
from __future__ import annotations

import pathlib

import yaml

REPO = pathlib.Path(__file__).resolve().parents[3]
STUB = REPO / "tasks" / "_TEMPLATE" / "elicitation_rubric.yaml"


def _stub() -> dict:
    return yaml.safe_load(STUB.read_text())


def test_the_stub_exists_where_the_docs_say_it_does():
    assert STUB.is_file(), f"tasks/README.md points at {STUB.name} and it is not there"
    assert "Ship an elicitation rubric" in (REPO / "tasks" / "README.md").read_text()
    assert "_TEMPLATE/elicitation_rubric.yaml" in (REPO / "tasks" / "README.md").read_text()


def test_the_stub_validates_against_the_schema():
    """The whole point. A stub that fails validation teaches the wrong shape to everyone who copies it."""
    from appsci_bench.eval.elicitation_rubric import validate_rubric_document

    assert validate_rubric_document(_stub()) == []


def test_the_stub_shows_both_an_atomic_and_a_bundled_item():
    # Bundling is the part authors get wrong, and they get it wrong by not stating a boundary. An example
    # of each shape is why this file is two items rather than one.
    items = _stub()["items"]
    assert len(items) == 2, "the stub demonstrates one atomic item and one bundle"
    atomic, bundled = items
    assert len(atomic["fact"]) < len(bundled["fact"])
    assert "exactly" in atomic["fact"] or "three" in atomic["fact"]


def test_the_bundled_example_states_its_own_completeness():
    _, bundled = _stub()["items"]
    assert "exactly four" in bundled["fact"]
    assert "Any single condition is enough" in bundled["fact"]
    assert bundled["judge_hint"] and "four" in bundled["judge_hint"]


def test_the_stub_carries_concrete_values_rather_than_descriptions_of_them():
    """A fact is what the judge compares an answer against, so the truth has to be in it. A stub full of
    "the status values" would teach authors to write labels."""
    atomic, _ = _stub()["items"]
    for value in ("ACCEPTED", "NEEDS_REVIEW", "REJECTED"):
        assert value in atomic["fact"]


def test_the_stub_points_at_the_design_doc_and_not_at_a_local_plan():
    text = STUB.read_text()
    assert "docs/elicitation_score_design.md" in text
    assert "schema/elicitation_rubric.schema.json" in text
    assert "tasks/README.md" in text
    # docs_local/ is gitignored, so a reference to it would be a dead link for everyone else.
    assert "docs_local" not in text


def test_the_stub_warns_about_the_two_things_that_break_a_rubric():
    text = STUB.read_text().lower()
    assert "never under" in text and "environment/" in text     # where it must not live
    assert "not from a run" in text or "never from a conversation" in text or "transcript" in text


def test_the_stub_is_not_wired_into_the_template_spec():
    """The template's spec must stay copyable without a dangling pointer -- the stub is opt-in, and a task
    that declares a rubric it does not have raises rather than degrading to "no rubric"."""
    spec = yaml.safe_load((REPO / "tasks" / "_TEMPLATE" / "spec.yaml").read_text())
    assert "elicitation_rubric" not in (spec.get("evaluation_data") or {})
