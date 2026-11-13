"""Tests for scripts/score_elicitation.py -- the hand-operated entry to the elicitation diagnostic.

The script lives in scripts/ (a runnable tool, not an importable package), so it is loaded by path,
mirroring test_score_redteam_trace.py. The judge is stubbed at its import site so nothing reaches a
model.

Two behaviours get most of the attention. Exit status is about whether the tool could RUN, never about
the score -- a diagnostic that failed the shell on a bad result would get wrapped in `|| true` and stop
being read. And `--update-report` has to add a section to a run's existing report without taking
anything out of it, which is the failure it shipped with on first try: re-rendering from the report
alone silently dropped every case transcript.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest
import yaml

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "score_elicitation.py"

_FACT = "The six fault_type values, spelled exactly as the finance team spells them, all six of them."


def _load():
    spec = importlib.util.spec_from_file_location("_score_elicitation_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def script():
    return _load()


@pytest.fixture
def task(tmp_path):
    """A spec on disk with two objectives, and optionally a rubric it points at."""
    def _build(items: list[dict] | None = None) -> pathlib.Path:
        spec = {
            "task_id": "demo_task", "name": "Demo", "description": "d",
            "task_source_type": "internal", "owner": "someone",
            # environment is irrelevant to elicitation scoring; `image`/`resources_doc` were removed
            # from the schema, so declaring them here would fail validation.
            "environment": {},
            "user_personas": ["tasks/demo_task/user_personas/l1.yaml"],
            "evaluation_data": {
                "type": "single_turn", "dataset": "tasks/demo_task/eval_data",
                "objectives": [{"name": n, "eval_function_path": f"demo_task.eval.{n}",
                                "positive": True, "weight": 1.0}
                               for n in ("fault_classification", "reason_quality")],
            },
        }
        if items is not None:
            rubric = tmp_path / "elicitation_rubric.yaml"
            rubric.write_text(yaml.safe_dump({"task_id": "demo_task", "items": items}))
            spec["evaluation_data"]["elicitation_rubric"] = str(rubric)
        path = tmp_path / "spec.yaml"
        path.write_text(yaml.safe_dump(spec))
        return path
    return _build


@pytest.fixture
def stub_judge(monkeypatch):
    """Stub the judge; returns the list of item ids it was asked about."""
    from appsci_bench.eval.elicitation import ElicitationVerdict

    def _stub(verdicts: dict[str, tuple[bool, bool]]) -> list[str]:
        asked_about: list[str] = []

        def judge(item, text, config=None):
            asked_about.append(item.item_id)
            a, e = verdicts[item.item_id]
            return ElicitationVerdict(reasoning="r", asked=a, elicited=e,
                                      evidence="sim > quoted" if a else "")

        monkeypatch.setattr("appsci_bench.eval.elicitation.judge_item", judge)
        return asked_about
    return _stub


def _items(*ids: str) -> list[dict]:
    return [{"item_id": i, "fact": _FACT, "objectives": ["fault_classification"]} for i in ids]


def _run_dir(tmp_path, name="run", exchanges=2) -> pathlib.Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for turn in range(1, exchanges + 1):
        lines.append({"event_type": "usersim.request",
                      "payload": {"question": f"q{turn}", "turn": turn}})
        lines.append({"event_type": "usersim.response",
                      "payload": {"answer": f"a{turn}", "turn": turn}})
    (d / "events-usersim.jsonl").write_text("".join(json.dumps(e) + "\n" for e in lines))
    return d


# --- usage ------------------------------------------------------------------------------------

def test_both_input_flags_together_is_a_usage_error(script, task, tmp_path):
    with pytest.raises(SystemExit) as e:
        script.main(["--task", str(task(_items("a"))), "--run", str(tmp_path),
                     "--transcript", str(tmp_path / "t.md")])
    assert e.value.code == 2


def test_neither_input_flag_is_a_usage_error(script, task):
    with pytest.raises(SystemExit) as e:
        script.main(["--task", str(task(_items("a")))])
    assert e.value.code == 2


def test_no_task_is_a_usage_error(script, tmp_path):
    with pytest.raises(SystemExit) as e:
        script.main(["--run", str(tmp_path)])
    assert e.value.code == 2


def test_update_report_without_a_run_is_rejected(script, task, tmp_path, capsys):
    # A captured transcript is a conversation, not a scored run -- there is no report to fold into.
    captured = tmp_path / "t.md"
    captured.write_text("you > q\nsim > a\n")
    rc = script.main(["--task", str(task(_items("a"))), "--transcript", str(captured),
                      "--update-report", "--no-write"])
    assert rc == 2
    assert "--update-report needs --run" in capsys.readouterr().err


# --- the statuses that exit zero --------------------------------------------------------------

def test_a_run_with_no_event_log_is_unmeasurable_and_exits_zero(script, task, tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = script.main(["--task", str(task(_items("a"))), "--run", str(empty), "--no-write"])
    out = capsys.readouterr().out
    assert rc == 0                                  # the tool ran; there was nothing to measure
    assert "unmeasurable" in out
    assert "NOT a score of zero" in out             # said in words, since there is no number to show


def test_a_task_with_no_rubric_exits_zero_and_says_so(script, task, tmp_path, capsys):
    rc = script.main(["--task", str(task(None)), "--run", str(_run_dir(tmp_path)), "--no-write"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no_rubric" in out
    # The exchange count survives: the conversation WAS read, only the rubric is missing.
    assert "exchanges  2" in out


def test_a_low_score_still_exits_zero(script, task, tmp_path, stub_judge, capsys):
    stub_judge({"a": (False, False), "b": (False, False)})
    rc = script.main(["--task", str(task(_items("a", "b"))), "--run", str(_run_dir(tmp_path)),
                      "--no-write"])
    assert rc == 0
    assert "never asked           1.000" in capsys.readouterr().out


def test_a_broken_rubric_pointer_raises_rather_than_reporting_no_rubric(script, task, tmp_path):
    # A declared-but-missing rubric is a task bug. Reporting it as "no rubric" would let a broken task
    # masquerade as one that simply has not been given a rubric yet.
    from appsci_bench.eval.datatypes import EvalTypeError

    spec_path = task(_items("a"))
    spec = yaml.safe_load(spec_path.read_text())
    spec["evaluation_data"]["elicitation_rubric"] = str(tmp_path / "gone.yaml")
    spec_path.write_text(yaml.safe_dump(spec))
    with pytest.raises(EvalTypeError):
        script.main(["--task", str(spec_path), "--run", str(_run_dir(tmp_path)), "--no-write"])


# --- the happy path ---------------------------------------------------------------------------

def test_the_summary_prints_every_item_and_its_verdict(script, task, tmp_path, stub_judge, capsys):
    stub_judge({"fault_taxonomy": (True, True), "price_tolerance": (True, False),
                "dedup_window": (False, False)})
    rc = script.main(["--task", str(task(_items("fault_taxonomy", "price_tolerance", "dedup_window"))),
                      "--run", str(_run_dir(tmp_path)), "--no-write"])
    out = capsys.readouterr().out
    assert rc == 0
    for item_id in ("fault_taxonomy", "price_tolerance", "dedup_window"):
        assert item_id in out
    assert "elicited              0.333" in out
    assert "asked, not elicited   0.333" in out
    assert "never asked           0.333" in out
    assert "sim > quoted" in out                    # the evidence is shown, one line per item


def test_the_json_is_written_next_to_the_run(script, task, tmp_path, stub_judge):
    stub_judge({"a": (True, True)})
    run = _run_dir(tmp_path)
    script.main(["--task", str(task(_items("a"))), "--run", str(run)])
    payload = json.loads((run / "elicitation.json").read_text())
    assert payload["status"] == "scored" and payload["elicited_rate"] == 1.0
    assert payload["items"][0]["item_id"] == "a"


def test_no_write_leaves_nothing_behind(script, task, tmp_path, stub_judge):
    stub_judge({"a": (True, True)})
    run = _run_dir(tmp_path)
    script.main(["--task", str(task(_items("a"))), "--run", str(run), "--no-write"])
    assert not (run / "elicitation.json").exists()


def test_a_captured_transcript_is_accepted(script, task, tmp_path, stub_judge, capsys):
    # The human-baseline shape. Scoring one is what gives a machine number something real to be read
    # against -- an abstract 1.0 is not a reference point.
    stub_judge({"a": (True, True)})
    captured = tmp_path / "logs" / "transcript.md"
    captured.parent.mkdir(parents=True)
    captured.write_text("you > which fault types?\nsim > all six, spelled exactly\n"
                        "you > and the tolerance?\nsim > $500 dollar impact\n")
    rc = script.main(["--task", str(task(_items("a"))), "--transcript", str(captured), "--no-write"])
    assert rc == 0
    assert "exchanges  2" in capsys.readouterr().out


def test_a_standalone_page_is_written(script, task, tmp_path, stub_judge):
    stub_judge({"a": (True, True)})
    dest = tmp_path / "out" / "page.html"
    script.main(["--task", str(task(_items("a"))), "--run", str(_run_dir(tmp_path)),
                 "--no-write", "--html", str(dest)])
    html = dest.read_text()
    assert "Elicitation diagnostic" in html and "demo_task" in html


# --- --update-report --------------------------------------------------------------------------

def _run_with_report(tmp_path) -> pathlib.Path:
    """A run directory carrying an event log plus the eval/ output a scored run leaves behind."""
    from appsci_bench.eval.aggregate import aggregate
    from appsci_bench.eval.datatypes import (
        ArtifactAgentOutput,
        ArtifactAgentQuery,
        ConversationTurn,
        DataPointMetricResult,
        GenerationRecord,
        Objective,
    )
    from appsci_bench.eval.report_html import render_html
    from appsci_bench.eval.store import TranscriptStore

    run = _run_dir(tmp_path, name="scored_run")
    eval_dir = run / "eval"
    eval_dir.mkdir()
    store = TranscriptStore(str(eval_dir))
    store.save(GenerationRecord("case_alpha", 0, conversation=[
        ConversationTurn(ArtifactAgentQuery(text="review INV-1023"),
                         ArtifactAgentOutput(text="flagged"))]))
    report = aggregate("demo_task",
                       [DataPointMetricResult("case_alpha", "fault_classification", 0, 0.75, {}, None)],
                       [Objective(name="fault_classification", eval_function_path="x.y",
                                  positive=True, weight=1.0)])
    (eval_dir / "report.json").write_text(json.dumps(report.to_dict(), indent=2))
    (eval_dir / "report.html").write_text(render_html(report, store))
    return run


def test_update_report_adds_the_section_without_touching_the_scores(script, task, tmp_path,
                                                                   stub_judge):
    stub_judge({"a": (True, True), "b": (False, False)})
    run = _run_with_report(tmp_path)
    before = json.loads((run / "eval" / "report.json").read_text())

    script.main(["--task", str(task(_items("a", "b"))), "--run", str(run), "--update-report",
                 "--no-write"])

    after = json.loads((run / "eval" / "report.json").read_text())
    assert after["final_score"] == before["final_score"]
    assert after["views"] == before["views"] and after["results"] == before["results"]
    assert after["elicitation"]["status"] == "scored"
    assert after["elicitation"]["elicited_rate"] == 0.5
    assert "Elicitation diagnostic" in (run / "eval" / "report.html").read_text()


def test_update_report_keeps_the_case_transcripts_it_found(script, task, tmp_path, stub_judge):
    """The bug this shipped with: re-rendering from report.json alone drops the per-case conversation
    turns, because those come from the transcript store. An "update" that silently shrinks the report
    it was asked to add a section to is worse than no update."""
    stub_judge({"a": (True, True)})
    run = _run_with_report(tmp_path)
    before = (run / "eval" / "report.html").read_text()

    script.main(["--task", str(task(_items("a"))), "--run", str(run), "--update-report", "--no-write"])

    after = (run / "eval" / "report.html").read_text()
    assert "case_alpha" in before and "case_alpha" in after
    assert "review INV-1023" in after, "the case transcript was dropped by the re-render"
    assert len(after) > len(before), "the report shrank"


def test_update_report_on_a_run_with_no_report_is_a_note_not_a_failure(script, task, tmp_path,
                                                                      stub_judge, capsys):
    stub_judge({"a": (True, True)})
    run = _run_dir(tmp_path)                       # event log but no eval/ directory
    rc = script.main(["--task", str(task(_items("a"))), "--run", str(run), "--update-report",
                      "--no-write"])
    assert rc == 0
    assert "no report to update" in capsys.readouterr().err


def test_the_summary_shows_the_denominator_when_items_went_unjudged(script, task, tmp_path,
                                                                   monkeypatch, capsys):
    """The rates are over the judged items only, so the printed summary has to name the denominator --
    otherwise a rate over two items reads identically to one over fifty."""
    from appsci_bench.eval.elicitation import ElicitationVerdict

    def judge(item, text, config=None):
        if item.item_id == "broken":
            raise RuntimeError("429 rate limited")
        return ElicitationVerdict(reasoning="r", asked=True, elicited=True, evidence="sim > q")

    monkeypatch.setattr("appsci_bench.eval.elicitation.judge_item", judge)
    rc = script.main(["--task", str(task(_items("a", "b", "broken"))),
                      "--run", str(_run_dir(tmp_path)), "--no-write"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "judged     2" in out
    assert "excluded, not counted against the agent" in out
    # 2 of 2 judged, NOT 2 of 3 -- the unjudged item is our failure, not the agent's omission.
    assert "elicited              1.000" in out
    assert "(of 2 judged)" in out


def test_the_summary_omits_the_denominator_when_everything_was_judged(script, task, tmp_path,
                                                                     stub_judge, capsys):
    stub_judge({"a": (True, True), "b": (True, True)})
    script.main(["--task", str(task(_items("a", "b"))), "--run", str(_run_dir(tmp_path)),
                 "--no-write"])
    out = capsys.readouterr().out
    assert "judged" not in out          # nothing to caveat
    assert "elicited              1.000" in out


# --- the whole path, with only the model stubbed -----------------------------------------------

def test_the_script_works_end_to_end_with_only_the_transport_stubbed(script, task, tmp_path, capsys):
    """Every other test here replaces the judge with a hand-written stub, and a stub is more forgiving
    than the function it stands in for -- which is how this shipped calling the judge with its model
    config positionally when that parameter is keyword-only. Every item failed against a live gateway
    while the suite stayed green.

    So this one stubs only the LiteLLM call and lets everything above it run for real: rubric load,
    transcript read, prompt assembly, the cached block list, the schema parse, the rate arithmetic and the
    printed summary. It is the closest thing to the command a person actually types.
    """
    from unittest.mock import patch

    raw = ('{"reasoning": "the stakeholder listed them", "asked": true, "elicited": true, '
           '"evidence": "sim > a1"}')
    run = _run_dir(tmp_path)
    with patch("appsci_bench.eval.helpers.llm.complete", return_value=raw) as transport:
        rc = script.main(["--task", str(task(_items("a", "b"))), "--run", str(run)])

    out = capsys.readouterr().out
    assert rc == 0, out
    assert "status     scored" in out, f"the real judge path failed: {out}"
    assert "elicited              1.000" in out
    assert transport.call_count == 2                       # one model call per item, no retries

    # And the real assembled request, not a stub's idea of one: a two-block user message whose first
    # block is marked for caching.
    messages = transport.call_args.args[0]
    assert [m["role"] for m in messages] == ["system", "user"]
    blocks = messages[1]["content"]
    assert len(blocks) == 2 and blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "BUILD CONVERSATION" in blocks[0]["text"]

    payload = json.loads((run / "elicitation.json").read_text())
    assert payload["judged_items"] == 2 and payload["elicited_rate"] == 1.0
