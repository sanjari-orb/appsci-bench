"""Unit tests for AppSciAgentContainerManager (appsci_bench/core/agent_container.py) -- docker run
command construction, LLM-key validation, and lifecycle (start/stop/wait_for_exit/snapshot_artifact).
subprocess is stubbed throughout (no real docker involved)."""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from appsci_bench.core import agent_container
from appsci_bench.core.agent_container import AppSciAgentContainerConfig, AppSciAgentContainerManager


class _R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _config(**overrides):
    defaults = dict(
        task_id="order_to_cash",
        workspace_container="o2c-workspace-1",
        appsci_usersim_url="http://usersim-order_to_cash:8000",
        appsci_usersim_token="tok",
        orchestrator_submit_url="http://host.docker.internal:9000/submit",
        spec_path="/tmp/spec.yaml",
    )
    defaults.update(overrides)
    return AppSciAgentContainerConfig(**defaults)


def test_container_name_is_appsci_prefixed_task_id():
    mgr = AppSciAgentContainerManager(_config(task_id="airline_rebooking"))
    assert mgr.container_name == "appsci-agent-airline_rebooking"


def test_get_artifact_url_uses_container_name_and_port():
    mgr = AppSciAgentContainerManager(_config(task_id="slides_generator"))
    assert mgr.get_artifact_url() == "http://appsci-agent-slides_generator:8080"


# --- start() ---------------------------------------------------------------------------------

def test_start_raises_without_any_llm_api_key(monkeypatch):
    # Every credential the check accepts has to go, Fireworks' two spellings included -- an operator
    # who exports FIREWORKS_API_KEY in their shell would otherwise satisfy the check and this test
    # would silently stop testing anything.
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "APPSCI_AGENT_API_KEY",
              *agent_container.FIREWORKS_KEY_VARS):
        monkeypatch.delenv(k, raising=False)
    mgr = AppSciAgentContainerManager(_config())
    with pytest.raises(RuntimeError, match="No LLM API key"):
        mgr.start()


def test_start_removes_stale_container_then_runs(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config(task_id="t1")).start()
    assert calls[0] == ["docker", "rm", "-f", "appsci-agent-t1"]
    run_cmd = calls[1]
    assert run_cmd[:3] == ["docker", "run", "-d"]
    assert "--name" in run_cmd and "appsci-agent-t1" in run_cmd
    assert "--network" in run_cmd and "bench-network" in run_cmd


def test_start_passes_add_host_for_host_gateway(monkeypatch):
    """The agent must be able to reach the orchestrator's host-side submit/deploy server.

    `ORCHESTRATOR_SUBMIT_URL` points at `host.docker.internal`, which Docker Desktop
    (macOS/Windows) injects implicitly but Docker Engine on **Linux** does not -- so without
    `--add-host=host.docker.internal:host-gateway` the callback fails with "Could not resolve host"
    and the BUILD phase can never complete on Linux. Passed unconditionally (harmless on Docker
    Desktop, where it maps to the same host gateway), so this is a regression guard on every
    platform, not a style check.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config()).start()
    run_cmd = calls[1]
    assert "--add-host" in run_cmd, f"missing --add-host: {run_cmd}"
    assert run_cmd[run_cmd.index("--add-host") + 1] == "host.docker.internal:host-gateway"
    # The flag must land in the `docker run` argv, not after the image name (which would be parsed
    # as the container's command).
    assert run_cmd.index("--add-host") < run_cmd.index("appsci-bench-agent:latest")


def test_start_does_not_mount_the_docker_socket(monkeypatch):
    """The agent-under-test must never get the host Docker daemon.

    Deployment is a host-side service (deploy_artifact -> the orchestrator's POST /deploy), so the
    agent has no reason to talk to the daemon: it authors a Dockerfile + server entry and the host
    builds and runs them. Nothing in the agent image shells out to docker, and the task environment
    is reached over the Docker network (DB DSNs, tool-API URLs) plus the read-only /data mount --
    never via `docker exec`. A socket mount would give the agent root-equivalent control of the host
    daemon and every other container in the run, so this is a regression guard, not a style check.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config()).start()
    run_cmd = calls[1]
    assert not any("docker.sock" in arg for arg in run_cmd), (
        f"agent container must not mount the Docker socket: {run_cmd}"
    )


