"""Unit tests: shared evaluator types (round-trip, sim_view stripping, file-type guard)."""
from __future__ import annotations

import json

import pytest

from appsci_bench.eval.datatypes import (
    ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn, DataPointMetricResult, EvalSample,
    EvalTypeError, File, MultiTurnInput, DEFAULT_STOP_CRITERIA, extract_json_object,
    materialize_files,
)


def test_file_rejects_disallowed_type():
    with pytest.raises(EvalTypeError):
        File(file_type="exe", file_name="x.exe", file_data=b"00")


def test_file_metadata_strips_bytes():
    f = File(file_type="pdf", file_name="a.pdf", file_data=b"PDFDATA")
    assert f.metadata() == {"file_type": "pdf", "file_name": "a.pdf"}
    assert "file_data" not in f.metadata()


def test_file_roundtrip_preserves_bytes():
    f = File(file_type="png", file_name="img.png", file_data=b"\x89PNG\x00\x01")
    assert File.from_dict(f.to_dict()) == f


def test_output_sim_view_drops_file_data_keeps_meta():
    out = ArtifactAgentOutput(text="hi", json='{"k":1}',
                              files=[File("pdf", "r.pdf", b"SECRETBYTES")])
    view = out.sim_view()
    assert view["text"] == "hi"
    assert view["json"] == '{"k":1}'
    assert view["files"] == [{"file_type": "pdf", "file_name": "r.pdf"}]
    # the bytes must not appear anywhere in the sim view
    assert "SECRETBYTES" not in str(view)


def test_query_output_turn_roundtrip():
    q = ArtifactAgentQuery(text="q", files=[File("txt", "n.txt", b"hello")])
    o = ArtifactAgentOutput(json='{"a":2}', files=[File("md", "o.md", b"world")])
    turn = ConversationTurn(query=q, response=o)
    assert ConversationTurn.from_dict(turn.to_dict()) == turn


def test_eval_sample_single_turn_roundtrip():
    s = EvalSample(data_point_id="d1",
                   single_turn_input=ArtifactAgentQuery(text="go"),
                   expected_output=ArtifactAgentOutput(text="done"),
                   eval_rubric="succeeds when done")
    s2 = EvalSample.from_dict(s.to_dict())
    assert s2 == s
    assert s2.multi_turn_input is None


def test_multi_turn_input_default_stop_criteria():
    m = MultiTurnInput.from_dict({"user_task": "t", "user_persona": "p"})
    assert m.user_stop_criteria == DEFAULT_STOP_CRITERIA


def test_data_point_metric_result_with_none_score_serializes_as_json_null():
    r = DataPointMetricResult("d1", "m", 0, None, {"applicable": False})
    d = r.to_dict()
    assert d["score"] is None
    assert json.loads(json.dumps(d))["score"] is None


def test_file_materialize_is_a_noop_when_data_already_resident():
    f = File(file_type="txt", file_name="x.txt", file_data=b"already here")
    f.materialize()
    assert f.file_data == b"already here"


def test_file_materialize_reads_from_file_path_once(tmp_path):
    p = tmp_path / "x.txt"
    p.write_bytes(b"on disk")
    f = File.from_dict({"file_type": "txt", "file_name": "x.txt", "file_path": str(p)})
    assert f.file_data == b""
    f.materialize()
    assert f.file_data == b"on disk"
    p.write_bytes(b"changed after first read")  # memoized -- second call must not re-read
    f.materialize()
    assert f.file_data == b"on disk"


def test_file_materialize_with_no_data_or_path_is_a_noop():
    f = File(file_type="txt", file_name="x.txt")
    f.materialize()
    assert f.file_data == b""


def test_file_from_dict_rejects_missing_file_path_target(tmp_path):
    with pytest.raises(EvalTypeError):
        File.from_dict({"file_type": "txt", "file_name": "x.txt",
                        "file_path": str(tmp_path / "nope.txt")})


def test_file_from_dict_resolves_file_path_relative_to_base_dir(tmp_path):
    (tmp_path / "x.txt").write_bytes(b"hi")
    f = File.from_dict({"file_type": "txt", "file_name": "x.txt", "file_path": "x.txt"},
                       base_dir=tmp_path)
    assert f.file_path == str(tmp_path / "x.txt")
    f.materialize()
    assert f.file_data == b"hi"


def test_file_to_dict_emits_file_path_instead_of_forcing_a_read(tmp_path):
    p = tmp_path / "x.txt"
    p.write_bytes(b"hi")
    f = File.from_dict({"file_type": "txt", "file_name": "x.txt", "file_path": str(p)})
    d = f.to_dict()
    assert d["file_path"] == str(p)
    assert "file_data" not in d
    assert f.file_data == b""            # to_dict() must not have triggered a read


