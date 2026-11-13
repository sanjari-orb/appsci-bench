"""Accounting outages must not turn into task scores or leak state across attempts."""
import json
import threading
from collections import Counter
from unittest.mock import Mock

import pytest

from appsci_bench.eval.artifact_agent_user_sim import ScriptedArtifactAgentUserSim
from appsci_bench.eval.artifact_client import InProcessArtifactClient
from appsci_bench.eval.dataset import parse_dataset
from appsci_bench.eval.datatypes import GenerationRecord
from appsci_bench.eval.eval_system import run_evaluation
from appsci_bench.eval.generation import GenerationIncompleteError, generate
from appsci_bench.eval.lanes import EvalLane, LanePool
from appsci_bench.eval.step_budget import LaneCleanupError, LocalStepBudget, StepBudgetUnavailable
from appsci_bench.eval.store import TranscriptStore
from appsci_bench.orchestrator.env_admin import EnvAdminError
from appsci_bench.orchestrator.eval_step_budget import EnvironmentStepBudget
from appsci_bench.orchestrator.run import persist_eval_report


def _spec(*ids, repeat=1):
    return {"task_id": "recovery", "evaluation_data": {
        "type": "multi_turn", "max_steps": 200, "repeat": repeat,
        "data": [{"data_point_id": case, "multi_turn_input": {
            "user_task": "t", "user_persona": "p"}, "expected_output": {"text": "ok"}}
                 for case in ids],
        "objectives": [{"name": "match", "positive": True, "weight": 1.0,
                        "eval_function_path": "eval_task.eval_fns.text_exact_match"}],
    }}


class _Environment:
    def __init__(self, *, persistent=False, stage="between_turns"):
        self.attempts = Counter()
        self.persistent = persistent
        self.stage = stage
        self.client = InProcessArtifactClient(self.respond)
        self.client.create_session = Mock(wraps=self.client.create_session)
        self.client.delete_session = Mock(wraps=self.client.delete_session)
        self.sim = ScriptedArtifactAgentUserSim(["hello", "again"])
        self.sim.create_session = Mock(wraps=self.sim.create_session)
        self.sim.delete_session = Mock(wraps=self.sim.delete_session)
        self.cancel = Mock()
        self.final_reads = []
        self.lane = EvalLane(client=self.client, setup_state=self.reset,
                            read_final_state=self.final_state, user_sim=self.sim,
                            step_budget_factory=lambda limit: EnvironmentStepBudget(self.admin, limit),
                            cancel_query=self.cancel)

    def reset(self, sample):
        self.case = sample.data_point_id
        self.attempts[self.case] += 1
        self.steps = 0
        self.writes = []
        self.stop_reads = 0

    def respond(self, query):
        self.writes.append(query.text)
        return "ok"

    def admin(self, method, path, payload=None):
        if method == "POST":
            if path.endswith("/advance"):
                self.steps += payload["steps"]
            else:
                self.steps = 0
        else:
            if self.steps == 5:
                self.stop_reads += 1
            should_fail = {"before_usersim": self.steps == 0,
                           "between_turns": self.steps == 3,
                           "final_snapshot": self.steps == 5 and self.stop_reads == 2}[self.stage]
            if (self.case == "bad" and should_fail
                    and (self.persistent or self.attempts[self.case] == 1)):
                raise EnvAdminError("counter read retries exhausted: docker exec timed out")
        return 200, json.dumps({"steps": self.steps, "max_steps": 200})

    def final_state(self, sample):
        self.final_reads.append(sample.data_point_id)
        return {"writes": list(self.writes)}


