"""Load + validate a task's elicitation rubric (``tasks/<id>/elicitation_rubric.yaml``).

The rubric is the per-task list of facts the applied-science agent had to obtain by ASKING the
build-time customer simulator, with the true answers written out. It grounds the standalone
elicitation diagnostic and nothing else -- it never touches the task score. Design:
docs/elicitation_score_design.md; schema: schema/elicitation_rubric.schema.json.

It is ANSWER-KEY material in the same tier as the eval dataset, which is why the pointer lives in the
task spec's ``evaluation_data`` block (dropped wholesale from the agent-facing view) and the file
itself sits at the top level of the task directory rather than under the mounted ``environment/``
tree.

Two validation layers, because one cannot do both jobs:

  - :func:`validate_rubric_document` checks the file in isolation -- shape, the item cap, id
    uniqueness. JSON Schema covers all of it except uniqueness (it has no keyword for "unique by
    property"), so that one check is done here in Python.
  - :func:`validate_rubric` is the cross-file check: every objective an item names has to be a real
    scored objective of the owning task. That is what makes criticality auditable -- a fact no
    objective scores cannot have been critical, because a perfect artifact is perfect as measured.

A declared-but-broken rubric raises rather than degrading to "no rubric": a task that points at a
rubric it does not have is a task bug, and swallowing it would report a full-marks-impossible run as
simply unmeasured.
"""
from __future__ import annotations

import json
import pathlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .datatypes import EvalTypeError

# This file lives at appsci_bench/eval/; parents[2] is the repo root.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
RUBRIC_SCHEMA_PATH = REPO_ROOT / "schema" / "elicitation_rubric.schema.json"

# The hard cap on rubric size, mirroring the schema's maxItems (a test pins the two together). It is a
# reviewability limit, not a technical one: a judged list nobody can hold in their head stops being a
# rubric. Overflowing tasks bundle related facts into one item rather than dropping them.
MAX_ITEMS = 50


@dataclass(frozen=True)
class RubricItem:
    """One rubric entry: a fact (or tightly-related bundle) judged as a single all-or-nothing unit."""

    item_id: str
    fact: str
    objectives: tuple[str, ...]
    judge_hint: str | None = None


@dataclass(frozen=True)
class ElicitationRubric:
    """A task's whole rubric, in file order. ``source`` is the path it was read from."""

    task_id: str
    items: tuple[RubricItem, ...]
    source: str

    def __len__(self) -> int:
        return len(self.items)


def validate_rubric_document(data: Any) -> list[str]:
    """Return human-readable problems with a rubric document (empty = clean).

    Runs the JSON Schema, then the one rule the schema cannot express: ``item_id`` unique across the
    file. Absence of ``jsonschema`` is a dev-time-guard miss, not a hard failure (mirrors
    ``appsci_bench.core.spec.validate_spec``); the uniqueness check is pure Python and always runs.
    """
    problems: list[str] = []
    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # pragma: no cover - the dep is present in dev/CI
        pass
    else:
        schema = json.loads(RUBRIC_SCHEMA_PATH.read_text())
        errs = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
        problems += [f"{list(e.path)}: {e.message}" for e in errs]

    problems += _duplicate_item_ids(data)
    return problems


def _duplicate_item_ids(data: Any) -> list[str]:
    """Duplicate `item_id`s, reported once each. Defensive about shape: the schema owns that, and
    this has to stay callable on a document the schema already rejected."""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return []
    seen: set[str] = set()
    dupes: list[str] = []
    for item in data["items"]:
        if not isinstance(item, dict):
            continue
        item_id = item.get("item_id")
        if not isinstance(item_id, str):
            continue
        if item_id in seen and item_id not in dupes:
            dupes.append(item_id)
        seen.add(item_id)
    return [f"duplicate item_id {d!r}: item ids must be unique within a rubric" for d in dupes]


