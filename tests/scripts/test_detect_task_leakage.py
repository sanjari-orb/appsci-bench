"""Tests for scripts/detect_task_leakage.py -- the runtime task-leakage detector (#77).

Complements tests/appsci_bench/core/test_agent_spec.py (the STATIC, build-time guardrail over
spec.yaml + the repo's environment/{data,sop} dirs). This tool instead scans an actual completed
run's trace -- what the agent-under-test's container had mounted, and what its model calls actually
carried -- for the three #77 failure modes: mounted_privileged_file, context_leak, eval_pointer_leak.

The script lives in scripts/ (a runnable tool, not an importable package), so we load it by path,
mirroring tests/scripts/test_score_redteam_trace.py.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import yaml

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "detect_task_leakage.py"


def _load():
    spec = importlib.util.spec_from_file_location("_detect_task_leakage_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


DTL = _load()

# A synthetic raw spec covering every ground-truth field the detector pulls literal snippets from.
_RAW_SPEC = {
    "task_id": "demo",
    "name": "Demo task",
    "description": "a business ask",
    "evaluation_data": {"dataset": "tasks/demo/eval_data/cases.jsonl"},
    "gold_solution": {"answer": "THE CORRECT ANSWER IS FORTY TWO DOLLARS"},
    "environment": {
        "resources": [
            {"name": "db", "type": "database", "location": "tasks/demo/environment/db/db.json",
             "description": "the store"},
        ],
    },
}


def _write_spec(tmp_path: pathlib.Path) -> pathlib.Path:
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(_RAW_SPEC))
    return spec_path


def _write_events(trace_dir: pathlib.Path, task_id: str = "demo") -> None:
    events = trace_dir / "events-orchestrator.jsonl"
    events.write_text(json.dumps({"event_id": "e1", "task_id": task_id, "source": "orchestrator"}) + "\n")


def _write_model_call(trace_dir: pathlib.Path, source: str, call_id: str, text: str,
                       tool_calls: list | None = None) -> None:
    path = trace_dir / f"model_calls-{source}.jsonl"
    rec = {"call_id": call_id, "source": source, "messages": [{"role": "user", "content": text}],
           "content": "ok", "tool_calls": tool_calls or []}
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


# --- literal_snippets --------------------------------------------------------------------------

def test_literal_snippets_pulls_gold_and_eval_pointer():
    """The literal corpus comes from the two spec blocks that name the ANSWER KEY: `gold_solution`
    and `evaluation_data.dataset`. Both are also content-hashed (`ground_truth_file_hashes`), so a
    pointer here is backed by the files it names."""
    literals = DTL.literal_snippets(_RAW_SPEC)
    assert literals["THE CORRECT ANSWER IS FORTY TWO DOLLARS"] == "context_leak"
    assert literals["tasks/demo/eval_data/cases.jsonl"] == "eval_pointer_leak"


def test_literal_snippets_does_not_flag_a_resources_own_location_unless_it_points_at_the_answer_key():
    # a resource's `location` is routinely the agent's OWN legitimate mount path / connection
    # string (e.g. compliance_review's policy_kb resource has location "/data/policies/" -- that
    # IS the container path the agent is meant to read from). Only eval_data/gold locations --
    # which point at the answer key itself -- should ever be treated as a leak.
    raw = {"environment": {"resources": [
        {"name": "db", "location": "postgres://sandbox:sandbox@db:5432/compliance"},
        {"name": "policy_kb", "location": "/data/policies/"},
    ]}}
    assert DTL.literal_snippets(raw) == {}


def test_literal_snippets_drops_short_strings():
    raw = {"gold_solution": {"a": "hi"}}
    assert DTL.literal_snippets(raw) == {}


# --- ground_truth_file_hashes / content-hash identity --------------------------------------------

def test_ground_truth_file_hashes_hashes_the_real_eval_data_and_gold_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(DTL, "REPO_ROOT", tmp_path)
    eval_dir = tmp_path / "tasks" / "demo" / "eval_data"
    eval_dir.mkdir(parents=True)
    (eval_dir / "cases.jsonl").write_bytes(b"held-out sample content")
    gold_dir = tmp_path / "gold" / "demo"
    gold_dir.mkdir(parents=True)
    (gold_dir / "answer.json").write_bytes(b"the real gold answer")
    raw = {"evaluation_data": {"dataset": "tasks/demo/eval_data"}, "gold_solution": {"path": "gold/demo"}}

    hashes = DTL.ground_truth_file_hashes(raw)

    assert hashes[DTL._hash_file(eval_dir / "cases.jsonl")] == ("tasks/demo/eval_data/cases.jsonl", "eval_pointer_leak")
    assert hashes[DTL._hash_file(gold_dir / "answer.json")] == ("gold/demo/answer.json", "mounted_privileged_file")


def test_ground_truth_file_hashes_tolerates_a_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(DTL, "REPO_ROOT", tmp_path)
    raw = {"evaluation_data": {"dataset": "tasks/demo/eval_data"}, "gold_solution": {"path": "gold/demo"}}
    assert DTL.ground_truth_file_hashes(raw) == {}


def test_check_mounted_files_flags_a_held_out_file_mounted_under_an_innocuous_name(tmp_path, monkeypatch):
    # the exact scenario a marker/literal-snippet scan misses: a REAL held-out eval file, copied
    # into the agent's mount under a benign name, with no forbidden wording in its content.
    monkeypatch.setattr(DTL, "REPO_ROOT", tmp_path)
    eval_dir = tmp_path / "tasks" / "demo" / "eval_data"
    eval_dir.mkdir(parents=True)
    held_out_content = b"contract_id,clause\n1,some ordinary-looking text\n"
    (eval_dir / "cases.jsonl").write_bytes(held_out_content)
    raw = {"evaluation_data": {"dataset": "tasks/demo/eval_data"}}
    gt_hashes = DTL.ground_truth_file_hashes(raw)

    mounted = tmp_path / "trace" / "agent_fs" / "appsci_agent" / "data"
    mounted.mkdir(parents=True)
    (mounted / "cases.jsonl").write_bytes(held_out_content)  # same bytes, innocuous path

    findings = DTL.check_mounted_files(tmp_path / "trace", {}, gt_hashes)
    assert any(f["surface"] == "mounted_file:content-hash" and f["failure_mode"] == "eval_pointer_leak"
               for f in findings)


def test_check_mounted_files_does_not_flag_an_unrelated_file_by_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(DTL, "REPO_ROOT", tmp_path)
    eval_dir = tmp_path / "tasks" / "demo" / "eval_data"
    eval_dir.mkdir(parents=True)
    (eval_dir / "cases.jsonl").write_bytes(b"held-out content")
    raw = {"evaluation_data": {"dataset": "tasks/demo/eval_data"}}
    gt_hashes = DTL.ground_truth_file_hashes(raw)

    mounted = tmp_path / "trace" / "agent_fs" / "appsci_agent" / "data"
    mounted.mkdir(parents=True)
    (mounted / "orders.csv").write_bytes(b"totally different content")

    findings = DTL.check_mounted_files(tmp_path / "trace", {}, gt_hashes)
    assert not any(f["surface"] == "mounted_file:content-hash" for f in findings)


# --- scan_text -----------------------------------------------------------------------------------

def test_scan_text_finds_generic_marker():
    matches = DTL.scan_text("see gold/answer.json for the reference solution", {})
    kinds = dict(matches)
    assert "eval_pointer_leak" in kinds.values() or "mounted_privileged_file" in kinds.values()


def test_scan_text_ignores_legit_prose():
    assert DTL.scan_text("customers can book, modify, or cancel a flight", {}) == []


def test_scan_text_finds_literal_snippet():
    literals = {"THE CORRECT ANSWER IS FORTY TWO DOLLARS": "context_leak"}
    matches = DTL.scan_text("the agent said: THE CORRECT ANSWER IS FORTY TWO DOLLARS, done", literals)
    assert matches == [("THE CORRECT ANSWER IS FORTY TWO DOLLARS", "context_leak")]


# --- check_mounted_files ---------------------------------------------------------------------------

def test_check_mounted_files_clean(tmp_path):
    d = tmp_path / "agent_fs" / "appsci_agent" / "data"
    d.mkdir(parents=True)
    (d / "orders.csv").write_text("order_id,amount\n1,10.00\n")
    findings = DTL.check_mounted_files(tmp_path, {})
    assert findings == []


def test_check_mounted_files_flags_a_leaked_gold_path_in_content(tmp_path):
    d = tmp_path / "agent_fs" / "appsci_agent" / "spec"
    d.mkdir(parents=True)
    (d / "notes.txt").write_text("see tasks/demo/gold/answer.json for the reference solution")
    findings = DTL.check_mounted_files(tmp_path, {})
    assert any(f["failure_mode"] == "mounted_privileged_file" and f["surface"] == "mounted_file:content"
               for f in findings)


def test_check_mounted_files_flags_a_leaked_path_in_the_filename_itself(tmp_path):
    d = tmp_path / "agent_fs" / "appsci_agent" / "data" / "gold"
    d.mkdir(parents=True)
    (d / "answer.json").write_text("{}")
    findings = DTL.check_mounted_files(tmp_path, {})
    assert any(f["surface"] == "mounted_file:path" for f in findings)


def test_check_mounted_files_flags_literal_gold_content(tmp_path):
    d = tmp_path / "artifact"
    d.mkdir(parents=True)
    (d / "app.py").write_text("# THE CORRECT ANSWER IS FORTY TWO DOLLARS\nprint('hi')\n")
    literals = DTL.literal_snippets(_RAW_SPEC)
    findings = DTL.check_mounted_files(tmp_path, literals)
    assert any(f["failure_mode"] == "context_leak" for f in findings)


def test_check_mounted_files_ignores_the_trusted_usersim_surface(tmp_path):
    d = tmp_path / "agent_fs" / "usersim"
    d.mkdir(parents=True)
    (d / "spec.yaml").write_text("gold/answer.json\nTHE CORRECT ANSWER IS FORTY TWO DOLLARS")
    literals = DTL.literal_snippets(_RAW_SPEC)
    findings = DTL.check_mounted_files(tmp_path, literals)
    assert findings == []


def test_check_mounted_files_does_not_flag_the_agents_own_legitimate_sop_mount(tmp_path):
    # /sop is a documented, intentional agent-facing mount (appsci_bench/core/spec.py: "the
    # requirement docs ... are intentionally exposed to the agent"). Its path must not collide
    # with the ground-truth `sop/` marker meant for spec TEXT.
    d = tmp_path / "agent_fs" / "appsci_agent" / "sop"
    d.mkdir(parents=True)
    (d / ".gitkeep").write_text("")
    (d / "policy_overview.md").write_text("Refunds require manager approval over $500.")
    findings = DTL.check_mounted_files(tmp_path, {})
    assert findings == []


def test_check_mounted_files_does_not_flag_the_agents_own_ls_output_mentioning_sop(tmp_path):
    # Found via a real e2e run: the agent ran `ls /` (exploring its own container) and its own
    # report.txt legitimately contains the bare token "sop/" as one of many directory names --
    # not a leak, just the agent describing its own legitimate mount. This is the CONTENT half of
    # the same collision the path-scan test above covers.
    d = tmp_path / "agent_fs" / "appsci_agent" / "workspace"
    d.mkdir(parents=True)
    (d / "report.txt").write_text("=== ls / ===\nsop/  spec/  srv/  sys/  tmp/  usr/  var/  work/\n")
    findings = DTL.check_mounted_files(tmp_path, {})
    assert findings == []


def test_check_mounted_files_still_flags_a_gold_dir_mounted_under_data(tmp_path):
    # the structural check must still catch a genuinely wrong mount (gold/eval_data showing up
    # where the agent can see it), even though the overloaded `sop/` marker was dropped.
    d = tmp_path / "agent_fs" / "appsci_agent" / "data" / "eval_data"
    d.mkdir(parents=True)
    (d / "cases.jsonl").write_text("{}")
    findings = DTL.check_mounted_files(tmp_path, {})
    assert any(f["surface"] == "mounted_file:path" and f["failure_mode"] == "eval_pointer_leak"
               for f in findings)


def test_check_mounted_files_flags_a_leaked_path_even_for_a_binary_file(tmp_path):
    # a binary's CONTENT is skipped (not read as text), but its PATH must still be scanned --
    # a leaked path like data/gold/answer.pdf is a real finding regardless of file type.
    d = tmp_path / "agent_fs" / "appsci_agent" / "data" / "gold"
    d.mkdir(parents=True)
    (d / "answer.pdf").write_bytes(b"%PDF-1.4 not real content")
    findings = DTL.check_mounted_files(tmp_path, {})
    assert any(f["surface"] == "mounted_file:path" and "answer.pdf" in f["location"] for f in findings)
    # and content scanning was indeed skipped for it (no mounted_file:content finding for this file)
    assert not any(f["surface"] == "mounted_file:content" and "answer.pdf" in f["location"]
                   for f in findings)


# --- check_model_context ---------------------------------------------------------------------------

def test_check_model_context_flags_a_leak_in_the_agent_under_tests_own_prompt(tmp_path):
    _write_model_call(tmp_path, "appsci_agent", "c1", "reminder: reward_basis is X")
    findings = DTL.check_model_context(tmp_path, {})
    assert any(f["failure_mode"] == "context_leak" and "c1" in f["location"] for f in findings)


def test_check_model_context_does_not_flag_the_agent_discussing_its_own_sop_mount(tmp_path):
    _write_model_call(tmp_path, "appsci_agent", "c1",
                       "I found the requirement docs under /sop -- reading sop/policy_overview.md now.")
    findings = DTL.check_model_context(tmp_path, {})
    assert findings == []


def test_check_model_context_flags_a_leak_in_a_tool_call_argument(tmp_path):
    # ModelCall.tool_calls carries e.g. a bash command's arguments -- a leak can hide there even
    # when the surrounding message text is clean.
    _write_model_call(tmp_path, "appsci_agent", "c1", "let me check something", tool_calls=[
        {"name": "bash", "arguments": {"command": "cat tasks/demo/eval_data/cases.jsonl"}},
    ])
    findings = DTL.check_model_context(tmp_path, {})
    assert any(f["failure_mode"] == "eval_pointer_leak" and "c1" in f["location"] for f in findings)



def test_check_model_context_ignores_the_trusted_usersim_source(tmp_path):
    _write_model_call(tmp_path, "usersim", "c1", "reward_basis is X, ground truth is Y")
    findings = DTL.check_model_context(tmp_path, {})
    assert findings == []


# --- resolve_task_id -------------------------------------------------------------------------------

def test_resolve_task_id_reads_it_from_events(tmp_path):
    _write_events(tmp_path, task_id="order_to_cash")
    assert DTL.resolve_task_id(tmp_path) == "order_to_cash"


def test_resolve_task_id_falls_back_to_trace_dir_name(tmp_path):
    trace_dir = tmp_path / "order_to_cash_20260101_120000"
    trace_dir.mkdir()
    assert DTL.resolve_task_id(trace_dir) == "order_to_cash"


# --- detect_leakage / main (integration) -------------------------------------------------------

def test_detect_leakage_clean_trace_is_clean(tmp_path):
    spec_path = _write_spec(tmp_path)
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    (trace_dir / "agent_fs" / "appsci_agent" / "data").mkdir(parents=True)
    (trace_dir / "agent_fs" / "appsci_agent" / "data" / "orders.csv").write_text("id,amt\n1,10\n")
    _write_model_call(trace_dir, "appsci_agent", "c1", "please build a dashboard for orders")
    findings = DTL.detect_leakage(trace_dir, spec_path=spec_path)
    assert findings == []


def test_detect_leakage_flags_a_context_leak_of_the_literal_gold_answer(tmp_path):
    spec_path = _write_spec(tmp_path)
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    _write_model_call(trace_dir, "mini_swe_agent", "c1",
                       "the reviewer noted THE CORRECT ANSWER IS FORTY TWO DOLLARS")
    findings = DTL.detect_leakage(trace_dir, spec_path=spec_path)
    assert any(f["failure_mode"] == "context_leak" for f in findings)


def test_detect_leakage_flags_an_eval_pointer_leak(tmp_path):
    spec_path = _write_spec(tmp_path)
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    _write_model_call(trace_dir, "appsci_agent", "c1", "dataset at tasks/demo/eval_data/cases.jsonl")
    findings = DTL.detect_leakage(trace_dir, spec_path=spec_path)
    assert any(f["failure_mode"] == "eval_pointer_leak" for f in findings)


def test_detect_leakage_resolves_task_id_from_events_when_no_spec_path_given(tmp_path, monkeypatch):
    spec_path = _write_spec(tmp_path)
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    _write_events(trace_dir, task_id="demo")
    monkeypatch.setattr(DTL, "resolve_spec_path", lambda task_id: spec_path)
    findings = DTL.detect_leakage(trace_dir)
    assert findings == []


def test_main_writes_report_and_exits_nonzero_on_a_leak(tmp_path, capsys):
    spec_path = _write_spec(tmp_path)
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    _write_model_call(trace_dir, "appsci_agent", "c1", "reward_basis leaked here")
    rc = DTL.main([str(trace_dir), "--spec-path", str(spec_path)])
    assert rc == 1
    report = json.loads((trace_dir / "leakage_report.json").read_text())
    assert report["clean"] is False and len(report["findings"]) >= 1
    assert "leak(s) found" in capsys.readouterr().out


def test_main_no_write_leaves_no_report_and_exits_zero_when_clean(tmp_path):
    spec_path = _write_spec(tmp_path)
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    rc = DTL.main([str(trace_dir), "--spec-path", str(spec_path), "--no-write"])
    assert rc == 0
    assert not (trace_dir / "leakage_report.json").exists()


# --- the elicitation rubric as a ground-truth source -------------------------------------------
#
# A rubric is the concentrated list of what the agent was supposed to have to ASK for, with the true
# answers written out. It leaks in three shapes and each needs a different layer: the pointer showing up
# somewhere the agent saw it, a fact's text turning up in the agent's context, and the file itself mounted
# under an innocuous name. The third is the one the marker and literal layers are worst at -- one small
# YAML file, and renaming it removes every structural tell -- which is why its hash goes in too.
#
# What makes a rubric leak worse than most is that nothing else notices. Every objective can still score
# perfectly while the diagnostic reports a high elicited rate for material that was handed over.

_FACT_A = ("The six fault_type values, spelled exactly: PRICE_DELTA, QTY_MISMATCH, TOTAL_MATH, "
           "OFF_CONTRACT_ITEM, OFF_CONTRACT_FEE, DUPLICATE.")
_FACT_B = ("All four cancellation eligibility conditions, including the 24-hour booking window and "
           "the business-fare exception.")


def _spec_with_rubric(tmp_path: pathlib.Path, monkeypatch, *, items=None) -> dict:
    """A raw spec whose rubric pointer resolves under a fake repo root."""
    monkeypatch.setattr(DTL, "REPO_ROOT", tmp_path)
    pointer = "tasks/demo/elicitation_rubric.yaml"
    rubric = tmp_path / pointer
    rubric.parent.mkdir(parents=True, exist_ok=True)
    rubric.write_text(yaml.safe_dump({
        "task_id": "demo",
        "items": items if items is not None else [
            {"item_id": "faults", "fact": _FACT_A, "objectives": ["m"]},
            {"item_id": "cancel", "fact": _FACT_B, "objectives": ["m"]},
        ]}, sort_keys=False))
    raw = dict(_RAW_SPEC)
    raw["evaluation_data"] = dict(_RAW_SPEC["evaluation_data"], elicitation_rubric=pointer)
    return raw


def test_the_rubric_pointer_is_an_eval_pointer_leak(tmp_path, monkeypatch):
    raw = _spec_with_rubric(tmp_path, monkeypatch)
    literals = DTL.literal_snippets(raw)
    assert literals["tasks/demo/elicitation_rubric.yaml"] == "eval_pointer_leak"


def test_every_rubric_fact_is_searched_for_as_a_context_leak(tmp_path, monkeypatch):
    raw = _spec_with_rubric(tmp_path, monkeypatch)
    literals = DTL.literal_snippets(raw)
    assert literals[_FACT_A] == "context_leak"
    assert literals[_FACT_B] == "context_leak"


def test_the_rubric_file_is_hashed_so_a_rename_does_not_hide_it(tmp_path, monkeypatch):
    raw = _spec_with_rubric(tmp_path, monkeypatch)
    hashes = DTL.ground_truth_file_hashes(raw)
    entry = [v for v in hashes.values() if "elicitation_rubric" in v[0]]
    assert entry, f"the rubric file was not hashed: {list(hashes.values())}"
    assert entry[0][1] == "mounted_privileged_file"


def test_a_rubric_fact_in_the_agents_context_is_flagged(tmp_path, monkeypatch):
    """The shape that inflates the diagnostic silently: the agent was told a fact it was being scored on
    having to ask for."""
    raw = _spec_with_rubric(tmp_path, monkeypatch)
    trace = tmp_path / "runs" / "demo_trace"
    trace.mkdir(parents=True)
    _write_events(trace)
    _write_model_call(trace, "appsci_agent", "c1",
                      f"Here is some helpful background: {_FACT_A} Now build the classifier.")
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(raw))

    findings = DTL.detect_leakage(trace, spec_path=spec_path)
    hits = [f for f in findings if "PRICE_DELTA" in f.get("marker", "")]
    assert hits, f"a rubric fact in the agent's context was not flagged: {findings}"
    assert hits[0]["failure_mode"] == "context_leak"


def test_the_rubric_copied_into_the_agents_mount_is_flagged_under_any_name(tmp_path, monkeypatch):
    raw = _spec_with_rubric(tmp_path, monkeypatch)
    trace = tmp_path / "runs" / "demo_trace"
    (trace / "agent_fs" / "appsci_agent" / "data").mkdir(parents=True)
    _write_events(trace)
    # Same bytes, innocuous name, no marker words in the path at all.
    smuggled = trace / "agent_fs" / "appsci_agent" / "data" / "notes.yaml"
    smuggled.write_text((tmp_path / "tasks" / "demo" / "elicitation_rubric.yaml").read_text())
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(raw))

    findings = DTL.detect_leakage(trace, spec_path=spec_path)
    assert findings, "an exact copy of the rubric under a harmless name was not flagged"
    assert any(f["failure_mode"] in ("mounted_privileged_file", "context_leak") for f in findings)


def test_a_task_with_no_rubric_is_unaffected(tmp_path, monkeypatch):
    monkeypatch.setattr(DTL, "REPO_ROOT", tmp_path)
    literals = DTL.literal_snippets(_RAW_SPEC)
    assert not any("elicitation" in k for k in literals)
    assert DTL.ground_truth_file_hashes(_RAW_SPEC) == {}


def test_a_declared_but_missing_or_malformed_rubric_does_not_break_the_scan(tmp_path, monkeypatch):
    """A task bug is not a reason to stop looking for leaks, so the rubric is read straight from YAML
    rather than through the loader, and a failure to read it costs only that one layer."""
    monkeypatch.setattr(DTL, "REPO_ROOT", tmp_path)
    raw = dict(_RAW_SPEC)
    raw["evaluation_data"] = dict(_RAW_SPEC["evaluation_data"],
                                  elicitation_rubric="tasks/demo/gone.yaml")
    assert DTL.literal_snippets(raw)["tasks/demo/gone.yaml"] == "eval_pointer_leak"

    broken = tmp_path / "tasks" / "demo" / "elicitation_rubric.yaml"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("task_id: demo\nitems: [oops\n")     # unparseable
    raw["evaluation_data"]["elicitation_rubric"] = "tasks/demo/elicitation_rubric.yaml"
    literals = DTL.literal_snippets(raw)                    # must not raise
    assert literals["tasks/demo/elicitation_rubric.yaml"] == "eval_pointer_leak"


def test_the_real_tasks_rubrics_are_all_reachable_by_the_scanner():
    """Not a fixture: every authored rubric has to be visible to this tool as it ships, or the guard
    exists only in tests."""
    from appsci_bench.core.spec import all_task_ids, load_task_spec

    seen = 0
    for task_id in all_task_ids():
        spec = load_task_spec(task_id)
        pointer = (spec.evaluation_data or {}).get("elicitation_rubric")
        if not pointer:
            continue
        raw = yaml.safe_load((_REPO / "tasks" / task_id / "spec.yaml").read_text())
        literals = DTL.literal_snippets(raw)
        assert literals.get(pointer) == "eval_pointer_leak", task_id
        assert any(v == "context_leak" and len(k) > 40 for k, v in literals.items()), task_id
        assert any("elicitation_rubric" in p for p, _ in
                   DTL.ground_truth_file_hashes(raw).values()), task_id
        seen += 1
    assert seen >= 7, f"expected all seven authored rubrics to be reachable, saw {seen}"
