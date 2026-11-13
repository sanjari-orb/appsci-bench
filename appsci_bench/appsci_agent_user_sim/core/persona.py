"""User persona template rendering -- user-simulator core.

Renders Jinja2 persona templates (L1/L2/L3) with merged context from a task (its description,
business goals, data/doc listings, and environment resources) and a user-persona YAML, producing the
system prompt the simulator runs under.

This module is part of the appsci_agent_user_sim subpackage, which is kept DECOUPLED from the rest of
appsci_bench: it imports nothing from appsci_bench.core/eval/runner, so the service can be
containerized and shipped on its own. It depends only on a structural view of the task (see
``TaskContextLike``) -- the benchmark's ``TaskSpec`` duck-types into it, but any object with the same
attributes works.
"""
from __future__ import annotations

import json
import math
import pathlib
import re
from typing import Any, Protocol, runtime_checkable

import yaml
from jinja2 import Environment, FileSystemLoader

# Text file extensions a sim-only document may use. `.json` is here for structured references (e.g. a
# tool/action-space reference); anything binary or spreadsheet-shaped is rejected rather than guessed at.
_LOADABLE_DOC_EXTENSIONS = frozenset({".md", ".txt", ".jsonl", ".json"})
# Per-file sanity cap (50 KB). Subordinate to the per-persona token budget below -- this only stops a
# single pathological file.
_MAX_DOC_BYTES = 50_000

# Sim-only documents are injected into the system prompt IN FULL, on EVERY turn (design D7: no
# retrieval, no read tool). So the budget is per BOUND PERSONA -- its own `known_docs` plus everything
# in `common/` -- because a run binds exactly one persona and only that persona's docs are ever in a
# prompt. SUGGESTED is advisory (the validator warns); HARD is enforced here (the declaration is
# invalid above it). Counts `known_docs` content only: inline `domain_knowledge` prose is deliberately
# not counted (design section 9).
SUGGESTED_DOC_TOKENS = 5_000
HARD_DOC_TOKENS = 20_000

# Documents a persona declares live under `tasks/<id>/user_sim_docs/`; everything in this subdirectory
# is included for EVERY persona of the task without being listed (design D3, D11: the doability floor).
COMMON_DOCS_DIRNAME = "common"

# Token estimation divisor. A deliberate approximation -- see estimate_tokens.
_CHARS_PER_TOKEN = 4

# This file lives at appsci_bench/appsci_agent_user_sim/core/persona.py; parents[3] is the repo root (used
# only to resolve relative persona paths). Templates are a sibling of this package
# (parents[1] == appsci_bench/appsci_agent_user_sim/).
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
TEMPLATES_DIR = pathlib.Path(__file__).resolve().parents[1] / "personas" / "templates"
PERSONA_SCHEMA_PATH = REPO_ROOT / "schema" / "user_persona.schema.json"

TEMPLATE_MAP = {
    "L1": "persona_L1_template.jinja2",
    "L2": "persona_L2_template.jinja2",
    "L3": "persona_L3_template.jinja2",
}


@runtime_checkable
class TaskContextLike(Protocol):
    """The slice of a task this module reads. Both the benchmark's (mutable) TaskSpec and the
    service's (frozen) TaskContext satisfy it structurally, keeping appsci_agent_user_sim decoupled from
    appsci_bench. Declared read-only (we only read these) so a frozen dataclass also conforms."""
    @property
    def description(self) -> str: ...
    @property
    def business_goals(self) -> list[str]: ...
    @property
    def dataset_files(self) -> list[dict]: ...
    @property
    def technical_docs(self) -> list[dict]: ...
    @property
    def interface_design(self) -> str: ...
    @property
    def environment(self) -> dict: ...


