"""Counter reads can be replayed; mutations with uncertain completion cannot."""
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from appsci_bench.orchestrator import env_admin
from appsci_bench.orchestrator.eval_step_budget import EnvironmentStepBudget
from appsci_bench.eval.step_budget import MaxStepsReached, StepBudgetUnavailable


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]

    def advance(seconds):
        now[0] += seconds

    monkeypatch.setattr(env_admin, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=advance))
    return now, advance


def _result(stdout='200\n{"steps": 3, "max_steps": 200}', *, error=None):
    return subprocess.CompletedProcess([], int(error is not None), stdout, error or "")


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired(["docker", "exec", "ADMIN_TOKEN=secret"], 20),
    OSError("unavailable"),
    _result(error="Docker daemon unavailable"),
    _result("503\nunavailable"),
    _result("invalid status"),
])
def test_counter_read_recovers_without_replaying_a_mutation(monkeypatch, clock, failure):
    docker = Mock(side_effect=[failure, _result()])
    monkeypatch.setattr(env_admin.subprocess, "run", docker)
    admin = env_admin.make_env_admin_request("lane-env", "secret")
    assert admin("GET", "/admin/simulation_steps") == (200, '{"steps": 3, "max_steps": 200}')
    assert docker.call_count == 2
    for call in docker.call_args_list:
        argv = call.args[0]
        assert "ADMIN_METHOD=GET" in argv
        assert "-i" not in argv  # reads do not attach/wait for stdin
        assert call.kwargs["timeout"] <= 20


def test_timeouts_share_a_single_deadline_and_do_not_leak_credentials(monkeypatch, clock, caplog):
    now, advance = clock
    timeouts = []

    def stalled(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        advance(kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(env_admin.subprocess, "run", stalled)
    admin = env_admin.make_env_admin_request("lane-env", "private-token")
    with pytest.raises(env_admin.EnvAdminError, match="retries exhausted") as caught:
        admin("GET", "/admin/simulation_steps")
    assert timeouts == [20, 20, 19.25]
    assert now[0] == 60
    assert "private-token" not in str(caught.value) + caplog.text


@pytest.mark.parametrize("stdout,stderr", [("", "private-token"), ("private-token", None)])
def test_transport_diagnostics_redact_tokens(monkeypatch, clock, stdout, stderr, caplog):
    monkeypatch.setattr(env_admin.subprocess, "run", Mock(return_value=_result(stdout, error=stderr)))
    with pytest.raises(env_admin.EnvAdminError) as caught:
        env_admin.make_env_admin_request("lane-env", "private-token")("GET", "/admin/simulation_steps")
    assert "private-token" not in str(caught.value) + caplog.text


@pytest.mark.parametrize("method,path,payload", [
    ("POST", "/admin/simulation_steps", {"max_steps": 200}),
    ("POST", "/admin/simulation_steps/advance", {"steps": 1}),
    ("POST", "/admin/reset", {}),
    ("GET", "/admin/state_hash", None),
])
def test_only_counter_get_is_retried(monkeypatch, clock, method, path, payload):
    docker = Mock(side_effect=subprocess.TimeoutExpired(["docker"], 60))
    monkeypatch.setattr(env_admin.subprocess, "run", docker)
    with pytest.raises(env_admin.EnvAdminError):
        env_admin.make_env_admin_request("lane-env", "secret")(method, path, payload)
    docker.assert_called_once()
    assert docker.call_args.kwargs["timeout"] == 60
    if method == "POST":
        assert "-i" in docker.call_args.args[0]
        assert json.loads(docker.call_args.kwargs["input"]) == payload


@pytest.mark.parametrize("status", [401, 404, 409])
def test_nontransient_http_responses_are_not_retried(monkeypatch, clock, status):
    docker = Mock(return_value=_result(f"{status}\nerror"))
    monkeypatch.setattr(env_admin.subprocess, "run", docker)
    assert env_admin.make_env_admin_request("lane-env", "secret")(
        "GET", "/admin/simulation_steps") == (status, "error")
    docker.assert_called_once()


@pytest.mark.parametrize("operation", ["initialize", "consume", "snapshot"])
def test_accounting_transport_failure_is_infrastructure_and_flags_uncertain_writes(operation):
    admin = Mock(return_value=(200, '{"steps": 0, "max_steps": 200}'))
    budget = EnvironmentStepBudget(admin, 200)
    admin.side_effect = subprocess.TimeoutExpired(["docker", "ADMIN_TOKEN=secret"], 60)
    action = {"initialize": lambda: EnvironmentStepBudget(admin, 200),
              "consume": budget.consume, "snapshot": budget.snapshot}[operation]
    with pytest.raises(StepBudgetUnavailable) as caught:
        action()
    assert caught.value.unsafe_to_reuse_lane is (operation != "snapshot")
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("status,body", [(503, "offline"), (200, "not json"),
                                        (200, "{}"), (200, "[]"),
                                        (200, '{"steps": true, "max_steps": 200}')])
def test_unusable_counter_response_is_infrastructure(status, body):
    admin = Mock(return_value=(200, '{"steps": 0, "max_steps": 200}'))
    budget = EnvironmentStepBudget(admin, 200)
    admin.return_value = status, body
    with pytest.raises(StepBudgetUnavailable):
        budget.snapshot()


def test_actual_step_cutoff_remains_normal_termination():
    with pytest.raises(MaxStepsReached):
        EnvironmentStepBudget(lambda *args: (409, "limit"), 200)
