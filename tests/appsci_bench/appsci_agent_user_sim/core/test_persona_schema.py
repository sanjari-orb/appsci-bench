"""Schema tests for the persona `known_docs` field (issue #76, phase A1).

`known_docs` names sim-only documents whose full content the persona has internalized. The paths are
relative to the task's `user_sim_docs/` directory -- a tree mounted ONLY into the user-sim container --
so the field's schema is the first line of defense on two things:

  - an entry must not escape that root (no absolute path, no `..` segment), because the resolver will
    load whatever it resolves to and inject it into the prompt;
  - the field must not silently absorb typos: `known_doc` (singular) has to fail, not vanish.

Everything under `user_sim_docs/common/` is auto-included and is deliberately NOT listed here, so a
persona declaring nothing is legal.

See docs_local/09_usersim_file_access_design.md (D3, D5) for the design these tests pin.
"""
import json
import pathlib

import jsonschema
import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[4]
SCHEMA = json.loads((REPO / "schema" / "user_persona.schema.json").read_text())
# Real task personas only. `tasks/_TEMPLATE/` holds an intentionally-nonconforming scaffold with
# `<placeholder>` values, which is why `load_user_persona` takes `validate=False` (persona.py:124-135).
ALL_PERSONAS = sorted(p for p in REPO.glob("tasks/*/user_personas/*.yaml")
                      if p.parent.parent.name != "_TEMPLATE")


def _persona(**overrides) -> dict:
    """A minimal schema-valid persona, plus overrides."""
    base = {"persona_id": "l1", "task_id": "demo", "level": "L1"}
    base.update(overrides)
    return base


def _errors(persona: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(SCHEMA)
    return [e.message for e in validator.iter_errors(persona)]


# --- accepted shapes -------------------------------------------------------------------------

@pytest.mark.parametrize("docs", [
    ["policy.md"],                          # the common case: one level-scoped doc
    ["sub/dir/doc.md"],                     # nesting under the docs root is allowed
    ["policy.md", "escalation_rules.txt"],  # several docs
    ["tools_reference.json"],               # .json matters: airline's tool reference is JSON
    [],                                     # explicit empty == only common/ applies
])
def test_valid_known_docs_accepted(docs):
    assert _errors(_persona(known_docs=docs)) == []


def test_absent_known_docs_is_valid():
    """Backward compatibility: every persona predating this field must still validate."""
    assert _errors(_persona()) == []


# --- rejected shapes -------------------------------------------------------------------------

@pytest.mark.parametrize("bad,why", [
    ("/sop/policy.md", "absolute path would escape the docs root and name an agent mount"),
    ("../TASK_SOP.md", "parent traversal would reach the ground-truth tree"),
    ("a/../../b.md", "traversal mid-path escapes just as effectively"),
    ("..", "bare parent segment"),
    ("a/..", "trailing parent segment"),
    ("sub\\win.md", "backslash is not a POSIX separator; would resolve as a filename"),
    ("", "empty path"),
])
def test_escaping_or_malformed_paths_rejected(bad, why):
    assert _errors(_persona(known_docs=[bad])), f"should reject {bad!r}: {why}"


def test_non_string_entry_rejected():
    assert _errors(_persona(known_docs=[3]))


def test_non_array_rejected():
    assert _errors(_persona(known_docs="policy.md"))


def test_singular_typo_rejected():
    """`additionalProperties: false` must catch `known_doc` -- a typo that would otherwise leave the
    persona silently without its documents, which is the exact silent degradation this work removes."""
    assert _errors(_persona(known_doc=["policy.md"]))


def test_one_bad_entry_invalidates_the_list():
    """A good entry must not mask a bad sibling."""
    assert _errors(_persona(known_docs=["policy.md", "../secret.md"]))


# --- the committed personas ------------------------------------------------------------------

def test_personas_found():
    assert ALL_PERSONAS, "no persona files found"


@pytest.mark.parametrize("path", ALL_PERSONAS,
                         ids=[f"{p.parent.parent.name}/{p.stem}" for p in ALL_PERSONAS])
def test_committed_persona_still_validates(path: pathlib.Path):
    """Adding an optional field must not invalidate anything already in the repo."""
    jsonschema.Draft202012Validator(SCHEMA).validate(yaml.safe_load(path.read_text()))
