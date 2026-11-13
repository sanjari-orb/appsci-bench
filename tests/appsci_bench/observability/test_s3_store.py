"""S3 run-mirror tests: key layout, manifest, skip-if-unchanged, path safety, credential refresh.

Runs entirely against a fake S3 client -- no network, no new test dependency (moto). The fake is
injected by monkeypatching ``s3_store._client``, which is also where the credential-rotation retry
hooks in, so the expiry tests exercise the real ``_with_retry`` code path.
"""
from __future__ import annotations

import io
import json

import pytest
from botocore.exceptions import ClientError, NoCredentialsError

from shared.observability import s3_store
from shared.observability.s3_store import (
    RemoteTraceNotFoundError,
    S3Config,
    S3CredentialsError,
    S3Error,
)

CFG = S3Config(bucket="test-bucket", prefix="runs", region="us-east-2")


class FakeS3:
    """Minimal in-memory stand-in for the boto3 S3 client surface we use."""

    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects: dict[str, bytes] = dict(objects or {})
        self.uploads: list[str] = []
        self.downloads: list[str] = []
        self.heads: list[str] = []

    # -- listing ---------------------------------------------------------
    def get_paginator(self, _name):
        fake = self

        class _Paginator:
            def paginate(self, Bucket, Prefix, Delimiter=None):  # noqa: N803 -- boto3 kwarg casing
                matching = {k: v for k, v in sorted(fake.objects.items()) if k.startswith(Prefix)}
                if Delimiter:
                    # Mirror S3: keys with the delimiter after Prefix roll up into CommonPrefixes.
                    commons, contents = set(), []
                    for k, v in matching.items():
                        rest = k[len(Prefix):]
                        if Delimiter in rest:
                            commons.add(Prefix + rest.split(Delimiter)[0] + Delimiter)
                        else:
                            contents.append({"Key": k, "Size": len(v), "LastModified": None})
                    yield {"Contents": contents,
                           "CommonPrefixes": [{"Prefix": c} for c in sorted(commons)]}
                    return
                yield {"Contents": [{"Key": k, "Size": len(v), "LastModified": None}
                                    for k, v in matching.items()]}

        return _Paginator()

    # -- objects ---------------------------------------------------------
    def upload_file(self, local, Bucket, Key):  # noqa: N803
        with open(local, "rb") as f:
            self.objects[Key] = f.read()
        self.uploads.append(Key)

    def download_file(self, Bucket, Key, local):  # noqa: N803
        with open(local, "wb") as f:
            f.write(self.objects[Key])
        self.downloads.append(Key)

    def put_object(self, Bucket, Key, Body, ContentType=None):  # noqa: N803
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, Bucket, Key):  # noqa: N803
        # S3 answers a HEAD for a missing key with a bare 404, not NoSuchKey.
        self.heads.append(Key)
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}


@pytest.fixture
def fake(monkeypatch):
    """Install a FakeS3 as the module's client and hand it back."""
    client = FakeS3()
    monkeypatch.setattr(s3_store, "_client", lambda cfg, refresh=False: client)
    return client


def _seed_run(tmp_path, trace="airline_20260729_1200", task="airline_rebooking"):
    """A realistic run dir: two per-source event logs, a model_calls store, artifact/ and eval/."""
    run = tmp_path / trace
    run.mkdir(parents=True)
    events = [
        {"event_id": "e1", "source": "orchestrator", "event_type": "build.start",
         "trace_id": trace, "task_id": task, "timestamp": "2026-07-29T12:00:00+00:00", "payload": {}},
        {"event_id": "e2", "source": "appsci_agent", "event_type": "agent.turn_start",
         "trace_id": trace, "task_id": task, "timestamp": "2026-07-29T12:05:00+00:00", "payload": {}},
    ]
    run.joinpath("events-orchestrator.jsonl").write_text(json.dumps(events[0]) + "\n")
    run.joinpath("events-appsci_agent.jsonl").write_text(json.dumps(events[1]) + "\n")
    run.joinpath("model_calls-appsci_agent.jsonl").write_text(
        json.dumps({"call_id": "c1", "content": "hi"}) + "\n"
    )
    (run / "artifact").mkdir()
    run.joinpath("artifact", "server.py").write_text("print('hello')\n")
    (run / "eval" / "conversations").mkdir(parents=True)
    run.joinpath("eval", "report.json").write_text(json.dumps({"final_score": 0.5}))
    run.joinpath("eval", "conversations", "d1__r0.json").write_text(json.dumps({"id": "d1"}))
    return run


