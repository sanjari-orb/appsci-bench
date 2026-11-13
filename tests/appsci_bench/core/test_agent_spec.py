"""The agent-facing spec redactor (core.spec.build_agent_spec / write_agent_spec).

The raw spec.yaml is GROUND TRUTH and must never reach the agent under test -- and the agent's
mini-swe shell can `cat` whatever we mount, so we mount a redacted view instead of the raw file.
These tests pin what the redaction drops (ground-truth + host-only fields) and keeps (agent-facing
fields, minus each resource's host `location`), and assert the real airline agent-view carries no
gated ground-truth paths.
"""
from __future__ import annotations


import pytest
import yaml

from appsci_bench.core.spec import (
    agent_spec_leaks,
    all_task_ids,
    build_agent_spec,
    resolve_spec_path,
    write_agent_spec,
    TASKS_DIR,
)

# A synthetic raw spec covering every field the redactor must decide on.
_RAW = {
    "task_id": "demo",
    "name": "Demo task",
    "description": "a business ask",
    "business_goals": ["do the thing"],
    "dataset_files": [{"name": "data", "description": "some data"}],
    "technical_docs": [{"name": "doc", "description": "some doc"}],
    "interface_design": "build an HTTP service",
    "artifacts": {"output_dir": "/work/out"},
    "budgets": {"build": {"turns": None}},
    # --- ground-truth / host-only: all must be dropped ---
    "evaluation_data": {"dataset": "tasks/demo/eval_data", "objectives": [{"name": "x"}]},
    "task_runner": {"env_admin_token_var": "ADMIN_TOKEN"},
    "source": {"notes": "cast from scripts/convert.py and seed_data/"},
    "user_personas": ["tasks/demo/personas/l1.yaml"],
    "gold_solution": {"path": "gold/demo/"},
    "owner": "Someone",
    "co_owners": [],
    "environment": {
        "docker_compose": "tasks/demo/environment/docker-compose.yml",
        "access_control": {"denies": ["secret_resource"]},
        "resources": [
            {"name": "db", "type": "database",
             "location": "tasks/demo/environment/db/db.json", "description": "the store"},
        ],
    },
}


def test_build_agent_spec_keeps_only_agent_facing_top_level_keys():
    agent = build_agent_spec(_RAW)
    assert set(agent) == {
        "task_id", "name", "description", "business_goals", "dataset_files",
        "technical_docs", "interface_design", "artifacts",
        "budgets", "environment",
    }
    for dropped in ("evaluation_data", "task_runner", "source",
                    "user_personas", "gold_solution", "owner", "co_owners"):
        assert dropped not in agent, dropped


def test_build_agent_spec_strips_resource_location_and_env_wiring():
    env = build_agent_spec(_RAW)["environment"]
    # env build-wiring / host paths dropped; access_control kept so the in-container deny filter works.
    assert set(env) == {"resources", "access_control"}
    assert "docker_compose" not in env
    (res,) = env["resources"]
    assert res == {"name": "db", "type": "database", "description": "the store"}
    assert "location" not in res  # host path to gated ground-truth is gone


def test_build_agent_spec_no_ground_truth_paths_leak():
    dump = yaml.safe_dump(build_agent_spec(_RAW))
    for needle in ("seed_data", "policy.md", "db.json", "gold/", "convert.py",
                   "ADMIN_TOKEN", "THE POLICY", "eval_data", "RESOURCES.md"):
        assert needle not in dump, f"leaked {needle!r} into agent spec"


def test_agent_spec_handles_missing_environment():
    agent = build_agent_spec({"task_id": "t", "name": "n", "description": "d"})
    assert agent["environment"] == {"resources": []}


def test_write_agent_spec_round_trips_to_file(tmp_path):
    dest = write_agent_spec(resolve_spec_path("airline_rebooking"), tmp_path / "sub" / "spec.yaml")
    assert dest.exists()
    written = yaml.safe_load(dest.read_text())
    assert "evaluation_data" not in written and "task_runner" not in written


def test_real_airline_agent_spec_has_no_gated_paths():
    """Regression for the airline SOP/ground-truth leak: the redacted agent-facing airline spec
    (what the agent container actually mounts) must not name the ANSWER KEY -- the oracle/eval
    dataset, the gold solution, host paths, or the internal seed corpus. (The requirement docs
    policy.md / tools_reference.json / kpis.md are intentionally agent-facing now -- mounted at
    /sop and named in dataset_files/technical_docs -- so they are NOT in this deny-list.)"""
    raw = yaml.safe_load(resolve_spec_path("airline_rebooking").read_text())
    dump = yaml.safe_dump(build_agent_spec(raw))
    for needle in ("seed_data", "gold/", "db.json",
                   "evaluation_data", "gated to L1", "ground truth",
                   "RESOURCES.md", "AIRLINE_ENV_ADMIN"):
        assert needle not in dump, f"airline agent spec leaks {needle!r}"


# --- generic leak guardrail (all tasks) ------------------------------------------------

def test_agent_spec_leaks_detects_ground_truth():
    """The scanner flags host paths / gated files / oracle markers wherever they hide,
    including the free-text fields the structural allowlist keeps."""
    assert agent_spec_leaks({"interface_design": "ref impl at gold/x/server.py"})
    assert agent_spec_leaks({"technical_docs": [{"description": "policy (gated to L1)"}]})
    assert agent_spec_leaks({"environment": {"resources": [{"location": "tasks/x/environment/db/db.json"}]}})
    assert agent_spec_leaks({"description": "see seed_data/policy.md"})


def test_agent_spec_leaks_ignores_legit_prose():
    """Word-lists and file-TYPE mentions are not leaks (no false positives)."""
    assert agent_spec_leaks({"d": "handle book/modify/cancel/refund requests"}) == []
    assert agent_spec_leaks({"d": "inputs may be .csv/.md/.pdf/.pptx"}) == []


