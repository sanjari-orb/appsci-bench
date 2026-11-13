"""Unit tests for the host-side DockerArtifactDeployer (appsci_bench.orchestrator.deployer).

Docker itself is never invoked: subprocess.run is faked and the reachability check is patched, so
these run without a Docker daemon. This is the shared deploy mechanism both callers use (the agent's
/deploy route and the eval-only pipeline); its contract is "turn an artifact dir into a reachable
endpoint, or fail with a specific, actionable reason at whichever step broke."
"""
from __future__ import annotations

import subprocess
import urllib.error
from unittest import mock

from appsci_bench.orchestrator.deployer import (
    DockerArtifactDeployer,
    llm_credential_env,
)


def _fake_run_factory(results: dict):
    """Build a fake subprocess.run that dispatches on the docker subcommand.

    `results` maps a docker subcommand (build/run/rm/logs/port) to a
    (returncode, stdout, stderr) tuple. Unlisted subcommands succeed empty.
    """
    def fake_run(cmd, *args, **kwargs):
        sub = cmd[1] if len(cmd) > 1 else ""
        rc, out, err = results.get(sub, (0, "", ""))
        return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout=out, stderr=err)
    return fake_run


def _deployer(artifact_dir):
    return DockerArtifactDeployer(
        artifact_dir=artifact_dir,
        image_name="artifact-test:latest",
        container_name="artifact-test",
        network="bench-network",
        port=8080,
    )


def _write_deployable(tmp_path):
    """A directory that passes validation: Dockerfile + a recognized server entry."""
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "server.py").write_text("# agentic endpoint\n")


# --- validation failures (before any docker work) ---------------------------

def test_missing_artifact_dir(tmp_path):
    result = _deployer(tmp_path / "does_not_exist").deploy()
    assert result.success is False
    assert "not found" in result.error.lower()


def test_missing_dockerfile(tmp_path):
    # Directory exists (with a server entry) but has no Dockerfile.
    (tmp_path / "server.py").write_text("# server\n")
    result = _deployer(tmp_path).deploy()
    assert result.success is False
    assert "Dockerfile" in result.error


def test_missing_server_entry(tmp_path):
    # Dockerfile present but no server.py / app.py / main.py.
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    result = _deployer(tmp_path).deploy()
    assert result.success is False
    assert "server entry" in result.error


# --- docker-step failures ----------------------------------------------------

def test_build_failure_surfaces_logs(tmp_path):
    _write_deployable(tmp_path)
    fake = _fake_run_factory({"build": (1, "step 1/2", "error: bad instruction")})
    with mock.patch("appsci_bench.orchestrator.deployer.subprocess.run", fake):
        result = _deployer(tmp_path).deploy()
    assert result.success is False
    assert "docker build failed" in result.error
    assert "bad instruction" in result.error  # build log tail is surfaced


def test_run_failure_surfaces_error(tmp_path):
    _write_deployable(tmp_path)
    fake = _fake_run_factory({
        "build": (0, "built", ""),
        "rm": (0, "", ""),
        "run": (1, "", "port is already allocated"),
    })
    with mock.patch("appsci_bench.orchestrator.deployer.subprocess.run", fake):
        result = _deployer(tmp_path).deploy()
    assert result.success is False
    assert "docker run failed" in result.error
    assert "already allocated" in result.error


def test_health_timeout_surfaces_container_logs(tmp_path):
    _write_deployable(tmp_path)
    dep = _deployer(tmp_path)
    fake = _fake_run_factory({"build": (0, "", ""), "rm": (0, "", ""), "run": (0, "cid", "")})
    with mock.patch("appsci_bench.orchestrator.deployer.subprocess.run", fake), \
         mock.patch.object(dep, "_wait_reachable", return_value=(False, False, "connection refused")), \
         mock.patch.object(dep, "_container_logs", return_value="Traceback: boom"):
        result = dep.deploy()
    assert result.success is False
    assert "never became reachable" in result.error
    assert "Traceback: boom" in result.error  # container logs surfaced for debugging


# --- success -----------------------------------------------------------------