def test_upload_then_download_round_trips_every_file(fake, tmp_path):
    run = _seed_run(tmp_path)
    result = s3_store.upload_trace(run, CFG)

    assert result.trace_id == run.name
    assert result.uri == f"s3://test-bucket/runs/{run.name}/"
    # Nested artifact/ and eval/ files keep their relative layout, and the merged events.jsonl
    # (built by consolidate_trace during upload) ships too.
    keys = set(fake.objects)
    for expected in (
        f"runs/{run.name}/events.jsonl",
        f"runs/{run.name}/events-orchestrator.jsonl",
        f"runs/{run.name}/model_calls-appsci_agent.jsonl",
        f"runs/{run.name}/artifact/server.py",
        f"runs/{run.name}/eval/conversations/d1__r0.json",
        f"runs/{run.name}/run_manifest.json",
    ):
        assert expected in keys, expected

    dest = tmp_path / "pulled"
    dl = s3_store.download_trace(run.name, dest, CFG)
    assert dl.downloaded == len(keys)
    # Byte-identical round trip for every uploaded file.
    for key in keys:
        rel = key[len(f"runs/{run.name}/"):]
        assert (dest / run.name / rel).read_bytes() == fake.objects[key]


def test_upload_subtree_mirrors_only_that_subtree(fake, tmp_path):
    """A mid-run push of one artifact checkpoint must not touch the rest of the run."""
    run = _seed_run(tmp_path)
    ckpt = run / "checkpoints" / "appsci-artifact-airline_rebooking-001"
    ckpt.mkdir(parents=True)
    ckpt.joinpath("server.py").write_text("print('candidate 1')\n")
    ckpt.joinpath("Dockerfile").write_text("FROM python:3.11-slim\n")

    result = s3_store.upload_subtree(run, "checkpoints/appsci-artifact-airline_rebooking-001", CFG)

    assert result.uploaded == 2
    base = f"runs/{run.name}/checkpoints/appsci-artifact-airline_rebooking-001"
    assert set(fake.objects) == {f"{base}/server.py", f"{base}/Dockerfile"}
    # Crucially NOT the completion manifest: writing it mid-run would claim a still-running run had
    # finished uploading, and the whole-run mirror uses its presence as exactly that signal.
    assert f"runs/{run.name}/{s3_store.MANIFEST_NAME}" not in fake.objects


def test_upload_subtree_keys_match_what_the_whole_run_mirror_would_write(fake, tmp_path):
    """Same namespace, so the end-of-run mirror sees these objects as already current and skips them."""
    run = _seed_run(tmp_path)
    ckpt = run / "checkpoints" / "appsci-artifact-airline_rebooking-001"
    ckpt.mkdir(parents=True)
    ckpt.joinpath("server.py").write_text("print('candidate 1')\n")

    s3_store.upload_subtree(run, "checkpoints/appsci-artifact-airline_rebooking-001", CFG)
    fake.uploads.clear()
    s3_store.upload_trace(run, CFG)

    assert f"runs/{run.name}/checkpoints/appsci-artifact-airline_rebooking-001/server.py" \
        not in fake.uploads


def test_upload_subtree_rejects_a_path_escaping_the_run(fake, tmp_path):
    run = _seed_run(tmp_path)
    with pytest.raises(Exception):
        s3_store.upload_subtree(run, "../elsewhere", CFG)


def test_upload_subtree_on_a_missing_subtree_raises(fake, tmp_path):
    run = _seed_run(tmp_path)
    with pytest.raises(s3_store.S3Error):
        s3_store.upload_subtree(run, "checkpoints/nope", CFG)


def test_manifest_summarizes_the_run(fake, tmp_path):
    """The manifest is the "upload finished" marker + bucket-side metadata."""
    run = _seed_run(tmp_path)
    s3_store.upload_trace(run, CFG)

    manifest = json.loads(fake.objects[f"runs/{run.name}/run_manifest.json"])
    assert manifest["trace_id"] == run.name
    assert manifest["task_id"] == "airline_rebooking"
    assert manifest["event_count"] == 2
    assert manifest["start_time"] == "2026-07-29T12:00:00+00:00"
    assert manifest["end_time"] == "2026-07-29T12:05:00+00:00"
    assert manifest["uploaded_at"]
    # The manifest never lists itself.
    assert all(f["path"] != s3_store.MANIFEST_NAME for f in manifest["files"])