def test_start_passes_usersim_workspace_and_submit_url_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config(
            appsci_usersim_url="http://usersim-x:8000",
            appsci_usersim_token="secret-tok",
            workspace_container="ws-1",
            orchestrator_submit_url="http://host.docker.internal:9000/submit",
        )).start()
    run_cmd = calls[1]
    assert "APPSCI_USERSIM_URL=http://usersim-x:8000" in run_cmd
    assert "APPSCI_USERSIM_TOKEN=secret-tok" in run_cmd
    assert "TASK_WORKSPACE=ws-1" in run_cmd
    assert "ORCHESTRATOR_SUBMIT_URL=http://host.docker.internal:9000/submit" in run_cmd


def test_start_injects_extra_env_vars(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config(
            extra_env={"AIRLINE_TOOLS_URL": "http://airline-tools:9000"}
        )).start()
    assert "AIRLINE_TOOLS_URL=http://airline-tools:9000" in calls[1]


# AGENT_TEMPERATURE: the build-side replicate control. There is no seed in this harness, so
# temperature is what makes `builds: n` independent samples rather than n attempts at one intention --
# and it has to reach the container, because that is where both build-side agents actually run.

def test_start_injects_agent_temperature_when_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config(temperature=1.0)).start()
    assert "AGENT_TEMPERATURE=1.0" in calls[1]


def test_start_injects_a_temperature_of_zero(monkeypatch):
    """`0.0` is a real request and a truthiness check would drop it, running the build at the
    provider default while the cell record claimed the sampler was pinned."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config(temperature=0.0)).start()
    assert "AGENT_TEMPERATURE=0.0" in calls[1]


def test_start_omits_agent_temperature_when_unset(monkeypatch):
    """Unset must leave the container env exactly as it was before this knob existed, so a run today
    is comparable to the airline and gradient runs already collected."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config()).start()
    assert not [a for a in calls[1] if str(a).startswith("AGENT_TEMPERATURE")]


# Gateway identity headers (APPSCI_AGENT_CUSTOM_HEADERS, e.g. "X-Gateway-User: <email>"): the agent
# makes its own LLM calls inside the container, and the gateway can reject a request that omits the
# header (403 identity_required), so the var has to be forwarded like the base URL/token. It is NOT a
# credential, so its absence must not trip the "No LLM API key" hard-fail.

def test_start_forwards_custom_headers_var_when_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", "X-Gateway-User: a@b.com")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config()).start()
    run_cmd = calls[1]
    # Forwarded as a bare `-e NAME` (Docker copies the value from the host env).
    assert "APPSCI_AGENT_CUSTOM_HEADERS" in run_cmd, f"header var not forwarded: {run_cmd}"
    assert run_cmd[run_cmd.index("APPSCI_AGENT_CUSTOM_HEADERS") - 1] == "-e"


def test_start_omits_custom_headers_var_when_unset(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("APPSCI_AGENT_CUSTOM_HEADERS", raising=False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    # No header var and no APPSCI_AGENT_API_KEY: still starts (ANTHROPIC_API_KEY satisfies the
    # credential check) -- a missing identity header is not a missing credential.
    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config()).start()
    assert "APPSCI_AGENT_CUSTOM_HEADERS" not in calls[1]


# --- Fireworks as the agent's provider -------------------------------------------------------
#
# Fireworks is reached as a first-party provider: LiteLLM resolves the endpoint from the model id's
# `fireworks_ai/` prefix, so the key is the whole credential. These cover the three ways that can go
# wrong: the key not counting as a credential, the key not reaching the container, and the model id
# being sent to a gateway that does not serve it.

def test_fireworks_key_alone_satisfies_the_credential_check(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "APPSCI_AGENT_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-token")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config()).start()
    run_cmd = calls[1]
    # Forwarded as a bare `-e NAME`, so Docker copies the value from the host env.
    assert "FIREWORKS_API_KEY" in run_cmd, f"key not forwarded: {run_cmd}"
    assert run_cmd[run_cmd.index("FIREWORKS_API_KEY") - 1] == "-e"