def test_successful_deploy_returns_container_dns_endpoint(tmp_path):
    _write_deployable(tmp_path)
    dep = _deployer(tmp_path)
    fake = _fake_run_factory({"build": (0, "", ""), "rm": (0, "", ""), "run": (0, "cid", "")})
    with mock.patch("appsci_bench.orchestrator.deployer.subprocess.run", fake), \
         mock.patch.object(dep, "_wait_reachable", return_value=(True, True, "healthy")):
        result = dep.deploy()
    assert result.success is True
    # container-DNS URL under the spec-declared name/port (host reaches it via docker-port translation)
    assert result.endpoint_url == "http://artifact-test:8080/respond"
    assert result.container_name == "artifact-test"
    assert result.healthy is True
    assert result.health_detail == "healthy"


# --- healthy vs merely reachable ---------------------------------------------
#
# `success` and `healthy` are different claims: success means the container answers (that's what the
# deploy gates on, and what eval needs), healthy means the health route returned 200. Flattening the
# two would tell the agent its artifact is healthy when the health route is broken -- hiding a defect
# until eval, by which point BUILD is over and the agent can no longer fix it.

def test_reachable_but_non_200_health_is_not_reported_healthy(tmp_path):
    _write_deployable(tmp_path)
    dep = _deployer(tmp_path)
    fake = _fake_run_factory({"build": (0, "", ""), "rm": (0, "", ""), "run": (0, "cid", "")})
    detail = "server is up but GET /health returned HTTP 404, not 200"
    with mock.patch("appsci_bench.orchestrator.deployer.subprocess.run", fake), \
         mock.patch.object(dep, "_wait_reachable", return_value=(True, False, detail)):
        result = dep.deploy()
    # Still a successful deploy -- the endpoint serves, so eval can query it ...
    assert result.success is True
    assert result.endpoint_url == "http://artifact-test:8080/respond"
    # ... but it must NOT be described as healthy, and the reason must survive to the caller.
    assert result.healthy is False
    assert result.health_detail == detail


def _wait_with_urlopen(dep, urlopen_side_effect):
    """Drive the real _wait_reachable against a faked urlopen, with the port already published."""
    with mock.patch.object(dep, "_host_health_url", return_value="http://localhost:1234/health"), \
         mock.patch("appsci_bench.orchestrator.deployer.urllib.request.urlopen",
                    side_effect=urlopen_side_effect):
        return dep._wait_reachable()


def test_wait_reachable_200_is_healthy(tmp_path):
    resp = mock.MagicMock(status=200)
    resp.__enter__ = mock.Mock(return_value=resp)
    resp.__exit__ = mock.Mock(return_value=False)
    reachable, healthy, detail = _wait_with_urlopen(_deployer(tmp_path), [resp])
    assert (reachable, healthy) == (True, True)
    assert detail == "healthy"


def test_wait_reachable_http_error_is_reachable_not_healthy(tmp_path):
    """A 404 (no health route) proves the server is up but is not a passing health check."""
    err = urllib.error.HTTPError("http://localhost:1234/health", 404, "Not Found", {}, None)
    reachable, healthy, detail = _wait_with_urlopen(_deployer(tmp_path), [err])
    assert (reachable, healthy) == (True, False)
    assert "HTTP 404" in detail and "not 200" in detail


def test_wait_reachable_non_200_success_status_is_reachable_not_healthy(tmp_path):
    """A 204 ends the poll like any other answer -- it used to keep polling and fail the deploy,
    the opposite outcome from a 500, for the same fact (the server is up)."""
    resp = mock.MagicMock(status=204)
    resp.__enter__ = mock.Mock(return_value=resp)
    resp.__exit__ = mock.Mock(return_value=False)
    reachable, healthy, detail = _wait_with_urlopen(_deployer(tmp_path), [resp])
    assert (reachable, healthy) == (True, False)
    assert "HTTP 204" in detail


def test_wait_reachable_never_answers_is_unreachable(tmp_path):
    dep = _deployer(tmp_path)
    dep.HEALTH_TIMEOUT_S = 0.05  # don't burn the real 60s poll window in a unit test
    dep.HEALTH_POLL_INTERVAL_S = 0.01
    err = urllib.error.URLError("connection refused")
    reachable, healthy, detail = _wait_with_urlopen(dep, err)
    assert (reachable, healthy) == (False, False)
    assert "connection refused" in detail


