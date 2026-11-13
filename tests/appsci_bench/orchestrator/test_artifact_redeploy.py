"""Does eval score the artifact the agent finally wrote, or a container it abandoned?

Deploy is agent-driven and idempotent: the agent may deploy, keep editing, and deploy again. Nothing
used to guarantee the running container matched the final artifact directory, and the gap was not
theoretical -- an airline artifact was deployed while it still named a model the gateway does not
serve, the agent corrected the file minutes later, never redeployed, and all 50 eval samples then
failed against the stale container. The cell reported 0.05, indistinguishable in a results table from
an agent that simply did the task badly.

Since the build and eval task environments became two sequential containers, the pre-eval redeploy has
a second job: the artifact is restarted so it reconnects to the eval environment, whatever pooling its
author chose. That makes the redeploy UNCONDITIONAL -- an unchanged artifact directory no longer means
"nothing to do".

Docker is never invoked here: the deployer is faked, so these assert the decision (redeploy or decline)
rather than the build.
"""
from __future__ import annotations

from unittest import mock

from appsci_bench.orchestrator.deployer import DeploymentResult, artifact_fingerprint
from appsci_bench.orchestrator.submission_server import (
    ArtifactSubmissionServer,
    DeployContext,
)


def _write_artifact(d, server_body="import os\nMODEL = os.environ['AGENT_MODEL']\n"):
    (d / "Dockerfile").write_text("FROM python:3.11-slim\nCOPY server.py .\n")
    (d / "server.py").write_text(server_body)


def _server_with_deploy(tmp_path) -> ArtifactSubmissionServer:
    server = ArtifactSubmissionServer()
    server.configure_deploy(
        DeployContext(
            artifact_dir=str(tmp_path),
            image_name="artifact-test:latest",
            container_name="artifact-test",
            network="bench-network",
            port=8080,
        )
    )
    return server


def _ok_result():
    return DeploymentResult(
        success=True,
        endpoint_url="http://artifact-test:8080/respond",
        container_name="artifact-test",
        healthy=True,
        health_detail="HTTP 200",
    )


# --- fingerprint: what counts as a change -----------------------------------

def test_fingerprint_changes_when_a_file_changes(tmp_path):
    _write_artifact(tmp_path)
    before = artifact_fingerprint(tmp_path)
    (tmp_path / "server.py").write_text("MODEL = 'gpt-4-turbo'\n")
    assert artifact_fingerprint(tmp_path) != before


def test_fingerprint_ignores_pycache(tmp_path):
    """Importing the server writes bytecode; that must not force a pointless rebuild."""
    _write_artifact(tmp_path)
    before = artifact_fingerprint(tmp_path)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "server.cpython-311.pyc").write_bytes(b"\x00\x01binary")
    (tmp_path / "stray.pyc").write_bytes(b"\x00\x02")
    assert artifact_fingerprint(tmp_path) == before


def test_fingerprint_notices_a_rename(tmp_path):
    """Path is hashed with the bytes: swapping the live file for a backup is a change."""
    _write_artifact(tmp_path)
    before = artifact_fingerprint(tmp_path)
    (tmp_path / "server.py").rename(tmp_path / "server.py.backup")
    assert artifact_fingerprint(tmp_path) != before


def test_fingerprint_of_missing_dir_is_empty(tmp_path):
    assert artifact_fingerprint(tmp_path / "nope") == ""


# --- the pre-eval decision --------------------------------------------------

def test_artifact_source_directory_is_available_to_eval(tmp_path):
    """The build-to-eval handoff must resolve the deployed source, not fail after complete."""
    assert ArtifactSubmissionServer().artifact_dir_or_none() is None
    assert _server_with_deploy(tmp_path).artifact_dir_or_none() == tmp_path


def test_unchanged_directory_is_still_redeployed(tmp_path):
    """The common case -- the agent deployed its final code -- must STILL restart the artifact.

    Nothing about the code changed, but the environment underneath it did: the build env was torn down
    and the eval env brought up in its place. An artifact holding a pooled connection is holding a dead
    socket, and whether it recovers is up to code the agent wrote.
    """
    _write_artifact(tmp_path)
    server = _server_with_deploy(tmp_path)
    server._deployed_fingerprint = artifact_fingerprint(tmp_path)

    with mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer_cls:
        deployer_cls.return_value.deploy.return_value = _ok_result()
        endpoint, detail = server.redeploy_before_eval()

    deployer_cls.return_value.deploy.assert_called_once()
    assert endpoint == "http://artifact-test:8080/respond"
    assert "reconnect it to the eval environment" in detail
    assert "directory unchanged" not in detail  # the detail describes what happened, not what didn't


