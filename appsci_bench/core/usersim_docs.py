"""Validation for a task's sim-only documents (`tasks/<id>/user_sim_docs/`), issue #76.

ONE definition of "valid declaration", used by three callers so they cannot disagree:

  1. the repo-wide CI test (every task x persona), which catches authoring errors pre-merge;
  2. the batch preflight (`scripts/preflight_usersim_docs.py`), before any row of a batch runs;
  3. the orchestrator's per-task fail-fast, before any container starts.

Errors vs warnings is the load-bearing distinction. An error means the declaration cannot be honored,
so the task must not run -- a user-sim that silently lacks its policy is indistinguishable in a trace
from an agent that never asked, and that ambiguity is what this whole mechanism exists to remove. A
warning means the declaration works but is questionable (over the SUGGESTED token budget, an empty
docs tree); those are reported and the run proceeds.

Errors are RETURNED, never raised: each caller applies its own policy (CI fails the build, the preflight
skips a row, the orchestrator fails one task and lets the batch continue).

This module is benchmark-side on purpose. It reads task specs and iterates personas, which the
`appsci_agent_user_sim` package must not do -- that package stays shippable on its own (guarded by
tests/.../test_persona.py::TestDecoupling). The dependency runs one way: this imports the sim's pure
resolver, never the reverse.
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field

import yaml

from ..appsci_agent_user_sim.core.persona import (
    SUGGESTED_DOC_TOKENS,
    UserSimDocsError,
    estimate_tokens,
    resolve_known_docs,
)
from .spec import TASKS_DIR, build_agent_spec

#: The per-task directory holding sim-only documents. A SIBLING of `environment/` by design: that
#: directory's `data/` and `sop/` subtrees are whole-dir bind-mounted to the agent, so keeping these
#: documents outside it makes the separation structural rather than a matter of remembering what to
#: mount.
DOCS_DIRNAME = "user_sim_docs"

#: The per-level policy extracts, under `tasks/<id>/user_personas/`. Holds the agent-facing rendering
#: of what each persona's `domain_knowledge` block says, one Markdown file per level. Unlike
#: DOCS_DIRNAME this tree is NOT sim-only: the simulator keeps answering out of the persona YAML, and
#: these files exist so the no-user arm has something to mount for the half of a stakeholder's
#: knowledge that was never a document.
POLICIES_DIRNAME = "domain_knowledge"

#: Agent-mounted subtrees of a task's `environment/` (see core.agent_container: /data and /sop).
#: Nothing under DOCS_DIRNAME may resolve inside one of these.
AGENT_MOUNTED_SUBTREES = ("data", "sop")

#: Files scanned for accidental mentions of a sim-only document. `RESOURCES.md` is the per-task
#: resource map -- author-facing documentation, and the natural place to write down a path by habit.
AGENT_FACING_DOCS = ("RESOURCES.md",)

#: Text extensions worth scanning for a leaked filename.
_SCANNABLE = frozenset({".md", ".txt", ".json", ".jsonl", ".csv", ".yaml", ".yml"})


def usersim_docs_dir(tasks_dir: pathlib.Path, task_id: str) -> str | None:
    """The task's sim-only documents directory, or None if it has none.

    Returns None when absent -- the normal state for a task that has not adopted `known_docs`. A
    persona that declares documents while this returns None fails user-sim startup loudly rather than
    yielding a sim that silently knows nothing.
    """
    candidate = pathlib.Path(tasks_dir) / task_id / DOCS_DIRNAME
    return str(candidate) if candidate.is_dir() else None


def persona_policies_dir(tasks_dir: pathlib.Path, task_id: str) -> str | None:
    """The task's per-level policy extracts directory, or None if it has none.

    The second source of stakeholder knowledge. `user_sim_docs/` holds documents a persona was handed
    whole; the rest of what a persona knows is written inline in its YAML as `domain_knowledge`, and a
    task owner who wants that half reachable in the no-user arm extracts it to
    `user_personas/<POLICIES_DIRNAME>/<level>.md`.

    A SUBDIRECTORY, not a glob beside the personas, and that is load-bearing: `user_personas/` holds
    the persona YAMLs themselves, which carry the adversarial `mistaken_belief_*` plants and the
    whole withholding design. Mounting that directory would hand the agent the answer key. A separate
    subtree can be bound entire, with nothing to filter and nothing to get wrong later.
    """
    candidate = pathlib.Path(tasks_dir) / task_id / "user_personas" / POLICIES_DIRNAME
    return str(candidate) if candidate.is_dir() else None


def validate_policies_dir(policies_dir: str | pathlib.Path) -> list[str]:
    """Errors that must stop a no-user run before the directory is mounted.

    Refuses a persona YAML inside the extracts tree. That is the one authoring slip with a real cost:
    the directory is mounted whole and read by the agent, so a stray `l3.yaml` copied in for
    reference would publish the mistaken beliefs and the level design it exists to hide, and nothing
    downstream would say so -- the run would simply score high.
    """
    root = pathlib.Path(policies_dir)
    errors: list[str] = []
    if not root.is_dir():
        return [f"{root} is not a directory"]
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in (".yaml", ".yml"):
            errors.append(
                f"{path.relative_to(root).as_posix()} is a YAML file inside {POLICIES_DIRNAME}/. "
                "This tree is mounted into the agent container in the no-user arm; a persona YAML "
                "here hands the agent the adversarial plants and the withholding design. Move it "
                "back beside the other personas, or extract the agent-facing part to Markdown."
            )
    return errors


@dataclass(frozen=True)
class DocsValidation:
    """The verdict for one task. `errors` block the run; `warnings` do not."""
    task_id: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return f"{self.task_id}: ok"
        parts = [f"{len(self.errors)} error(s)", f"{len(self.warnings)} warning(s)"]
        return f"{self.task_id}: " + ", ".join(parts)


def _persona_paths(task_dir: pathlib.Path) -> list[pathlib.Path]:
    """Every persona file a run could bind for this task.

    Globs the directory rather than reading `spec.user_personas`: the `*_adversarial.yaml` variants are
    bound BY PATH (`--persona <file>`) and never appear in the spec, so a spec-driven list would skip
    exactly the personas most likely to drift.
    """
    persona_dir = task_dir / "user_personas"
    if not persona_dir.is_dir():
        return []
    # Dotfiles excluded, exactly as `_declared_doc_names` already does for documents -- `*` matches a
    # leading dot, so this glob otherwise returns macOS AppleDouble companions (`._l3.yaml`): binary
    # resource forks that shadow a real persona's name and are not YAML. A snapshot published from a
    # Mac once shipped 1,038 of them, and every task's pre-flight validation died reading one, with a
    # UnicodeDecodeError that named neither macOS nor tar. Fixed at the source too
    # (publish_code_snapshot.sh now refuses to ship them), but a validator must not be the component
    # that fails when a stray file appears next to the ones it cares about.
    return sorted(p for p in persona_dir.glob("*.yaml") if not p.name.startswith("."))


def _declared_doc_names(docs_dir: pathlib.Path) -> set[str]:
    """Basenames of every sim-only document present, for the leak scan. Dotfiles excluded (OS noise)."""
    return {
        p.name
        for p in docs_dir.rglob("*")
        if p.is_file() and not p.name.startswith(".")
    }


def _check_docs_tree_placement(task_dir: pathlib.Path, docs_dir: pathlib.Path) -> list[str]:
    """The docs tree must not sit inside anything bind-mounted to the agent."""
    errors = []
    resolved = docs_dir.resolve()
    for sub in AGENT_MOUNTED_SUBTREES:
        mount = (task_dir / "environment" / sub).resolve()
        if resolved == mount or resolved.is_relative_to(mount):
            errors.append(
                f"{DOCS_DIRNAME}/ resolves inside environment/{sub}/, which is bind-mounted to the "
                f"agent -- the agent would READ what it is supposed to elicit. Move it to "
                f"tasks/<id>/{DOCS_DIRNAME}/."
            )
    return errors


def _check_no_leaked_filenames(
    task_dir: pathlib.Path, task_id: str, doc_names: set[str]
) -> list[str]:
    """No sim-only document may be NAMED anywhere the agent can see.

    Covers the redacted agent spec, `RESOURCES.md`, and every file under the agent-mounted subtrees.
    Naming a document the agent cannot open sends it hunting for a file that does not exist (see
    tasks/airline_rebooking/KNOWN_ISSUES.md #1, where the agent filesystem-searched for absent docs and
    then fell back to memory).

    Caveat: this matches on basename, so a task with BOTH an agent-facing and a sim-only document of the
    same name will be flagged. That is the right default -- rename one -- because the alternative is
    silently permitting the ambiguous case.
    """
    if not doc_names:
        return []
    errors: list[str] = []

    spec_file = task_dir / "spec.yaml"
    if spec_file.is_file():
        try:
            raw = yaml.safe_load(spec_file.read_text(encoding="utf-8")) or {}
            agent_view = yaml.safe_dump(build_agent_spec(raw), sort_keys=False, allow_unicode=True)
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
            errors.append(f"could not read spec.yaml for the leak scan: {exc}")
            agent_view = ""
        for name in sorted(doc_names):
            if name in agent_view:
                errors.append(
                    f"sim-only document {name!r} is named in the AGENT-FACING spec view. The agent "
                    "must not learn that this document exists -- drop the entry (its resource or "
                    "technical_docs listing); the fact that rules exist belongs in prose."
                )

    scan_targets: list[pathlib.Path] = [
        task_dir / doc for doc in AGENT_FACING_DOCS if (task_dir / doc).is_file()
    ]
    for sub in AGENT_MOUNTED_SUBTREES:
        mount = task_dir / "environment" / sub
        if mount.is_dir():
            scan_targets.extend(
                p for p in mount.rglob("*") if p.is_file() and p.suffix.lower() in _SCANNABLE
            )

    for target in scan_targets:
        try:
            text = target.read_text(errors="ignore")
        except OSError:
            continue
        for name in sorted(doc_names):
            if name in text:
                rel = target.relative_to(task_dir.parent.parent) if task_dir.is_absolute() else target
                errors.append(
                    f"sim-only document {name!r} is named in {rel}, which the agent can read. "
                    "Remove the reference."
                )
    return errors


def _persona_prose_strings(persona: dict) -> list[str]:
    """Every free-text value in a persona that lands in the rendered prompt."""
    out: list[str] = []
    role = persona.get("role")
    if isinstance(role, str):
        out.append(role)
    knowledge = persona.get("domain_knowledge")
    if isinstance(knowledge, dict):
        for value in knowledge.values():
            if isinstance(value, str):
                out.append(value)
            elif isinstance(value, list):
                out.extend(v for v in value if isinstance(v, str))
    return out


def _check_persona_prose(task_dir: pathlib.Path, task_id: str, doc_names: set[str]) -> list[str]:
    """A persona must not tell the agent to read a sim-only document at an agent mount path.

    The failure this catches is subtle and survives every other guard: the document is correctly
    sim-only and correctly rendered with no path, but the persona's own `domain_knowledge` prose says
    something like "...the terms are in /sop/contract_terms.md". The sim then dutifully sends the agent
    to a path that does not exist in its container -- which is worse than saying nothing, because the
    agent burns turns looking and may conclude the material is unavailable.

    Real instance: order_to_cash's `l2.yaml` / `l2_adversarial.yaml` carry exactly this sentence today.
    It is not an error yet because contract_terms.md is still agent-facing; it becomes one the moment
    that document moves into user_sim_docs/.

    Matches only agent MOUNT paths (`/sop/<name>`, `/data/<name>`), never the bare filename: a persona
    legitimately says "the policy" or "the contract terms" in prose all the time.
    """
    if not doc_names:
        return []
    errors: list[str] = []
    mounts = ("/sop/", "/data/")
    for persona_path in _persona_paths(task_dir):
        try:
            persona = yaml.safe_load(persona_path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            continue  # reported by the main loop
        if not isinstance(persona, dict):
            continue
        prose = " ".join(_persona_prose_strings(persona))
        for name in sorted(doc_names):
            for mount in mounts:
                if f"{mount}{name}" in prose:
                    errors.append(
                        f"{task_id}/{persona_path.stem}: prose points the agent at "
                        f"{mount}{name}, but {name!r} is sim-only -- that path does not exist in the "
                        "agent's container. Describe the content as knowledge, without a path."
                    )
    return errors


def _location_is_a_host_path(location: str) -> str | None:
    """Return the offending token if `location` names a path on the repo/host filesystem, else None.

    A resource `location` is shown to the persona verbatim (the L1/L2/L3 templates render
    ``at `{{ resource.location }}` ``), so it must be somewhere the AGENT can actually go: a container
    path (`/data/...`, `/sop/...`), a connection string, or prose. A repo-relative path is reachable from
    neither container and sends the agent hunting.

    Detection is deliberately evidence-based rather than pattern-based: a token is a host path if it
    resolves to something that exists in the repo, or if it is visibly repo-shaped (`tasks/...`) even
    after being moved. That keeps prose containing slashes -- "read/write/transfer tools", "flights/
    users/reservations" -- from being flagged, which a naive "contains a slash" rule would do.
    """
    repo_root = TASKS_DIR.parent
    for token in re.split(r"[\s,()`'\"]+", location or ""):
        token = token.rstrip(".;:")
        if not token or "/" not in token or "://" in token or token.startswith("/"):
            continue
        if token.startswith("tasks/") or (repo_root / token).exists():
            return token
    return None


def _check_resource_locations(task_dir: pathlib.Path, task_id: str) -> list[str]:
    """No resource a persona can name may carry a repo-relative `location`.

    This is the fourth way a sim can point the agent at something it cannot open, and the one that
    survives every other guard in this module because it never touches `user_sim_docs/` at all: the
    document is agent-facing and correctly mounted, but the spec records where it lives *in the repo*
    rather than where it appears *in the container*, and `resolve_known_resources` hands that string
    straight to the template.

    Instances at the time this check was written (both pre-existing, neither involving `known_docs`):
    `order_to_cash` L1/L2 were told `contract_terms_doc` was at
    `tasks/order_to_cash/environment/sop/contract_terms.md` (the agent's copy is `/sop/contract_terms.md`),
    and `airline_rebooking` L1/L3 were given a repo path to the KPI doc rather than the sim-only
    copy the personas actually read (`user_sim_docs/success_kpis.md`).

    Scoped to resources some persona actually lists in `known_resources`: an unlisted resource's location
    never reaches a prompt, and flagging it would mean rewriting spec entries no sim can see. An unlisted
    resource with a repo path is therefore a disarmed trap -- it fails loudly the moment a persona adds
    it, which is when it starts to matter.
    """
    spec_file = task_dir / "spec.yaml"
    if not spec_file.is_file():
        return []
    try:
        spec = yaml.safe_load(spec_file.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return []  # reported elsewhere

    locations = {
        r.get("name"): r.get("location")
        for r in ((spec.get("environment") or {}).get("resources") or [])
        if isinstance(r, dict) and isinstance(r.get("location"), str)
    }

    errors: list[str] = []
    for persona_path in _persona_paths(task_dir):
        try:
            persona = yaml.safe_load(persona_path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            continue
        if not isinstance(persona, dict):
            continue
        for name in persona.get("known_resources", []) or []:
            offender = _location_is_a_host_path(locations.get(name, ""))
            if offender:
                errors.append(
                    f"{task_id}/{persona_path.stem}: known_resources lists {name!r}, whose spec "
                    f"`location` is the repo path {offender!r}. The persona is shown that string "
                    "verbatim, so it would send the agent to a path that exists in neither container. "
                    "Use the path the AGENT sees (e.g. /sop/<file>, /data/<file>), a connection string, "
                    "or prose."
                )
    return errors


def _check_adversarial_consistency(task_dir: pathlib.Path, task_id: str) -> list[str]:
    """An `*_adversarial.yaml` variant should declare the same documents as its base persona.

    A variant is the same stakeholder with a different stance, so a different document set almost always
    means one of the two was edited and the other forgotten. A WARNING, not an error: a task could have a
    deliberate reason, and this cannot tell the difference.
    """
    warnings: list[str] = []
    for variant in sorted((task_dir / "user_personas").glob("*_adversarial.yaml")):
        # `*` matches a leading dot, so this glob also finds macOS AppleDouble companions like
        # `._l3_adversarial.yaml` -- binary resource forks that shadow the real file's name. They
        # should never reach a worker (see publish_code_snapshot.sh, which now refuses to ship them),
        # but a pre-flight *validator* must not be the thing that dies when one does.
        if variant.name.startswith("._"):
            continue
        base = variant.with_name(variant.name.replace("_adversarial", ""))
        if not base.is_file():
            continue
        try:
            # `encoding="utf-8"` explicitly, and `UnicodeDecodeError` caught: `read_text()` with no
            # encoding follows the locale, and either way a non-UTF-8 byte raises a ValueError that
            # `(yaml.YAMLError, OSError)` does not catch. That combination once turned one stray
            # binary file into "every run of every task fails", twelve frames deep in a traceback
            # that mentioned neither the file nor why it was being read. This check is a WARNING
            # generator; a file it cannot read is a file it has nothing to say about, not a reason to
            # stop a benchmark that was about to spend an hour of GPU time.
            variant_docs = (
                yaml.safe_load(variant.read_text(encoding="utf-8")) or {}
            ).get("known_docs") or []
            base_docs = (
                yaml.safe_load(base.read_text(encoding="utf-8")) or {}
            ).get("known_docs") or []
        except (yaml.YAMLError, OSError, UnicodeDecodeError):
            continue
        if list(variant_docs) != list(base_docs):
            warnings.append(
                f"{task_id}/{variant.stem}: known_docs {list(variant_docs)} differs from its base "
                f"{base.stem} {list(base_docs)}. A variant is the same stakeholder with a different "
                "stance -- if this is deliberate, say so in a comment; otherwise one was forgotten."
            )
    return warnings


def validate_usersim_docs(
    task_id: str, tasks_dir: pathlib.Path | None = None
) -> DocsValidation:
    """Validate one task's sim-only document declarations.

    Checks, per persona: every `known_docs` entry resolves, loads, is an allowed type, stays inside the
    docs tree, and the persona's total stays within the HARD token budget (all errors, surfaced by the
    shared resolver); and whether it exceeds the SUGGESTED budget (warning).

    Checks, per task: the docs tree is not inside an agent-mounted subtree; no document is NAMED in
    anything the agent can see; no persona's prose points the agent at a sim-only document via an agent
    mount path; no resource a persona can name carries a repo-relative `location`; and adversarial
    variants agree with their base personas (warning).

    A task with no docs tree and no declarations validates clean -- that is every task today. The
    resource-location check is the exception: it applies to every task regardless of `known_docs`,
    because that channel exists whether or not a task has adopted sim-only documents.
    """
    tasks_dir = pathlib.Path(tasks_dir) if tasks_dir is not None else TASKS_DIR
    task_dir = tasks_dir / task_id
    errors: list[str] = []
    warnings: list[str] = []

    if not task_dir.is_dir():
        return DocsValidation(task_id, errors=[f"no such task directory: {task_dir}"])

    docs_dir_str = usersim_docs_dir(tasks_dir, task_id)
    docs_dir = pathlib.Path(docs_dir_str) if docs_dir_str else None

    if docs_dir is not None:
        errors.extend(_check_docs_tree_placement(task_dir, docs_dir))
        doc_names = _declared_doc_names(docs_dir)
        if not doc_names:
            warnings.append(
                f"{DOCS_DIRNAME}/ exists but holds no documents -- personas will get nothing from it."
            )
        errors.extend(_check_no_leaked_filenames(task_dir, task_id, doc_names))
        errors.extend(_check_persona_prose(task_dir, task_id, doc_names))

    warnings.extend(_check_adversarial_consistency(task_dir, task_id))
    errors.extend(_check_resource_locations(task_dir, task_id))

    for persona_path in _persona_paths(task_dir):
        label = f"{task_id}/{persona_path.stem}"
        try:
            persona = yaml.safe_load(persona_path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as exc:
            errors.append(f"{label}: unreadable persona: {exc}")
            continue
        if not isinstance(persona, dict):
            errors.append(f"{label}: persona is not a mapping")
            continue
        try:
            docs = resolve_known_docs(persona, docs_dir_str)
        except UserSimDocsError as exc:
            errors.append(f"{label}: {exc}")
            continue
        total = sum(estimate_tokens(d["content"]) for d in docs)
        if total > SUGGESTED_DOC_TOKENS:
            warnings.append(
                f"{label}: known_docs total ~{total} tokens, over the suggested "
                f"{SUGGESTED_DOC_TOKENS}. These sit in the system prompt on EVERY turn; trim if you can."
            )

    return DocsValidation(task_id, errors=errors, warnings=warnings)


def validate_many(
    task_ids: list[str], tasks_dir: pathlib.Path | None = None
) -> list[DocsValidation]:
    """Validate several tasks, preserving order. Never raises -- callers decide what a failure means."""
    return [validate_usersim_docs(t, tasks_dir) for t in task_ids]