def test_every_task_agent_view_is_clean():
    """Guardrail across ALL tasks (not just airline): the agent-facing view of every task spec
    exposes only allowlisted top-level keys, carries no resource `location`, and trips no
    ground-truth marker. A new task or a leaky spec edit fails here."""
    allowed = {
        "task_id", "name", "description", "business_goals", "dataset_files",
        "technical_docs", "interface_design", "artifacts",
        "budgets", "environment",
    }
    for tid in all_task_ids():
        raw = yaml.safe_load(resolve_spec_path(tid).read_text())
        view = build_agent_spec(raw)
        assert set(view) <= allowed, f"{tid}: unexpected agent-facing keys {set(view) - allowed}"
        for r in view.get("environment", {}).get("resources", []):
            assert "location" not in r, f"{tid}: resource {r.get('name')!r} still has a host location"
        assert agent_spec_leaks(view) == [], f"{tid}: agent view leaks ground truth: {agent_spec_leaks(view)}"


def test_write_agent_spec_rejects_leaky_spec(tmp_path):
    """Runtime fail-fast: if a spec would leak ground truth into an agent-facing field,
    write_agent_spec aborts rather than mounting it."""
    leaky = tmp_path / "spec.yaml"
    leaky.write_text(yaml.safe_dump({
        "task_id": "t", "name": "n", "description": "d",
        "interface_design": "a reference solution lives at gold/t/server.py",
    }))
    with pytest.raises(ValueError, match="leaks ground truth"):
        write_agent_spec(leaky, tmp_path / "out" / "spec.yaml")


def test_every_task_agent_data_dir_is_clean():
    """`tasks/<id>/environment/{data,sop}/` are bind-mounted to the agent (at /data and /sop), so
    EVERY file in them is agent-visible. Guard that whole surface (not just the spec): no file may
    name a repo ground-truth path or oracle/gating marker. Catches dev-docs/READMEs that leak
    `eval_data/`, `gold/`, etc. when mounted.

    `build/data` is here too, and for the same reason: a phase declaration bundles its data directory into
    the container, so the build phase's store is agent-visible exactly as the mounted dirs are. It was NOT
    covered when the phase directories were introduced, and the first README written beside that store
    named the eval fixture and the split -- which is the leak this test already exists to catch, just in a
    directory it could not see. The eval phase's data is deliberately absent from this list: the agent
    never meets that environment.
    """
    from appsci_bench.core.spec import _AGENT_SPEC_FORBIDDEN
    loadable = {".md", ".txt", ".json", ".jsonl", ".csv", ".yaml", ".yml"}
    for tid in all_task_ids():
        for sub in ("data", "sop", "build/data"):
            mount_dir = TASKS_DIR / tid / "environment" / sub
            if not mount_dir.exists():
                continue
            for f in mount_dir.rglob("*"):
                if f.is_file() and f.suffix.lower() in loadable:
                    hits = sorted({m.group(0).strip()
                                   for m in _AGENT_SPEC_FORBIDDEN.finditer(f.read_text(errors="ignore"))})
                    assert not hits, f"{tid}: agent-mounted /{sub} file {f.name} leaks {hits}"


# --- the elicitation-rubric pointer (held-out answer key) -------------------------------------

_RUBRIC_POINTER = "tasks/demo/elicitation_rubric.yaml"


def test_spec_schema_accepts_the_elicitation_rubric_pointer_and_its_absence():
    """The pointer is optional: a task without a rubric reports `no_rubric` and is unaffected, so
    every spec predating the field has to keep validating."""
    from appsci_bench.core.spec import validate_spec

    raw = yaml.safe_load(resolve_spec_path("order_to_cash").read_text())
    assert validate_spec(raw, strict=True) == []
    raw["evaluation_data"]["elicitation_rubric"] = _RUBRIC_POINTER
    assert validate_spec(raw, strict=True) == []
    del raw["evaluation_data"]["elicitation_rubric"]
    assert validate_spec(raw, strict=True) == []


def test_elicitation_rubric_pointer_is_absent_from_the_agent_view():
    """The rubric is the concentrated answer key -- the exact facts the agent was supposed to have to
    ask for, with the true answers written out. It rides inside `evaluation_data`, which the redactor
    drops wholesale, so the pointer cannot leak by construction. Asserted, not assumed."""
    raw = dict(_RAW)
    raw["evaluation_data"] = dict(_RAW["evaluation_data"], elicitation_rubric=_RUBRIC_POINTER)
    view = build_agent_spec(raw)
    assert "evaluation_data" not in view
    assert "elicitation_rubric" not in yaml.safe_dump(view)
    assert _RUBRIC_POINTER not in yaml.safe_dump(view)


def test_a_rubric_path_smuggled_into_a_free_text_field_is_flagged():
    """The structural leak scanner is the backstop for the pointer reaching an agent-facing field by
    another route -- a description that names the file, say. The generic `tasks/` prefix marker
    catches it; confirm that rather than trusting it."""
    assert agent_spec_leaks({"description": f"do not read {_RUBRIC_POINTER}"})
    assert agent_spec_leaks({"interface_design": "the answers are in tasks/demo/elicitation_rubric.yaml"})
    assert agent_spec_leaks({"technical_docs": [{"description": _RUBRIC_POINTER}]})


def test_every_real_task_spec_still_validates():
    """Adding a field to `evaluation_data` must not invalidate anything already authored."""
    from appsci_bench.core.spec import validate_spec

    for tid in all_task_ids():
        raw = yaml.safe_load(resolve_spec_path(tid).read_text())
        assert validate_spec(raw, strict=True) == [], f"{tid} no longer validates"

