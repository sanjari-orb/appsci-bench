"""Unit tests for DockerAppsciAgentUserSimService (appsci_bench/core/appsci_agent_usersim.py) --
docker run command construction and lifecycle (start/stop/health-wait). subprocess is stubbed
throughout (no real docker involved)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from appsci_bench.core.appsci_agent_usersim import DockerAppsciAgentUserSimService


class _R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _svc(**overrides):
    defaults = dict(
        task_id="order_to_cash",
        spec_path="/tmp/spec.yaml",
        persona_path="/tmp/persona.yaml",
    )
    defaults.update(overrides)
    return DockerAppsciAgentUserSimService(**defaults)


def test_container_name_is_usersim_prefixed_task_id():
    svc = _svc(task_id="airline_rebooking")
    assert svc.container_name == "appsci-usersim-airline_rebooking"


def test_start_removes_stale_container_then_runs_and_waits_for_health():
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.appsci_agent_usersim.subprocess.run", side_effect=fake_run):
        svc = _svc(task_id="t1").start()
    assert calls[0] == ["docker", "rm", "-f", "appsci-usersim-t1"]
    run_cmd = calls[1]
    assert run_cmd[:3] == ["docker", "run", "-d"]
    assert "--name" in run_cmd and "appsci-usersim-t1" in run_cmd
    # last call is the health check (docker exec ...)
    assert calls[-1][:2] == ["docker", "exec"]
    assert svc.url == "http://appsci-usersim-t1:8000"
    assert svc.token  # a fresh per-run token was generated


def test_start_mounts_spec_and_persona_readonly():
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.appsci_agent_usersim.subprocess.run", side_effect=fake_run):
        _svc(spec_path="/a/spec.yaml", persona_path="/a/persona.yaml").start()
    run_cmd = calls[1]
    assert "/a/spec.yaml:/spec/spec.yaml:ro" in run_cmd
    assert "/a/persona.yaml:/spec/persona.yaml:ro" in run_cmd


def test_start_passes_through_model_credentials(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "gw-key")
    monkeypatch.setenv("MODEL_BASE_URL", "https://gw.example.com")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.appsci_agent_usersim.subprocess.run", side_effect=fake_run):
        _svc(model="claude-opus-4-8").start()
    run_cmd = calls[1]
    # This sim's own model var, not the shared MODEL_NAME (which stays its fallback).
    assert "APPSCI_USERSIM_MODEL=claude-opus-4-8" in run_cmd
    assert "MODEL_API_KEY=gw-key" in run_cmd
    assert "MODEL_BASE_URL=https://gw.example.com" in run_cmd


def test_start_passes_through_model_custom_headers(monkeypatch):
    """The user-sim makes its own gateway LLM calls, so it needs the gateway's identity headers
    (X-Gateway-User) too -- the gateway can reject a request that omits them. Absent => not passed."""
    monkeypatch.setenv("MODEL_CUSTOM_HEADERS", "X-Gateway-User: a@b.com")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.appsci_agent_usersim.subprocess.run", side_effect=fake_run):
        _svc().start()
    assert "MODEL_CUSTOM_HEADERS=X-Gateway-User: a@b.com" in calls[1]

    monkeypatch.delenv("MODEL_CUSTOM_HEADERS", raising=False)
    calls.clear()
    with patch("appsci_bench.core.appsci_agent_usersim.subprocess.run", side_effect=fake_run):
        _svc().start()
    assert not any(arg.startswith("MODEL_CUSTOM_HEADERS") for arg in calls[1])


def test_start_passes_through_the_llm_timeout(monkeypatch):
    """The user-sim calls its model through `appsci_bench.common.model_config`, which reads this var
    -- and that module runs INSIDE this container. Unforwarded, the user-sim silently keeps the 120s
    default while the host and the run manifest both say otherwise, and a slow-serving model turns
    that into repeated per-attempt timeouts plus litellm's retries instead of one honest wait.
    Absent => not passed, so the container keeps its own documented default."""
    monkeypatch.setenv("APPSCI_LLM_TIMEOUT_S", "900")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.appsci_agent_usersim.subprocess.run", side_effect=fake_run):
        _svc().start()
    assert "APPSCI_LLM_TIMEOUT_S=900" in calls[1]

    monkeypatch.delenv("APPSCI_LLM_TIMEOUT_S", raising=False)
    calls.clear()
    with patch("appsci_bench.core.appsci_agent_usersim.subprocess.run", side_effect=fake_run):
        _svc().start()
    assert not any(arg.startswith("APPSCI_LLM_TIMEOUT_S") for arg in calls[1])


# OBSERVABILITY_LOG_EVENTS forwarding (added for the --verbose CLI flag): only forwarded into the
# container when both the env var is set AND observability itself is enabled for this run.

def test_start_forwards_observability_log_events_when_set_and_observability_enabled(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_LOG_EVENTS", "1")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.appsci_agent_usersim.subprocess.run", side_effect=fake_run):
        _svc(observability_trace_id="trace123", runs_dir="/tmp/runs").start()
    run_cmd = calls[1]
    assert "OBSERVABILITY_LOG_EVENTS=1" in run_cmd


def test_start_does_not_forward_observability_log_events_when_unset(monkeypatch):
    monkeypatch.delenv("OBSERVABILITY_LOG_EVENTS", raising=False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.appsci_agent_usersim.subprocess.run", side_effect=fake_run):
        _svc(observability_trace_id="trace123", runs_dir="/tmp/runs").start()
    run_cmd = calls[1]
    assert not any(a.startswith("OBSERVABILITY_LOG_EVENTS=") for a in run_cmd)


def test_start_skips_observability_log_events_without_observability_enabled(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_LOG_EVENTS", "1")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.appsci_agent_usersim.subprocess.run", side_effect=fake_run):
        _svc().start()  # no observability_trace_id/runs_dir
    run_cmd = calls[1]
    assert not any("OBSERVABILITY_LOG_EVENTS" in a for a in run_cmd)


# --- _wait_for_health ----------------------------------------------------------------------------

def test_wait_for_health_raises_without_started_service():
    svc = _svc()
    with pytest.raises(RuntimeError, match="Service not started"):
        svc._wait_for_health()


def test_wait_for_health_times_out(monkeypatch):
    monkeypatch.setattr(DockerAppsciAgentUserSimService, "HEALTH_TIMEOUT", 0)
    svc = _svc(task_id="t1")
    svc._container_name = "appsci-usersim-t1"
    svc.token = "tok"
    with pytest.raises(TimeoutError):
        svc._wait_for_health()


# --- stop() / context manager --------------------------------------------------------------------

def test_stop_before_start_is_a_noop():
    svc = _svc()
    with patch("appsci_bench.core.appsci_agent_usersim.subprocess.run") as mock_run:
        svc.stop()
    mock_run.assert_not_called()


def test_stop_calls_docker_stop_then_rm_and_clears_state():
    with patch("appsci_bench.core.appsci_agent_usersim.subprocess.run", return_value=_R()):
        svc = _svc(task_id="t1").start()
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.appsci_agent_usersim.subprocess.run", side_effect=fake_run):
        svc.stop()
    assert calls == [
        ["docker", "stop", "appsci-usersim-t1"],
        ["docker", "rm", "-f", "appsci-usersim-t1"],
    ]
    assert svc.url is None


def test_context_manager_starts_and_stops():
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.appsci_agent_usersim.subprocess.run", side_effect=fake_run):
        with _svc(task_id="t1") as svc:
            assert svc.container_name == "appsci-usersim-t1"
    assert calls[-2:] == [
        ["docker", "stop", "appsci-usersim-t1"],
        ["docker", "rm", "-f", "appsci-usersim-t1"],
    ]
