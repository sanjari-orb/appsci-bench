"""Customer-call-insights environment-layer tests. Docker-free and credential-free.

These exist because the framework validates the *shape* of the environment block but never checks that
anything it declares is really there. This task shipped with `spec.yaml` advertising
`/sop/crm_schema.md` and `/sop/extraction_rules.md` while `environment/sop/` held only `kpis.md` --
`python -m appsci_bench.core.spec` passed the whole time.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

TASK_DIR = Path(__file__).resolve().parents[3] / "tasks" / "customer_call_insights"


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load((TASK_DIR / "spec.yaml").read_text())


def _resources(spec: dict) -> list[dict]:
    return (spec.get("environment") or {}).get("resources") or []


def _environments(spec: dict) -> dict[str, dict]:
    """The per-phase task environments (docs/task_env_split.md).

    Read as a dict of phase -> env rather than assuming a single shared one: the two phases are allowed
    to diverge, and a check that silently looked at only `build` would stop covering `eval` the day they
    do. This task currently points both at the same anchor.
    """
    envs = (spec.get("task_runner") or {}).get("environments") or {}
    return {phase: env or {} for phase, env in envs.items()}


def _bundle(spec: dict) -> list[dict]:
    """Every bundle entry across all phases, de-duplicated.

    Reads under `environments`, which is where the build/eval split moved it. The previous version read
    `task_runner.bundle`, a key the schema no longer allows -- it returned an empty list, so every check
    built on it passed by examining nothing.
    """
    seen: list[dict] = []
    for env in _environments(spec).values():
        for entry in env.get("bundle") or []:
            if entry not in seen:
                seen.append(entry)
    return seen


# --- 1. every declared resource must be reachable ---------------------------------------------------

def test_every_declared_resource_location_is_real(spec: dict) -> None:
    """A `/sop` file must exist on disk; anything else must be a recognised non-file location.

    The orchestrator mounts environment/sop only when the dir exists, so a declared-but-absent doc
    costs the agent a resource with no diagnostic anywhere. Unrecognised location shapes fail rather
    than being skipped -- a test that quietly ignores what it does not understand reintroduces the very
    gap it exists to close.
    """
    problems = []
    for res in _resources(spec):
        name, location = res.get("name"), res.get("location") or ""
        if location.startswith("/sop/"):
            on_disk = TASK_DIR / "environment" / "sop" / location[len("/sop/"):]
            if not on_disk.is_file():
                problems.append(f"{name} -> {location} (expected {on_disk})")
        elif location.startswith("postgres://"):
            # Backed by the compose `db` service and populated by db/setup.sh, so there is nothing on
            # disk to check here. The stand-up assertions live in setup.sh itself.
            continue
        else:
            problems.append(
                f"{name} -> {location!r} is not a shape this test knows how to verify; add a case here"
            )
    assert not problems, "resources are not reachable as declared: " + "; ".join(problems)


def test_no_data_mount_is_declared(spec: dict) -> None:
    """`/data` is deliberately absent: the calls table is the single access path to the transcripts.

    Re-adding a `/data` resource without creating environment/data/ would mount nothing -- the
    orchestrator skips a missing dir silently -- so this pins the decision rather than the mechanism.
    """
    data_resources = [r.get("name") for r in _resources(spec) if (r.get("location") or "").startswith("/data")]
    assert not data_resources, f"resources declare a /data location but /data is not mounted: {data_resources}"
    assert not (TASK_DIR / "environment" / "data").exists(), (
        "environment/data/ exists again; either drop it or update the /data decision in KNOWN_ISSUES.md"
    )


def test_no_resource_location_is_a_repo_path(spec: dict) -> None:
    """core/usersim_docs.py checks this only for resources a persona names via known_resources; an
    unlisted one with a bad path is a trap that fires when someone adds it. Check all of them."""
    offenders = [
        f"{r.get('name')} -> {r.get('location')}"
        for r in _resources(spec)
        if (r.get("location") or "").startswith(("tasks/", "./", "../"))
    ]
    assert not offenders, "resource locations must be container paths, not repo paths: " + "; ".join(offenders)


# --- 2. bundle sources must exist and carry files ---------------------------------------------------

def test_every_bundle_source_exists(spec: dict) -> None:
    """A missing bundle `src` is a hard FileNotFoundError that aborts the run at startup."""
    missing = [e["src"] for e in _bundle(spec) if not (TASK_DIR / e["src"]).exists()]
    assert not missing, f"bundle sources do not exist: {missing}"


def test_every_bundle_source_contains_at_least_one_file(spec: dict) -> None:
    """bundle.py adds only `is_file()` entries, so an empty dir produces no tar members and the
    destination never appears in the container -- surfacing later as a confusing missing-script error."""
    empty = []
    for entry in _bundle(spec):
        src = TASK_DIR / entry["src"]
        if src.is_dir() and not any(p.is_file() for p in src.rglob("*")):
            empty.append(entry["src"])
    assert not empty, f"bundle sources contain no files, so they vanish from the tarball: {empty}"


# --- 3. the setup script must actually be delivered -------------------------------------------------

def test_setup_script_is_delivered_by_a_bundle_entry(spec: dict) -> None:
    """It is a container path run after extraction, so a bundle entry must deliver it. The schema's
    "relative to task directory" description is stale -- runner.py execs the value verbatim.

    Checked per phase: each declared environment runs its own setup script, so one that is delivered in
    the build env but not the eval env would abort only the second half of a run.
    """
    environments = _environments(spec)
    assert environments, "task_runner declares no environments"

    for phase, env in environments.items():
        script = env.get("setup_script") or ""
        assert script.startswith("/"), \
            f"{phase}: setup_script must be an absolute container path, got {script!r}"

        for entry in env.get("bundle") or []:
            dest = entry["dest"]
            if not script.startswith(dest.rstrip("/") + "/"):
                continue
            relative = script[len(dest.rstrip("/")) + 1:]
            if (TASK_DIR / entry["src"] / relative).is_file():
                break
        else:
            pytest.fail(
                f"{phase}: no bundle entry delivers {script} -- the run would abort at the setup step")


# --- 4. the declared input contract must match how the evaluator really delivers a call -------------
#
# The checks above verify that declared resources EXIST. They cannot catch a declaration that LIES,
# which is what shipped: `interface_design` said the transcript "arrives in the user message", but the
# evaluator attaches it in a top-level `files` array beside `messages` (HttpArtifact.respond,
# appsci_bench/eval/artifact.py). The agent implemented the sentence it was given, never read the
# attachment, and returned an empty reading for every call -- scoring 0.375 on a run where the
# environment was entirely healthy. `interface_design` is one of the few free-text fields that survives
# redaction into the agent's system prompt, so a false claim there is handed over as fact.

_SOP_DIR = TASK_DIR / "environment" / "sop"


def _interface_design(spec: dict) -> str:
    return spec.get("interface_design") or ""


def test_interface_design_does_not_claim_the_call_arrives_in_the_user_message(spec: dict) -> None:
    """The exact false claim that caused the first E2E run to score a blind artifact."""
    text = " ".join(_interface_design(spec).lower().split())
    for claim in ("arrives in the user message", "in the user message", "transcript in the user message"):
        assert claim not in text, (
            f"interface_design claims the call {claim!r}; the evaluator attaches it as a file instead. "
            "An agent that believes this never reads the transcript -- see KNOWN_ISSUES.md."
        )


def test_interface_design_describes_the_files_channel(spec: dict) -> None:
    """Stating where the call really arrives is the fix; transport is legitimately handed over
    (docs/task_information_placement.md), so this must be explicit rather than left to discovery."""
    text = _interface_design(spec).lower()
    assert "files" in text, "interface_design must name the top-level `files` array"
    assert "file_data" in text, "interface_design must name the base64 `file_data` field"


def test_sop_docs_on_disk_and_declared_resources_agree(spec: dict) -> None:
    """Inverse of the existence check: a doc that ships but is never advertised is as invisible as one
    that is advertised but missing. Either declare each `/sop` file individually or cover the whole
    directory with one resource, as slides_generator does."""
    on_disk = {p.name for p in _SOP_DIR.glob("*.md")} if _SOP_DIR.is_dir() else set()
    locations = [(r.get("location") or "") for r in _resources(spec)]
    if any(loc.rstrip("/") == "/sop" for loc in locations):
        return  # a directory-level resource covers everything under it
    declared = {loc[len("/sop/"):] for loc in locations if loc.startswith("/sop/")}
    undeclared = on_disk - declared
    assert not undeclared, (
        f"these /sop docs ship to the agent but no resource points at them: {sorted(undeclared)}"
    )


# Field names and sentiment values are what this task exists to make the agent ELICIT. `/sop` is
# agent-visible, so the guard lives next to the files it guards rather than in a grep someone forgets.
# `risks` is absent from this list on purpose: kpis.md legitimately discusses account risk in business
# prose, and the field NAME is what must not leak, which the field-set terms below already cover.
_ORACLE_TERMS = ("next_steps", "next steps", "commitments", "sentiment",
                 "positive", "neutral", "negative", "at_risk", "at risk")


def test_no_sop_doc_leaks_the_field_set_or_sentiment_vocabulary() -> None:
    if not _SOP_DIR.is_dir():
        pytest.skip("no /sop directory")
    offenders = []
    for path in sorted(_SOP_DIR.glob("*.md")):
        body = path.read_text().lower()
        for term in _ORACLE_TERMS:
            if term in body:
                offenders.append(f"{path.name}: {term!r}")
    assert not offenders, (
        "agent-visible /sop docs name what must be elicited from the personas: " + "; ".join(offenders)
    )
