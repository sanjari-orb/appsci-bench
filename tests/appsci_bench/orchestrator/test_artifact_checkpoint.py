"""Does every artifact an agent deploys survive the next thing it writes?

The artifact directory is one mutable directory: each deploy builds whatever is in it, and the next
deploy overwrites it. Once the optimization sub-agent iterates, that directory holds a succession of
distinct candidates and only the last one outlives the run -- so a candidate cannot be rebuilt or
scored later, and a candidate whose deploy FAILED leaves no trace at all.

Docker is never invoked here: the deployer is faked, so these assert what gets kept and what it is
labelled, not the build.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from unittest import mock

import pytest
from shutil import rmtree as shutil_rmtree
from fastapi.testclient import TestClient

from appsci_bench.orchestrator import checkpoint as cp
from appsci_bench.orchestrator.deployer import DeploymentResult, artifact_fingerprint
from appsci_bench.orchestrator.run import BenchmarkOrchestrator
from appsci_bench.orchestrator.submission_server import ArtifactSubmissionServer, DeployContext

PREFIX = "appsci-artifact-airline_rebooking"


def _artifact(d, body="print('v1')\n"):
    d.mkdir(parents=True, exist_ok=True)
    (d / "Dockerfile").write_text("FROM python:3.11-slim\nCOPY server.py .\nCMD python server.py\n")
    (d / "server.py").write_text(body)
    (d / "requirements.txt").write_text("fastapi\n")
    return d


def _manifest(run_dir):
    return json.loads(cp.manifest_path(run_dir).read_text())


# --------------------------------------------------------------------------------------
# What gets written, and what it's called
# --------------------------------------------------------------------------------------

def test_checkpoint_copies_the_artifact_files(tmp_path):
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact")

    record = cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)

    assert record is not None
    dest = cp.checkpoints_dir(run) / record.name
    assert (dest / "server.py").read_text() == "print('v1')\n"
    assert (dest / "Dockerfile").exists()
    assert (dest / "requirements.txt").exists()


def test_ordinal_increments_and_nothing_is_overwritten(tmp_path):
    """Successive deploys must land side by side, in deploy order, not on top of each other."""
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact", body="print('v1')\n")

    first = cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)
    (src / "server.py").write_text("print('v2')\n")
    second = cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)

    assert (first.ordinal, second.ordinal) == (1, 2)
    assert first.name == f"{PREFIX}-001"
    assert second.name == f"{PREFIX}-002"
    root = cp.checkpoints_dir(run)
    assert (root / first.name / "server.py").read_text() == "print('v1')\n"
    assert (root / second.name / "server.py").read_text() == "print('v2')\n"


def test_ordinal_continues_from_what_is_already_on_disk(tmp_path):
    """Numbering is derived from the directory listing, so it never restarts and collides."""
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact")
    (cp.checkpoints_dir(run) / f"{PREFIX}-007").mkdir(parents=True)

    record = cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)

    assert record.ordinal == 8


def test_concurrent_deploys_get_distinct_ordinals(tmp_path):
    """The deploy route is served by a threaded server: two writers must not claim one ordinal, and
    must never merge two candidates into a single directory."""
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact")
    records: list[cp.CheckpointRecord | None] = []
    lock = threading.Lock()

    def write():
        record = cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)
        with lock:
            records.append(record)

    threads = [threading.Thread(target=write) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r is not None for r in records)
    assert sorted(r.ordinal for r in records) == list(range(1, 9))
    assert len({r.name for r in records}) == 8
    assert [c["ordinal"] for c in _manifest(run)["checkpoints"]] == list(range(1, 9))


def test_nothing_but_the_artifact_files_lands_inside_a_checkpoint(tmp_path):
    """A checkpoint IS the build context when it is rebuilt, so metadata may not sit inside it."""
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact")

    record = cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)

    dest = cp.checkpoints_dir(run) / record.name
    assert sorted(p.name for p in dest.iterdir()) == [
        "Dockerfile", "requirements.txt", "server.py",
    ]
    # The manifest lives beside the checkpoints directory, mirroring artifact_manifest.json.
    assert cp.manifest_path(run).parent == run
    assert cp.manifest_path(run).name not in {p.name for p in dest.iterdir()}


def test_build_leftovers_are_not_copied(tmp_path):
    """Caches are import side effects, not authored files, and a stale one can shadow a rebuild."""
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "server.cpython-311.pyc").write_bytes(b"\x00stale")

    record = cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)

    dest = cp.checkpoints_dir(run) / record.name
    assert not (dest / "__pycache__").exists()
    # Hashing both sides with the same exclusions is what makes contents and hash agree.
    assert record.content_hash == artifact_fingerprint(src, cp._EXCLUDE_DIRS)


def test_dependency_trees_are_not_copied(tmp_path):
    """The shared container-snapshot list, not a second one maintained here: same problem, same
    directory, and where an unbounded per-deploy copy actually gets big."""
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact")
    for heavy in ("node_modules", ".venv", "dist", ".mypy_cache"):
        (src / heavy).mkdir()
        (src / heavy / "payload.bin").write_bytes(b"x" * 4096)

    record = cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)

    dest = cp.checkpoints_dir(run) / record.name
    assert sorted(p.name for p in dest.iterdir()) == [
        "Dockerfile", "requirements.txt", "server.py",
    ]
    assert record.file_count == 3


def test_widening_the_fingerprint_exclusions_is_opt_in(tmp_path):
    """The deploy staleness check calls the fingerprint with no exclusion set and must keep hashing
    exactly what it always did -- otherwise this change quietly alters when a redeploy is triggered."""
    src = _artifact(tmp_path / "artifact")
    (src / ".venv").mkdir()
    (src / ".venv" / "payload.bin").write_bytes(b"x" * 4096)

    assert artifact_fingerprint(src) != artifact_fingerprint(src, cp._EXCLUDE_DIRS)
    # Dropping the dependency tree leaves the default hash unchanged; the wider one is already blind
    # to it, so only the default moves.
    default_before = artifact_fingerprint(src)
    wide_before = artifact_fingerprint(src, cp._EXCLUDE_DIRS)
    shutil_rmtree(src / ".venv")
    assert artifact_fingerprint(src) != default_before
    assert artifact_fingerprint(src, cp._EXCLUDE_DIRS) == wide_before


def test_the_submitted_marker_still_matches_when_a_dependency_tree_is_present(tmp_path):
    """The live artifact dir keeps the trees a checkpoint drops, so both sides of the comparison have
    to be hashed the same way -- otherwise every run with a virtualenv reports "matched nothing"."""
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact")
    (src / ".venv").mkdir()
    (src / ".venv" / "payload.bin").write_bytes(b"x" * 4096)

    record = cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)

    assert cp.mark_submitted(run, src) == record.name
    assert _manifest(run)["submitted"]["matched"] is True


# --------------------------------------------------------------------------------------
# The outcome label
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "success, healthy, expected",
    [
        (True, True, cp.OUTCOME_DEPLOYED),
        (True, False, cp.OUTCOME_UNHEALTHY),   # answers, but its health route did not return 200
        (False, False, cp.OUTCOME_FAILED),
    ],
)
def test_outcome_distinguishes_all_three_cases(tmp_path, success, healthy, expected):
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact")

    record = cp.write_checkpoint(src, run, PREFIX, success=success, healthy=healthy)

    assert record.outcome == expected
    assert _manifest(run)["checkpoints"][0]["outcome"] == expected


def test_a_failed_deploy_is_still_checkpointed(tmp_path):
    """The one candidate that gets no score later, so its files are the only evidence it existed."""
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact", body="syntax error(\n")

    record = cp.write_checkpoint(src, run, PREFIX, success=False, healthy=False)

    assert record is not None
    assert (cp.checkpoints_dir(run) / record.name / "server.py").read_text() == "syntax error(\n"


# --------------------------------------------------------------------------------------
# The manifest
# --------------------------------------------------------------------------------------

def test_manifest_accumulates_one_entry_per_deploy(tmp_path):
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact")

    with mock.patch.object(cp, "_image_digest", return_value="python@sha256:abc"):
        cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)
        cp.write_checkpoint(src, run, PREFIX, success=False, healthy=False)

    data = _manifest(run)
    assert data["trace_id"] == run.name
    assert [c["ordinal"] for c in data["checkpoints"]] == [1, 2]
    entry = data["checkpoints"][0]
    assert entry["file_count"] == 3
    assert entry["total_bytes"] > 0
    assert entry["content_hash"]
    # The base image is named by a moving tag; the digest is what a later rebuild is compared against.
    assert entry["base_images"] == [{"ref": "python:3.11-slim", "digest": "python@sha256:abc"}]


def test_base_image_digest_is_null_when_it_cannot_be_resolved(tmp_path):
    """An unpulled or locally-built base has no registry digest -- recorded, not treated as failure."""
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact")

    with mock.patch.object(cp, "_image_digest", return_value=None):
        record = cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)

    assert record.base_images == [{"ref": "python:3.11-slim", "digest": None}]


def test_multi_stage_dockerfile_skips_internal_stage_references(tmp_path):
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact")
    (src / "Dockerfile").write_text(
        "FROM python:3.11-slim AS builder\n"
        "RUN pip install build\n"
        "FROM --platform=linux/amd64 gcr.io/distroless/python3 AS final\n"
        "COPY --from=builder /app /app\n"
    )

    refs = cp._base_image_refs(src)

    assert refs == ["python:3.11-slim", "gcr.io/distroless/python3"]


# --------------------------------------------------------------------------------------
# Which checkpoint is the deliverable
# --------------------------------------------------------------------------------------

def test_submitted_marker_points_at_the_matching_checkpoint(tmp_path):
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact", body="print('v1')\n")
    cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)
    (src / "server.py").write_text("print('v2')\n")
    second = cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)

    name = cp.mark_submitted(run, src)

    assert name == second.name
    submitted = _manifest(run)["submitted"]
    assert submitted["matched"] is True
    assert submitted["ordinal"] == second.ordinal


def test_submitted_marker_names_the_last_retry_not_the_failed_first_attempt(tmp_path):
    """A retry the agent did not edit before -- transient build failure, health check that timed out on
    a slow first boot -- captures the SAME bytes under a different outcome. Naming the earliest match
    would report the delivered artifact as never having deployed."""
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact", body="print('v1')\n")
    failed = cp.write_checkpoint(src, run, PREFIX, success=False, healthy=False)
    unhealthy = cp.write_checkpoint(src, run, PREFIX, success=True, healthy=False)
    deployed = cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)
    assert failed.content_hash == unhealthy.content_hash == deployed.content_hash

    name = cp.mark_submitted(run, src)

    assert name == deployed.name
    submitted = _manifest(run)["submitted"]
    assert submitted["ordinal"] == deployed.ordinal
    # Every capture holding these bytes, so a reader can compare outcomes across the retries.
    assert submitted["matching_ordinals"] == [failed.ordinal, unhealthy.ordinal, deployed.ordinal]


def test_submitted_marker_prefers_the_last_match_over_the_healthiest_one(tmp_path):
    """The reverse order: a healthy deploy, then a redeploy of the same bytes that came up unhealthy.
    Eval queried the LAST container, so reporting the earlier healthy capture would hide that the
    delivered artifact was left unhealthy."""
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact", body="print('v1')\n")
    cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)
    last = cp.write_checkpoint(src, run, PREFIX, success=True, healthy=False)

    assert cp.mark_submitted(run, src) == last.name
    assert _manifest(run)["submitted"]["ordinal"] == last.ordinal


def test_submitted_marker_matches_nothing_when_the_agent_edited_after_deploying(tmp_path):
    """The staleness case: an agent that deploys, edits, and never redeploys submits something no
    checkpoint holds. A later reader must not be able to assume the last checkpoint is the artifact."""
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact", body="print('deployed')\n")
    cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)
    (src / "server.py").write_text("print('edited, never redeployed')\n")

    name = cp.mark_submitted(run, src)

    assert name is None
    submitted = _manifest(run)["submitted"]
    assert submitted["matched"] is False
    assert submitted["ordinal"] is None


def test_marking_a_run_with_no_checkpoints_is_a_no_op(tmp_path):
    run = tmp_path / "airline_rebooking_20260827_101500"
    _artifact(run / "artifact")

    assert cp.mark_submitted(run, run / "artifact") is None
    assert not cp.manifest_path(run).exists()


# --------------------------------------------------------------------------------------
# A lost candidate ends the run
# --------------------------------------------------------------------------------------

def test_missing_artifact_dir_is_not_a_lost_candidate(tmp_path):
    """No artifact directory means no candidate exists to lose, and the deploy fails on its own
    merits -- so this reports rather than ending the run."""
    run = tmp_path / "airline_rebooking_20260827_101500"
    run.mkdir(parents=True)

    assert cp.write_checkpoint(run / "nope", run, PREFIX, success=True, healthy=True) is None


def test_a_copy_failure_raises_rather_than_leaving_a_silent_gap(tmp_path):
    """The next deploy overwrites this directory, so a swallowed failure loses the candidate for good
    and no later pass could tell an incomplete candidate set from a short one."""
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact")

    with mock.patch.object(cp.shutil, "copytree", side_effect=OSError("disk full")):
        with pytest.raises(cp.CheckpointError, match="cannot be recovered"):
            cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)


def test_a_corrupt_manifest_is_replaced_rather_than_crashing(tmp_path):
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact")
    run.mkdir(parents=True, exist_ok=True)
    cp.manifest_path(run).write_text("{not json")

    record = cp.write_checkpoint(src, run, PREFIX, success=True, healthy=True)

    assert record is not None
    assert [c["ordinal"] for c in _manifest(run)["checkpoints"]] == [1]


# --------------------------------------------------------------------------------------
# Capture does not depend on observability, so neither does marking
# --------------------------------------------------------------------------------------

def test_an_observability_off_run_still_gets_its_delivered_capture_marked(tmp_path):
    """Captures are written for every run, keyed on the run tag. Keying the MARKER on the trace id
    instead left an observability-off run holding candidates with no record of which one shipped --
    the one question the manifest is read for later."""
    from appsci_bench.orchestrator.run import _mark_submitted_checkpoint

    # No trace id: the name a run with observability off gets.
    tag = "airline_rebooking-opus48-l2-20260827-101500"
    src = _artifact(tmp_path / tag / "artifact")
    cp.write_checkpoint(src, tmp_path / tag, PREFIX, success=True, healthy=True)
    (src / "server.py").write_text("print('v2')\n")
    delivered = cp.write_checkpoint(src, tmp_path / tag, PREFIX, success=True, healthy=True)

    assert _mark_submitted_checkpoint(tmp_path, tag) == delivered.name
    assert _manifest(tmp_path / tag)["submitted"]["ordinal"] == delivered.ordinal


def test_marking_is_skipped_when_the_run_never_provisioned_a_directory(tmp_path):
    """A run that died before its artifact dir existed has nothing to mark and must not invent it."""
    from appsci_bench.orchestrator.run import _mark_submitted_checkpoint

    assert _mark_submitted_checkpoint(tmp_path, None) is None
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------------------
# The seam: only agent-initiated deploys are captured
# --------------------------------------------------------------------------------------

def _server_with_context(tmp_path, *, checkpointing: bool = True, s3_config=None):
    """A configured server plus its run dir and artifact dir. `checkpointing=False` models the
    local/in-process flow, which has no run directory."""
    run = tmp_path / "airline_rebooking_20260827_101500"
    src = _artifact(run / "artifact")
    server = ArtifactSubmissionServer()
    server.configure_deploy(DeployContext(
        artifact_dir=str(src),
        image_name=f"{PREFIX}:latest",
        container_name=PREFIX,
        network="bench-network",
        port=8080,
        run_dir=str(run) if checkpointing else None,
        checkpoint_s3_config=s3_config,
    ))
    return server, run, src


def test_the_deploy_route_checkpoints_every_attempt(tmp_path):
    server, run, _src = _server_with_context(tmp_path)
    outcomes = [
        DeploymentResult(success=True, endpoint_url="http://a:8080/respond", healthy=True),
        DeploymentResult(success=False, error="build failed"),
    ]
    client_calls = iter(outcomes)

    with mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer_cls:
        deployer_cls.return_value.deploy.side_effect = lambda: next(client_calls)
        app = server._create_app()
        with TestClient(app) as client:
            assert client.post("/deploy").json()["success"] is True
            assert client.post("/deploy").json()["success"] is False

    entries = _manifest(run)["checkpoints"]
    assert [(e["ordinal"], e["outcome"]) for e in entries] == [
        (1, cp.OUTCOME_DEPLOYED), (2, cp.OUTCOME_FAILED),
    ]


def test_no_run_dir_means_no_checkpointing(tmp_path):
    """Local / in-process flows have no run directory; deploy must still work untouched."""
    server, run, _src = _server_with_context(tmp_path, checkpointing=False)

    with mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer_cls:
        deployer_cls.return_value.deploy.return_value = DeploymentResult(
            success=True, endpoint_url="http://a:8080/respond", healthy=True
        )
        app = server._create_app()
        with TestClient(app) as client:
            assert client.post("/deploy").json()["success"] is True

    assert not cp.checkpoints_dir(run).exists()
    assert not cp.manifest_path(run).exists()


def test_a_checkpoint_failure_records_a_fatal_error_without_lying_to_the_agent(tmp_path):
    """The container IS up, so the deploy response stays truthful: telling the system under test its
    code broke when the host's disk did would have it rewrite working code. The run ends instead --
    the orchestrator reads the recorded error and aborts."""
    server, run, _src = _server_with_context(tmp_path)

    with mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer_cls, mock.patch.object(
        cp.shutil, "copytree", side_effect=OSError("disk full")
    ):
        deployer_cls.return_value.deploy.return_value = DeploymentResult(
            success=True, endpoint_url="http://a:8080/respond", healthy=True
        )
        app = server._create_app()
        with TestClient(app) as client:
            body = client.post("/deploy").json()

    assert body["success"] is True
    assert body["endpoint_url"] == "http://a:8080/respond"
    assert server.checkpoint_error is not None
    assert "cannot be recovered" in server.checkpoint_error


def test_a_failed_s3_upload_also_ends_the_run(tmp_path):
    """A candidate reachable only on the host that built it cannot be scored later, so "copied but not
    mirrored" counts as not captured. The local copy is kept as evidence; the run still ends."""
    server, run, _src = _server_with_context(tmp_path, s3_config=object())

    with mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer_cls, mock.patch(
        "shared.observability.s3_store.upload_subtree",
        side_effect=RuntimeError("Token has expired and refresh failed"),
    ):
        deployer_cls.return_value.deploy.return_value = DeploymentResult(
            success=True, endpoint_url="http://a:8080/respond", healthy=True
        )
        app = server._create_app()
        with TestClient(app) as client:
            body = client.post("/deploy").json()

    assert body["success"] is True          # the deploy itself did succeed
    assert server.checkpoint_error is not None
    assert "could not be mirrored" in server.checkpoint_error
    assert (cp.checkpoints_dir(run) / f"{PREFIX}-001").is_dir()


