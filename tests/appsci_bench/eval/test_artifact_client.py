"""Unit tests: artifact-agent session client."""
from __future__ import annotations

import io
import json
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from appsci_bench.eval.artifact_client import (
    ArtifactAgentClient, ArtifactClientError, HttpArtifactClient, InProcessArtifactClient,
    McpArtifactClient, UnknownSessionError,
)
from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery


def _echo(q: ArtifactAgentQuery) -> ArtifactAgentOutput:
    return ArtifactAgentOutput(text=(q.text or "").upper())


def test_lifecycle_create_query_delete():
    c = InProcessArtifactClient(_echo)
    s1 = c.create_session()
    s2 = c.create_session()
    assert s1 != s2                                  # unique ids
    out = c.query_session(s1, ArtifactAgentQuery(text="hi"))
    assert out.text == "HI"
    assert c.delete_session(s1) is True


def test_query_unknown_session_raises():
    c = InProcessArtifactClient(_echo)
    with pytest.raises(UnknownSessionError):
        c.query_session("nope", ArtifactAgentQuery(text="x"))


def test_double_delete_and_unknown_delete_return_false():
    c = InProcessArtifactClient(_echo)
    s = c.create_session()
    assert c.delete_session(s) is True
    assert c.delete_session(s) is False              # already deleted -> idempotent False
    assert c.delete_session("ghost") is False
    with pytest.raises(UnknownSessionError):          # cannot query after delete
        c.query_session(s, ArtifactAgentQuery(text="x"))


@pytest.mark.parametrize("ret,expected_text", [
    (ArtifactAgentOutput(text="t"), "t"),
    ({"text": "d", "json": "{\"k\": 1}"}, "d"),
    ("plain", "plain"),
])
def test_output_coercion(ret, expected_text):
    c = InProcessArtifactClient(lambda q: ret)
    s = c.create_session()
    assert c.query_session(s, ArtifactAgentQuery()).text == expected_text


def test_bad_return_type_raises():
    c = InProcessArtifactClient(lambda q: 123)
    s = c.create_session()
    with pytest.raises(ArtifactClientError):
        c.query_session(s, ArtifactAgentQuery())


def test_application_json_enforced():
    # valid json passes
    c_ok = InProcessArtifactClient(lambda q: ArtifactAgentOutput(json='{"a": 1}'))
    s = c_ok.create_session()
    assert c_ok.query_session(s, ArtifactAgentQuery()).json == '{"a": 1}'
    # invalid json string is dropped (warning logged) rather than failing the turn
    c_bad = InProcessArtifactClient(lambda q: ArtifactAgentOutput(json="{not json"))
    s2 = c_bad.create_session()
    assert c_bad.query_session(s2, ArtifactAgentQuery()).json is None


def test_inprocess_satisfies_protocol():
    assert isinstance(InProcessArtifactClient(_echo), ArtifactAgentClient)


@pytest.mark.parametrize("client", [McpArtifactClient("mcp://x")])
def test_transport_stubs_raise_not_implemented(client):
    assert isinstance(client, ArtifactAgentClient)
    with pytest.raises(NotImplementedError):
        client.create_session()


def test_http_artifact_client_session_tracking():
    # HttpArtifactClient is implemented (session->/respond transport). Its session bookkeeping is
    # testable without a network: create returns an id, delete is idempotent, and querying an
    # unknown session raises (no HTTP call is made in that case).
    client = HttpArtifactClient("http://x/respond")
    assert isinstance(client, ArtifactAgentClient)
    sid = client.create_session()
    assert isinstance(sid, str) and sid
    with pytest.raises(UnknownSessionError):
        client.query_session("not-open", ArtifactAgentQuery(text="x"))
    assert client.delete_session(sid) is True
    assert client.delete_session(sid) is False


# --- error-response + empty-content handling (review findings) --------------------------------

from appsci_bench.eval.artifact import ArtifactResponse
from appsci_bench.eval.artifact_client import ArtifactQueryError, _history_for_send