def test_run_is_idempotent_removes_stale_container_first(tmp_path):
    _write_deployable(tmp_path)
    dep = _deployer(tmp_path)
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="cid", stderr="")

    with mock.patch("appsci_bench.orchestrator.deployer.subprocess.run", side_effect=fake_run), \
         mock.patch.object(dep, "_wait_reachable", return_value=(True, True, "healthy")):
        dep.deploy()

    # A `docker rm -f <name>` precedes `docker run` so a crashed prior attempt never wedges the next.
    subs = [c[1] for c in calls if len(c) > 1]
    assert subs.index("rm") < subs.index("run")
    rm_cmd = next(c for c in calls if len(c) > 1 and c[1] == "rm")
    assert rm_cmd[:3] == ["docker", "rm", "-f"] and "artifact-test" in rm_cmd


def test_env_injected_into_run(tmp_path):
    _write_deployable(tmp_path)
    dep = DockerArtifactDeployer(
        artifact_dir=tmp_path,
        image_name="img:latest",
        container_name="artifact-test",
        network="bench-network",
        port=8080,
        env={"AIRLINE_TOOLS_URL": "http://tools", "AIRLINE_TOOLS_TOKEN": "tok"},
    )
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="cid", stderr="")

    with mock.patch("appsci_bench.orchestrator.deployer.subprocess.run", side_effect=fake_run), \
         mock.patch.object(dep, "_wait_reachable", return_value=(True, True, "healthy")):
        dep.deploy()

    run_cmd = next(c for c in calls if len(c) > 1 and c[1] == "run")
    joined = " ".join(run_cmd)
    assert "AIRLINE_TOOLS_URL=http://tools" in joined
    assert "AIRLINE_TOOLS_TOKEN=tok" in joined
    assert "--network" in run_cmd and "bench-network" in run_cmd


def test_port_injected_and_authoritative_over_agent_env(tmp_path):
    # The deployer publishes container port self.port, so it must also tell the container to bind that
    # port via $PORT -- and that injected PORT must win even if a task's agent_env sets its own, else
    # the server could bind a port the deployer never published (the "never became reachable" failure).
    _write_deployable(tmp_path)
    dep = DockerArtifactDeployer(
        artifact_dir=tmp_path,
        image_name="img:latest",
        container_name="artifact-test",
        network="bench-network",
        port=8080,
        env={"PORT": "9999", "AIRLINE_TOOLS_URL": "http://tools"},
    )
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="cid", stderr="")

    with mock.patch("appsci_bench.orchestrator.deployer.subprocess.run", side_effect=fake_run), \
         mock.patch.object(dep, "_wait_reachable", return_value=(True, True, "healthy")):
        dep.deploy()

    run_cmd = next(c for c in calls if len(c) > 1 and c[1] == "run")
    # The published port matches the deployer's port ...
    assert "-p" in run_cmd and "8080" in run_cmd
    # ... and PORT is injected to match it. Collect each "-e PORT=<v>" value in command order; docker
    # honors the LAST one, and it must be the deployer's port (not the agent_env override of 9999).
    port_vals = [
        tok.split("=", 1)[1]
        for i, tok in enumerate(run_cmd)
        if tok.startswith("PORT=") and i > 0 and run_cmd[i - 1] == "-e"
    ]
    assert port_vals, "deployer did not inject a PORT env var"
    assert port_vals[-1] == "8080"


# --- llm_credential_env: what the artifact needs to reach its model provider ------------------
#
# The deployed artifact is itself an LLM agent; without provider creds it crashes at import, which is
# what once pushed an agent to bake a plaintext proxy token into its image. `llm_credential_env`
# resolves those creds from the host env so both callers (the /deploy route and the eval CLI) can
# inject them -- removing any reason to hardcode a secret.

def test_llm_credential_env_returns_only_present_vars():
    env = {"OPENAI_API_KEY": "sk-openai", "APPSCI_AGENT_BASE_URL": "https://proxy"}
    with mock.patch.dict("os.environ", env, clear=True):
        creds = llm_credential_env()
    # Only the vars actually set are returned; absent ones are omitted (not blanked to "").
    assert creds == {"OPENAI_API_KEY": "sk-openai", "APPSCI_AGENT_BASE_URL": "https://proxy"}
    assert "ANTHROPIC_API_KEY" not in creds
    assert "APPSCI_AGENT_API_KEY" not in creds