@pytest.mark.parametrize("stage", ["before_usersim", "between_turns", "final_snapshot"])
def test_read_failure_reruns_a_clean_case_and_scores_only_the_completed_attempt(tmp_path, stage):
    env = _Environment(stage=stage)
    report = run_evaluation(_spec("bad", "healthy"), env.client, user_sim=env.sim,
                            lanes=LanePool.exclusive([env.lane]), package="tests.fixtures",
                            workdir=str(tmp_path), conformance="off")
    assert report.cases_scored == 2 and not report.partial
    assert env.attempts == {"bad": 2, "healthy": 1}
    assert env.final_reads.count("bad") == 1
    assert env.client.create_session.call_count == env.client.delete_session.call_count == 3
    assert env.sim.create_session.call_count == env.sim.delete_session.call_count == 3
    env.cancel.assert_not_called()  # all failures here occur outside an artifact request
    store = TranscriptStore(tmp_path)
    saved = store.load(tmp_path / "conversations/bad__r0.json")
    assert saved["generation_attempt"] == 2
    assert saved["final_state"] == {"writes": ["hello", "again"]}
    assert len(saved["conversation"]) == 2
    failed = json.loads((tmp_path / "infrastructure_attempts/bad__r0__a1.json").read_text())
    assert failed["termination_reason"] == "infrastructure_error"
    assert failed["error"] is None and failed["infrastructure_error"]
    assert GenerationRecord.from_dict(failed).infrastructure_error == failed["infrastructure_error"]
    assert len(store.list_results()) == len(store.list_conversations()) == 2
    assert any("infrastructure retry" in warning for warning in report.warnings)
    dest = tmp_path / "durable"
    persist_eval_report(report, dest)
    assert json.loads((dest / "infrastructure_attempts/bad__r0__a1.json").read_text()) == failed


def test_persistent_failure_is_bounded_unscored_and_preserves_healthy_results(tmp_path):
    env = _Environment(persistent=True)
    with pytest.raises(GenerationIncompleteError, match="infrastructure failures.*completed 2/4"):
        run_evaluation(_spec("bad", "healthy", repeat=2), env.client, user_sim=env.sim,
                       lanes=LanePool.exclusive([env.lane]), package="tests.fixtures",
                       workdir=str(tmp_path), conformance="off")
    assert env.attempts == {"bad": 4, "healthy": 2}  # exactly two attempts per repeat
    doc = json.loads((tmp_path / "report.json").read_text())
    assert doc["partial"] and doc["cases_scored"] == 2 and doc["cases_total"] == 4
    assert {row["data_point_id"] for row in doc["results"]} == {"healthy"}
    assert len(list((tmp_path / "infrastructure_attempts").glob("*.json"))) == 4
    assert {p.stem for p in (tmp_path / "results").glob("*.json")} == {"healthy__r0", "healthy__r1"}
    assert env.final_reads == ["healthy", "healthy"]


def test_failed_retry_reset_does_not_turn_the_original_outage_into_a_zero(tmp_path):
    resets = []
    client = InProcessArtifactClient(lambda query: "ok")

    def reset(sample):
        resets.append(sample.data_point_id)
        if len(resets) == 2:
            raise EnvAdminError("environment still unavailable during reset")

    factory = Mock(side_effect=StepBudgetUnavailable("counter unavailable"))
    lane = EvalLane(client=client, setup_state=reset, step_budget_factory=factory)
    lanes = LanePool.exclusive([lane])
    with pytest.raises(GenerationIncompleteError, match="completed 0/1"):
        run_evaluation(_spec("case"), client, user_sim=ScriptedArtifactAgentUserSim(["hello"]),
                       lanes=lanes, workdir=str(tmp_path), package="tests.fixtures", conformance="off")
    assert resets == ["case", "case"]
    factory.assert_called_once()
    assert lanes.retired_lanes
    assert not list((tmp_path / "results").glob("*.json"))
    assert len(list((tmp_path / "infrastructure_attempts").glob("*.json"))) == 2