def test_reupload_skips_unchanged_files(fake, tmp_path):
    run = _seed_run(tmp_path)
    first = s3_store.upload_trace(run, CFG)
    assert first.uploaded > 0 and first.skipped == 0

    fake.uploads.clear()
    second = s3_store.upload_trace(run, CFG)
    assert second.uploaded == 0
    assert second.skipped == first.uploaded
    assert fake.uploads == []  # nothing re-sent

    # A changed file is re-sent; its unchanged neighbours are not.
    run.joinpath("artifact", "server.py").write_text("print('changed and longer')\n")
    third = s3_store.upload_trace(run, CFG)
    assert third.uploaded == 1
    assert fake.uploads == [f"runs/{run.name}/artifact/server.py"]


def test_force_reuploads_everything(fake, tmp_path):
    run = _seed_run(tmp_path)
    s3_store.upload_trace(run, CFG)
    forced = s3_store.upload_trace(run, CFG, force=True)
    assert forced.skipped == 0 and forced.uploaded > 0


def test_download_skips_files_already_current(fake, tmp_path):
    run = _seed_run(tmp_path)
    s3_store.upload_trace(run, CFG)
    dest = tmp_path / "pulled"

    first = s3_store.download_trace(run.name, dest, CFG)
    second = s3_store.download_trace(run.name, dest, CFG)
    assert second.downloaded == 0
    assert second.skipped == first.downloaded


def test_download_missing_run_raises_not_found(fake, tmp_path):
    with pytest.raises(RemoteTraceNotFoundError, match="nope_run"):
        s3_store.download_trace("nope_run", tmp_path, CFG)


@pytest.mark.parametrize("bad", ["../etc", "a/b", "", ".", "..", "/abs"])
def test_invalid_run_ids_are_rejected(fake, tmp_path, bad):
    with pytest.raises(S3Error, match="Invalid run id"):
        s3_store.download_trace(bad, tmp_path, CFG)


def test_download_refuses_key_escaping_destination(fake, tmp_path):
    """A crafted key must not write outside the runs dir."""
    fake.objects["runs/evil_run/../../../escaped.txt"] = b"pwned"
    with pytest.raises(S3Error, match="escapes destination"):
        s3_store.download_trace("evil_run", tmp_path / "dest", CFG)
    assert not (tmp_path / "escaped.txt").exists()


# ---------------------------------------------------------------------------
# Credential rotation
# ---------------------------------------------------------------------------


def _expired_error():
    return ClientError({"Error": {"Code": "ExpiredToken", "Message": "token expired"}}, "ListObjectsV2")


def test_expired_credentials_rebuild_session_and_retry_once(monkeypatch, tmp_path):
    """The rotation story: a token that lapses mid-operation self-heals on one retry."""

    class FlakyS3(FakeS3):
        """Fails its first operation with an expired token, then behaves normally."""

        def __init__(self, objects=None):
            super().__init__(objects)
            self.failed_once = False

        def _maybe_fail(self):
            if not self.failed_once:
                self.failed_once = True
                raise _expired_error()

        def get_paginator(self, name):
            self._maybe_fail()
            return super().get_paginator(name)

        def download_file(self, Bucket, Key, local):  # noqa: N803
            self._maybe_fail()
            return super().download_file(Bucket, Key, local)

    client = FlakyS3({"runs/r1/events.jsonl": b"{}\n"})
    calls: list[bool] = []

    def fake_client(cfg, refresh=False):
        calls.append(refresh)
        return client

    monkeypatch.setattr(s3_store, "_client", fake_client)

    result = s3_store.download_trace("r1", tmp_path, CFG)
    assert result.downloaded == 1
    assert (tmp_path / "r1" / "events.jsonl").read_bytes() == b"{}\n"
    # Exactly one rebuild: the LIST expired and was retried; the download that followed reused
    # the healed session rather than triggering another refresh.
    assert calls[:2] == [False, True]
    assert calls.count(True) == 1