def _deploy_with_upload_failure(server, error):
    """Drive one deploy whose S3 push raises `error`."""
    with mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer_cls, mock.patch(
        "shared.observability.s3_store.upload_subtree", side_effect=error,
    ):
        deployer_cls.return_value.deploy.return_value = DeploymentResult(
            success=True, endpoint_url="http://a:8080/respond", healthy=True
        )
        with TestClient(server._create_app()) as client:
            client.post("/deploy")


def test_an_unexpected_upload_failure_is_logged_with_its_traceback(tmp_path, caplog):
    """This is the last place the cause is visible -- the error travels onward as a string -- and it
    now ends the run, so an undiagnosable one costs a whole run."""
    server, _run, _src = _server_with_context(tmp_path, s3_config=object())

    with caplog.at_level(logging.ERROR):
        _deploy_with_upload_failure(server, RuntimeError("bucket policy denies PutObject"))

    aborts = [r for r in caplog.records if "could not be mirrored" in r.getMessage()]
    assert aborts and aborts[-1].exc_info


def test_an_expired_session_is_logged_as_remediation_without_a_traceback(tmp_path, caplog):
    """The commonest failure by far, and its message already IS the remediation -- a botocore stack
    would bury the one line the reader needs. Same split as the whole-run mirror."""
    from shared.observability.s3_store import S3CredentialsError

    server, _run, _src = _server_with_context(tmp_path, s3_config=object())

    with caplog.at_level(logging.ERROR):
        _deploy_with_upload_failure(server, S3CredentialsError("run ./scripts/setup_s3_sso.sh"))

    aborts = [r for r in caplog.records if "could not be mirrored" in r.getMessage()]
    # `logging` stores a falsy exc_info as False rather than None.
    assert aborts and not aborts[-1].exc_info
    assert "setup_s3_sso.sh" in aborts[-1].getMessage()


