"""A failed restart must cost one lane, not other cases or their saved scores."""
import json
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from appsci_bench.eval.artifact_agent_user_sim import ScriptedArtifactAgentUserSim
from appsci_bench.eval.artifact_client import ArtifactQueryError, HttpArtifactClient, InProcessArtifactClient
from appsci_bench.eval.dataset import parse_dataset
from appsci_bench.eval.datatypes import ArtifactAgentOutput, ArtifactAgentQuery, GenerationRecord
from appsci_bench.eval.eval_system import run_evaluation
from appsci_bench.eval.generation import GenerationIncompleteError, _query_with_timeout, generate
from appsci_bench.eval.lanes import EvalLane, LanePool, NoAvailableLanes
from appsci_bench.eval.step_budget import LaneCleanupError, LocalStepBudget
from appsci_bench.eval.store import TranscriptStore


def _spec(n):
    return {"task_id": "recovery", "evaluation_data": {
        "type": "multi_turn", "max_steps": 3,
        "data": [{"data_point_id": f"case-{i}",
                  "multi_turn_input": {"user_task": "t", "user_persona": "p"},
                  "expected_output": {"text": "ok"}} for i in range(n)],
        "objectives": [{"name": "match", "positive": True, "weight": 1.0,
                        "eval_function_path": "eval_task.eval_fns.text_exact_match"}],
    }}


def _broken_lane(recovered, *, label="broken"):
    current = {}
    stopped = threading.Event()
    setups = []

    def factory(limit):
        current["budget"] = LocalStepBudget(limit)
        return current["budget"]

    def respond(query):
        current["budget"].consume(2)  # opening message + tool pair exhausts three steps
        assert stopped.wait(5)
        return ArtifactAgentOutput(text="late response must be discarded")

    def cancel():
        stopped.set()
        recovered.set()
        raise LaneCleanupError("restart health check failed")

    return EvalLane(client=InProcessArtifactClient(respond), label=label,
                    setup_state=lambda sample: setups.append(sample.data_point_id),
                    read_final_state=lambda sample: {"writes": 1},
                    step_budget_factory=factory, cancel_query=cancel), setups


def _healthy_lane(recovered):
    def respond(query):
        assert recovered.wait(5), "broken lane never reached recovery"
        return ArtifactAgentOutput(text="ok")

    return EvalLane(client=InProcessArtifactClient(respond), label="healthy",
                    setup_state=lambda sample: None,
                    read_final_state=lambda sample: {"writes": 0},
                    step_budget_factory=LocalStepBudget)


def test_failed_recovery_preserves_cutoff_record_and_all_other_cases(tmp_path):
    recovered = threading.Event()
    broken, setups = _broken_lane(recovered)
    lanes = LanePool.exclusive([broken, _healthy_lane(recovered)])
    spec = _spec(6)
    store = TranscriptStore(tmp_path)
    handed_off = []
    recs = generate(parse_dataset(spec["evaluation_data"], spec["evaluation_data"]["data"]),
                    broken.client, user_sim=ScriptedArtifactAgentUserSim(["hi"]), lanes=lanes,
                    store=store, on_record=handed_off.append, query_timeout_s=10)
    assert len(recs) == len(handed_off) == len(store.list_conversations()) == 6
    assert len(setups) == 1  # the old DB was never reset for a second case
    bad, = [r for r in recs if r.lane == "broken"]
    assert bad.termination_reason == "max_steps" and bad.error is None
    assert bad.final_state == {"writes": 1}
    assert bad.conversation[0].response.text is None
    assert bad.lane_cleanup_error == "restart health check failed"
    saved = store.load(next(p for p in store.list_conversations()
                            if store.load(p)["lane"] == "broken"))
    assert GenerationRecord.from_dict(saved).lane_cleanup_error == bad.lane_cleanup_error
    assert lanes.retired_lanes == {"broken": bad.lane_cleanup_error}
    assert all(r.error is None for r in recs)