def test_the_alternate_fireworks_key_spelling_is_also_forwarded(monkeypatch):
    """LiteLLM accepts either name, so either has to reach the container -- and only the one that is
    set. Pinned separately from the primary spelling: with both names forwarded from one loop, an
    assertion on `FIREWORKS_API_KEY` alone would pass while `FIREWORKS_AI_API_KEY` silently dropped."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    monkeypatch.setenv("FIREWORKS_AI_API_KEY", "fw-alt")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config()).start()
    run_cmd = calls[1]
    assert "FIREWORKS_AI_API_KEY" in run_cmd
    assert run_cmd[run_cmd.index("FIREWORKS_AI_API_KEY") - 1] == "-e"
    assert "FIREWORKS_API_KEY" not in run_cmd    # not set on the host => not forwarded


def test_an_empty_credential_does_not_count_as_a_key(monkeypatch):
    """`KEY=` in a systemd EnvironmentFile assigns the empty string, and an empty string authenticates
    nothing. Read as mere presence, one blank line in /etc/appsci-bench/experiments.env would satisfy
    this check for every cell on the host and retire the fail-fast that catches a failed gateway
    refresh."""
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "APPSCI_AGENT_API_KEY",
              *agent_container.FIREWORKS_KEY_VARS):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FIREWORKS_API_KEY", "")
    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=AssertionError):
        with pytest.raises(RuntimeError, match="No LLM API key"):
            AppSciAgentContainerManager(_config()).start()


def test_fireworks_key_vars_omitted_when_unset(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    for k in agent_container.FIREWORKS_KEY_VARS:
        monkeypatch.delenv(k, raising=False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config()).start()
    for name in agent_container.FIREWORKS_KEY_VARS:
        assert name not in calls[1]


def test_start_refuses_a_fireworks_model_without_a_fireworks_key(monkeypatch):
    """A key that arrives too late costs a container start, a workspace build, and an auth error
    buried in the agent's own log minutes later. ANTHROPIC_API_KEY does not substitute: it satisfies
    the generic credential check, and the point is that it cannot authenticate to Fireworks."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    for k in agent_container.FIREWORKS_KEY_VARS:
        monkeypatch.delenv(k, raising=False)
    mgr = AppSciAgentContainerManager(
        _config(model="fireworks_ai/accounts/fireworks/models/kimi-k3")
    )
    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=AssertionError):
        with pytest.raises(RuntimeError, match="FIREWORKS_API_KEY"):
            mgr.start()


def test_start_allows_a_fireworks_model_with_either_key_spelling(monkeypatch):
    monkeypatch.delenv("APPSCI_AGENT_BASE_URL", raising=False)
    monkeypatch.delenv("APPSCI_AGENT_API_KEY", raising=False)
    for present in agent_container.FIREWORKS_KEY_VARS:
        for k in agent_container.FIREWORKS_KEY_VARS:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv(present, "fw-token")
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return _R()

        with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
            AppSciAgentContainerManager(
                _config(model="fireworks_ai/kimi-k3")
            ).start()
        run_cmd = calls[1]
        assert "AGENT_MODEL=fireworks_ai/kimi-k3" in run_cmd, f"{present} rejected"
        # ... and it is THIS spelling that travelled, which is the part a shared assertion would miss.
        assert present in run_cmd, f"{present} satisfied the guard but was not forwarded"


def test_start_refuses_a_gateway_url_with_no_gateway_token(monkeypatch):
    """The credential-leak case, which is why it refuses where the others warn: with a base URL and no
    token of its own, LiteLLM sends `Authorization: Bearer <the Fireworks key>` to that endpoint. That
    is reachable by accident -- setup_local_with_gateway.sh exports the URL and the token under two
    separate guards, so a half-loaded session yields URL-without-token."""
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-token")
    monkeypatch.setenv("APPSCI_AGENT_BASE_URL", "https://gateway.example")
    monkeypatch.delenv("APPSCI_AGENT_API_KEY", raising=False)
    mgr = AppSciAgentContainerManager(_config(model="fireworks_ai/kimi-k3"))
    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=AssertionError):
        with pytest.raises(RuntimeError, match="APPSCI_AGENT_BASE_URL"):
            mgr.start()


