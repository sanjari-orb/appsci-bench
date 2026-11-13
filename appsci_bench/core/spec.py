"""Load + validate a task spec (tasks/<id>/spec.yaml) against schema/task_spec.schema.json.

A task spec is GROUND TRUTH (description + environment + evaluation_data/oracle). It is never shown
to the agent under test under test; the agent learns the task from the personas (the appsci agent user sim,
which PROVIDES requirements) and by exploring the environment. See docs/task_spec_reference.md.
"""
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schema" / "task_spec.schema.json"
TASKS_DIR = REPO_ROOT / "tasks"


@dataclass
class TaskSpec:
    task_id: str
    name: str
    description: str
    task_source_type: str
    environment: dict
    user_personas: list[str]
    evaluation_data: dict
    source: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)
    budgets: dict = field(default_factory=dict)
    gold_solution: dict = field(default_factory=dict)
    owner: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    # Fields for persona template rendering
    business_goals: list[str] = field(default_factory=list)
    dataset_files: list[dict] = field(default_factory=list)
    technical_docs: list[dict] = field(default_factory=list)
    interface_design: str = ""

    @property
    def dataset(self) -> str | None:
        return self.evaluation_data.get("dataset")


def _load_yaml(path: pathlib.Path) -> dict:
    import yaml
    return yaml.safe_load(path.read_text())


def validate_spec(data: dict, *, strict: bool = False) -> list[str]:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # pragma: no cover
        if strict:
            raise
        return []
    schema = json.loads(SCHEMA_PATH.read_text())
    errs = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    return [f"{list(e.path)}: {e.message}" for e in errs]


# --- Agent-facing spec redaction -------------------------------------------------
#
# The raw spec.yaml is GROUND TRUTH (see the module docstring): it carries the
# eval dataset + scoring functions (`evaluation_data`), the
# orchestration wiring (`task_runner`), provenance that names the seed corpus (`source`),
# the persona files (`user_personas`), the reference solution (`gold_solution`), and host
# file paths (each resource's `location`). None of that may reach the
# agent under test. The agent container mounts the spec at /spec/spec.yaml and its
# mini-swe shell can `cat` whatever is mounted, so filtering only inside the in-container
# loader is NOT enough -- we must mount an already-redacted file. `build_agent_spec` is
# that redaction; the orchestrator writes it out and mounts it in the raw spec's place.
# (The user-sim is a trusted benchmark component that role-plays the persona who KNOWS
# the ground truth -- it keeps the raw spec.)

# Top-level keys that are safe to show the agent. Allowlist, not blocklist: any
# ground-truth field added to the schema later is dropped by default rather than leaking.
#
# Deliberately EXCLUDED (ground truth or host-only wiring -- never agent-facing):
#   evaluation_data  - the eval dataset + scoring functions (the answer key)
#   task_runner      - orchestration wiring (admin tokens, env build)
#   source           - provenance that names the seed corpus
#   user_personas    - the persona files (delivered via the trusted user-sim, not the agent)
#   gold_solution    - the reference solution
#   owner, co_owners - task-authoring metadata
#   task_source_type - authoring metadata
# `environment` is NOT in this list but is kept in a reshaped form (see _AGENT_ENV_KEYS /
# build_agent_spec below): only the resource list (minus host `location`) + access_control.
_AGENT_SPEC_TOP_LEVEL_KEYS = (
    "task_id", "name", "description", "business_goals",
    "dataset_files", "technical_docs", "interface_design",
    "artifacts", "budgets",
)
# Within `environment`, the only agent-facing pieces are the resource list (minus each
# resource's host `location`) and any `access_control` (which further hides resources
# from the agent). `docker_compose` is build wiring and is dropped.
_AGENT_ENV_KEYS = ("access_control",)
# Per-resource keys the agent may see. `location` (the host path) is deliberately absent.
_AGENT_RESOURCE_KEYS = ("name", "type", "description")


def build_agent_spec(data: dict) -> dict:
    """Return the redacted, agent-facing view of a raw task-spec dict.

    Keeps an allowlist of agent-facing top-level fields and, within ``environment``, only
    the resource list with each resource's host ``location`` stripped (plus any
    ``access_control``). Everything else -- ``evaluation_data``,
    ``task_runner``, ``source``, ``user_personas``, ``gold_solution``, ``owner`` -- is
    ground truth or host-only wiring and is dropped. Apply the reference agent's deny
    filtering before writing any public file, so another harness cannot bypass it by
    reading /spec/spec.yaml instead of context.json. See the section comment above.
    """
    out: dict[str, Any] = {k: data[k] for k in _AGENT_SPEC_TOP_LEVEL_KEYS if k in data}

    env = data.get("environment") or {}
    denies = set((env.get("access_control") or {}).get("denies") or [])
    agent_env: dict[str, Any] = {k: env[k] for k in _AGENT_ENV_KEYS if k in env}
    agent_env["resources"] = [
        {k: r[k] for k in _AGENT_RESOURCE_KEYS if k in r}
        for r in (env.get("resources") or [])
        if r.get("name") not in denies and r.get("type") not in denies
    ]
    out["environment"] = agent_env
    for key in ("dataset_files", "technical_docs"):
        if key in out:
            out[key] = [entry for entry in out[key] if entry.get("name") not in denies]
    return out


