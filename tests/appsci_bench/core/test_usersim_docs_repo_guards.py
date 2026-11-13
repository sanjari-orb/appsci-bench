"""Repo-wide guards on sim-only documents (issue #76, phase E).

These run over EVERY task and persona in the repo, so a task owner adopting `known_docs` cannot merge a
declaration that would silently break their run or leak a document name to the agent.

Read this caveat before trusting a green run: no task has adopted `known_docs` yet, so most of these
currently pass VACANTLY. They are future-proofing, and the proof that they actually fire lives in
test_usersim_docs_validation.py, which plants each leak in a synthetic task. Do not "simplify" these by
deleting the ones that look trivially true -- they look trivially true only until the first migration.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

from appsci_bench.appsci_agent_user_sim.core.persona import (
    HARD_DOC_TOKENS,
    SUGGESTED_DOC_TOKENS,
    UserSimDocsError,
    estimate_tokens,
    resolve_known_docs,
)
from appsci_bench.core.spec import TASKS_DIR
from appsci_bench.core.usersim_docs import (
    DOCS_DIRNAME,
    usersim_docs_dir,
    validate_usersim_docs,
)

#: Real tasks. `_TEMPLATE` is excluded: its persona is an intentionally non-conforming scaffold with
#: `<placeholder>` values, which is why load_user_persona takes validate=False.
TASK_IDS = sorted(
    p.name for p in TASKS_DIR.iterdir()
    if p.is_dir() and not p.name.startswith(("_", "."))
)

#: (task_id, persona_path) for every persona a run could bind, including *_adversarial.yaml (bound by
#: path, absent from spec.user_personas).
PERSONAS: list[tuple[str, pathlib.Path]] = [
    (tid, p)
    for tid in TASK_IDS
    for p in sorted((TASKS_DIR / tid / "user_personas").glob("*.yaml"))
]
PERSONA_IDS = [f"{tid}/{p.stem}" for tid, p in PERSONAS]


def test_tasks_and_personas_discovered() -> None:
    """If these lists are ever empty the whole file passes for the wrong reason."""
    assert TASK_IDS, "no tasks discovered"
    assert PERSONAS, "no personas discovered"


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_task_declarations_are_valid(task_id: str) -> None:
    """The full validator, per task -- the same one the orchestrator and batch preflight use."""
    result = validate_usersim_docs(task_id)

    assert result.ok, f"{task_id}:\n  " + "\n  ".join(result.errors)


@pytest.mark.parametrize("task_id,persona_path", PERSONAS, ids=PERSONA_IDS)
def test_persona_docs_resolve_and_fit_the_hard_budget(task_id: str, persona_path: pathlib.Path) -> None:
    """Per-persona so a failure names the persona, not just the task."""
    persona = yaml.safe_load(persona_path.read_text()) or {}
    docs_root = usersim_docs_dir(TASKS_DIR, task_id)

    try:
        docs = resolve_known_docs(persona, docs_root)
    except UserSimDocsError as exc:
        pytest.fail(f"{task_id}/{persona_path.stem}: {exc}")

    total = sum(estimate_tokens(d["content"]) for d in docs)
    assert total <= HARD_DOC_TOKENS, (
        f"{task_id}/{persona_path.stem}: ~{total} tokens exceeds the hard cap {HARD_DOC_TOKENS}"
    )


@pytest.mark.parametrize("task_id,persona_path", PERSONAS, ids=PERSONA_IDS)
def test_persona_docs_within_suggested_budget(task_id: str, persona_path: pathlib.Path) -> None:
    """Advisory, not blocking: over the SUGGESTED budget is legal (D7), so this is a warn-style check.

    It is skipped rather than failed so CI stays green while still printing the number -- these documents
    sit in the system prompt on EVERY turn, so a task drifting upward should be visible without holding
    up a merge.
    """
    persona = yaml.safe_load(persona_path.read_text()) or {}
    docs = resolve_known_docs(persona, usersim_docs_dir(TASKS_DIR, task_id))
    total = sum(estimate_tokens(d["content"]) for d in docs)

    if total > SUGGESTED_DOC_TOKENS:
        pytest.skip(
            f"{task_id}/{persona_path.stem}: ~{total} tokens is over the suggested "
            f"{SUGGESTED_DOC_TOKENS} (allowed, but it costs on every turn)"
        )


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_docs_tree_never_inside_an_agent_mounted_tree(task_id: str) -> None:
    """The mirror of test_every_task_agent_data_dir_is_clean: that one scans what the agent CAN see for
    ground-truth markers; this one checks the sim-only tree is not somewhere the agent can see at all."""
    docs = usersim_docs_dir(TASKS_DIR, task_id)
    if docs is None:
        pytest.skip(f"{task_id} has no {DOCS_DIRNAME}/")

    resolved = pathlib.Path(docs).resolve()
    for sub in ("data", "sop"):
        mount = (TASKS_DIR / task_id / "environment" / sub).resolve()
        assert not (resolved == mount or resolved.is_relative_to(mount)), (
            f"{task_id}: {DOCS_DIRNAME}/ resolves inside the agent-mounted environment/{sub}/"
        )


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_no_sim_only_document_is_named_where_the_agent_can_read(task_id: str) -> None:
    """Naming a document the agent cannot open sends it hunting for a file that does not exist, then
    falling back to memory (tasks/airline_rebooking/KNOWN_ISSUES.md #1 records exactly that)."""
    docs = usersim_docs_dir(TASKS_DIR, task_id)
    if docs is None:
        pytest.skip(f"{task_id} has no {DOCS_DIRNAME}/")

    result = validate_usersim_docs(task_id)
    leaks = [e for e in result.errors if "is named in" in e or "AGENT-FACING spec" in e]

    assert not leaks, f"{task_id}:\n  " + "\n  ".join(leaks)


@pytest.mark.parametrize("task_id,persona_path", PERSONAS, ids=PERSONA_IDS)
def test_persona_prose_does_not_point_at_a_sim_only_doc(task_id: str, persona_path: pathlib.Path) -> None:
    """A persona whose prose says "it's in /sop/x.md" defeats the mechanism even when x.md is correctly
    sim-only and correctly rendered without a path: the sim sends the agent to a path that is not there.

    order_to_cash's l2 personas carry such a sentence today; it becomes a failure the moment
    contract_terms.md moves into user_sim_docs/.
    """
    result = validate_usersim_docs(task_id)
    offending = [e for e in result.errors if f"{task_id}/{persona_path.stem}: prose points" in e]

    assert not offending, "\n  ".join(offending)


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_adversarial_variants_agree_with_their_base(task_id: str) -> None:
    """Convention check (warning-level in the validator): a variant is the same stakeholder with a
    different stance, so a differing document set is almost always a forgotten edit. Reported here as a
    skip so it is visible without blocking a deliberate deviation."""
    result = validate_usersim_docs(task_id)
    drift = [w for w in result.warnings if "differs from its base" in w]

    if drift:
        pytest.skip(f"{task_id}: " + "; ".join(drift))


@pytest.mark.parametrize("task_id,persona_path", PERSONAS, ids=PERSONA_IDS)
def test_persona_resource_locations_are_reachable_by_the_agent(
    task_id: str, persona_path: pathlib.Path
) -> None:
    """Every resource this persona lists must have a `location` the agent can actually reach.

    Unlike the other guards in this file, this one is NOT vacuous today: it was added because
    order_to_cash (l1/l2 + adversarial) and airline_rebooking (l1/l3) were both shipping repo-relative
    locations, so their personas would tell the agent a document lives at a path present in neither
    container. Those specs are fixed in this change; this keeps them fixed.
    """
    result = validate_usersim_docs(task_id)
    offending = [e for e in result.errors
                 if e.startswith(f"{task_id}/{persona_path.stem}: known_resources lists")]

    assert not offending, "\n  ".join(offending)


# --- elicitation rubrics, repo-wide -----------------------------------------------------------
#
# Each task with a rubric also has its own test under tests/tasks/, so a rubric edit fails that task's
# suite rather than a distant one. These two exist so the NEXT task cannot land a broken rubric simply
# by not writing a test of its own.

def test_every_declared_elicitation_rubric_loads_and_validates():
    from appsci_bench.core.spec import all_task_ids, load_task_spec
    from appsci_bench.eval.elicitation_rubric import MAX_ITEMS, load_rubric

    declared = []
    for task_id in all_task_ids():
        spec = load_task_spec(task_id)
        if not (spec.evaluation_data or {}).get("elicitation_rubric"):
            continue
        rubric = load_rubric(spec)          # raises on a declared-but-broken pointer
        assert rubric is not None and rubric.task_id == task_id
        assert 0 < len(rubric.items) <= MAX_ITEMS
        ids = [i.item_id for i in rubric.items]
        assert len(set(ids)) == len(ids), f"{task_id}: duplicate item ids"
        declared.append(task_id)
    # Seven tasks ship one. An eighth appearing here is fine; any of these seven dropping out is not.
    assert set(declared) >= {"airline_rebooking", "banking_support", "compliance_review",
                             "crm_insights", "customer_call_insights", "order_to_cash",
                             "slides_generator"}


def test_every_rubric_item_maps_to_a_real_objective_of_its_own_task():
    """The cross-file rule that keeps the denominator auditable: a fact no objective scores cannot have
    been critical, because a perfect artifact is perfect as measured."""
    from appsci_bench.core.spec import all_task_ids, load_task_spec
    from appsci_bench.eval.elicitation_rubric import load_rubric, validate_rubric

    for task_id in all_task_ids():
        spec = load_task_spec(task_id)
        rubric = load_rubric(spec)
        if rubric is None:
            continue
        problems = validate_rubric(rubric, (spec.evaluation_data or {}).get("objectives") or [])
        assert problems == [], f"{task_id}: {problems}"


def _rubrics_under_mount(task_dir) -> list:
    """Rubric files anywhere under a task's agent-facing tree. One function so the guard and the test
    that proves the guard bites cannot drift apart."""
    return sorted((task_dir / "environment").rglob("*elicitation_rubric*"))


def test_no_rubric_lives_under_a_task_environment_tree():
    """`environment/` is bind-mounted into the agent's container, so a rubric there would be the answer
    key handed over wholesale -- and unlike most leaks, nothing else would notice: every objective could
    still score perfectly while the diagnostic reported a high rate for material that was handed over."""
    from appsci_bench.core.spec import TASKS_DIR, all_task_ids

    for task_id in all_task_ids():
        stray = _rubrics_under_mount(TASKS_DIR / task_id)
        assert stray == [], f"{task_id}: rubric inside the agent-facing mount: {stray}"


def test_the_misplaced_rubric_guard_actually_fires(tmp_path):
    """A guard that only ever runs against tasks that pass it proves nothing. This plants one where it
    must not be and confirms the check catches it, with the path in the message."""
    task = tmp_path / "demo_task"
    (task / "environment" / "sop").mkdir(parents=True)
    (task / "environment" / "sop" / "elicitation_rubric.yaml").write_text("task_id: demo_task\n")
    found = _rubrics_under_mount(task)
    assert found, "a rubric under environment/sop/ went undetected"
    assert "elicitation_rubric" in str(found[0]) and "environment" in str(found[0])


def test_a_rubric_at_the_task_top_level_passes_the_guard(tmp_path):
    task = tmp_path / "demo_task"
    (task / "environment" / "sop").mkdir(parents=True)
    (task / "elicitation_rubric.yaml").write_text("task_id: demo_task\n")
    assert _rubrics_under_mount(task) == []


def test_a_rubric_renamed_inside_the_mount_is_still_caught(tmp_path):
    # The guard keys on the name fragment, so `my_elicitation_rubric_draft.yaml` under the mount is caught
    # too. Renaming to something with no tell at all is the runtime scanner's job (it hashes the file).
    task = tmp_path / "demo_task"
    (task / "environment" / "data").mkdir(parents=True)
    (task / "environment" / "data" / "old_elicitation_rubric_v2.yaml").write_text("x: 1\n")
    assert _rubrics_under_mount(task)


def test_every_declared_rubric_sits_at_the_top_level_of_its_task():
    from appsci_bench.core.spec import all_task_ids, load_task_spec

    for task_id in all_task_ids():
        pointer = (load_task_spec(task_id).evaluation_data or {}).get("elicitation_rubric")
        if not pointer:
            continue
        assert pointer == f"tasks/{task_id}/elicitation_rubric.yaml", (
            f"{task_id}: rubric pointer {pointer!r} is not the conventional top-level location")


# --- test-record ids a persona may name ---------------------------------------------------------
#
# A persona may hand the agent ids of records in its task's BUILD store, because nothing in these
# environments enumerates records: the read tools take an id the caller already holds, the setup
# script deletes the raw fixture once it is loaded, and the load/state routes are admin-gated. Without
# a starting id the agent cannot call a single read tool, so it cannot exercise the tools it is
# building against or point an internal eval harness at anything real.
#
# THE IDS LIVE IN THEIR OWN FIELD SO THIS GUARD CAN BE EXACT. Scanning `domain_knowledge` prose for
# id-shaped tokens does not work -- `README`, `ORACLE`, `PUBLIC` and `ABSENT` all match a six-character
# uppercase id pattern, so a shape rule either misses real ids or fires on ordinary words. A field
# that holds nothing but a comma-separated id list needs no shape rule at all.

#: The one `domain_knowledge` key whose value is parsed as ids. Anything else is prose and is ignored.
TEST_RECORD_IDS_KEY = "test_record_ids"

def _phase_ids(task_id: str, phase: str) -> set[str]:
    """Every record id in one run phase's bundled store, or empty if the task bundles none.

    Reads every collection rather than a fixed list of names, so a task that adds one is covered
    without editing this. Two store shapes are in the repo: airline keys records directly off the
    collection, while banking wraps each in `{"data": {...}, "notes": ...}` -- unwrapping `data` is
    what stops the wrapper keys themselves being read as record ids (they were, and `data` matched
    the word "data" in every banking persona).
    """
    import json

    store = TASKS_DIR / task_id / "environment" / phase / "data" / "db.json"
    if not store.is_file():
        return set()
    data = json.loads(store.read_text())
    if not isinstance(data, dict):
        return set()
    found: set[str] = set()
    for collection in data.values():
        if not isinstance(collection, dict):
            continue
        inner = collection.get("data")
        records = inner if isinstance(inner, dict) else collection
        found |= {key for key in records if isinstance(key, str)}
    return found


def _declared_test_record_ids(persona_path: pathlib.Path) -> list[str]:
    persona = yaml.safe_load(persona_path.read_text()) or {}
    raw = (persona.get("domain_knowledge") or {}).get(TEST_RECORD_IDS_KEY)
    if not raw:
        return []
    return [tok.strip() for tok in str(raw).split(",") if tok.strip()]


def _missing_from_store(declared: list[str], store_ids: set[str]) -> list[str]:
    """Declared ids that are not records of the store. The guard itself, extracted.

    Lives here rather than inline in the test so the "does this guard actually fire" test below can
    call the same code. Re-implemented there, it went green whatever the real test asserted.
    """
    return [rid for rid in declared if rid not in store_ids]


@pytest.mark.parametrize("task_id,persona_path", PERSONAS, ids=PERSONA_IDS)
def test_named_test_records_exist_in_the_build_store(task_id, persona_path) -> None:
    """Every id a persona hands out is a real record of that task's BUILD store.

    This is what keeps the hint honest when someone regenerates the build seed: new ids in the store
    and stale ones in the persona means the sim confidently names records the tools then 404 on, which
    reads to the agent as a broken environment rather than a bad hint.
    """
    declared = _declared_test_record_ids(persona_path)
    if not declared:
        pytest.skip(f"{task_id}/{persona_path.stem} names no test records")

    build_ids = _phase_ids(task_id, "build")
    assert build_ids, (
        f"{task_id}/{persona_path.stem} names test records but the task bundles no build store to "
        f"check them against"
    )
    missing = _missing_from_store(declared, build_ids)
    assert not missing, (
        f"{task_id}/{persona_path.stem}: {missing} are not records of the build store -- regenerate "
        f"the persona's {TEST_RECORD_IDS_KEY} from environment/build/data/db.json"
    )


@pytest.mark.parametrize("task_id,persona_path", PERSONAS, ids=PERSONA_IDS)
def test_no_persona_names_an_eval_store_record(task_id, persona_path) -> None:
    """No persona mentions a record of the EVAL store, anywhere in its text.

    Stronger than the field check above and pointed at a different failure: the eval store holds the
    rows the 50 held-out cases are scored on, so naming one hands the agent a row it will be graded
    against. Exact rather than shape-based -- candidate tokens are filtered by membership in the eval
    key set, so a word that merely looks like an id cannot trigger it.
    """
    eval_ids = _phase_ids(task_id, "eval")
    if not eval_ids:
        pytest.skip(f"{task_id} bundles no eval store")

    tokens = set(re.findall(r"[A-Za-z0-9_]{4,}", persona_path.read_text()))
    leaked = sorted(tokens & eval_ids)
    assert not leaked, (
        f"{task_id}/{persona_path.stem} names eval-store records {leaked} -- these are rows the "
        f"held-out cases are scored against and must never reach the agent"
    )


def test_the_build_store_guard_actually_fires(tmp_path) -> None:
    """A guard that only runs against personas that pass it proves nothing."""
    persona = tmp_path / "l9.yaml"
    persona.write_text('domain_knowledge:\n  test_record_ids: "REAL01, GONE99"\n')
    declared = _declared_test_record_ids(persona)
    assert declared == ["REAL01", "GONE99"]
    # The real guard, not a copy of it: GONE99 is absent from the store and must be reported.
    assert _missing_from_store(declared, {"REAL01"}) == ["GONE99"]
    assert _missing_from_store(declared, {"REAL01", "GONE99"}) == []


def test_a_persona_naming_no_test_records_declares_none(tmp_path) -> None:
    persona = tmp_path / "l9.yaml"
    persona.write_text("domain_knowledge:\n  goal: 'ship it'\n")
    assert _declared_test_record_ids(persona) == []