def test_a_gateway_url_with_no_token_is_fine_for_a_non_fireworks_model(monkeypatch):
    """A gateway URL alone is the ordinary gateway-ish shape for any other model, so the refusal must
    be scoped to a Fireworks id -- otherwise this change would break existing runs."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("APPSCI_AGENT_BASE_URL", "https://gateway.example")
    monkeypatch.delenv("APPSCI_AGENT_API_KEY", raising=False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config(model="claude-opus-4-8")).start()
    assert calls, "the container should still start"


def test_non_fireworks_model_is_unaffected_by_the_fireworks_check(monkeypatch):
    """The guard reads the resolved model id and nothing else, so every existing run is untouched."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    for k in agent_container.FIREWORKS_KEY_VARS:
        monkeypatch.delenv(k, raising=False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config(model="claude-opus-4-8")).start()
    assert "AGENT_MODEL=claude-opus-4-8" in calls[1]


def test_fireworks_model_with_a_gateway_url_warns_but_still_starts(monkeypatch, caplog):
    """URL *and* token: a broken run, not a leak, and the same shape is legitimate when that URL is a
    proxy in front of Fireworks -- so this one warns where URL-without-token refuses."""
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-token")
    monkeypatch.setenv("APPSCI_AGENT_BASE_URL", "https://gateway.example")
    monkeypatch.setenv("APPSCI_AGENT_API_KEY", "gateway-token")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with caplog.at_level("WARNING", logger="appsci_bench.core.agent_container"):
        with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
            AppSciAgentContainerManager(_config(model="fireworks_ai/kimi-k3")).start()
    assert calls, "the container should still start"
    assert "APPSCI_AGENT_BASE_URL" in caplog.text


def test_fireworks_model_with_a_gateway_token_alone_also_warns(monkeypatch, caplog):
    """The silent case worth naming: `llm_client` sets litellm's api_key and base_url independently, so
    a token with no URL is offered to Fireworks AS the Fireworks key, FIREWORKS_API_KEY is never read,
    and every call 401s minutes into the build with nothing pointing at the cause."""
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-token")
    monkeypatch.delenv("APPSCI_AGENT_BASE_URL", raising=False)
    monkeypatch.setenv("APPSCI_AGENT_API_KEY", "gateway-token")

    with caplog.at_level("WARNING", logger="appsci_bench.core.agent_container"):
        with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R()):
            AppSciAgentContainerManager(_config(model="fireworks_ai/kimi-k3")).start()
    assert "APPSCI_AGENT_API_KEY" in caplog.text


def test_no_gateway_warning_without_a_gateway_url(monkeypatch, caplog):
    monkeypatch.setenv("FIREWORKS_API_KEY", "fw-token")
    monkeypatch.delenv("APPSCI_AGENT_BASE_URL", raising=False)
    monkeypatch.delenv("APPSCI_AGENT_API_KEY", raising=False)

    with caplog.at_level("WARNING", logger="appsci_bench.core.agent_container"):
        with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R()):
            AppSciAgentContainerManager(_config(model="fireworks_ai/kimi-k3")).start()
    assert "APPSCI_AGENT" not in caplog.text


# OBSERVABILITY_LOG_EVENTS forwarding (added for the --verbose CLI flag): only forwarded into the
# container when both the env var is set AND observability itself is enabled for this run.
def test_start_forwards_observability_log_events_when_set_and_observability_enabled(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OBSERVABILITY_LOG_EVENTS", "1")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config(
            observability_trace_id="trace123", runs_dir="/tmp/runs",
        )).start()
    assert "OBSERVABILITY_LOG_EVENTS=1" in calls[1]


def test_start_does_not_forward_observability_log_events_when_unset(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("OBSERVABILITY_LOG_EVENTS", raising=False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config(
            observability_trace_id="trace123", runs_dir="/tmp/runs",
        )).start()
    assert not any(a.startswith("OBSERVABILITY_LOG_EVENTS=") for a in calls[1])


def test_start_skips_observability_log_events_without_observability_enabled(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OBSERVABILITY_LOG_EVENTS", "1")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(_config()).start()  # no observability_trace_id/runs_dir
    assert not any("OBSERVABILITY_LOG_EVENTS" in a for a in calls[1])


# --- wait_for_exit / get_logs / is_running / get_exit_code ------------------------------------

def test_wait_for_exit_raises_without_started_container():
    mgr = AppSciAgentContainerManager(_config())
    with pytest.raises(RuntimeError, match="Agent not started"):
        mgr.wait_for_exit()


def test_wait_for_exit_returns_minus_one_when_container_gone(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    mgr = AppSciAgentContainerManager(_config())
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R()):
        mgr.start()
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R(returncode=1)):
        assert mgr.wait_for_exit(timeout=5) == -1


def test_wait_for_exit_returns_exit_code_when_stopped(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    mgr = AppSciAgentContainerManager(_config())
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R()):
        mgr.start()
    responses = [_R(stdout="false\n"), _R(stdout="3\n")]
    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=responses):
        assert mgr.wait_for_exit(timeout=5) == 3


