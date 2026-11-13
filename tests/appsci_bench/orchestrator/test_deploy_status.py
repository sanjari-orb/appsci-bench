"""The deploy-status route: what is deployed, is it stale, and which checkpoint is it.

The agent container cannot answer any of those for itself. The fingerprint has one implementation and
it lives host-side, and the run's checkpoints are deliberately never mounted into the agent container,
so `GET /deploy/status` is the only way `optimize_agent` learns whether its candidate is still the code
running, and which saved copy that is. It refuses to score when the route says stale.

The join is keyed on a checkpoint's content hash, and getting it right takes care because the two
hashes in play use DIFFERENT exclusion sets -- see
`test_the_checkpoint_join_survives_a_dependency_tree_in_the_artifact_dir`, which is the case a naive
implementation gets wrong. That nothing writes into the artifact directory per-deploy (so a hash stays
a stable identity for a candidate) is the agent side's job, asserted in
`tests/appsci_agent/test_optimize_agent.py`.

Docker is never invoked: the deployer is faked throughout.
"""
from __future__ import annotations

from unittest import mock

from fastapi.testclient import TestClient

from appsci_bench.orchestrator.deployer import DeploymentResult
from appsci_bench.orchestrator.submission_server import ArtifactSubmissionServer, DeployContext


def _write_artifact(d) -> None:
    (d / "Dockerfile").write_text("FROM python:3.12-slim\nCOPY server.py .\n")
    (d / "server.py").write_text("# v1\n")


def _server(artifact_dir, run_dir=None) -> ArtifactSubmissionServer:
    server = ArtifactSubmissionServer()
    server.configure_deploy(
        DeployContext(
            artifact_dir=str(artifact_dir),
            image_name="artifact-test:latest",
            container_name="artifact-test",
            network="bench-network",
            port=8080,
            run_dir=str(run_dir) if run_dir else None,
        )
    )
    return server


def _ok_result(endpoint="http://artifact-test:8080/respond"):
    return DeploymentResult(
        success=True, endpoint_url=endpoint, container_name="artifact-test",
        healthy=True, health_detail="HTTP 200",
    )


def _client(server) -> TestClient:
    return TestClient(server._create_app())


def _deploy(client) -> dict:
    with mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer:
        deployer.return_value.deploy.return_value = _ok_result()
        return client.post("/deploy", json={}).json()


# --- GET /deploy/status --------------------------------------------------------------------------

def test_status_reports_not_configured_without_a_deploy_contract(tmp_path):
    client = _client(ArtifactSubmissionServer())
    body = client.get("/deploy/status").json()
    assert body["configured"] is False and body["deployed"] is False
    assert "not configured" in body["reason"]


def test_status_reports_nothing_deployed_before_the_first_deploy(tmp_path):
    _write_artifact(tmp_path)
    body = _client(_server(tmp_path)).get("/deploy/status").json()
    assert body["configured"] is True
    assert body["deployed"] is False
    assert body["deployed_fingerprint"] is None
    assert body["stale"] is False                     # nothing deployed => nothing to be stale from
    assert body["checkpoint"] is None


def test_status_reports_the_endpoint_the_deploy_produced_not_the_submitted_one(tmp_path):
    """`/submit` is not called during BUILD, so reusing its field would report null all run."""
    _write_artifact(tmp_path)
    client = _client(_server(tmp_path))
    _deploy(client)
    body = client.get("/deploy/status").json()
    assert body["deployed"] is True
    assert body["endpoint_url"] == "http://artifact-test:8080/respond"
    assert body["container_name"] == "artifact-test"


def test_status_is_not_stale_right_after_a_deploy(tmp_path):
    _write_artifact(tmp_path)
    client = _client(_server(tmp_path))
    _deploy(client)
    body = client.get("/deploy/status").json()
    assert body["stale"] is False
    assert body["deployed_fingerprint"] == body["current_fingerprint"]


def test_status_goes_stale_when_the_candidate_is_edited(tmp_path):
    _write_artifact(tmp_path)
    client = _client(_server(tmp_path))
    _deploy(client)
    (tmp_path / "server.py").write_text("# edited after the deploy\n")
    body = client.get("/deploy/status").json()
    assert body["stale"] is True
    assert body["deployed_fingerprint"] != body["current_fingerprint"]