def test_llm_credential_env_forwards_all_with_values():
    env = {
        "ANTHROPIC_API_KEY": "sk-ant",
        "ANTHROPIC_AUTH_TOKEN": "gw-token",
        "ANTHROPIC_BASE_URL": "https://anthropic-gw",
        "OPENAI_API_KEY": "sk-oai",
        "APPSCI_AGENT_API_KEY": "proxy-token",
        "APPSCI_AGENT_BASE_URL": "https://gateway",
        "UNRELATED_VAR": "ignored",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        creds = llm_credential_env()
    assert creds == {
        "ANTHROPIC_API_KEY": "sk-ant",
        "ANTHROPIC_AUTH_TOKEN": "gw-token",
        "ANTHROPIC_BASE_URL": "https://anthropic-gw",
        "OPENAI_API_KEY": "sk-oai",
        "APPSCI_AGENT_API_KEY": "proxy-token",
        "APPSCI_AGENT_BASE_URL": "https://gateway",
    }
    assert "UNRELATED_VAR" not in creds


def test_llm_credential_env_empty_when_none_set():
    with mock.patch.dict("os.environ", {}, clear=True):
        assert llm_credential_env() == {}


def test_llm_credential_env_forwards_fireworks_key():
    """A Fireworks-served agent means a Fireworks-served artifact, and its only credential is the key:
    LiteLLM resolves the endpoint from the model id's prefix, so there is no base URL to pair with it.
    Absent from the host env => not forwarded, same as every other provider in this list."""
    with mock.patch.dict("os.environ", {"FIREWORKS_API_KEY": "fw-token"}, clear=True):
        assert llm_credential_env() == {"FIREWORKS_API_KEY": "fw-token"}

    with mock.patch.dict("os.environ", {"FIREWORKS_AI_API_KEY": "fw-alt"}, clear=True):
        assert llm_credential_env() == {"FIREWORKS_AI_API_KEY": "fw-alt"}

    with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant"}, clear=True):
        creds = llm_credential_env()
    assert "FIREWORKS_API_KEY" not in creds
    assert "FIREWORKS_AI_API_KEY" not in creds


def test_llm_credential_env_forwards_gateway_custom_headers():
    """The deployed artifact is itself an LLM agent, so it needs the gateway's identity headers
    (X-Gateway-User) as well as the token -- the gateway can reject a request that omits them. Not a
    credential, but it rides the same forwarding path; absent from the host env => not forwarded."""
    env = {"APPSCI_AGENT_API_KEY": "tok", "APPSCI_AGENT_CUSTOM_HEADERS": "X-Gateway-User: a@b.com"}
    with mock.patch.dict("os.environ", env, clear=True):
        creds = llm_credential_env()
    assert creds["APPSCI_AGENT_CUSTOM_HEADERS"] == "X-Gateway-User: a@b.com"

    with mock.patch.dict("os.environ", {"APPSCI_AGENT_API_KEY": "tok"}, clear=True):
        assert "APPSCI_AGENT_CUSTOM_HEADERS" not in llm_credential_env()


def test_deploy_env_merge_task_env_wins_over_creds():
    # The callers build `{**llm_credential_env(), **agent_env}` so a task that explicitly pins e.g.
    # APPSCI_AGENT_BASE_URL in its agent_env overrides the host-derived credential. This guards that
    # precedence (task agent_env last => wins).
    env = {"APPSCI_AGENT_BASE_URL": "https://host-proxy", "APPSCI_AGENT_API_KEY": "host-key"}
    agent_env = {"APPSCI_AGENT_BASE_URL": "https://task-proxy", "AIRLINE_TOOLS_URL": "http://tools"}
    with mock.patch.dict("os.environ", env, clear=True):
        merged = {**llm_credential_env(), **agent_env}
    assert merged["APPSCI_AGENT_BASE_URL"] == "https://task-proxy"  # task value wins
    assert merged["APPSCI_AGENT_API_KEY"] == "host-key"             # host cred still present
    assert merged["AIRLINE_TOOLS_URL"] == "http://tools"
