"""Gap-filling tests found during the post-Part-1 coverage review: validation edges, the deferred
setup_state hook, session cleanup, the legacy artifact bridge variants, transport stubs."""
from __future__ import annotations

import json

import pytest

from appsci_bench.eval.artifact import ArtifactResponse, InProcessArtifact
from appsci_bench.eval.artifact_client import (
    HttpArtifactClient, InProcessArtifactClient, McpArtifactClient,
)
from appsci_bench.eval.dataset import load_dataset, parse_dataset
from appsci_bench.eval.generation import generate
from appsci_bench.eval.eval_system import artifact_to_client
from appsci_bench.eval.scoring import score_records
from appsci_bench.eval.datatypes import (
    ArtifactAgentOutput, ArtifactAgentQuery, ConversationTurn, EvalTypeError, File,
    GenerationRecord, MultiTurnInput, Objective,
)
from tests.fixtures.eval_task import eval_fns


# --- dataset validation edges -----------------------------------------------------------------
def test_sample_missing_data_point_id_raises():
    with pytest.raises(EvalTypeError):
        parse_dataset({"type": "single_turn"}, [{"single_turn_input": {"text": "x"}}])


def test_load_dataset_missing_directory_raises(tmp_path):
    spec = {"evaluation_data": {"type": "single_turn", "dataset": "nope"}}
    with pytest.raises(EvalTypeError):
        load_dataset(spec, repo_root=tmp_path)


def test_load_dataset_neither_data_nor_pointer_raises():
    with pytest.raises(EvalTypeError):
        load_dataset({"evaluation_data": {"type": "single_turn"}})


def test_load_dataset_no_evaluation_data_block_raises():
    with pytest.raises(EvalTypeError):
        load_dataset({"task_id": "t"})


def test_load_dataset_from_taskspec_object():
    from types import SimpleNamespace
    spec = SimpleNamespace(task_id="t", evaluation_data={
        "type": "single_turn", "data": [{"data_point_id": "d1", "single_turn_input": {"text": "x"}}]})
    ds = load_dataset(spec)
    assert ds.data[0].data_point_id == "d1"


# --- generation: deferred setup_state hook + session cleanup ----------------------------------
def test_generation_calls_setup_state_and_deletes_sessions():
    class TrackingClient(InProcessArtifactClient):
        def __init__(self, fn):
            super().__init__(fn)
            self.deleted = []
        def delete_session(self, sid):
            self.deleted.append(sid)
            return super().delete_session(sid)

    client = TrackingClient(lambda q: ArtifactAgentOutput(text="ok"))
    ds = parse_dataset({"type": "single_turn", "repeat": 2}, [
        {"data_point_id": "d1", "single_turn_input": {"text": "a"}},
        {"data_point_id": "d2", "single_turn_input": {"text": "b"}}])
    seen = []
    generate(ds, client, setup_state=lambda s: seen.append(s.data_point_id))
    assert set(seen) == {"d1", "d2"}                 # hook called once per (sample, repeat) unit start
    assert len(client.deleted) == 4                  # every session deleted in finally (2 dp x 2 repeats)


# --- scoring: record with no matching dataset sample ------------------------------------------
def test_scoring_no_matching_sample():
    ds = parse_dataset({"type": "single_turn"}, [{"data_point_id": "d1", "single_turn_input": {"text": "x"}}])
    ghost = GenerationRecord("ghost", 0, conversation=[
        ConversationTurn(ArtifactAgentQuery(text="x"), ArtifactAgentOutput(text="y"))])
    obj = Objective("m", "x.m", True, 1.0, eval_function=eval_fns.always_one)
    res = score_records([ghost], ds, [obj])
    assert res[0].score == 0.0 and "no dataset sample matches" in res[0].error


# --- orchestrator: legacy artifact bridge variants + non-adaptable -----------------------------
def test_legacy_bridge_string_output():
    legacy = InProcessArtifact(lambda q: ArtifactResponse(output="plain text"))
    client = artifact_to_client(legacy)
    sid = client.create_session()
    assert client.query_session(sid, ArtifactAgentQuery(text="x")).text == "plain text"


def test_legacy_bridge_dict_without_known_keys_goes_to_json():
    legacy = InProcessArtifact(lambda q: ArtifactResponse(output={"foo": 1, "bar": 2}))
    client = artifact_to_client(legacy)
    sid = client.create_session()
    out = client.query_session(sid, ArtifactAgentQuery(text="x"))
    assert json.loads(out.json) == {"foo": 1, "bar": 2}


def test_artifact_to_client_rejects_non_adaptable():
    with pytest.raises(EvalTypeError):
        artifact_to_client(42)


# --- transport stubs: query + delete also raise ------------------------------------------------
@pytest.mark.parametrize("client", [McpArtifactClient("mcp://x")])
def test_transport_stub_query_and_delete_raise(client):
    with pytest.raises(NotImplementedError):
        client.query_session("s", ArtifactAgentQuery(text="x"))
    with pytest.raises(NotImplementedError):
        client.delete_session("s")


# --- types: round-trips not previously covered -------------------------------------------------
def test_generation_record_roundtrip_with_file():
    rec = GenerationRecord("d1", 2, conversation=[ConversationTurn(
        ArtifactAgentQuery(text="q", files=[File("txt", "a.txt", b"hi")]),
        ArtifactAgentOutput(json='{"k": 1}'))], error="oops")
    assert GenerationRecord.from_dict(rec.to_dict()) == rec


def test_eval_sample_multi_turn_roundtrip():
    from appsci_bench.eval.datatypes import EvalSample
    s = EvalSample("m1", multi_turn_input=MultiTurnInput(user_task="t", user_persona="p"))
    s2 = EvalSample.from_dict(s.to_dict())
    assert s2 == s and s2.single_turn_input is None