def load_rubric(spec: Any, *, repo_root: pathlib.Path | None = None) -> ElicitationRubric | None:
    """Load a task's rubric from its spec, or ``None`` if the spec declares no rubric.

    Accepts a ``TaskSpec`` or a plain dict with an ``evaluation_data`` block, and resolves the
    pointer exactly as ``load_dataset`` resolves ``dataset``: repo-root-relative unless absolute,
    with the same ``repo_root`` override for tests.

    Raises ``EvalTypeError`` when the pointer is set but the file is missing, unparseable, fails
    validation, or names a different task than the spec -- see the module docstring on why a
    declared-but-broken rubric is not a silent skip.
    """
    pointer = _evaluation_data(spec).get("elicitation_rubric")
    if not pointer:
        return None

    if repo_root is None:
        from ..core.spec import REPO_ROOT as SPEC_REPO_ROOT
        repo_root = SPEC_REPO_ROOT
    path = pathlib.Path(pointer)
    if not path.is_absolute():
        path = repo_root / path
    if not path.is_file():
        raise EvalTypeError(f"elicitation rubric not found: {path}")

    import yaml
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise EvalTypeError(f"elicitation rubric {path} is not readable YAML: {e}") from e

    problems = validate_rubric_document(data)
    if problems:
        raise EvalTypeError(f"elicitation rubric {path} invalid:\n  " + "\n  ".join(problems))

    spec_task_id = _spec_task_id(spec)
    if spec_task_id and data["task_id"] != spec_task_id:
        raise EvalTypeError(
            f"elicitation rubric {path} declares task_id {data['task_id']!r} but belongs to task "
            f"{spec_task_id!r} -- rubrics are per-task and not comparable across tasks")

    return ElicitationRubric(
        task_id=data["task_id"],
        items=tuple(
            RubricItem(
                item_id=item["item_id"],
                fact=item["fact"],
                objectives=tuple(item["objectives"]),
                judge_hint=item.get("judge_hint"),
            )
            for item in data["items"]
        ),
        source=str(path),
    )


def validate_rubric(rubric: ElicitationRubric, objectives: Iterable[Any]) -> list[str]:
    """Return problems with a rubric's objective references (empty = clean).

    ``objectives`` is the task's scored objectives in any of the three shapes a caller actually has:
    plain names (a task test), the raw dicts from ``evaluation_data.objectives`` (a spec), or resolved
    ``Objective`` records (the eval phase). Accepting all three is not generosity -- the eval phase
    passes the third, and rejecting it turned a scored diagnostic into `unmeasurable`.

    One problem per offending item, naming every unknown objective it references, so an item with one
    good and one bad reference produces exactly one line.

    Returned rather than raised so the caller decides: a task test fails on it, a scoring run warns.
    """
    known = {_objective_name(o) for o in objectives}
    known.discard(None)
    problems: list[str] = []
    for item in rubric.items:
        unknown = [o for o in item.objectives if o not in known]
        if unknown:
            problems.append(
                f"item {item.item_id!r} references objective(s) {unknown} that task "
                f"{rubric.task_id!r} does not score (known: {sorted(known)})")
    return problems


def _objective_name(objective: Any) -> str | None:
    """An objective's name, whether it arrived as a name, a spec dict, or a resolved record."""
    if isinstance(objective, str):
        return objective
    if isinstance(objective, dict):
        name = objective.get("name")
    else:
        name = getattr(objective, "name", None)
    return name if isinstance(name, str) else None


def _evaluation_data(spec: Any) -> dict:
    """The spec's ``evaluation_data`` block, or ``{}``.

    Deliberately tolerant where ``load_dataset`` is strict: a spec with no evaluation_data has no
    rubric pointer, and "no rubric" is a reportable status rather than an error.
    """
    ev = getattr(spec, "evaluation_data", None)
    if ev is None and isinstance(spec, dict):
        ev = spec.get("evaluation_data")
    return ev if isinstance(ev, dict) else {}


def _spec_task_id(spec: Any) -> str | None:
    task_id = getattr(spec, "task_id", None)
    if task_id is None and isinstance(spec, dict):
        task_id = spec.get("task_id")
    return task_id if isinstance(task_id, str) and task_id else None