def test_a_local_copy_failure_keeps_its_traceback_too(tmp_path, caplog):
    """Not a credentials case, so the disk error that caused it must survive to the log."""
    server, _run, _src = _server_with_context(tmp_path)

    with caplog.at_level(logging.ERROR), mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer_cls, mock.patch(
        "appsci_bench.orchestrator.checkpoint.shutil.copytree",
        side_effect=OSError("No space left on device"),
    ):
        deployer_cls.return_value.deploy.return_value = DeploymentResult(
            success=True, endpoint_url="http://a:8080/respond", healthy=True
        )
        with TestClient(server._create_app()) as client:
            client.post("/deploy")

    aborts = [r for r in caplog.records if "cannot be recovered" in r.getMessage()]
    assert aborts and aborts[-1].exc_info


def test_the_upload_is_synchronous(tmp_path):
    """Held inline rather than handed to a thread, so the failure above is reachable at all -- a
    backgrounded upload cannot fail a run that has already moved on."""
    server, run, _src = _server_with_context(tmp_path, s3_config=object())
    calls: list[str] = []

    with mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer_cls, mock.patch(
        "shared.observability.s3_store.upload_subtree",
        side_effect=lambda *a, **k: calls.append("uploaded") or mock.Mock(uploaded=2),
    ):
        deployer_cls.return_value.deploy.return_value = DeploymentResult(
            success=True, endpoint_url="http://a:8080/respond", healthy=True
        )
        app = server._create_app()
        with TestClient(app) as client:
            client.post("/deploy")

    # Already recorded by the time the response came back -- no join, no wait.
    assert calls == ["uploaded"]
    assert server.checkpoint_error is None