def _load_yaml(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


class UserSimDocsError(ValueError):
    """A persona's `known_docs` declaration cannot be honored (missing, unreadable, or over budget).

    Always raised, never swallowed: a sim that silently lacks the document it was supposed to have
    reads in a trace exactly like an agent that failed to elicit, which makes the failure invisible
    where it matters most. Subclasses ValueError so callers already handling persona-authoring errors
    (see load_user_persona) behave sensibly.
    """


def estimate_tokens(text: str) -> int:
    """Approximate the token count of `text` as ceil(len/4).

    Deliberately crude: this exists to enforce a prompt-size budget, and a real tokenizer would add a
    heavyweight dependency to the user-sim image for a number that only ever gates an order-of-magnitude
    decision. Being wrong by 20% does not change whether an 8 KB policy fits in a 20,000-token cap. It
    is deterministic, which matters more -- the same declaration must always produce the same verdict.
    """
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


def _load_doc_content(path: pathlib.Path, declared_as: str) -> str:
    """Read one sim-only document, raising UserSimDocsError with an actionable message on any problem.

    `declared_as` is the path as the persona wrote it (relative to the docs root), so an error names
    what the author typed rather than an absolute container path they never see.
    """
    if not path.exists():
        raise UserSimDocsError(
            f"known_docs entry {declared_as!r} does not exist (looked at {path}). "
            "Check the path is relative to the task's user_sim_docs/ directory."
        )
    if not path.is_file():
        raise UserSimDocsError(f"known_docs entry {declared_as!r} is not a file (at {path})")
    suffix = path.suffix.lower()
    if suffix not in _LOADABLE_DOC_EXTENSIONS:
        raise UserSimDocsError(
            f"known_docs entry {declared_as!r} has unsupported extension {suffix!r}; "
            f"allowed: {sorted(_LOADABLE_DOC_EXTENSIONS)}"
        )
    size = path.stat().st_size
    if size > _MAX_DOC_BYTES:
        raise UserSimDocsError(
            f"known_docs entry {declared_as!r} is {size} bytes, over the {_MAX_DOC_BYTES}-byte "
            "per-file cap. Split it or trim it to what the persona genuinely needs."
        )
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UserSimDocsError(f"known_docs entry {declared_as!r} is unreadable: {exc}") from exc


def _common_doc_paths(docs_root: pathlib.Path) -> list[pathlib.Path]:
    """Every file under `<docs_root>/common/`, sorted by relative path for a deterministic prompt.

    Dotfiles are skipped: `.DS_Store` and editor droppings are OS noise, not documents, and failing a
    whole run over one would be absurd. Any other unsupported extension still fails loud in
    _load_doc_content -- silently omitting a document a task owner deliberately placed here would be
    the same invisible degradation this module exists to prevent.
    """
    common = docs_root / COMMON_DOCS_DIRNAME
    if not common.is_dir():
        return []
    return sorted(
        (p for p in common.rglob("*") if p.is_file() and not p.name.startswith(".")),
        key=lambda p: p.relative_to(docs_root).as_posix(),
    )


def resolve_known_docs(
    persona: dict[str, Any],
    docs_root: str | pathlib.Path | None,
) -> list[dict[str, str]]:
    """Load the sim-only documents this persona has internalized.

    Returns `common/` documents (sorted) followed by the persona's `known_docs` in declaration order,
    each as `{"content": <full text>}`. Content ONLY -- no filename, no path, nothing the template
    could render as a location, because the agent has no way to open these and must not be pointed at
    them (design D5).

    Args:
        persona: Loaded persona dict; may carry `known_docs: [<path relative to docs_root>, ...]`.
        docs_root: The task's user-sim docs directory (`/user_sim_docs` in the container). None or
            absent is fine when the persona declares nothing.

    Raises:
        UserSimDocsError: a declared document is missing, unreadable, wrongly typed, escapes
            `docs_root`, exceeds the per-file cap, or the persona's total exceeds HARD_DOC_TOKENS.
    """
    declared = list(persona.get("known_docs", []) or [])
    root = pathlib.Path(docs_root).resolve() if docs_root else None

    if root is None or not root.is_dir():
        if declared:
            raise UserSimDocsError(
                f"persona declares known_docs {declared} but the docs root "
                f"{docs_root if docs_root else '<unset>'} does not exist. In a run this is the mount "
                "of tasks/<id>/user_sim_docs/ -- check it was provided."
            )
        return []

    paths: list[tuple[pathlib.Path, str]] = [
        (p, p.relative_to(root).as_posix()) for p in _common_doc_paths(root)
    ]

    for entry in declared:
        # Resolve before comparing: this is what catches `..` and symlinks pointing out of the tree.
        # The schema rejects the obvious cases, but personas can be loaded unvalidated, so re-check.
        candidate = (root / entry).resolve()
        if not candidate.is_relative_to(root):
            raise UserSimDocsError(
                f"known_docs entry {entry!r} resolves outside the docs root ({candidate}). "
                "Entries must be relative paths inside tasks/<id>/user_sim_docs/."
            )
        paths.append((candidate, entry))

    docs: list[dict[str, str]] = []
    seen: set[pathlib.Path] = set()
    total = 0
    for path, declared_as in paths:
        if path in seen:  # listed explicitly AND present in common/ -- include once.
            continue
        seen.add(path)
        content = _load_doc_content(path, declared_as)
        total += estimate_tokens(content)
        docs.append({"content": content})

    if total > HARD_DOC_TOKENS:
        raise UserSimDocsError(
            f"persona's known_docs total ~{total} tokens, over the hard cap of {HARD_DOC_TOKENS} "
            f"(suggested: {SUGGESTED_DOC_TOKENS}). These sit in the system prompt on every turn -- "
            "declare less, or move detail into the persona's domain_knowledge facts."
        )
    return docs


def validate_persona(data: dict) -> list[str]:
    """Validate a loaded persona dict against schema/user_persona.schema.json.

    Absence of jsonschema is a dev-time-guard miss, not a hard failure (mirrors
    appsci_bench.core.spec.validate_spec) -- keeps this decoupled module usable without the dep.
    """
    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # pragma: no cover
        return []
    schema = json.loads(PERSONA_SCHEMA_PATH.read_text())
    errs = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    return [f"{list(e.path)}: {e.message}" for e in errs]


def load_user_persona(persona_path: str | pathlib.Path, *, validate: bool = True) -> dict[str, Any]:
    """Load a user persona YAML file.

    Args:
        persona_path: Path to the persona YAML file.
        validate: Validate against schema/user_persona.schema.json (default True). Set False to
            load a work-in-progress or intentionally-nonconforming file (e.g. a template scaffold)
            without raising.

    Returns:
        Parsed persona dict with keys: persona_id, task_id, level, role,
        domain_knowledge.
    """
    path = pathlib.Path(persona_path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    persona = _load_yaml(path)
    if validate:
        errs = validate_persona(persona)
        if errs:
            raise ValueError(f"persona {path} invalid:\n  " + "\n  ".join(errs))
    return persona


def resolve_known_resources(task: TaskContextLike, persona: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve a persona's ``known_resources`` names against the task's environment resources.

    The persona lists resource *names*; the task's ``environment.resources`` holds the full
    resource dicts. This looks each name up and returns the resolved dicts (name, type, location,
    description) so the template can tell the persona which systems it knows exist.

    METADATA ONLY -- file contents are never loaded here. The persona learns that these resources exist
    and where the AGENT can find them; it cannot read or quote them. (They are agent-facing, so pointing
    the agent at one is useful behavior.) Sim-only document CONTENT comes from ``known_docs`` /
    :func:`resolve_known_docs` instead.

    A resource's ``location`` is rendered into the prompt verbatim, so it must be a path the agent can
    actually reach (``/data/...``, ``/sop/...``), a connection string, or prose -- never a repo-relative
    path, which exists in neither container. Enforced by
    ``appsci_bench.core.usersim_docs._check_resource_locations``.
    (Content injection used to live here; it was removed in #76. It could not work in a containerized
    run -- the user-sim image ships no ``tasks/`` tree, so a repo-relative ``location`` resolved to
    nothing and the load failed silently -- while looking functional on a developer's host, which
    invited task owners to build elicitation on it.)

    Fails loud on authoring bugs (per design doc 3.9):
      - duplicate resource ``name`` in the task spec -> ValueError;
      - a ``known_resources`` name with no matching resource -> ValueError.

    Args:
        task: The task (its ``environment.resources`` is the canonical resource list).
        persona: Loaded persona dict; may carry ``known_resources: [<name>, ...]``.

    Returns:
        List of resolved resource dicts, in the order the persona listed them. Empty if the persona
        declares no ``known_resources``.
    """
    resources = task.environment.get("resources", []) or []

    index: dict[str, dict[str, Any]] = {}
    for resource in resources:
        name = resource.get("name")
        if name is None:
            # Schema requires `name`, but resources may be loaded from an unvalidated source.
            raise ValueError(f"Resource missing required 'name': {resource!r}")
        if name in index:
            raise ValueError(f"Duplicate resource name in task spec: {name!r}")
        index[name] = resource

    resolved: list[dict[str, Any]] = []
    for name in persona.get("known_resources", []) or []:
        if name not in index:
            raise ValueError(
                f"Persona references unknown resource {name!r}; "
                f"known names: {sorted(index)}"
            )
        # Shallow copy so callers/templates cannot mutate the task's resource list.
        resolved.append(dict(index[name]))
    return resolved


def build_template_context(
    task: TaskContextLike,
    persona: dict[str, Any],
    docs_root: str | pathlib.Path | None = None,
) -> dict[str, Any]:
    """Merge task and persona into a single context dict for Jinja rendering.

    Args:
        task: The task (TaskSpec or any TaskContextLike).
        persona: Loaded persona dict from YAML.
        docs_root: The task's user-sim docs directory, for the persona's ``known_docs``. None means
            the persona must not declare any (see resolve_known_docs).

    Returns:
        Merged context dict with all variables expected by persona templates.
    """
    return {
        # From the task
        "task_description": task.description,
        "business_goals": task.business_goals,
        "dataset_files": task.dataset_files,
        "technical_docs": task.technical_docs,
        "interface_design": task.interface_design,

        # From persona
        "role": persona.get("role", ""),
        "domain_knowledge": persona.get("domain_knowledge", {}),
        # Resources (resolved from the task by name) this persona knows EXIST. METADATA only -- these
        # are agent-visible, so the persona may point the agent at them, but never quote them.
        "known_resources": resolve_known_resources(task, persona),
        # Sim-only document CONTENT the persona has internalized. Each entry is {"content": ...} with
        # no path or filename: the agent cannot open these, so the template must be unable to render a
        # location the sim could point at (design D5).
        "known_docs": resolve_known_docs(persona, docs_root),
        # Opt-in safety-testing overlay: named stakeholder pressures the adversarial contract gates
        # on. Absent/empty = a normal cooperative persona (the templates include the standard
        # contract instead). The no-fabrication rule is never relaxed; see docs/adversarial_personas.md.
        "adversarial_pressures": persona.get("adversarial_pressures", []) or [],
    }


def render_persona_prompt(
    task: TaskContextLike,
    persona_path: str | pathlib.Path,
    docs_root: str | pathlib.Path | None = None,
) -> str:
    """Render a persona Jinja template with merged context.

    This is the main entry point for persona rendering.

    Args:
        task: The task (TaskSpec or any TaskContextLike).
        persona_path: Path to the user persona YAML file.
        docs_root: The task's user-sim docs directory (``/user_sim_docs`` in the container), holding
            the documents the persona declares in ``known_docs``. Optional: a persona that declares
            none does not need it.

    Returns:
        Rendered system prompt string.

    Raises:
        ValueError: If persona level is not L1, L2, or L3.
        UserSimDocsError: If a declared ``known_docs`` entry cannot be honored.
        FileNotFoundError: If template or persona file not found.
    """
    persona = load_user_persona(persona_path)
    level = persona.get("level", "L1")

    if level not in TEMPLATE_MAP:
        raise ValueError(f"Unknown persona level: {level}. Must be L1, L2, or L3.")

    template_name = TEMPLATE_MAP[level]
    context = build_template_context(task, persona, docs_root)

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    template = env.get_template(template_name)

    rendered = template.render(**context)
    # Optional sections ({% if %}/{% for %} blocks) leave runs of blank lines when empty; collapse
    # any run of 3+ newlines to a single blank line and trim leading/trailing whitespace. (We do NOT
    # use Jinja trim_blocks/lstrip_blocks: they also strip the newline after {% endfor %}, which runs
    # adjacent list items together.)
    return re.sub(r"\n{3,}", "\n\n", rendered).strip() + "\n"


def _smoke() -> None:
    """Smoke test: render all personas for the order_to_cash task.

    Imports the benchmark loader LOCALLY (dev convenience only) so module import stays decoupled.
    """
    from appsci_bench.core.spec import load_task_spec

    spec = load_task_spec("order_to_cash")
    print(f"Task: {spec.task_id}")
    print(f"  Description: {spec.description[:60]}...")
    print(f"  Personas: {len(spec.user_personas)}")
    print()

    for persona_path in spec.user_personas:
        try:
            prompt = render_persona_prompt(spec, persona_path)
            print(f"Rendered {persona_path}: {len(prompt)} chars")
        except Exception as e:
            print(f"Failed {persona_path}: {e}")


if __name__ == "__main__":
    _smoke()
