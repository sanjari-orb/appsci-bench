"""The environment owns tool accounting and refuses writes after the shared ceiling."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from appsci_bench.eval.artifact_agent_user_sim import ScriptedArtifactAgentUserSim
from appsci_bench.eval.artifact_client import InProcessArtifactClient
from appsci_bench.eval.dataset import parse_dataset
from appsci_bench.eval.generation import generate
from appsci_bench.eval.lanes import EvalLane, LanePool
from appsci_bench.eval.step_budget import check_budget
from appsci_bench.orchestrator.eval_step_budget import EnvironmentStepBudget
from tasks.banking_support.environment.service import create_app
from tasks.banking_support.environment.simulation_steps import SimulationSteps, StepLimitReached


class FakeEnvironment:
    def __init__(self):
        self.writes = 0

    def fresh(self):
        return FakeEnvironment()

    def execute(self, name, arguments, requestor):
        if name == "bad":
            raise ValueError("bad tool")
        self.writes += 1
        return self.writes


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setenv("BANKING_ENV_TOKEN", "tool")
    monkeypatch.setenv("BANKING_ENV_ADMIN_TOKEN", "admin")
    client = TestClient(create_app(FakeEnvironment()))

    def admin(method, path, payload=None):
        r = client.request(method, path, json=payload, headers={"Authorization": "Bearer admin"})
        return r.status_code, r.text

    def tool(name="write", requestor="assistant"):
        return client.post(f"/tools/{name}", json={"requestor": requestor},
                           headers={"Authorization": "Bearer tool"})

    return client, admin, tool


def test_user_and_agent_tools_and_errors_share_the_same_counter(service):
    client, admin, tool = service
    budget = EnvironmentStepBudget(admin, 7)
    budget.consume()  # opening customer message
    assert tool().status_code == 200
    assert tool(requestor="user").status_code == 200
    assert tool("bad").status_code == 400
    assert budget.snapshot()["steps"] == 7
    for requestor in ("assistant", "user"):
        assert tool(requestor=requestor).status_code == 409
    assert client.app.state.env.writes == 2
    assert len(client.app.state.action_trace) == 2  # failed/blocked calls cannot satisfy ACTION


def test_result_completes_one_step_past_limit_and_reset_starts_fresh(service):
    client, admin, tool = service
    budget = EnvironmentStepBudget(admin, 2)
    budget.consume()
    assert tool().status_code == 200
    assert budget.snapshot()["steps"] == 3
    assert tool().status_code == 409
    admin("POST", "/admin/reset")
    next_budget = EnvironmentStepBudget(admin, 200)
    assert next_budget.snapshot()["steps"] == 0
    assert client.app.state.env.writes == 0
    assert tool().status_code == 200


def test_counter_is_disabled_for_build_exploration(service):
    _, admin, tool = service
    assert tool().status_code == 200
    assert '"max_steps":null' in admin("GET", "/admin/simulation_steps")[1]


def test_counter_controls_are_admin_only_and_hidden(service):
    client, _, _ = service
    paths = client.get("/openapi.json").json()["paths"]
    assert not any("simulation_steps" in p for p in paths)
    for method, path, payload in [
        ("GET", "/admin/simulation_steps", None),
        ("POST", "/admin/simulation_steps", {"max_steps": 200}),
        ("POST", "/admin/simulation_steps/advance", {"steps": 1}),
    ]:
        assert client.request(method, path, json=payload,
                              headers={"Authorization": "Bearer tool"}).status_code == 401


def test_simultaneous_tool_requests_cannot_overrun_budget(service):
    client, admin, tool = service
    budget = EnvironmentStepBudget(admin, 6)
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: tool().status_code, range(12)))
    assert statuses.count(200) == 3
    assert statuses.count(409) == 9
    assert client.app.state.env.writes == 3
    assert budget.snapshot()["steps"] == 6


def test_pending_environment_result_is_not_cut_off():
    counter = SimulationSteps()
    counter.reset(1)
    with counter.tool():
        assert counter.snapshot() == {"steps": 1, "max_steps": 1, "pending_environment": True}
        check_budget(counter)  # upstream skips termination while waiting for environment results
    assert counter.snapshot()["steps"] == 2
    with pytest.raises(StepLimitReached):
        with counter.tool():
            pytest.fail("executed a tool after exhaustion")


def test_status_poll_does_not_block_on_a_slow_tool():
    counter = SimulationSteps()
    counter.reset(200)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with counter.tool():
            state = pool.submit(counter.snapshot).result(timeout=1)
            assert state["pending_environment"] is True


@pytest.mark.parametrize("raise_on_cutoff", [False, True])
def test_limit_ends_an_inflight_artifact_and_keeps_the_lane_reusable(service, raise_on_cutoff):
    client, admin, tool = service
    stopped = threading.Event()
    cancels = []
    budgets = []

    def setup(sample):
        stopped.clear()
        admin("POST", "/admin/reset")

    def factory(limit):
        budget = EnvironmentStepBudget(admin, limit)
        budgets.append(budget)
        return budget

    def artifact(query):
        assert tool().status_code == 200
        assert tool().status_code == 200
        assert tool().status_code == 409
        if raise_on_cutoff:
            raise RuntimeError("artifact surfaced its tool's HTTP error")
        assert stopped.wait(5), "harness did not cancel a query that exhausted the step budget"
        return "late answer"

    def cancel():
        cancels.append(True)
        stopped.set()

    artifact_client = InProcessArtifactClient(artifact)
    lane = EvalLane(client=artifact_client, setup_state=setup,
                    read_final_state=lambda _: {"writes": client.app.state.env.writes},
                    step_budget_factory=factory, cancel_query=cancel)
    ds = parse_dataset({"type": "multi_turn", "max_steps": 5, "repeat": 2}, [{
        "data_point_id": "case", "multi_turn_input": {"user_task": "t", "user_persona": "p"},
    }])
    recs = generate(ds, artifact_client, user_sim=ScriptedArtifactAgentUserSim(["hi"]),
                    lanes=LanePool.exclusive([lane]), query_timeout_s=10)
    if not raise_on_cutoff:
        assert len(cancels) == 2
    assert all(r.termination_reason == "max_steps" and r.error is None for r in recs)
    assert all(r.simulation_steps["steps"] == 5 and r.final_state == {"writes": 2} for r in recs)
    assert all(len(r.conversation) == 1 for r in recs)


def test_real_simulator_tools_are_not_double_counted(service, monkeypatch):
    import json
    from appsci_bench.common.model_config import ModelConfig
    from appsci_bench.eval.artifact_agent_user_sim import ArtifactAgentUserSim

    _, admin, tool = service
    replies = iter([
        *[{"stopped": False, "message": "", "call_user_tool": {"name": "write", "arguments": {}}}] * 17,
        {"stopped": False, "message": "Done"},
        {"stopped": True, "message": ""},
    ])
    monkeypatch.setattr("appsci_bench.eval.helpers.llm.complete",
                        lambda *args, **kwargs: json.dumps({"reasoning": "test", **next(replies)}))
    sim = ArtifactAgentUserSim(config=ModelConfig(model="fake/model"),
                               tool_executor=lambda name, arguments: tool(name, "user").json())
    client = InProcessArtifactClient(lambda _: "ok")
    lane = EvalLane(client=client,
                    step_budget_factory=lambda limit: EnvironmentStepBudget(admin, limit))
    ds = parse_dataset({"type": "multi_turn", "max_steps": 200}, [{
        "data_point_id": "case", "multi_turn_input": {
            "user_task": "t", "user_persona": "p", "user_tools": ["write"],
        },
    }])
    rec, = generate(ds, client, user_sim=sim, lanes=LanePool.exclusive([lane]))
    assert rec.termination_reason == "user_stop"
    assert rec.error is None
    assert rec.simulation_steps["steps"] == 37  # 17 * 2 + customer text + agent text + stop