def test_materialize_files_resolves_every_file_in_an_io_object(tmp_path):
    p = tmp_path / "x.txt"
    p.write_bytes(b"hi")
    q = ArtifactAgentQuery(text="q", files=[
        File.from_dict({"file_type": "txt", "file_name": "x.txt", "file_path": str(p)})])
    materialize_files(q)
    assert q.files[0].file_data == b"hi"


def test_materialize_files_accepts_none():
    materialize_files(None)  # must not raise


# --- extract_json_object -----------------------------------------------------------------------
#
# Fills the session API's structured `json` slot for an artifact served over the completions protocol
# (one content string back). STRICT: the whole body is a JSON object, or nothing. See its docstring.

def test_extract_json_object_reads_a_whole_json_body():
    assert json.loads(extract_json_object('{"flag": true}')) == {"flag": True}


def test_extract_json_object_strips_padding_and_returns_the_body_unchanged():
    # The caller hands the result straight to ArtifactAgentOutput.json, so it must be the body itself
    # (stripped of surrounding whitespace), not a re-serialization -- key order and inner spacing are
    # the artifact's, not ours.
    body = '{"flag": true,   "fault_type": "DUPLICATE"}'
    assert extract_json_object(f'\n  {body}\n ') == body


def test_extract_json_object_keeps_a_nested_body_whole():
    raw = '{"flagged": true, "detail": {"delta": 512.5, "lines": [1, 2]}}'
    assert json.loads(extract_json_object(raw))["detail"]["delta"] == 512.5


def test_extract_json_object_handles_braces_inside_a_string_value():
    raw = '{"reason": "unit price {see PO} differs", "flagged": true}'
    assert json.loads(extract_json_object(raw))["flagged"] is True


# A reply that is not a bare object is a CONTRACT FAILURE, not something to fish a payload out of.
# An earlier version scanned for an embedded object and took the first that parsed, which handed the
# evaluator the wrong object whenever an artifact narrated before answering (a printed DB row, a
# fenced tool call) -- its absent flag then read as "not flagged", inventing a miss the artifact never
# committed. There is no schema here to reject a wrong candidate, so the first hit would be final.
# The task's own metrics report the empty slot loudly instead (see tests/tasks/order_to_cash).
@pytest.mark.parametrize("raw", [
    # summary first, then the payload
    'FLAG: YES\nFault type: DUPLICATE\n\n{"flagged": true, "fault_type": "DUPLICATE"}',
    # a printed DB row, then the verdict -- first-wins used to recover the ROW
    'row:\n{"invoice_number": "INV-3046-DUP", "amount": 134568.0}\n'
    'Final answer:\n{"flag": true, "fault_type": "DUPLICATE"}',
    # a fenced tool call, then the verdict
    'Calling a tool:\n```json\n{"tool": "lookup", "args": {"id": "INV-1"}}\n```\n'
    'Verdict:\n{"flag": false, "fault_type": null}',
    # a fenced payload with nothing else
    'Here is the result:\n```json\n{"flagged": true, "fault_type": "TOTAL_MATH"}\n```',
    # trailing prose after a valid object
    '{"flagged": true, "fault_type": "TOTAL_MATH"} -- let me know if you need more detail.',
])
def test_extract_json_object_rejects_a_reply_that_is_not_a_bare_object(raw):
    assert extract_json_object(raw) is None


@pytest.mark.parametrize("raw", [None, "", "   ", "no payload here", "FLAG: YES"])
def test_extract_json_object_returns_none_when_there_is_no_object(raw):
    assert extract_json_object(raw) is None


@pytest.mark.parametrize("raw", ["true", "42", '"a string"', "[1, 2, 3]", "null"])
def test_extract_json_object_rejects_valid_json_that_is_not_an_object(raw):
    # Objects only: the response contract every task states is a key/value dict.
    assert extract_json_object(raw) is None


# --- the elicitation diagnostic on the report ------------------------------------------------
#
# The field sits BESIDE results/views/final_score and must never join any of them: it measures the
# build conversation, not the artifact. These tests cover the serialization contract; the proof that it
# cannot move the score lives in test_aggregate.py.