@pytest.mark.parametrize("recovery_fails", [False, True])
def test_inflight_request_is_contained_before_case_retry(tmp_path, recovery_fails):
    active = threading.Event()
    stopped = threading.Event()
    events = []
    attempt = [0]

    def setup(sample):
        attempt[0] += 1
        events.append("reset")
        if attempt[0] > 1:
            assert stopped.is_set(), "retry reset while artifact could still mutate the DB"

    def respond(query):
        if attempt[0] == 1:
            active.set()
            assert stopped.wait(5)
            return "discard this late output"
        return "ok"

    def cancel():
        events.append("stop")
        stopped.set()
        if recovery_fails:
            raise LaneCleanupError("restart failed")

    class Budget(LocalStepBudget):
        def snapshot(self):
            if attempt[0] == 1 and active.is_set():
                raise StepBudgetUnavailable("counter unavailable during /respond")
            return super().snapshot()

    client = InProcessArtifactClient(respond)
    broken = EvalLane(client=client, setup_state=setup, step_budget_factory=Budget,
                      cancel_query=cancel, label="broken")
    healthy = EvalLane(client=InProcessArtifactClient(lambda query: "ok"), label="healthy",
                       step_budget_factory=LocalStepBudget)
    lanes = LanePool.exclusive([broken, healthy] if recovery_fails else [broken])
    config = _spec("case")["evaluation_data"]
    rec, = generate(parse_dataset(config, config["data"]), client,
                    user_sim=ScriptedArtifactAgentUserSim(["hello"]), lanes=lanes,
                    store=TranscriptStore(tmp_path), query_timeout_s=5)
    assert rec.error is None and rec.generation_attempt == 2
    assert rec.conversation[0].response.text == "ok"
    assert events == (["reset", "stop"] if recovery_fails else ["reset", "stop", "reset"])
    assert bool(lanes.retired_lanes) is recovery_fails


@pytest.mark.parametrize("other_lane", [False, True])
@pytest.mark.parametrize("operation", ["initialize", "advance"])
def test_uncertain_counter_write_never_reuses_its_environment(tmp_path, other_lane, operation):
    setups = Mock()

    def admin(method, path, payload=None):
        if method == "POST" and (operation == "initialize" or path.endswith("/advance")):
            raise EnvAdminError("POST timed out; may still apply")
        return 200, '{"steps": 0, "max_steps": 200}'

    bad_admin = Mock(side_effect=admin)
    client = InProcessArtifactClient(lambda query: "ok")
    bad = EvalLane(client=client, setup_state=setups, label="uncertain",
                   step_budget_factory=lambda limit: EnvironmentStepBudget(bad_admin, limit))
    healthy = EvalLane(client=client, step_budget_factory=LocalStepBudget, label="healthy")
    lanes = LanePool.exclusive([bad, healthy] if other_lane else [bad])
    config = _spec("case")["evaluation_data"]

    def run():
        return generate(parse_dataset(config, config["data"]), client,
                        user_sim=ScriptedArtifactAgentUserSim(["hello"]), lanes=lanes,
                        store=TranscriptStore(tmp_path), max_workers=1)

    if other_lane:
        rec, = run()
        assert rec.generation_attempt == 2 and rec.lane == "healthy"
    else:
        with pytest.raises(GenerationIncompleteError, match="completed 0/1"):
            run()
    setups.assert_called_once()
    assert bad_admin.call_count == (1 if operation == "initialize" else 3)
    assert bad_admin.call_args.args[0] == "POST"  # no further read can mask the uncertain write
    assert "uncertain" in lanes.retired_lanes


def test_artifact_error_is_scored_once_without_infrastructure_retry(tmp_path):
    def respond(query):
        raise RuntimeError("artifact bug")

    client = InProcessArtifactClient(respond)
    setup = Mock()
    report = run_evaluation(_spec("case"), client, setup_state=setup,
                            user_sim=ScriptedArtifactAgentUserSim(["hello"]),
                            package="tests.fixtures", workdir=str(tmp_path), conformance="off")
    setup.assert_called_once()
    assert report.cases_scored == 1 and not report.partial
    assert report.final_score == 0
    assert not (tmp_path / "infrastructure_attempts").exists()