# Patterns that must NEVER appear in an agent-facing spec: host paths into the ground-truth
# dirs and oracle/gating markers. This is the generic, TASK-AGNOSTIC leak detector -- it keys
# off structural repo markers (ground-truth dir prefixes, env-internal dirs, gating/oracle
# annotations) only, NEVER task-specific filenames, so it generalizes to every task without
# edits. It catches ground truth wherever it hides, including the free-text fields the
# structural allowlist keeps (interface_design/descriptions), independent of the task.
# Deliberately narrow to avoid false positives on legit prose: word-lists ("book/modify/
# cancel") and file-*type* mentions (".csv/.pdf") don't match; only real repo paths + markers.
# NOTE: gated files referenced as a real repo path (e.g. `sop/policy.md`) are already caught by
# the dir prefixes below -- we intentionally do NOT enumerate per-task basenames here.
_AGENT_SPEC_FORBIDDEN = re.compile(
    r"""(?xi)
      \b(?:tasks|gold|sop|seed_data|eval_data|scripts)/  # repo ground-truth dir prefixes
    | \benvironment/(?:db|data|setup)\b                 # env-internal dirs
    | gated\s+to\s+L                                    # gating annotation
    | ground[\s-]?truth
    | reference\s+solution
    | golden\s+action
    | \breward_basis\b | \bnl_assertion | \boracle\.data\b
    """
)


def agent_spec_leaks(view: dict) -> list[str]:
    """Return the sorted, unique ground-truth markers found in an agent-facing spec view
    (empty if clean). Used by both the guardrail test and the runtime fail-fast so there is
    one definition of "leak"."""
    import yaml
    text = yaml.safe_dump(view, sort_keys=False, allow_unicode=True)
    return sorted({m.group(0).strip() for m in _AGENT_SPEC_FORBIDDEN.finditer(text)})


def write_agent_spec(spec_path: str | pathlib.Path, dest_path: str | pathlib.Path) -> pathlib.Path:
    """Redact ``spec_path`` (via :func:`build_agent_spec`) and write the result to ``dest_path``.

    Returns ``dest_path``. This is what the orchestrator mounts into the agent container in
    place of the raw spec, so the ground-truth fields never reach the agent's filesystem.

    Fails fast (``ValueError``) if the redacted view still contains a ground-truth marker
    (see :func:`agent_spec_leaks`) -- better to abort the run than mount a leaking spec.
    """
    import yaml
    data = _load_yaml(pathlib.Path(spec_path))
    view = build_agent_spec(data)
    leaks = agent_spec_leaks(view)
    if leaks:
        raise ValueError(
            f"Refusing to mount an agent spec that leaks ground truth from {spec_path}: {leaks}. "
            "Remove the offending path(s)/marker(s) from the agent-facing fields "
            "(description, interface_design, resource descriptions, ...) in the task spec."
        )
    dest = pathlib.Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(yaml.safe_dump(view, sort_keys=False, allow_unicode=True))
    return dest


def resolve_spec_path(task_id_or_path: str | pathlib.Path) -> pathlib.Path:
    p = pathlib.Path(task_id_or_path)
    if p.exists():
        return p
    cand = TASKS_DIR / str(task_id_or_path) / "spec.yaml"
    if cand.exists():
        return cand
    raise FileNotFoundError(f"no spec for {task_id_or_path!r} (looked at {cand})")


def load_task_spec(task_id_or_path: str | pathlib.Path, *, validate: bool = True) -> TaskSpec:
    path = resolve_spec_path(task_id_or_path)
    data = _load_yaml(path)
    if validate:
        errs = validate_spec(data, strict=False)
        if errs:
            raise ValueError(f"spec {path} invalid:\n  " + "\n  ".join(errs))
    return TaskSpec(
        task_id=data["task_id"], name=data["name"], description=data["description"],
        task_source_type=data["task_source_type"], environment=dict(data.get("environment", {})),
        user_personas=list(data.get("user_personas", [])), evaluation_data=dict(data.get("evaluation_data", {})),
        source=dict(data.get("source", {})),
        artifacts=dict(data.get("artifacts", {})),
        budgets=dict(data.get("budgets", {})), gold_solution=dict(data.get("gold_solution", {})),
        owner=data.get("owner", ""), raw=data,
        # Persona template fields
        business_goals=list(data.get("business_goals", [])),
        dataset_files=list(data.get("dataset_files", [])),
        technical_docs=list(data.get("technical_docs", [])),
        interface_design=data.get("interface_design", ""),
    )


def all_task_ids() -> list[str]:
    """Every real task directory under tasks/ (excludes the `_TEMPLATE` fill-in example)."""
    import glob
    specs = [p for p in glob.glob(str(TASKS_DIR / "*" / "spec.yaml")) if "_TEMPLATE" not in p]
    return sorted(pathlib.Path(p).parent.name for p in specs)


def _smoke() -> None:
    task_ids = all_task_ids()
    for task_id in task_ids:
        spec = load_task_spec(task_id)
        print(f"ok {spec.task_id:26s} src={spec.task_source_type:11s} user_personas={len(spec.user_personas)}")
    print(f"\nloaded + validated {len(task_ids)} specs")


if __name__ == "__main__":
    _smoke()