def test_all_retired_lanes_wake_waiting_cases():
    client = InProcessArtifactClient(lambda q: "ok")
    lane = EvalLane(client=client)
    lanes = LanePool.exclusive([lane])
    waiting = threading.Event()

    def acquire():
        waiting.set()
        with lanes.acquire():
            pytest.fail("retired lane was reused")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with lanes.acquire():
            pending = pool.submit(acquire)
            assert waiting.wait(1)
            lanes.retire(lane, "restart failed")
            with pytest.raises(NoAvailableLanes):
                pending.result(timeout=2)
    with pytest.raises(NoAvailableLanes):
        with lanes.acquire():
            pytest.fail("all-retired pool should refuse immediately")


@pytest.mark.parametrize("healthy,broken_count", [(False, 1), (False, 2), (True, 1)])
def test_reports_and_scores_survive_failed_recovery(tmp_path, healthy, broken_count):
    recovered = threading.Event()
    broken, _ = _broken_lane(recovered)
    broken_lanes = [broken] + [_broken_lane(recovered, label=f"broken-{i}")[0]
                              for i in range(1, broken_count)]
    lanes = LanePool.exclusive(broken_lanes + ([_healthy_lane(recovered)] if healthy else []))
    kwargs = dict(user_sim=ScriptedArtifactAgentUserSim(["hi"]), lanes=lanes,
                  package="tests.fixtures", workdir=str(tmp_path), conformance=False,
                  query_timeout_s=10)
    if healthy:
        report = run_evaluation(_spec(4), broken.client, **kwargs)
        assert not report.partial and report.cases_scored == 4
    else:
        with pytest.raises(GenerationIncompleteError, match=f"completed {broken_count}/4"):
            run_evaluation(_spec(4), broken.client, **kwargs)
    doc = json.loads((tmp_path / "report.json").read_text())
    assert doc["partial"] is (not healthy)
    assert doc["cases_total"] == 4
    assert doc["cases_scored"] == (4 if healthy else broken_count)
    assert doc["eval_lanes"] == broken_count + int(healthy)
    assert len(list((tmp_path / "results").glob("*.json"))) == doc["cases_scored"]
    assert any("broken retired" in warning for warning in doc["warnings"])
    assert {r["data_point_id"] for r in doc["results"]} == (
        {f"case-{i}" for i in range(4 if healthy else broken_count)})


@pytest.mark.parametrize("failure", [TimeoutError("socket timed out"),
                                    urllib.error.URLError("connection closed"),
                                    urllib.error.HTTPError("http://artifact", 500, "error", {}, None)])
def test_finished_transport_failure_still_recovers_but_http_response_does_not(monkeypatch, failure):
    monkeypatch.setattr("appsci_bench.eval.artifact.urllib.request.urlopen", Mock(side_effect=failure))
    client = HttpArtifactClient("http://localhost:123/respond")
    cancel = Mock()
    with pytest.raises(ArtifactQueryError):
        _query_with_timeout(client, client.create_session(), ArtifactAgentQuery(text="hi"), 5,
                            budget=LocalStepBudget(200), cancel_query=cancel)
    assert cancel.call_count == (0 if isinstance(failure, urllib.error.HTTPError) else 1)


def test_harness_request_deadline_recovers_before_lane_reuse():
    stopped = threading.Event()

    def respond(query):
        assert stopped.wait(5)
        return ArtifactAgentOutput(text="late")

    client = InProcessArtifactClient(respond)
    cancel = Mock(side_effect=stopped.set)
    with pytest.raises(TimeoutError):
        _query_with_timeout(client, client.create_session(), ArtifactAgentQuery(text="hi"), .02,
                            budget=LocalStepBudget(200), cancel_query=cancel)
    cancel.assert_called_once()


@pytest.mark.parametrize("limit", [1, 2])
def test_speaker_boundary_does_not_restart_the_artifact(limit):
    spec = _spec(1)["evaluation_data"]
    spec["max_steps"] = limit
    cancel = Mock()
    client = InProcessArtifactClient(lambda q: ArtifactAgentOutput(text="ok"))
    lane = EvalLane(client=client, step_budget_factory=LocalStepBudget, cancel_query=cancel)
    rec, = generate(parse_dataset(spec, spec["data"]), client,
                    user_sim=ScriptedArtifactAgentUserSim(["hi"]), lanes=LanePool.exclusive([lane]))
    assert rec.termination_reason == "max_steps" and rec.error is None
    cancel.assert_not_called()