def test_a_run_with_no_mirror_configured_uploads_nothing(tmp_path):
    """Observability off, or uploads disabled: a deliberate configuration, not a failure."""
    server, run, _src = _server_with_context(tmp_path)   # no s3 target

    with mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer_cls, mock.patch(
        "shared.observability.s3_store.upload_subtree",
        side_effect=AssertionError("must not upload without a target"),
    ):
        deployer_cls.return_value.deploy.return_value = DeploymentResult(
            success=True, endpoint_url="http://a:8080/respond", healthy=True
        )
        app = server._create_app()
        with TestClient(app) as client:
            client.post("/deploy")

    assert server.checkpoint_error is None
    assert (cp.checkpoints_dir(run) / f"{PREFIX}-001").is_dir()


def test_no_checkpoint_error_is_recorded_on_a_healthy_run(tmp_path):
    server, _run, _src = _server_with_context(tmp_path)

    with mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer_cls:
        deployer_cls.return_value.deploy.return_value = DeploymentResult(
            success=True, endpoint_url="http://a:8080/respond", healthy=True
        )
        app = server._create_app()
        with TestClient(app) as client:
            client.post("/deploy")

    assert server.checkpoint_error is None


def test_the_pre_eval_redeploy_does_not_checkpoint(tmp_path):
    """It rebuilds bytes /deploy already captured, so a checkpoint here duplicates every run."""
    server, run, src = _server_with_context(tmp_path)
    server._deployed_fingerprint = artifact_fingerprint(src)

    with mock.patch(
        "appsci_bench.orchestrator.submission_server.DockerArtifactDeployer"
    ) as deployer_cls:
        deployer_cls.return_value.deploy.return_value = DeploymentResult(
            success=True, endpoint_url="http://a:8080/respond", healthy=True
        )
        endpoint, _detail = server.redeploy_before_eval()

    assert endpoint is not None
    assert not cp.checkpoints_dir(run).exists()