def test_status_names_the_checkpoint_the_running_container_was_built_from(tmp_path):
    """The join `optimize_agent` stamps onto every score it records."""
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    _write_artifact(artifact)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    client = _client(_server(artifact, run_dir=run_dir))
    _deploy(client)                                   # the deploy route writes the checkpoint

    body = client.get("/deploy/status").json()
    checkpoint = body["checkpoint"]
    assert checkpoint is not None
    assert checkpoint["name"] == "artifact-test-001"
    assert checkpoint["ordinal"] == 1
    assert checkpoint["content_hash"] == body["deployed_fingerprint"]
    assert checkpoint["outcome"] == "deployed"


def test_status_has_no_checkpoint_without_a_run_directory(tmp_path):
    """Local/in-process flows keep no checkpoints; null is the honest answer, not an error."""
    _write_artifact(tmp_path)
    client = _client(_server(tmp_path, run_dir=None))
    _deploy(client)
    assert client.get("/deploy/status").json()["checkpoint"] is None


def test_status_survives_a_missing_artifact_directory(tmp_path):
    """A status call that 500s tells the agent nothing it can act on."""
    body = _client(_server(tmp_path / "gone")).get("/deploy/status").json()
    assert body["configured"] is True
    assert body["current_fingerprint"] is None
    assert body["deployed"] is False


def test_status_does_not_write_a_checkpoint(tmp_path):
    """It is a read: only an agent-initiated deploy captures a candidate."""
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    _write_artifact(artifact)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    client = _client(_server(artifact, run_dir=run_dir))
    _deploy(client)
    before = sorted(p.name for p in (run_dir / "checkpoints").iterdir())
    client.get("/deploy/status")
    client.get("/deploy/status")
    assert sorted(p.name for p in (run_dir / "checkpoints").iterdir()) == before


# --- checkpoint.find_by_hash --------------------------------------------------------------------

def test_find_by_hash_returns_none_for_an_unknown_hash(tmp_path):
    from appsci_bench.orchestrator import checkpoint
    assert checkpoint.find_by_hash(tmp_path, "nope") is None
    assert checkpoint.find_by_hash(tmp_path, "") is None


def test_find_by_hash_prefers_the_newest_checkpoint_with_that_hash(tmp_path):
    """Redeploying unchanged bytes writes a second copy; the container was built from the later one."""
    from appsci_bench.orchestrator import checkpoint

    artifact = tmp_path / "artifact"
    artifact.mkdir()
    _write_artifact(artifact)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    first = checkpoint.write_checkpoint(artifact, run_dir, "cand", success=True, healthy=True)
    second = checkpoint.write_checkpoint(artifact, run_dir, "cand", success=True, healthy=True)
    assert first.content_hash == second.content_hash

    found = checkpoint.find_by_hash(run_dir, second.content_hash)
    assert found["ordinal"] == second.ordinal == 2


def test_find_by_hash_survives_a_corrupt_manifest(tmp_path):
    from appsci_bench.orchestrator import checkpoint
    checkpoint.manifest_path(tmp_path).write_text("{not json")
    assert checkpoint.find_by_hash(tmp_path, "abc") is None


def test_the_checkpoint_join_survives_a_dependency_tree_in_the_artifact_dir(tmp_path):
    """The staleness fingerprint and the checkpoint hash use DIFFERENT exclusion sets.

    Checkpoints drop dependency and build trees; the staleness fingerprint keeps them. So on any run
    where the agent installed into its own output directory the two hashes differ, and a lookup keyed
    off the staleness fingerprint would return null on exactly those runs. `find_for_artifact` re-hashes
    with the checkpoint set instead, so the join holds.
    """
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    _write_artifact(artifact)
    # A dependency tree of the kind a pip install leaves behind.
    venv = artifact / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "somedep.py").write_text("# vendored\n")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    client = _client(_server(artifact, run_dir=run_dir))
    _deploy(client)
    body = client.get("/deploy/status").json()

    # The precondition for this test to be meaningful: the two hashes really do differ here.
    assert body["checkpoint"]["content_hash"] != body["deployed_fingerprint"]
    # And the join still resolves.
    assert body["checkpoint"]["name"] == "artifact-test-001"
    assert body["stale"] is False


def test_no_checkpoint_is_reported_while_the_artifact_is_stale(tmp_path):
    """A checkpoint named for a stale directory would identify code that is not running."""
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    _write_artifact(artifact)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    client = _client(_server(artifact, run_dir=run_dir))
    _deploy(client)
    assert client.get("/deploy/status").json()["checkpoint"] is not None

    (artifact / "server.py").write_text("# edited, never redeployed\n")
    body = client.get("/deploy/status").json()
    assert body["stale"] is True
    assert body["checkpoint"] is None