def test_wait_for_exit_times_out(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    mgr = AppSciAgentContainerManager(_config())
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R()):
        mgr.start()
    with pytest.raises(TimeoutError):
        mgr.wait_for_exit(timeout=0)


def test_get_logs_combines_stdout_and_stderr(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    mgr = AppSciAgentContainerManager(_config())
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R()):
        mgr.start()
    with patch("appsci_bench.core.agent_container.subprocess.run",
               return_value=_R(stdout="out\n", stderr="err\n")):
        assert mgr.get_logs() == "out\nerr\n"


def test_get_logs_before_start_returns_empty_string():
    mgr = AppSciAgentContainerManager(_config())
    assert mgr.get_logs() == ""


def test_is_running_true_false_and_missing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    mgr = AppSciAgentContainerManager(_config())
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R()):
        mgr.start()
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R(stdout="true\n")):
        assert mgr.is_running() is True
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R(stdout="false\n")):
        assert mgr.is_running() is False
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R(returncode=1)):
        assert mgr.is_running() is False


def test_get_exit_code_parses_running_stopped_and_missing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    mgr = AppSciAgentContainerManager(_config())
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R()):
        mgr.start()
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R(stdout="true 0\n")):
        assert mgr.get_exit_code() is None  # still running
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R(stdout="false 7\n")):
        assert mgr.get_exit_code() == 7
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R(returncode=1)):
        assert mgr.get_exit_code() is None


# --- snapshot_artifact -------------------------------------------------------------------------

def test_snapshot_artifact_returns_none_before_start():
    mgr = AppSciAgentContainerManager(_config())
    assert mgr.snapshot_artifact("/tmp/whatever") is None


def test_snapshot_artifact_returns_none_when_docker_cp_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    mgr = AppSciAgentContainerManager(_config())
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R()):
        mgr.start()
    with patch("appsci_bench.core.agent_container.subprocess.run",
               return_value=_R(returncode=1, stderr="no such dir")):
        assert mgr.snapshot_artifact(str(tmp_path / "artifact")) is None


def test_snapshot_artifact_writes_manifest_on_success(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    mgr = AppSciAgentContainerManager(_config(task_id="t1"))
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R()):
        mgr.start()
    dest = tmp_path / "artifact"

    def fake_cp(cmd, **kw):
        # docker cp doesn't actually write files under mocking -- simulate what it would have
        # left behind so the manifest-building walk below has something to find.
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "out.txt").write_text("hello")
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_cp):
        manifest = mgr.snapshot_artifact(str(dest))

    assert manifest["file_count"] == 1
    assert manifest["files"][0]["path"] == "out.txt"
    assert manifest["total_bytes"] == len("hello")
    manifest_path = tmp_path / "artifact_manifest.json"
    assert manifest_path.exists()
    assert json.loads(manifest_path.read_text())["file_count"] == 1


# --- the agent-writable log mount budget -------------------------------------------------------
#
# The four events-*/model_calls-* files are bind-mounted rw so the agent's own streams land on the host
# live, and the writer is the benchmark's subject. Docker cannot bound a bind mount, so the container
# is killed when the mounts blow AGENT_LOG_BUDGET_BYTES: one failed cell instead of a full host disk,
# which would fail every later cell and block the S3 mirror. These tests pin the kill, the exemptions,
# and that the check is on the poll path the orchestrator actually uses.

def _observability_mgr(tmp_path, task_id="t1"):
    return AppSciAgentContainerManager(_config(
        task_id=task_id, observability_trace_id="trace123", runs_dir=str(tmp_path / "runs"),
    ))


