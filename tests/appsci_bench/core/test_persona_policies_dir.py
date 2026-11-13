"""Resolving and validating `tasks/<id>/user_personas/domain_knowledge/`.

The second source of stakeholder knowledge, and the one that did not exist as a file until task
owners extracted it. `user_sim_docs/` holds what a persona was handed whole; everything else a
persona knows is written inline in its YAML as `domain_knowledge`, which nothing can mount. These
extracts are that half, per level, in a subtree that can be bound entire.

Everything here is about the subtree being SEPARATE from the personas beside it. `user_personas/`
holds the persona YAMLs, with the adversarial `mistaken_belief_*` plants and the whole withholding
design in them; the no-user arm mounts this directory into the agent container, so a YAML that ends
up inside it publishes the answer key and the run still comes back with a score.
"""
from __future__ import annotations

import pathlib

from appsci_bench.core.usersim_docs import (
    POLICIES_DIRNAME,
    persona_policies_dir,
    validate_policies_dir,
)


def _task(tmp_path: pathlib.Path, *, with_extracts: bool = True) -> pathlib.Path:
    tasks_dir = tmp_path / "tasks"
    personas = tasks_dir / "demo" / "user_personas"
    personas.mkdir(parents=True)
    (personas / "l3.yaml").write_text("persona_id: l3\ndomain_knowledge:\n  bar: '$500'\n")
    if with_extracts:
        extracts = personas / POLICIES_DIRNAME
        extracts.mkdir()
        (extracts / "l3.md").write_text("# L3\n\nFlag anything over $500.\n")
    return tasks_dir


# --- resolution ---------------------------------------------------------------------------------

def test_resolves_the_extracts_subtree(tmp_path):
    tasks_dir = _task(tmp_path)
    resolved = persona_policies_dir(tasks_dir, "demo")
    assert resolved == str(tasks_dir / "demo" / "user_personas" / POLICIES_DIRNAME)


def test_absent_tree_resolves_to_none(tmp_path):
    """The normal state for a task that has not extracted anything yet. None, not an empty string:
    the caller decides whether that is fatal, and in the control arm it never is."""
    tasks_dir = _task(tmp_path, with_extracts=False)
    assert persona_policies_dir(tasks_dir, "demo") is None


def test_a_file_of_that_name_is_not_a_tree(tmp_path):
    """`l3_domain_knowledge.md` sitting beside the personas -- the shape the first extraction PR
    used -- must not resolve as the directory. Mounting a path that is a file would put a single
    level's extract where the agent is told to find all of them."""
    tasks_dir = _task(tmp_path, with_extracts=False)
    (tasks_dir / "demo" / "user_personas" / POLICIES_DIRNAME).write_text("not a directory\n")
    assert persona_policies_dir(tasks_dir, "demo") is None


def test_resolution_does_not_reach_into_the_personas_themselves(tmp_path):
    """The resolved path is the subtree, never `user_personas/`. Binding the parent would hand the
    agent every persona YAML in the task."""
    tasks_dir = _task(tmp_path)
    resolved = pathlib.Path(persona_policies_dir(tasks_dir, "demo"))
    assert resolved.name == POLICIES_DIRNAME
    assert not any(p.suffix == ".yaml" for p in resolved.rglob("*"))


# --- validation ---------------------------------------------------------------------------------

def test_a_clean_tree_has_no_errors(tmp_path):
    tasks_dir = _task(tmp_path)
    assert validate_policies_dir(persona_policies_dir(tasks_dir, "demo")) == []


def test_a_persona_yaml_inside_the_tree_is_an_error(tmp_path):
    """The leak this guard exists for: the tree is mounted whole and read by the agent."""
    tasks_dir = _task(tmp_path)
    tree = pathlib.Path(persona_policies_dir(tasks_dir, "demo"))
    (tree / "l1.yaml").write_text("persona_id: l1\n")

    errors = validate_policies_dir(tree)

    assert len(errors) == 1
    assert "l1.yaml" in errors[0]
    # The message has to say what to do, not just what is wrong: the author who copied the file in
    # was reaching for the content, and needs to be told to extract it rather than to delete it.
    assert "extract" in errors[0].lower()


def test_the_check_reaches_nested_directories(tmp_path):
    """One level down is exactly where a copied file hides."""
    tasks_dir = _task(tmp_path)
    tree = pathlib.Path(persona_policies_dir(tasks_dir, "demo"))
    (tree / "old").mkdir()
    (tree / "old" / "l2.yml").write_text("persona_id: l2\n")

    errors = validate_policies_dir(tree)

    assert len(errors) == 1
    assert "old/l2.yml" in errors[0]


def test_an_empty_tree_is_valid(tmp_path):
    """Empty is a task owner mid-extraction, not a corruption. The arm's own guard refuses a run
    with nothing to hand over; this function's job is only to catch what must not be mounted."""
    tasks_dir = _task(tmp_path, with_extracts=False)
    tree = tasks_dir / "demo" / "user_personas" / POLICIES_DIRNAME
    tree.mkdir(parents=True)
    assert validate_policies_dir(tree) == []


def test_a_missing_tree_reports_rather_than_raises(tmp_path):
    errors = validate_policies_dir(tmp_path / "nope")
    assert len(errors) == 1
    assert "not a directory" in errors[0]