def test_changed_directory_is_rebuilt_and_new_endpoint_returned(tmp_path):
    """The original bug this exists for: edited after deploy, so the container is stale."""
    _write_artifact(tmp_path, "MODEL = 'gpt-4-turbo'\n")
    server = _server_with_deploy(tmp_path)
    server._deployed_fingerprint = artifact_fingerprint(tmp_path)

    # The agent fixes its model id but never redeploys.
    _write_artifact(tmp_path, "import os\nMODEL = os.environ['AGENT_MODEL']\n")

    with mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer_cls:
        deployer_cls.return_value.deploy.return_value = _ok_result()
        endpoint, detail = server.redeploy_before_eval()

    deployer_cls.return_value.deploy.assert_called_once()
    assert endpoint == "http://artifact-test:8080/respond"
    assert "rebuilt from its final directory" in detail
    # The staleness is called out, not silently folded into the reconnect.
    assert "had changed since the agent's last deploy" in detail
    assert server._deployed_fingerprint == artifact_fingerprint(tmp_path)


def test_the_endpoint_must_be_used_because_the_host_port_is_reassigned(tmp_path):
    """A redeploy always returns an endpoint, because the old one stops resolving.

    The artifact's container port is published with no fixed host port, so Docker assigns an ephemeral
    one at every start. Returning None on the unchanged path (as this used to) would leave the caller
    scoring against a host port that no longer exists.
    """
    _write_artifact(tmp_path)
    server = _server_with_deploy(tmp_path)
    server._deployed_fingerprint = artifact_fingerprint(tmp_path)

    with mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer_cls:
        deployer_cls.return_value.deploy.return_value = _ok_result()
        endpoint, _ = server.redeploy_before_eval()

    assert endpoint is not None


def test_failed_redeploy_keeps_the_old_container_and_says_so(tmp_path):
    """A redeploy failure must not abort the run: the old container is still the agent's work.

    It must be loud, though -- the reported detail is the only thing that tells a reader the scored
    container was never restarted against the eval environment.
    """
    _write_artifact(tmp_path)
    server = _server_with_deploy(tmp_path)
    server._deployed_fingerprint = "stale"

    with mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer_cls:
        deployer_cls.return_value.deploy.return_value = DeploymentResult(
            success=False, error="build failed: syntax error in server.py"
        )
        endpoint, detail = server.redeploy_before_eval()

    assert endpoint is None
    assert "FAILED" in detail
    assert "build failed" in detail
    assert "not restarted against the eval environment" in detail
    assert "does not match the submitted artifact" in detail
    # Not recorded as deployed: the failed code never shipped.
    assert server._deployed_fingerprint == "stale"


def test_no_deploy_context_is_a_no_op(tmp_path):
    server = ArtifactSubmissionServer()
    endpoint, detail = server.redeploy_before_eval()
    assert endpoint is None
    assert "no host deploy" in detail


def test_artifact_never_deployed_through_this_route_is_left_alone(tmp_path):
    """An agent that stood its endpoint up some other way owns that container, not us.

    It also owns reconnecting across the handover, which we cannot do for it.
    """
    _write_artifact(tmp_path)
    server = _server_with_deploy(tmp_path)
    assert server._deployed_fingerprint is None

    with mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer_cls:
        endpoint, detail = server.redeploy_before_eval()

    deployer_cls.assert_not_called()
    assert endpoint is None
    assert "never deployed" in detail


def test_the_run_path_redeploys_after_the_eval_env_is_up_and_before_scoring():
    """The redeploy is worthless if nothing calls it, and every test above would still pass.

    A source-order assertion rather than a behavioural one: driving a full orchestrator run needs
    Docker, a task environment and an agent container. What can regress silently is the CALL SITE.
    Two orderings matter, and both are load-bearing:

      * redeploy BEFORE scoring -- restarting the container afterwards changes nothing about what was
        scored (the original regression this guarded);
      * redeploy AFTER the eval environment is started -- restarting while the eval env is still
        coming up reconnects the artifact to nothing.
    """
    import inspect

    from appsci_bench.orchestrator import run as run_mod

    src = inspect.getsource(run_mod)
    assert "redeploy_before_eval()" in src, (
        "the orchestrator no longer redeploys the artifact before eval; it can once again score a "
        "container the agent abandoned, or one still pointed at the torn-down build environment"
    )
    at_redeploy = src.index("redeploy_before_eval()")
    assert at_redeploy < src.index("report = evaluate_deployed("), (
        "the redeploy must run BEFORE eval -- rebuilding afterwards changes nothing about what was "
        "scored"
    )
    assert src.index("phase=PHASE_EVAL") < at_redeploy, (
        "the redeploy must run AFTER the eval environment is started -- otherwise the artifact "
        "reconnects to an environment that is not up yet"
    )