def test_still_expired_after_retry_raises_actionable_error(monkeypatch, tmp_path):
    class Failing(FakeS3):
        def get_paginator(self, name):
            raise _expired_error()

    monkeypatch.setattr(s3_store, "_client", lambda cfg, refresh=False: Failing())

    with pytest.raises(S3CredentialsError) as exc:
        s3_store.download_trace("r1", tmp_path, CFG)
    assert "setup_s3_sso.sh" in str(exc.value)
    assert "aws sso login" in str(exc.value)


def test_missing_credentials_gives_actionable_error(monkeypatch, tmp_path):
    class NoCreds(FakeS3):
        def get_paginator(self, name):
            raise NoCredentialsError()

    monkeypatch.setattr(s3_store, "_client", lambda cfg, refresh=False: NoCreds())

    with pytest.raises(S3CredentialsError) as exc:
        s3_store.download_trace("r1", tmp_path, CFG)
    assert "setup_s3_sso.sh" in str(exc.value)


def test_access_denied_is_not_retried_as_credential_expiry(monkeypatch, tmp_path):
    """A permission problem is a different failure than an expired token -- don't mask it."""
    attempts: list[bool] = []

    class Denied(FakeS3):
        def get_paginator(self, name):
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")

    def fake_client(cfg, refresh=False):
        attempts.append(refresh)
        return Denied()

    monkeypatch.setattr(s3_store, "_client", fake_client)

    with pytest.raises(S3Error, match="Access denied"):
        s3_store.download_trace("r1", tmp_path, CFG)
    assert attempts == [False], "must not retry a permission error"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_precedence_args_then_env_then_default(monkeypatch):
    monkeypatch.delenv("APPSCI_S3_BUCKET", raising=False)
    monkeypatch.delenv("APPSCI_S3_PREFIX", raising=False)
    monkeypatch.delenv("APPSCI_S3_PROFILE", raising=False)
    monkeypatch.delenv("APPSCI_S3_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    default = S3Config.from_env()
    assert (default.bucket, default.prefix, default.region, default.profile) == (
        s3_store.DEFAULT_BUCKET, s3_store.DEFAULT_PREFIX, s3_store.DEFAULT_REGION, None,
    )

    monkeypatch.setenv("APPSCI_S3_BUCKET", "env-bucket")
    monkeypatch.setenv("APPSCI_S3_PROFILE", "env-profile")
    assert S3Config.from_env().bucket == "env-bucket"
    assert S3Config.from_env().profile == "env-profile"
    # Explicit args win over the environment.
    assert S3Config.from_env(bucket="arg-bucket").bucket == "arg-bucket"


def test_appsci_region_overrides_generic_aws_region(monkeypatch):
    """A shell pointed at another region for unrelated reasons must not break this bucket."""
    monkeypatch.delenv("APPSCI_S3_REGION", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    assert S3Config.from_env().region == "us-east-1"
    monkeypatch.setenv("APPSCI_S3_REGION", "us-east-2")
    assert S3Config.from_env().region == "us-east-2"


def test_uri_and_run_prefix():
    assert CFG.uri() == "s3://test-bucket/runs/"
    assert CFG.uri("r1") == "s3://test-bucket/runs/r1/"
    assert CFG.run_prefix("r1") == "runs/r1"
    flat = S3Config(bucket="b", prefix="")
    assert flat.uri() == "s3://b/" and flat.run_prefix("r1") == "r1"


def test_credential_failure_still_leaves_local_run_complete(monkeypatch, tmp_path):
    """An expired session must cost you the mirror, not the run data.

    upload_trace consolidates the per-source logs *before* its first S3 call, so a credential
    failure still leaves a complete, renderable run on disk -- which is what makes retrying the
    upload later (scripts/upload_runs_to_s3.py) a pure no-data-loss operation.
    """
    run = _seed_run(tmp_path)

    class Failing(FakeS3):
        def get_paginator(self, name):
            raise _expired_error()

    monkeypatch.setattr(s3_store, "_client", lambda cfg, refresh=False: Failing())

    with pytest.raises(S3CredentialsError):
        s3_store.upload_trace(run, CFG)

    # The merged log the dashboard reads exists and has every event from both source logs.
    consolidated = run / "events.jsonl"
    assert consolidated.is_file()
    assert len([ln for ln in consolidated.read_text().splitlines() if ln.strip()]) == 2
    # Nothing was moved or deleted on the way to a failed upload.
    for rel in ("events-orchestrator.jsonl", "model_calls-appsci_agent.jsonl",
                "artifact/server.py", "eval/report.json"):
        assert (run / rel).is_file(), rel


def test_upload_after_a_failure_succeeds_with_no_data_loss(fake, tmp_path, monkeypatch):
    """Retrying a previously-failed upload uploads everything, including the consolidated log."""
    run = _seed_run(tmp_path)

    class Failing(FakeS3):
        def get_paginator(self, name):
            raise _expired_error()

    monkeypatch.setattr(s3_store, "_client", lambda cfg, refresh=False: Failing())
    with pytest.raises(S3CredentialsError):
        s3_store.upload_trace(run, CFG)

    # Credentials come back; the same call now completes.
    monkeypatch.setattr(s3_store, "_client", lambda cfg, refresh=False: fake)
    result = s3_store.upload_trace(run, CFG)
    assert result.uploaded > 0
    assert f"runs/{run.name}/events.jsonl" in fake.objects
    assert f"runs/{run.name}/{s3_store.MANIFEST_NAME}" in fake.objects


def test_list_mirrored_run_ids_uses_one_delimited_list(fake, tmp_path):
    """Backs the run list's badge, so it must not fan out into per-run calls."""
    run = _seed_run(tmp_path)
    s3_store.upload_trace(run, CFG)
    fake.objects["runs/some_other_run/events.jsonl"] = b"{}\n"
    fake.objects["runs/stray_object_at_root"] = b"ignored"

    ids = s3_store.list_mirrored_run_ids(CFG)
    assert ids == {run.name, "some_other_run"}


def test_list_mirrored_run_ids_empty_mirror(fake):
    assert s3_store.list_mirrored_run_ids(CFG) == set()


# ---------------------------------------------------------------------------
# Interrupted pulls
# ---------------------------------------------------------------------------


def test_interrupted_pull_leaves_a_marker(fake, tmp_path):
    """Without this, an aborted download leaves a partial run that renders as if it were whole."""
    run = _seed_run(tmp_path)
    s3_store.upload_trace(run, CFG)
    dest = tmp_path / "pulled"

    calls = {"n": 0}
    real_download = fake.download_file

    def flaky(Bucket, Key, local):  # noqa: N803
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("connection reset mid-pull")
        return real_download(Bucket, Key, local)

    fake.download_file = flaky
    with pytest.raises(OSError):
        s3_store.download_trace(run.name, dest, CFG)

    run_root = dest / run.name
    assert s3_store.pull_incomplete(run_root), "a broken pull must be detectable"
    assert (run_root / s3_store.PULL_MARKER).is_file()


def test_completed_pull_clears_the_marker(fake, tmp_path):
    run = _seed_run(tmp_path)
    s3_store.upload_trace(run, CFG)
    dest = tmp_path / "pulled"
    s3_store.download_trace(run.name, dest, CFG)
    assert not s3_store.pull_incomplete(dest / run.name)


def test_retrying_an_interrupted_pull_completes_it(fake, tmp_path):
    run = _seed_run(tmp_path)
    s3_store.upload_trace(run, CFG)
    dest = tmp_path / "pulled"

    calls = {"n": 0}
    real_download = fake.download_file

    def flaky(Bucket, Key, local):  # noqa: N803
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("connection reset mid-pull")
        return real_download(Bucket, Key, local)

    fake.download_file = flaky
    with pytest.raises(OSError):
        s3_store.download_trace(run.name, dest, CFG)

    fake.download_file = real_download          # network comes back
    s3_store.download_trace(run.name, dest, CFG)

    run_root = dest / run.name
    assert not s3_store.pull_incomplete(run_root)
    for rel in ("events.jsonl", "artifact/server.py", "eval/report.json"):
        assert (run_root / rel).is_file(), rel


def test_upload_refuses_a_half_pulled_run(fake, tmp_path):
    """Uploading a truncated run would publish it as whole and regenerate a manifest from it."""
    run = _seed_run(tmp_path)
    (run / s3_store.PULL_MARKER).write_text("interrupted\n")
    with pytest.raises(S3Error, match="half-pulled"):
        s3_store.upload_trace(run, CFG)
    assert fake.objects == {}, "nothing should have been uploaded"


def test_upload_refuses_an_empty_run_dir(fake, tmp_path):
    empty = tmp_path / "empty_run_20260730"
    empty.mkdir()
    with pytest.raises(S3Error, match="half-pulled"):
        s3_store.upload_trace(empty, CFG)


def test_pull_marker_is_never_uploaded_even_when_forced(fake, tmp_path):
    """It is local bookkeeping; mirroring it would mark every puller's copy as broken."""
    run = _seed_run(tmp_path)
    (run / s3_store.PULL_MARKER).write_text("stray marker\n")
    s3_store.upload_trace(run, CFG, force=True)
    assert f"runs/{run.name}/{s3_store.PULL_MARKER}" not in fake.objects
    assert f"runs/{run.name}/events.jsonl" in fake.objects


def test_empty_run_dir_counts_as_incomplete(tmp_path):
    """download_trace must mkdir before it can write the marker; that window must not read as done."""
    empty = tmp_path / "half"
    empty.mkdir()
    assert s3_store.pull_incomplete(empty)
    (empty / "events.jsonl").write_text("{}\n")
    assert not s3_store.pull_incomplete(empty)


def test_manifest_present_answers_only_about_the_ids_it_is_given(fake, tmp_path):
    """The run list's per-page mirror check: one HEAD per displayed row, nothing bucket-wide."""
    done = _seed_run(tmp_path, "done_run_20260730")
    s3_store.upload_trace(done, CFG)
    fake.objects["runs/half_run_20260730/events.jsonl"] = b"{}\n"   # objects, but no manifest
    fake.heads.clear()

    complete = s3_store.manifest_present(
        [done.name, "half_run_20260730", "never_uploaded_20260730"], CFG
    )

    assert complete == {done.name}
    # Exactly the three asked about, and only the manifest key of each.
    assert sorted(fake.heads) == sorted(
        f"runs/{rid}/{s3_store.MANIFEST_NAME}"
        for rid in (done.name, "half_run_20260730", "never_uploaded_20260730")
    )


def test_manifest_present_skips_an_unusable_id_instead_of_failing_the_page(fake):
    """Ids come off a disk this process does not control; one bad name must not cost the column."""
    assert s3_store.manifest_present(["../etc", "", "a/b"], CFG) == set()
    assert fake.heads == []


def test_manifest_present_is_empty_for_no_ids(fake):
    assert s3_store.manifest_present([], CFG) == set()
    assert fake.heads == []


def test_manifest_present_caps_how_many_it_will_probe(fake, tmp_path):
    """A caller that hands over the whole mirror gets a bounded fan-out, not 400 HEADs."""
    ids = [f"run_{i:04d}_20260730" for i in range(s3_store.MANIFEST_PROBE_MAX + 25)]

    s3_store.manifest_present(ids, CFG)

    assert len(fake.heads) == s3_store.MANIFEST_PROBE_MAX


def test_manifest_present_propagates_a_credential_failure(monkeypatch):
    """An expired session must not render every run as "upload never finished"."""
    class Expired:
        def head_object(self, Bucket, Key):  # noqa: N803
            raise ClientError(
                {"Error": {"Code": "ExpiredToken", "Message": "token expired"}}, "HeadObject"
            )

    monkeypatch.setattr(s3_store, "_client", lambda cfg, refresh=False: Expired())
    with pytest.raises(S3CredentialsError):
        s3_store.manifest_present(["run_a_20260730"], CFG)


def test_manifest_present_propagates_access_denied(monkeypatch):
    class Denied:
        def head_object(self, Bucket, Key):  # noqa: N803
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "HeadObject")

    monkeypatch.setattr(s3_store, "_client", lambda cfg, refresh=False: Denied())
    with pytest.raises(S3Error):
        s3_store.manifest_present(["run_a_20260730"], CFG)


def test_list_complete_run_ids_separates_finished_uploads(fake, tmp_path):
    """A run whose manifest never made it up must not be reported as fully mirrored."""
    run = _seed_run(tmp_path)
    s3_store.upload_trace(run, CFG)
    # Simulate an upload that died before writing the manifest.
    fake.objects["runs/partial_run_20260730/events.jsonl"] = b"{}\n"
    fake.objects["runs/partial_run_20260730/artifact/x.py"] = b"x\n"

    complete, partial = s3_store.list_complete_run_ids(CFG)
    assert complete == {run.name}
    assert partial == {"partial_run_20260730"}
    # The cheaper call cannot tell them apart -- which is the bug this addresses.
    assert s3_store.list_mirrored_run_ids(CFG) == {run.name, "partial_run_20260730"}