def _elicitation(**overrides):
    from appsci_bench.eval.datatypes import ElicitationResult, ItemResult
    base = {
        "task_id": "demo_task", "status": "scored", "total_items": 3,
        "elicited_rate": 1 / 3, "asked_not_elicited_rate": 1 / 3, "missed_rate": 1 / 3,
        "error_items": 0, "num_exchanges": 7,
        "items": [ItemResult(item_id="a", verdict="elicited", evidence="sim > yes",
                             reasoning="clear"),
                  ItemResult(item_id="b", verdict="asked_not_elicited", evidence="sim > dunno"),
                  ItemResult(item_id="c", verdict="missed")],
        "transcript_source": "runs/demo_20260823", "warnings": ["a note"]}
    base.update(overrides)
    return ElicitationResult(**base)


def test_eval_report_round_trips_an_elicitation_result():
    from appsci_bench.eval.datatypes import EvalReport

    report = EvalReport(task_id="demo_task", final_score=0.9, elicitation=_elicitation())
    back = EvalReport.from_dict(report.to_dict())
    assert back.elicitation == report.elicitation
    assert back.to_dict() == report.to_dict()
    # nested items survive as objects, not raw dicts
    assert back.elicitation.items[0].item_id == "a"
    assert back.elicitation.items[2].verdict == "missed"


def test_eval_report_round_trips_without_an_elicitation_result():
    from appsci_bench.eval.datatypes import EvalReport

    report = EvalReport(task_id="demo_task", final_score=0.9)
    assert report.to_dict()["elicitation"] is None
    assert EvalReport.from_dict(report.to_dict()).elicitation is None


def test_a_report_predating_the_field_still_loads():
    # Backward compatibility with every report.json already on disk.
    from appsci_bench.eval.datatypes import EvalReport

    old = {"task_id": "demo_task", "results": [], "views": {}, "final_score": 0.75,
           "warnings": [], "run_id": "abc", "store_root": "/tmp/x"}
    report = EvalReport.from_dict(old)
    assert report.elicitation is None and report.final_score == 0.75


def test_the_unscored_statuses_keep_null_rates_through_serialization():
    # A 0.0 surviving where None belongs is the whole failure mode: "unmeasurable" would start reading
    # as "the agent asked nothing", which is a different run entirely.
    from appsci_bench.eval.datatypes import ElicitationResult

    for status in ("no_rubric", "unmeasurable"):
        el = ElicitationResult(task_id="t", status=status, total_items=4)
        d = el.to_dict()
        assert d["elicited_rate"] is None and d["missed_rate"] is None
        assert ElicitationResult.from_dict(d).elicited_rate is None


def test_a_zero_rate_survives_as_zero_not_as_null():
    # The other direction: an agent that never contacted the stakeholder scores a real 0.0.
    from appsci_bench.eval.datatypes import ElicitationResult

    el = ElicitationResult(task_id="t", status="no_exchanges", total_items=4,
                           elicited_rate=0.0, asked_not_elicited_rate=0.0, missed_rate=1.0)
    back = ElicitationResult.from_dict(el.to_dict())
    assert back.elicited_rate == 0.0 and back.elicited_rate is not None
    assert back.missed_rate == 1.0


def test_the_result_is_json_serializable_as_written():
    import json

    from appsci_bench.eval.datatypes import ElicitationResult

    text = json.dumps(_elicitation().to_dict())
    assert ElicitationResult.from_dict(json.loads(text)) == _elicitation()


def test_item_result_defaults_survive_a_minimal_dict():
    from appsci_bench.eval.datatypes import ItemResult

    item = ItemResult.from_dict({"item_id": "a", "verdict": "missed"})
    assert (item.evidence, item.reasoning, item.error) == ("", "", None)


def test_the_judged_count_round_trips_and_defaults_for_older_reports():
    # The rates' denominator. It has to survive serialization, because a rate without it is not a claim.
    from appsci_bench.eval.datatypes import ElicitationResult

    el = ElicitationResult(task_id="t", status="scored", total_items=20, judged_items=17,
                           error_items=3, elicited_rate=12 / 17)
    back = ElicitationResult.from_dict(el.to_dict())
    assert (back.judged_items, back.error_items, back.total_items) == (17, 3, 20)
    # A result written before the field existed reads back as 0 rather than raising.
    assert ElicitationResult.from_dict({"status": "no_rubric"}).judged_items == 0


def test_an_item_results_fact_round_trips_and_defaults_for_older_results():
    from appsci_bench.eval.datatypes import ItemResult

    item = ItemResult(item_id="a", verdict="elicited", fact="The six values, spelled exactly.",
                      evidence="sim > yes", reasoning="clear")
    assert ItemResult.from_dict(item.to_dict()) == item
    # A result written before the field existed reads back with an empty fact, not a KeyError.
    assert ItemResult.from_dict({"item_id": "a", "verdict": "missed"}).fact == ""