def _fill_log(mgr, nbytes):
    """Write nbytes into the first mounted log path (creating the trace dir)."""
    path = mgr._mounted_log_paths()[0]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"x" * nbytes)
    return path


def test_log_budget_exceeded_is_none_without_observability_mounts():
    # No runs_dir/trace id => nothing is mounted rw, so there is nothing to bound.
    mgr = AppSciAgentContainerManager(_config())
    assert mgr._mounted_log_paths() == []
    assert mgr.log_budget_exceeded() is None


def test_log_budget_exceeded_reports_total_and_largest(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_container, "AGENT_LOG_BUDGET_BYTES", 100)
    mgr = _observability_mgr(tmp_path)
    _fill_log(mgr, 50)
    assert mgr.log_budget_exceeded() is None, "under budget must not trip"
    _fill_log(mgr, 101)
    msg = mgr.log_budget_exceeded()
    assert msg is not None
    assert "101" in msg and "events-appsci_agent.jsonl" in msg


def test_is_running_kills_the_container_when_the_log_mounts_blow_the_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(agent_container, "AGENT_LOG_BUDGET_BYTES", 100)
    mgr = _observability_mgr(tmp_path)
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R()):
        mgr.start()
    _fill_log(mgr, 200)

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        # Docker still reports it as running: the kill is what makes this False, and if the manager
        # believed docker instead the runaway writer would keep going for the whole build phase.
        return _R(stdout="true\n")

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        running = mgr.is_running()

    assert ["docker", "kill", "appsci-agent-t1"] in calls, f"container not killed: {calls}"
    assert running is False
    assert mgr.log_budget_error is not None and "budget" in mgr.log_budget_error


def test_is_running_does_not_kill_when_under_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(agent_container, "AGENT_LOG_BUDGET_BYTES", 1000)
    mgr = _observability_mgr(tmp_path)
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R()):
        mgr.start()
    _fill_log(mgr, 10)

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R(stdout="true\n")

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        assert mgr.is_running() is True
    assert not any("kill" in cmd for cmd in calls), f"killed a healthy container: {calls}"
    assert mgr.log_budget_error is None


def test_wait_for_exit_also_enforces_the_budget(tmp_path, monkeypatch):
    # wait_for_exit is the other place a caller can sit for hours (default 2h timeout), so the check
    # has to be on that loop too or an unbounded writer gets the whole timeout to fill the disk.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(agent_container, "AGENT_LOG_BUDGET_BYTES", 100)
    mgr = _observability_mgr(tmp_path)
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R()):
        mgr.start()
    _fill_log(mgr, 200)

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        # After the kill, report stopped with 137 so the loop returns rather than spinning.
        if cmd[:2] == ["docker", "inspect"] and "{{.State.Running}}" in cmd:
            return _R(stdout="false\n")
        return _R(stdout="137\n")

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        assert mgr.wait_for_exit(timeout=5) == 137
    assert ["docker", "kill", "appsci-agent-t1"] in calls, f"container not killed: {calls}"


def test_start_mounts_exactly_the_paths_the_budget_watches(tmp_path, monkeypatch):
    # The mount list and the watched list are the same function, and this is the guard on that: a fifth
    # rw mount added to start() without going through _mounted_log_paths would be unwatched.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    mgr = _observability_mgr(tmp_path)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        mgr.start()
    run_cmd = calls[1]
    rw_log_mounts = [a for a in run_cmd if a.endswith(":rw") and ".jsonl" in a]
    assert len(rw_log_mounts) == 4, f"expected the four log mounts, got {rw_log_mounts}"
    for host_path in mgr._mounted_log_paths():
        assert any(a.startswith(f"{host_path}:") for a in rw_log_mounts), f"{host_path} not mounted"


# --- stop() / context manager --------------------------------------------------------------------

def test_stop_before_start_is_a_noop():
    mgr = AppSciAgentContainerManager(_config())
    with patch("appsci_bench.core.agent_container.subprocess.run") as mock_run:
        mgr.stop()
    mock_run.assert_not_called()