class _FakeArtifact:
    """Stands in for HttpArtifact: returns scripted ArtifactResponses and records each wire history."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.sent_histories = []

    def respond(self, query):
        self.sent_histories.append(list(query.history))
        r = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return r


def _client_with(fake) -> HttpArtifactClient:
    c = HttpArtifactClient("http://x/respond")
    c._artifact = fake
    return c


def test_query_session_raises_on_artifact_error_output():
    # HttpArtifact returns {"error": ...} with empty messages on an unreachable/broken endpoint;
    # query_session must raise (so the unit is marked errored) not silently return an empty turn.
    c = _client_with(_FakeArtifact([ArtifactResponse(output={"error": "connection refused"}, messages=[])]))
    sid = c.create_session()
    with pytest.raises(ArtifactQueryError):
        c.query_session(sid, ArtifactAgentQuery(text="hi"))


@pytest.mark.parametrize("body", [
    '{"detail":"unsupported model: diagnostic-model"}',
    "upstream unavailable",
    "",
    " \n ",
    None,
])
def test_query_session_preserves_error_body(body):
    c = _client_with(_FakeArtifact([
        ArtifactResponse(output={"error": "HTTP 500: Internal Server Error", "body": body}),
    ]))
    sid = c.create_session()

    with pytest.raises(ArtifactQueryError) as caught:
        c.query_session(sid, ArtifactAgentQuery(text="hi"))

    expected = "artifact returned an error: HTTP 500: Internal Server Error"
    if body and body.strip():
        expected += f" -- {body.strip()}"
    assert str(caught.value) == expected
    assert c._sessions[sid] == []


def test_http_error_detail_is_saved_without_changing_failed_eval(monkeypatch, tmp_path):
    from appsci_bench.eval.artifact_agent_user_sim import ScriptedArtifactAgentUserSim
    from appsci_bench.eval.dataset import parse_dataset
    from appsci_bench.eval.datatypes import TERMINATION_ERROR, Objective
    from appsci_bench.eval.generation import generate
    from appsci_bench.eval.scoring import score_records
    from appsci_bench.eval.store import TranscriptStore

    detail = "unsupported model: diagnostic-model"
    urlopen = Mock(side_effect=HTTPError(
        "http://artifact/respond", 500, "Internal Server Error", {},
        io.BytesIO(json.dumps({"detail": detail}).encode()),
    ))
    monkeypatch.setattr("appsci_bench.eval.artifact.urllib.request.urlopen", urlopen)
    dataset = parse_dataset({"type": "multi_turn", "max_turns": 5}, [
        {"data_point_id": "failure", "multi_turn_input": {"user_task": "t", "user_persona": "p"}},
    ])
    store = TranscriptStore(tmp_path)
    records = generate(
        dataset, HttpArtifactClient("http://artifact/respond"),
        user_sim=ScriptedArtifactAgentUserSim(["hi", "try again"]),
        store=store, query_timeout_s=None, max_workers=1,
    )

    assert records[0].termination_reason == TERMINATION_ERROR
    assert len(records[0].conversation) == 1
    urlopen.assert_called_once()  # Error reporting must not retry or continue the conversation.
    saved = json.loads((store.conversations_dir / "failure__r0.json").read_text())
    assert detail in saved["error"]
    assert "HTTP 500: Internal Server Error" in saved["error"]

    evaluate = Mock(return_value=(1.0, {}))
    objective = Objective("quality", "unused", True, 1.0, eval_function=evaluate)
    results = score_records(records, dataset, [objective])
    assert results[0].score == 0.0
    assert detail in results[0].error
    evaluate.assert_not_called()  # Generation failures still bypass task scorers and judges.


def test_empty_agent_turn_not_persisted_as_placeholder_and_dropped_on_wire():
    # An empty agent reply is stored truthfully (empty), never as a "(no content)" placeholder, so it
    # can't be re-sent verbatim and corrupt the model's view on later turns; it's dropped on the wire.
    c = _client_with(_FakeArtifact([
        ArtifactResponse(output=None, messages=[{"role": "assistant", "content": ""}]),
        ArtifactResponse(output=None, messages=[{"role": "assistant", "content": "hello"}]),
    ]))
    sid = c.create_session()
    c.query_session(sid, ArtifactAgentQuery(text="turn1"))
    c.query_session(sid, ArtifactAgentQuery(text="turn2"))
    stored = c._sessions[sid]
    assert not any("(no content)" in (m["content"] or "") for m in stored)   # no placeholder persisted
    assert stored[1] == {"role": "assistant", "content": ""}                 # empty kept truthfully
    second_wire = c._artifact.sent_histories[1]                              # history sent on turn2
    assert all((m["content"] or "").strip() for m in second_wire)            # no empty block on the wire


def test_history_for_send_drops_empties_and_coalesces_same_role():
    hist = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "   "},   # whitespace-only -> dropped
        {"role": "user", "content": "b"},          # now adjacent to prior user -> coalesced
    ]
    assert _history_for_send(hist) == [{"role": "user", "content": "a\nb"}]


def test_query_session_fills_the_json_slot_when_the_reply_is_a_json_object():
    # The completions transport has one channel back (the assistant content), so the client fills the
    # structured half from it: `text` stays the artifact's full reply, `json` carries the same object.
    # Without this every structured objective scores against an empty payload over this transport.
    content = '{"flag": true, "fault_type": "DUPLICATE", "reason": "duplicate of INV-3350"}'
    c = _client_with(_FakeArtifact([
        ArtifactResponse(output={"response": content},
                         messages=[{"role": "assistant", "content": content}])]))
    out = c.query_session(c.create_session(), ArtifactAgentQuery(text="review it"))
    assert out.text == content                                   # unchanged: judges see what they saw
    assert json.loads(out.json)["fault_type"] == "DUPLICATE"      # and structured objectives can score


def test_query_session_leaves_json_none_when_the_reply_narrates_around_a_payload():
    # A reply that isn't a bare object leaves the slot EMPTY -- the client does not fish an object out
    # of the prose. With no schema to reject a wrong candidate, picking one is a guess: an artifact
    # that prints a DB row before its verdict would hand the evaluator the row. The task's own metrics
    # report the empty slot as the contract failure it is.
    content = ('I looked up the row:\n{"invoice_number": "INV-3350", "amount": 134568.0}\n'
               'Final answer:\n{"flag": true, "fault_type": "DUPLICATE"}')
    c = _client_with(_FakeArtifact([
        ArtifactResponse(output={"response": content},
                         messages=[{"role": "assistant", "content": content}])]))
    out = c.query_session(c.create_session(), ArtifactAgentQuery(text="review it"))
    assert out.text == content        # the full reply is still there for transcript judges
    assert out.json is None           # but nothing is guessed into the structured slot


def test_query_session_leaves_json_none_when_the_reply_carries_no_object():
    content = "No discrepancies found; this invoice matches the PO and contract."
    c = _client_with(_FakeArtifact([
        ArtifactResponse(output={"response": content},
                         messages=[{"role": "assistant", "content": content}])]))
    out = c.query_session(c.create_session(), ArtifactAgentQuery(text="review it"))
    assert out.text == content and out.json is None


def test_json_slot_is_populated_per_turn_in_a_multi_turn_session():
    c = _client_with(_FakeArtifact([
        ArtifactResponse(output=None, messages=[{"role": "assistant", "content": '{"step": 1}'}]),
        ArtifactResponse(output=None, messages=[{"role": "assistant", "content": '{"step": 2}'}]),
    ]))
    sid = c.create_session()
    first = c.query_session(sid, ArtifactAgentQuery(text="t1"))
    second = c.query_session(sid, ArtifactAgentQuery(text="t2"))
    assert json.loads(first.json)["step"] == 1
    assert json.loads(second.json)["step"] == 2