# --------------------------------------------------------------------------------------
# ... and the orchestrator acts on it
# --------------------------------------------------------------------------------------

class _StubAgent:
    """Agent container that is alive and quiet -- so only the checkpoint error can end the wait."""

    def is_running(self):
        return True

    def get_logs(self):
        return "agent log tail"

    def get_exit_code(self):
        return None


def _wait(server, timeout=5):
    orch = BenchmarkOrchestrator(tasks_dir=Path("tasks"))
    return asyncio.run(
        orch._wait_for_agent_completion(
            submission_server=server, agent_svc=_StubAgent(), timeout=timeout
        )
    )


def test_a_recorded_checkpoint_error_ends_the_wait_as_a_failure():
    server = ArtifactSubmissionServer()
    server._checkpoint_error = "disk full; candidate cannot be recovered"

    result = _wait(server)

    assert result["status"] == "checkpoint_failed"
    assert result["error"] == "disk full; candidate cannot be recovered"
    assert result["logs"] == "agent log tail"


def test_a_lost_candidate_outranks_a_successful_submission():
    """The candidate is already unrecoverable, so scoring the submitted artifact would report a number
    beside a candidate set that is silently short one entry."""
    server = ArtifactSubmissionServer()
    server._artifact_endpoint = "http://a:8080/respond"
    server._checkpoint_error = "disk full; candidate cannot be recovered"

    assert _wait(server)["status"] == "checkpoint_failed"


def test_a_clean_run_still_completes_normally():
    server = ArtifactSubmissionServer()
    server._artifact_endpoint = "http://a:8080/respond"

    result = _wait(server)

    assert result["status"] == "completed"
    assert result["artifact_endpoint"] == "http://a:8080/respond"