def test_stop_calls_docker_stop_then_rm_and_clears_container_name(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    mgr = AppSciAgentContainerManager(_config(task_id="t1"))
    with patch("appsci_bench.core.agent_container.subprocess.run", return_value=_R()):
        mgr.start()
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        mgr.stop()
    assert calls == [["docker", "stop", "appsci-agent-t1"], ["docker", "rm", "-f", "appsci-agent-t1"]]
    assert mgr.get_logs() == ""  # container_name cleared -> get_logs short-circuits


def test_context_manager_starts_and_stops(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        with AppSciAgentContainerManager(_config(task_id="t1")) as mgr:
            assert mgr.container_name == "appsci-agent-t1"
    assert calls[-2:] == [["docker", "stop", "appsci-agent-t1"], ["docker", "rm", "-f", "appsci-agent-t1"]]


# --- APPSCI_LLM_TIMEOUT_S forwarding ----------------------------------------------------------

def _run_argv(monkeypatch, cfg=None):
    """Return the `docker run` argv `start()` builds (calls[0] is the stale-container `rm`)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = []
    with patch("appsci_bench.core.agent_container.subprocess.run",
               side_effect=lambda cmd, **kw: (calls.append(cmd), _R())[1]):
        AppSciAgentContainerManager(cfg or _config()).start()
    return calls[1]


def test_llm_timeout_is_forwarded_into_the_container(monkeypatch):
    """Regression guard on a knob that was silently dead.

    Both readers of this variable run INSIDE the agent container (the agent's LLM client and the
    mini-swe sub-agent), so a value exported only on the host had no effect: they saw the 120s
    default. That produced builds that looked hung for many minutes -- one over-long generation
    exceeding 120s, then re-issued by litellm's retries with backoff -- and a manifest that recorded
    a timeout the run never used.
    """
    monkeypatch.setenv("APPSCI_LLM_TIMEOUT_S", "300")
    argv = _run_argv(monkeypatch)
    assert "APPSCI_LLM_TIMEOUT_S=300.0" in argv, f"timeout not forwarded: {argv}"


def test_llm_timeout_absent_injects_nothing(monkeypatch):
    """Unset must leave the container env exactly as it was before this knob existed, so the
    in-container default applies rather than an explicit value chosen here."""
    monkeypatch.delenv("APPSCI_LLM_TIMEOUT_S", raising=False)
    argv = _run_argv(monkeypatch)
    assert not [a for a in argv if a.startswith("APPSCI_LLM_TIMEOUT_S")]


@pytest.mark.parametrize("blank", ["", "   "])
def test_llm_timeout_blank_injects_nothing_rather_than_an_empty_value(monkeypatch, blank):
    """`NAME=` in an EnvironmentFile assigns the empty string, which is NOT absent.

    Forwarding it verbatim would put an empty value in the container, where
    `float(os.getenv(NAME, "120"))` does NOT fall back to its default -- it raises ValueError at the
    first model call, after the run has already paid for containers. Treated as unset instead.
    """
    monkeypatch.setenv("APPSCI_LLM_TIMEOUT_S", blank)
    argv = _run_argv(monkeypatch)
    assert not [a for a in argv if a.startswith("APPSCI_LLM_TIMEOUT_S")]


@pytest.mark.parametrize("bad", ["abc", "5s", "1e"])
def test_llm_timeout_unparseable_fails_before_any_docker_call(monkeypatch, bad):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("APPSCI_LLM_TIMEOUT_S", bad)
    # side_effect=AssertionError: the guard must fire before docker is invoked at all, so a bad
    # value costs no container.
    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=AssertionError):
        with pytest.raises(RuntimeError, match="is not a number"):
            AppSciAgentContainerManager(_config()).start()


@pytest.mark.parametrize("bad", ["0", "-1", "-30.5"])
def test_llm_timeout_non_positive_is_rejected(monkeypatch, bad):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("APPSCI_LLM_TIMEOUT_S", bad)
    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=AssertionError):
        with pytest.raises(RuntimeError, match="greater than zero"):
            AppSciAgentContainerManager(_config()).start()


def test_llm_timeout_accepts_a_fractional_value(monkeypatch):
    monkeypatch.setenv("APPSCI_LLM_TIMEOUT_S", "90.5")
    assert "APPSCI_LLM_TIMEOUT_S=90.5" in _run_argv(monkeypatch)
