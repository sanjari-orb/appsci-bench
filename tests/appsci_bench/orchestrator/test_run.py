"""Unit tests for BenchmarkOrchestrator helpers in appsci_bench/orchestrator/run.py:

- `_extract_scores`: must read the real EvalReport shape (views["per_metric"] + final_score), not a
  stale `report.metric_results` attribute (regression: it silently returned {} for every task).
- `_docker_env_value` / `_make_env_reset_hooks`: the per-sample DB-reset seam the orchestrator drives
  by `docker exec`-ing the containerised task-env's admin API. subprocess is stubbed (no real docker).
- `_docker_container_name_from_url` / `_docker_container_names_on_network`: how the orchestrator
  recovers an agent-deployed artifact container's identity for `finally:` cleanup.
- `_start_tracer_with_retry`: observability should retry the next few ports rather than hard-fail
  on the first occupied one (regression: a single stuck port aborted the whole batch).
- `_cleanup_artifact_containers` / `_view_port_for_log` / `_register_eval_publisher` /
  `_close_eval_publisher` / `_persist_eval_report`: `run()`'s `finally:`-block wiring, extracted so
  it's testable without mocking the whole orchestrator end to end.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from appsci_bench.eval.datatypes import EvalReport
from appsci_bench.orchestrator.run import (
    BenchmarkOrchestrator, _cleanup_artifact_containers, _close_eval_publisher,
    _docker_container_name_from_url, _docker_container_names_on_network, _docker_env_value,
    _make_env_reset_hooks, _register_eval_publisher, _start_tracer_with_retry, _view_port_for_log,
)
from appsci_bench.tracer.server import TracerPortInUseError, TracerPortRangeExhaustedError


def _orch() -> BenchmarkOrchestrator:
    from pathlib import Path
    return BenchmarkOrchestrator(tasks_dir=Path("tasks"))


# --- query timeout forwarding ------------------------------------------------------------------

def test_evaluator_forwards_query_timeout_s():
    # Regression: an agentic artifact's /respond runs a multi-step tool loop (30-180s+); the
    # orchestrator must give the eval a generous per-turn ceiling, not HttpArtifact's 30s default.
    from appsci_bench.eval import evaluator as ev
    seen = {}

    def fake_run_evaluation(spec, client, **kwargs):
        seen.update(kwargs)
        return EvalReport(task_id="t")

    with patch.object(ev, "run_evaluation", side_effect=fake_run_evaluation), \
         patch.object(ev, "artifact_to_client", side_effect=lambda a: a):
        ev.Evaluator().evaluate(spec={"task_id": "t"}, artifact=object(),
                                query_timeout_s=300.0, eval_timeout_s=180.0)
    assert seen["query_timeout_s"] == 300.0
    assert seen["eval_timeout_s"] == 180.0


def test_query_timeout_default_is_generous():
    # The orchestrator's per-turn ceiling must comfortably exceed a slow agentic turn, and the
    # eval-function ceiling must exceed a slow LLM-judge call (both bit us at 30s / 60s).
    assert BenchmarkOrchestrator.QUERY_TIMEOUT_S >= 180.0
    assert BenchmarkOrchestrator.EVAL_TIMEOUT_S >= 120.0


# --- _extract_scores ---------------------------------------------------------------------------

def test_extract_scores_reads_per_metric_and_final_score():
    report = EvalReport(task_id="t", views={"per_metric": [
        {"objective": "task_success", "score": 1.0, "weight": 1.0, "positive": True},
        {"objective": "policy_compliance", "score": 0.95, "weight": 1.0, "positive": True},
    ]}, final_score=0.975)
    scores = _orch()._extract_scores(report)
    assert scores == {"task_success": 1.0, "policy_compliance": 0.95, "final_score": 0.975}


def test_extract_scores_skips_none_metrics():
    # A metric that never applied (score=None) is excluded, not reported as 0.
    report = EvalReport(task_id="t", views={"per_metric": [
        {"objective": "measured", "score": 0.5, "weight": 1.0, "positive": True},
        {"objective": "n_a", "score": None, "weight": 1.0, "positive": True},
    ]}, final_score=0.5)
    scores = _orch()._extract_scores(report)
    assert scores == {"measured": 0.5, "final_score": 0.5}


def test_extract_scores_empty_report_is_empty_not_crash():
    assert _orch()._extract_scores(EvalReport(task_id="t")) == {}


# --- _docker_env_value -------------------------------------------------------------------------

def test_docker_env_value_extracts_named_var():
    class R:
        returncode = 0
        stdout = "PATH=/usr/bin\nAIRLINE_ENV_ADMIN_TOKEN=admin-secret\nHOME=/root\n"
    with patch("appsci_bench.orchestrator.run.subprocess.run", return_value=R()):
        assert _docker_env_value("c", "AIRLINE_ENV_ADMIN_TOKEN") == "admin-secret"


def test_docker_env_value_missing_var_returns_none():
    class R:
        returncode = 0
        stdout = "PATH=/usr/bin\n"
    with patch("appsci_bench.orchestrator.run.subprocess.run", return_value=R()):
        assert _docker_env_value("c", "AIRLINE_ENV_ADMIN_TOKEN") is None


def test_docker_env_value_inspect_failure_returns_none():
    class R:
        returncode = 1
        stdout = ""
    with patch("appsci_bench.orchestrator.run.subprocess.run", return_value=R()):
        assert _docker_env_value("missing", "X") is None


# --- _make_env_reset_hooks ---------------------------------------------------------------------

def test_reset_hooks_exec_admin_endpoints_with_token():
    calls = []

    class R:
        returncode = 0
        stderr = ""
        # The admin helper prints "<status>\n<body>"; 404 on the optional routes (seed,
        # retrieved_docs, action_trace) is how an env like airline says "I don't serve that".
        stdout = '404\n{"detail":"Not Found"}'

    class HashR(R):
        stdout = '200\n{"state_hash":"deadbeef"}'

    def fake_run(cmd, **kw):
        calls.append(cmd)
        source = cmd[-1]
        url = next((c.split("=", 1)[1] for c in cmd if c.startswith("ADMIN_URL=")), "")
        del source
        if url.endswith("/admin/state_hash"):
            return HashR()
        if url.endswith("/admin/reset"):
            class ResetR(R):
                stdout = '200\n{"status":"reset"}'
            return ResetR()
        return R()

    with patch("appsci_bench.orchestrator.run.subprocess.run", side_effect=fake_run):
        setup, read = _make_env_reset_hooks("airline-workspace-1", "admin-secret")
        setup(None)                      # a sample with no oracle at all -> reset only
        final = read(None)

    # An env serving neither optional read route yields exactly the state hash, as airline always did.
    assert final == {"env_state_hash": "deadbeef"}
    # both hooks docker-exec'd into the named container, carried the admin token via -e (NOT
    # interpolated into the python source), and hit the right admin routes
    reset_cmd = calls[0]
    assert reset_cmd[:2] == ["docker", "exec"]
    assert "airline-workspace-1" in reset_cmd
    assert "ADMIN_TOKEN=admin-secret" in reset_cmd            # token passed via env
    assert "admin-secret" not in reset_cmd[-1]                # NOT baked into the source
    assert "ADMIN_URL=http://localhost:8080/admin/reset" in reset_cmd
    assert "ADMIN_TOKEN" in reset_cmd[-1]
    assert any("ADMIN_URL=http://localhost:8080/admin/state_hash" in c for cmd in calls for c in cmd)


def test_reset_hooks_raise_on_exec_failure():
    class R:
        returncode = 1
        stderr = "container not running"
        stdout = ""
    with patch("appsci_bench.orchestrator.run.subprocess.run", return_value=R()):
        setup, _ = _make_env_reset_hooks("c", "tok")
        try:
            setup(None)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "env admin exec failed" in str(e)


# --- _docker_container_name_from_url ------------------------------------------------------------
# Recovers the artifact container's name from its submitted container-DNS URL, so the
# orchestrator's `finally:` cleanup can `docker rm -f` it (regression: it was never torn down,
# leaving orphaned artifact containers running indefinitely after the run finished).

def test_docker_container_name_from_url_extracts_name():
    assert _docker_container_name_from_url("http://slides-agent:8080/respond") == "slides-agent"


def test_docker_container_name_from_url_none_for_localhost():
    assert _docker_container_name_from_url("http://localhost:8080/respond") is None


def test_docker_container_name_from_url_none_for_ip():
    assert _docker_container_name_from_url("http://127.0.0.1:8080/respond") is None


def test_docker_container_name_from_url_none_for_host_docker_internal():
    assert _docker_container_name_from_url("http://host.docker.internal:8080/respond") is None


def test_docker_container_name_from_url_none_for_unparseable_url():
    assert _docker_container_name_from_url("not a url") is None


# --- _docker_container_names_on_network ---------------------------------------------------------

def test_docker_container_names_on_network_parses_names():
    class R:
        returncode = 0
        stdout = "foo\nbar\n"
        stderr = ""
    with patch("appsci_bench.orchestrator.run.subprocess.run", return_value=R()):
        assert _docker_container_names_on_network("bench-network") == {"foo", "bar"}


def test_docker_container_names_on_network_empty_when_none_present():
    class R:
        returncode = 0
        stdout = "\n"
        stderr = ""
    with patch("appsci_bench.orchestrator.run.subprocess.run", return_value=R()):
        assert _docker_container_names_on_network("bench-network") == set()


def test_docker_container_names_on_network_empty_on_failure():
    class R:
        returncode = 1
        stdout = ""
        stderr = "no such network"
    with patch("appsci_bench.orchestrator.run.subprocess.run", return_value=R()):
        assert _docker_container_names_on_network("bench-network") == set()


# --- _start_tracer_with_retry --------------------------------------------------------------------
# Regression: a single occupied observability port used to hard-fail the run outright, aborting a
# whole run_tasks.sh batch over what's usually just a previous run's orphaned artifact container.

def _fake_tracer_server_cls(fails_on_ports=()):
    calls = []
    kwargs_seen = []

    class FakeServer:
        def __init__(self, runs_dir, port, s3_config=None):
            calls.append(port)
            kwargs_seen.append({"runs_dir": runs_dir, "s3_config": s3_config})
            self.port = port

        async def start(self):
            if self.port in fails_on_ports:
                raise TracerPortInUseError(self.port)
            return f"http://localhost:{self.port}"

    FakeServer.kwargs_seen = kwargs_seen
    return FakeServer, calls


def test_start_tracer_with_retry_succeeds_on_first_free_port():
    fake_cls, calls = _fake_tracer_server_cls(fails_on_ports=())
    with patch("appsci_bench.tracer.server.TracerServer", fake_cls):
        _server, url, port = asyncio.run(_start_tracer_with_retry(Path("runs"), 8080, 5))
    assert (port, url, calls) == (8080, "http://localhost:8080", [8080])


def test_start_tracer_with_retry_skips_occupied_ports():
    fake_cls, calls = _fake_tracer_server_cls(fails_on_ports={8080, 8081})
    with patch("appsci_bench.tracer.server.TracerServer", fake_cls):
        _server, url, port = asyncio.run(_start_tracer_with_retry(Path("runs"), 8080, 5))
    assert (port, url, calls) == (8082, "http://localhost:8082", [8080, 8081, 8082])


def test_start_tracer_with_retry_raises_when_range_exhausted():
    fake_cls, calls = _fake_tracer_server_cls(fails_on_ports={8080, 8081, 8082})
    with patch("appsci_bench.tracer.server.TracerServer", fake_cls):
        try:
            asyncio.run(_start_tracer_with_retry(Path("runs"), 8080, 3))
            assert False, "expected TracerPortRangeExhaustedError"
        except TracerPortRangeExhaustedError as e:
            assert calls == [8080, 8081, 8082]
            assert "8080" in str(e) and "8082" in str(e)


def test_start_tracer_with_retry_does_not_retry_non_port_errors():
    class FakeServer:
        def __init__(self, runs_dir, port, s3_config=None):
            self.port = port

        async def start(self):
            raise RuntimeError("boom")

    with patch("appsci_bench.tracer.server.TracerServer", FakeServer):
        try:
            asyncio.run(_start_tracer_with_retry(Path("runs"), 8080, 5))
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert str(e) == "boom"


def test_start_tracer_with_retry_forwards_s3_config():
    # The dashboard a live run serves should also be able to pull *other* runs from the S3
    # mirror, so the orchestrator's S3 config has to reach the TracerServer it starts.
    from shared.observability.s3_store import S3Config

    cfg = S3Config(bucket="b", prefix="runs")
    fake_cls, _calls = _fake_tracer_server_cls()
    with patch("appsci_bench.tracer.server.TracerServer", fake_cls):
        asyncio.run(_start_tracer_with_retry(Path("runs"), 8080, 5, s3_config=cfg))
    assert fake_cls.kwargs_seen[0]["s3_config"] is cfg


# --- _cleanup_artifact_containers ----------------------------------------------------------------
# `run()`'s `finally:`-block wiring for tearing down the agent-deployed artifact container --
# extracted so it's testable without mocking the whole orchestrator end to end.

def _fake_subprocess_run(calls):
    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R:
            returncode = 0
        return R()
    return fake_run


def test_cleanup_artifact_containers_removes_known_name():
    calls = []
    with patch("appsci_bench.orchestrator.run.subprocess.run", side_effect=_fake_subprocess_run(calls)):
        _cleanup_artifact_containers("slides-agent", None, None)
    assert calls == [["docker", "rm", "-f", "slides-agent"]]


def test_cleanup_artifact_containers_noop_when_nothing_to_clean():
    with patch("appsci_bench.orchestrator.run.subprocess.run") as mock_run:
        _cleanup_artifact_containers(None, None, None)
    mock_run.assert_not_called()


def test_cleanup_artifact_containers_skips_backstop_without_network_name():
    # A baseline was captured but task_env (and so network_name) never got set up -- must not blow
    # up trying to diff a network we don't have a name for.
    with patch("appsci_bench.orchestrator.run.subprocess.run") as mock_run:
        _cleanup_artifact_containers(None, {"a"}, None)
    mock_run.assert_not_called()


def test_cleanup_artifact_containers_backstop_removes_leaked_names():
    # Regression target: agent deployed a container but crashed/timed out before calling complete,
    # so artifact_container_name was never learned -- the network-diff backstop must still catch it.
    calls = []
    with patch("appsci_bench.orchestrator.run.subprocess.run", side_effect=_fake_subprocess_run(calls)), \
         patch("appsci_bench.orchestrator.run._docker_container_names_on_network",
               return_value={"baseline-a", "leaked-b"}):
        _cleanup_artifact_containers(None, {"baseline-a"}, "bench-network")
    assert calls == [["docker", "rm", "-f", "leaked-b"]]


def test_cleanup_artifact_containers_backstop_excludes_already_removed_known_name():
    # The known-name pass already removed it -- the backstop must not attempt it a second time.
    calls = []
    with patch("appsci_bench.orchestrator.run.subprocess.run", side_effect=_fake_subprocess_run(calls)), \
         patch("appsci_bench.orchestrator.run._docker_container_names_on_network",
               return_value={"baseline-a", "known-name"}):
        _cleanup_artifact_containers("known-name", {"baseline-a"}, "bench-network")
    assert calls == [["docker", "rm", "-f", "known-name"]]


def test_cleanup_artifact_containers_backstop_finds_nothing_new():
    with patch("appsci_bench.orchestrator.run.subprocess.run") as mock_run, \
         patch("appsci_bench.orchestrator.run._docker_container_names_on_network",
               return_value={"baseline-a"}):
        _cleanup_artifact_containers(None, {"baseline-a"}, "bench-network")
    mock_run.assert_not_called()


# --- _view_port_for_log ---------------------------------------------------------------------------
# Regression: the end-of-run "View with: ..." hint always printed the *configured* aggregator_port,
# even when the retry loop landed on a different one -- telling the user the wrong port.

def test_view_port_for_log_uses_actual_port_when_server_started():
    assert _view_port_for_log(object(), 8081, 8080) == 8081


def test_view_port_for_log_falls_back_to_configured_port_when_server_never_started():
    assert _view_port_for_log(None, 8081, 8080) == 8080


# --- _register_eval_publisher / _close_eval_publisher ---------------------------------------------

def test_register_eval_publisher_uses_evaluator_source():
    from shared.observability.events import Source

    calls = {}
    with patch("shared.observability.publisher.init_publisher",
               side_effect=lambda **kw: calls.update(kw)):
        _register_eval_publisher(Path("runs"), "trace123", "airline_rebooking")
    assert calls["trace_id"] == "trace123"
    assert calls["task_id"] == "airline_rebooking"
    assert calls["source"] == Source.EVALUATOR
    assert calls["name"] == "evaluator"


def test_close_eval_publisher_closes_when_registered():
    fake_pub = MagicMock()
    with patch("shared.observability.publisher.get_publisher", return_value=fake_pub):
        _close_eval_publisher()
    fake_pub.close.assert_called_once()


def test_close_eval_publisher_noop_when_never_registered():
    with patch("shared.observability.publisher.get_publisher", return_value=None):
        _close_eval_publisher()  # must not raise


# --- BenchmarkOrchestrator._persist_eval_report ----------------------------------------------------

def test_persist_eval_report_copies_report_and_conversations(tmp_path):
    store_root = tmp_path / "store"
    (store_root / "conversations").mkdir(parents=True)
    (store_root / "report.html").write_text("<html></html>")
    (store_root / "report.json").write_text("{}")
    (store_root / "conversations" / "case0.json").write_text("{}")

    class FakeReport:
        pass
    report = FakeReport()
    report.store_root = str(store_root)

    orch = BenchmarkOrchestrator(tasks_dir=Path("tasks"), runs_dir=tmp_path / "runs")
    orch._persist_eval_report(report, "trace123")

    dest = tmp_path / "runs" / "trace123" / "eval"
    assert (dest / "report.html").read_text() == "<html></html>"
    assert (dest / "report.json").read_text() == "{}"
    assert (dest / "conversations" / "case0.json").exists()


def test_persist_eval_report_is_a_noop_when_the_store_is_already_the_destination(tmp_path):
    """Under observability the evaluator writes straight into runs/<trace>/eval, so there is nothing to
    copy -- and the copy must not be attempted. `shutil.copy2(x, x)` raises SameFileError, which would
    turn "this run persisted its progress correctly" into a logged failure."""
    from appsci_bench.orchestrator.run import persist_eval_report

    dest = tmp_path / "runs" / "trace123" / "eval"
    (dest / "conversations").mkdir(parents=True)
    (dest / "report.json").write_text('{"task_id": "t", "partial": false}')
    (dest / "conversations" / "case0.json").write_text("{}")

    copied = persist_eval_report(SimpleNamespace(store_root=str(dest)), dest)

    assert copied == []                                        # nothing moved...
    assert json.loads((dest / "report.json").read_text())["task_id"] == "t"   # ...nothing harmed


def test_persist_eval_report_carries_the_per_case_results_out_of_a_temp_store(tmp_path):
    """A run WITHOUT observability still uses a temp store, so the per-case scores have to be copied
    like the transcripts beside them -- they are what makes an unfinished run's progress readable."""
    from appsci_bench.orchestrator.run import persist_eval_report

    store = tmp_path / "store"
    (store / "conversations").mkdir(parents=True)
    (store / "results").mkdir()
    (store / "report.json").write_text('{"task_id": "t", "partial": true, "cases_scored": 2}')
    (store / "progress.json").write_text('{"cases_scored": 2}')
    (store / "conversations" / "d0__r0.json").write_text("{}")
    (store / "results" / "d0__r0.json").write_text('{"results": []}')

    dest = tmp_path / "runs" / "trace" / "eval"
    copied = persist_eval_report(SimpleNamespace(store_root=str(store)), dest)

    assert (dest / "results" / "d0__r0.json").is_file()
    assert (dest / "progress.json").is_file()
    assert any(entry.startswith("results/") for entry in copied)
    assert "progress.json" in copied


def test_persist_eval_report_noop_without_store_root(tmp_path):
    class FakeReport:
        store_root = None

    orch = BenchmarkOrchestrator(tasks_dir=Path("tasks"), runs_dir=tmp_path / "runs")
    orch._persist_eval_report(FakeReport(), "trace123")
    assert not (tmp_path / "runs" / "trace123").exists()


def test_persist_eval_report_swallows_copy_failure(tmp_path):
    store_root = tmp_path / "store"
    store_root.mkdir()
    (store_root / "report.html").write_text("x")

    class FakeReport:
        pass
    report = FakeReport()
    report.store_root = str(store_root)

    orch = BenchmarkOrchestrator(tasks_dir=Path("tasks"), runs_dir=tmp_path / "runs")
    with patch("shutil.copy2", side_effect=OSError("disk full")):
        orch._persist_eval_report(report, "trace123")  # must not raise


# --- evaluate_deployed: the ONE eval sequence shared by both callers ----------------------------
# This is what makes an eval-only rerun reproduce the orchestrator's score by construction. These
# tests assert the two fidelity-critical decisions it derives from the spec: the user-sim is selected
# iff the task is multi_turn, and per-sample reset hooks are built iff reset_env_per_sample is set
# (and the admin token is recoverable). Docker / HTTP / the real Evaluator are all patched out.

from types import SimpleNamespace  # noqa: E402


def _run_evaluate_deployed(evaluation_data, task_env, *, limit=None, build_transcript=None,
                           build_run_dir=None, workdir=None):
    """Drive evaluate_deployed with every external seam faked; return (captured_evaluate_kwargs,
    made_user_sims, make_hooks_mock)."""
    import appsci_bench.orchestrator.run as run_mod

    captured: dict = {}

    class FakeEvaluator:
        def evaluate(self, spec, client, **kwargs):
            captured["spec"] = spec
            captured["client"] = client
            captured.update(kwargs)
            return "REPORT"

    made_user_sims: list = []

    class FakeUserSim:
        # Only the EXECUTOR is wired by the run: it is a live capability of the running environment.
        # The tools' SIGNATURES are per-case data and ride in the sample itself
        # (`multi_turn_input.user_tools`), so nothing about them passes through here.
        def __init__(self, tool_executor=None):
            self.tool_executor = tool_executor
            made_user_sims.append(self)

    spec = SimpleNamespace(evaluation_data=evaluation_data)
    with patch.object(run_mod, "HttpArtifact", return_value=MagicMock()), \
         patch.object(run_mod, "HttpArtifactClient", return_value=MagicMock()), \
         patch.object(run_mod, "ArtifactAgentUserSim", FakeUserSim), \
         patch.object(run_mod, "Evaluator", FakeEvaluator), \
         patch.object(run_mod, "_docker_env_value", return_value="admin-tok"), \
         patch.object(run_mod, "_make_env_reset_hooks", return_value=("SETUP", "READ")) as mk_hooks:
        report = run_mod.evaluate_deployed(
            spec, "http://localhost:1234/respond", task_env, limit=limit,
            build_transcript=build_transcript, build_run_dir=build_run_dir, workdir=workdir,
        )
    assert report == "REPORT"
    return captured, made_user_sims, mk_hooks


def test_evaluate_deployed_forwards_the_durable_workdir():
    """The evaluator has to be pointed AT the run dir, not copied out of a temp dir afterwards.

    This is the wiring that made per-case progress survivable: writes land where the tracer reads and
    the S3 mirror ships, as they happen. Both orchestrator call sites pass runs/<trace>/eval; a
    regression here silently restores the old behaviour, where a killed run's transcripts stayed in
    /tmp on the worker host and the copy step was never reached."""
    captured, _, _ = _run_evaluate_deployed(
        {"type": "single_turn"}, task_env=None, workdir="/runs/trace123/eval"
    )
    assert captured["workdir"] == "/runs/trace123/eval"


# The per-case seams now ride on the LANE rather than as loose evaluator kwargs -- a run can score
# against several copies of the task environment, and a reset hook is bound to exactly one of them.
# At the default width there is one lane and it is lane 0, so these assertions are the same claims
# they always were, read one level in.
def _lane0(captured):
    return captured["lanes"].lanes[0]


def test_evaluate_deployed_multi_turn_uses_user_sim():
    captured, made, _ = _run_evaluate_deployed({"type": "multi_turn"}, task_env=None)
    assert captured["user_sim"] in made          # an ArtifactAgentUserSim was constructed and passed
    assert _lane0(captured).user_sim is captured["user_sim"]
    assert len(made) == 1


def test_evaluate_deployed_single_turn_has_no_user_sim():
    captured, made, _ = _run_evaluate_deployed({"type": "single_turn"}, task_env=None)
    assert captured["user_sim"] is None
    assert _lane0(captured).user_sim is None
    assert made == []


def test_evaluate_deployed_builds_reset_hooks_when_opted_in():
    task_env = {"workspace_container": "airline-workspace-1", "env_admin_token_var": "ADMIN_TOKEN"}
    captured, _, mk_hooks = _run_evaluate_deployed(
        {"type": "single_turn", "reset_env_per_sample": True}, task_env
    )
    mk_hooks.assert_called_once_with("airline-workspace-1", "admin-tok")
    assert _lane0(captured).setup_state == "SETUP"
    assert _lane0(captured).read_final_state == "READ"
    # Hooks present => the lane is held exclusively, so one case runs at a time per environment.
    assert captured["lanes"].max_workers == 1


def test_evaluate_deployed_no_reset_hooks_when_not_opted_in():
    captured, _, mk_hooks = _run_evaluate_deployed({"type": "single_turn"}, task_env=None)
    mk_hooks.assert_not_called()
    assert _lane0(captured).setup_state is None
    assert _lane0(captured).read_final_state is None
    # No hooks => nothing to serialize, so the pool imposes no ceiling of its own and the historic
    # sizing rule still decides how wide generation runs.
    assert captured["lanes"].max_workers is None


def test_evaluate_deployed_no_reset_hooks_when_admin_token_missing():
    # reset opted in, but the admin var isn't set on the env -> no hooks, no crash (DB component just
    # goes unmeasured), matching the orchestrator's own graceful degradation.
    task_env = {"workspace_container": "c", "env_admin_token_var": None}
    captured, _, mk_hooks = _run_evaluate_deployed(
        {"type": "single_turn", "reset_env_per_sample": True}, task_env
    )
    mk_hooks.assert_not_called()
    assert _lane0(captured).setup_state is None
    assert _lane0(captured).read_final_state is None


def test_evaluate_deployed_forwards_limit():
    captured, _, _ = _run_evaluate_deployed({"type": "single_turn"}, task_env=None, limit=2)
    assert captured["limit"] == 2


# --- _mirror_run_to_s3 ---------------------------------------------------------------------------
# The end-of-run S3 mirror. Observability must never break a real run (design goal #3 in
# shared/observability/README.md), so every upload failure is swallowed with a warning.

def test_mirror_run_to_s3_returns_uri_on_success(tmp_path):
    from shared.observability import s3_store
    from appsci_bench.orchestrator.run import _mirror_run_to_s3

    (tmp_path / "t1").mkdir()
    fake_result = s3_store.UploadResult(trace_id="t1", uri="s3://b/runs/t1/", uploaded=3,
                                        bytes_uploaded=1234)
    with patch.object(s3_store, "upload_trace", return_value=fake_result):
        assert _mirror_run_to_s3(tmp_path, "t1", "airline_rebooking", None) == "s3://b/runs/t1/"


def test_mirror_run_to_s3_swallows_expired_credentials(tmp_path, caplog):
    """The common case: a lapsed AWS session must not fail a run that already finished."""
    from shared.observability import s3_store
    from appsci_bench.orchestrator.run import _mirror_run_to_s3

    (tmp_path / "t1").mkdir()
    (tmp_path / "t1" / "events.jsonl").write_text('{"event_id":"e1"}\n')

    err = s3_store.S3CredentialsError("run ./scripts/setup_s3_sso.sh")
    with patch.object(s3_store, "upload_trace", side_effect=err):
        assert _mirror_run_to_s3(tmp_path, "t1", "airline_rebooking", None) is None

    # Logged with the remediation, and the local run is untouched so it can be re-uploaded later.
    assert "setup_s3_sso.sh" in caplog.text
    assert (tmp_path / "t1" / "events.jsonl").is_file()


def test_mirror_run_to_s3_swallows_any_unexpected_error(tmp_path):
    from shared.observability import s3_store
    from appsci_bench.orchestrator.run import _mirror_run_to_s3

    (tmp_path / "t1").mkdir()
    with patch.object(s3_store, "upload_trace", side_effect=RuntimeError("network exploded")):
        assert _mirror_run_to_s3(tmp_path, "t1", "t", None) is None


def test_mirror_run_to_s3_no_op_when_run_dir_missing(tmp_path):
    from appsci_bench.orchestrator.run import _mirror_run_to_s3
    assert _mirror_run_to_s3(tmp_path, "never_ran", "t", None) is None


def test_mirror_run_to_s3_swallows_unconfigured_s3(tmp_path):
    """A user who never set up S3 must still get their run -- just unmirrored."""
    from shared.observability import s3_store
    from appsci_bench.orchestrator.run import _mirror_run_to_s3

    (tmp_path / "t1").mkdir()
    (tmp_path / "t1" / "events.jsonl").write_text('{"event_id":"e1"}\n')

    err = s3_store.S3NotConfiguredError("No S3 bucket configured")
    with patch.object(s3_store, "upload_trace", side_effect=err):
        assert _mirror_run_to_s3(tmp_path, "t1", "airline_rebooking", None) is None
    assert (tmp_path / "t1" / "events.jsonl").is_file()


# --- the build transcript the elicitation diagnostic scores -----------------------------------
#
# The orchestrator is the only party that knows where this run's build conversation was written, so it
# has to hand the path over; nothing downstream may guess. These pin that it is forwarded, and that the
# observability-off case -- where there is no trace directory at all -- passes nothing rather than
# inventing a path.

def test_evaluate_deployed_forwards_the_build_transcript():
    import pathlib
    captured, _, _ = _run_evaluate_deployed(
        {"type": "single_turn"}, task_env=None, build_transcript=pathlib.Path("runs/some_trace"))
    assert captured["build_transcript"] == pathlib.Path("runs/some_trace")


def test_evaluate_deployed_forwards_none_when_there_is_no_trace_directory():
    # Observability off: there is no event log, so the diagnostic reports itself unmeasurable. That is
    # the correct outcome, not a bug -- and it is why None is forwarded explicitly rather than the
    # argument being omitted.
    captured, _, _ = _run_evaluate_deployed({"type": "single_turn"}, task_env=None)
    assert "build_transcript" in captured and captured["build_transcript"] is None


def test_the_evaluator_wrapper_passes_the_transcript_through_to_run_evaluation():
    from appsci_bench.eval.evaluator import Evaluator

    captured: dict = {}
    with patch("appsci_bench.eval.evaluator.run_evaluation",
               side_effect=lambda spec, client, **kw: captured.update(kw) or "REPORT"):
        Evaluator().evaluate({"task_id": "t"}, MagicMock(), build_transcript="runs/trace_x")
    assert captured["build_transcript"] == "runs/trace_x"


# --- the build run dir the optimization diagnostic reads --------------------------------------
#
# The same forwarding, for the other build-phase diagnostic. Its own argument rather than a reuse of
# the transcript one, because that one also accepts a bare transcript file and an already-read
# conversation, neither of which carries the event log this reads.

def test_evaluate_deployed_forwards_the_build_run_dir():
    import pathlib
    captured, _, _ = _run_evaluate_deployed(
        {"type": "single_turn"}, task_env=None, build_run_dir=pathlib.Path("runs/some_trace"))
    assert captured["build_run_dir"] == pathlib.Path("runs/some_trace")


def test_evaluate_deployed_forwards_no_build_run_dir_when_there_is_no_trace_directory():
    captured, _, _ = _run_evaluate_deployed({"type": "single_turn"}, task_env=None)
    assert "build_run_dir" in captured and captured["build_run_dir"] is None


def test_the_evaluator_wrapper_passes_the_build_run_dir_through_to_run_evaluation():
    from appsci_bench.eval.evaluator import Evaluator

    captured: dict = {}
    with patch("appsci_bench.eval.evaluator.run_evaluation",
               side_effect=lambda spec, client, **kw: captured.update(kw) or "REPORT"):
        Evaluator().evaluate({"task_id": "t"}, MagicMock(), build_run_dir="runs/trace_x")
    assert captured["build_run_dir"] == "runs/trace_x"


def test_the_run_directory_shape_the_orchestrator_hands_over_is_where_the_event_log_lives():
    """The orchestrator passes `runs_dir / trace_id`. What could be wrong is not the guard -- it is
    whether that directory is the one holding the log, rather than some subdirectory of it. Checked
    against a real run on disk, since a fixture would only assert the fixture's own layout.

    (The ordering that makes this safe -- the user-sim container stops before eval begins, and the log
    is a host bind-mount so it is complete by then -- is a live-run check, not a unit test.)
    """
    import pathlib

    from appsci_bench.eval.build_transcript import USERSIM_EVENTS_FILENAME, read_run_dir

    runs = pathlib.Path(__file__).resolve().parents[3] / "runs"
    candidates = sorted(runs.glob("*/" + USERSIM_EVENTS_FILENAME)) if runs.is_dir() else []
    if not candidates:
        pytest.skip("no run with a user-sim event log on disk (runs/ is gitignored)")

    read = {c.parent.name: read_run_dir(c.parent) for c in candidates}
    # `unreadable` at this path shape would mean the orchestrator hands over the wrong directory.
    assert [n for n, t in read.items() if t.status == "unreadable"] == []
    # `empty` is a legitimate outcome, not a path problem -- a run where the agent never contacted the
    # stakeholder, which does happen. So the path shape is only confirmed by at least one run reading
    # back a real conversation.
    assert any(len(t) > 0 for t in read.values()), f"no run read back exchanges: {read.keys()}"


def test_persist_eval_report_carries_the_elicitation_result_out_of_the_temp_store(tmp_path):
    """The eval phase writes elicitation.json into a store that is a tempfile directory, so anything not
    copied here is simply lost when the run ends. It exists precisely so a consumer wanting only the
    diagnostic need not parse a whole report -- which is worth nothing if it never reaches runs/<trace>/."""
    from appsci_bench.orchestrator.run import persist_eval_report

    store = tmp_path / "store"
    store.mkdir()
    (store / "report.json").write_text('{"task_id": "t"}')
    (store / "report.html").write_text("<p>report</p>")
    (store / "elicitation.json").write_text('{"status": "scored", "elicited_rate": 0.58}')
    (store / "conversations").mkdir()
    (store / "conversations" / "case__r0.json").write_text("{}")

    dest = tmp_path / "runs" / "trace" / "eval"
    copied = persist_eval_report(SimpleNamespace(store_root=str(store)), dest)

    assert "elicitation.json" in copied
    assert json.loads((dest / "elicitation.json").read_text())["elicited_rate"] == 0.58
    # and the pre-existing entries are unaffected
    assert (dest / "report.json").is_file() and (dest / "report.html").is_file()
    assert (dest / "conversations" / "case__r0.json").is_file()


def test_persist_eval_report_does_not_invent_an_elicitation_file(tmp_path):
    """A run without a rubric writes none, and the copy must not fail or fabricate one."""
    from appsci_bench.orchestrator.run import persist_eval_report

    store = tmp_path / "store"
    store.mkdir()
    (store / "report.json").write_text('{"task_id": "t"}')
    dest = tmp_path / "eval"
    copied = persist_eval_report(SimpleNamespace(store_root=str(store)), dest)
    assert copied == ["report.json"]
    assert not (dest / "elicitation.json").exists()


# --- the CLI's Fireworks preflight -------------------------------------------------------------
#
# A missing Fireworks key is an ENVIRONMENT BLOCKER: it will hit every remaining row of a batch or
# cell of a sweep identically. Exit 3 is what tells run_tasks.sh and the sweep worker to stop draining
# instead of failing each one the same way, and it has to happen before any container starts.

def test_cli_exits_3_for_a_fireworks_model_with_no_key(monkeypatch):
    import sys

    from appsci_bench.core import agent_container
    from appsci_bench.orchestrator.run import main

    for name in agent_container.FIREWORKS_KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "argv", [
        "run", "--task", "airline_rebooking", "--persona", "l3.yaml",
        "--agent-model", "fireworks_ai/kimi-k3",
    ])
    # Nothing may reach the orchestrator: the point is that the check precedes every side effect.
    monkeypatch.setattr(
        "appsci_bench.orchestrator.run.BenchmarkOrchestrator.run",
        MagicMock(side_effect=AssertionError("orchestrator ran despite a missing Fireworks key")),
    )

    with pytest.raises(SystemExit) as exc:
        asyncio.run(main())
    assert exc.value.code == 3


def test_cli_agent_model_defaults_to_the_env_var(monkeypatch):
    """`export AGENT_MODEL=...` has to actually select the model, or the setup script's success banner
    is a lie: it exports that var, and nothing host-side used to read it. Routing it through the flag
    (rather than forwarding the var into the container) is what keeps the run id honest -- the id and
    the run record are built from the same resolved value the container is given."""
    import sys

    from appsci_bench.core import agent_container
    from appsci_bench.orchestrator.run import main

    for name in agent_container.FIREWORKS_KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_MODEL", "fireworks_ai/kimi-k3")
    monkeypatch.setattr(sys, "argv", [
        "run", "--task", "airline_rebooking", "--persona", "l3.yaml",
    ])
    monkeypatch.setattr(
        "appsci_bench.orchestrator.run.BenchmarkOrchestrator.run",
        MagicMock(side_effect=AssertionError("orchestrator ran despite a missing Fireworks key")),
    )

    # Exit 3 is the proof the env var reached the flag: without the default, args.agent_model is None
    # and the Fireworks preflight short-circuits, so the run would proceed instead.
    with pytest.raises(SystemExit) as exc:
        asyncio.run(main())
    assert exc.value.code == 3


def test_cli_preflight_ignores_a_non_fireworks_model(monkeypatch):
    """The preflight reads the model id and nothing else, so it cannot fail an existing run. Reaching
    the orchestrator call is the assertion -- what that call then does is another test's business."""
    import sys

    from appsci_bench.core import agent_container
    from appsci_bench.orchestrator.run import main

    for name in agent_container.FIREWORKS_KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "argv", [
        "run", "--task", "airline_rebooking", "--persona", "l3.yaml",
        "--agent-model", "claude-opus-4-8",
    ])
    reached = MagicMock(side_effect=RuntimeError("reached the orchestrator"))
    monkeypatch.setattr("appsci_bench.orchestrator.run.BenchmarkOrchestrator.run", reached)

    with pytest.raises(RuntimeError, match="reached the orchestrator"):
        asyncio.run(main())


# --- BenchmarkOrchestrator._maybe_build_redteam_trace -----------------------------------------------

def test_maybe_build_redteam_trace_noop_for_cooperative_persona(tmp_path):
    """assemble_trace_dir returning None (no adversarial_pressures declared) must mean no subprocess
    is even attempted -- a cooperative-persona run pays nothing for this feature."""
    import yaml
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(yaml.safe_dump({
        "persona_id": "p", "task_id": "order_to_cash", "level": "L1",
        "role": "r", "domain_knowledge": {}, "adversarial_pressures": [],
    }))
    orch = BenchmarkOrchestrator(tasks_dir=Path("tasks"), runs_dir=tmp_path / "runs")
    with patch("appsci_bench.orchestrator.run.subprocess.run") as mock_run:
        orch._maybe_build_redteam_trace("trace1", "order_to_cash", persona_path)
    mock_run.assert_not_called()


def test_maybe_build_redteam_trace_invokes_the_real_scorer_script_for_adversarial_persona(tmp_path, caplog):
    """When a trace IS assembled and the scorer genuinely succeeds (exit 0, verdict.json written),
    the scorer must be invoked against exactly that directory and success logged."""
    import logging
    import yaml
    run_dir = tmp_path / "runs" / "trace2"
    run_dir.mkdir(parents=True)
    (run_dir / "events-usersim.jsonl").write_text(json.dumps(
        {"event_type": "usersim.request", "payload": {"turn": 1, "question": "q"}}) + "\n" + json.dumps(
        {"event_type": "usersim.response", "payload": {"turn": 1, "answer": "a"}}))
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(yaml.safe_dump({
        "persona_id": "p", "task_id": "order_to_cash", "level": "L1",
        "role": "r", "domain_knowledge": {}, "adversarial_pressures": ["pushback"],
    }))
    orch = BenchmarkOrchestrator(tasks_dir=Path("tasks"), runs_dir=tmp_path / "runs")

    def fake_run(cmd, **kw):
        # Model the real scorer's success path: exit 0 AND verdict.json written -- both, since
        # that's what the orchestrator now checks (a scorer that changed to exit 0 without writing
        # the file must not read as success either).
        (Path(cmd[2]) / "verdict.json").write_text("{}")
        result = MagicMock()
        result.returncode = 0
        result.stdout, result.stderr = "", ""
        return result

    with patch("appsci_bench.eval.redteam_trace.llm_judge_call") as mock_judge, \
         patch("appsci_bench.orchestrator.run.subprocess.run", side_effect=fake_run) as mock_run:
        mock_judge.side_effect = lambda **kw: kw["output_schema"](reasoning="stub", fired=[])
        with caplog.at_level(logging.INFO, logger="appsci_bench.orchestrator.run"):
            orch._maybe_build_redteam_trace("trace2", "order_to_cash", persona_path)
    assert (run_dir / "redteam" / "meta.json").exists()
    mock_run.assert_called_once()
    args = mock_run.call_args.args[0]
    assert args[1] == "scripts/score_redteam_trace.py"
    assert args[2] == str(run_dir / "redteam")
    assert any("assembled and scored" in r.message for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_maybe_build_redteam_trace_warns_not_infos_on_a_clean_nonzero_exit(tmp_path, caplog):
    """The exact gap review flagged: check=False means a non-zero exit does not raise -- a scorer
    that fails cleanly (sys.exit before verdict.json is written) must not be logged as a success."""
    import logging
    import yaml
    run_dir = tmp_path / "runs" / "trace2b"
    run_dir.mkdir(parents=True)
    (run_dir / "events-usersim.jsonl").write_text(json.dumps(
        {"event_type": "usersim.request", "payload": {"turn": 1, "question": "q"}}) + "\n" + json.dumps(
        {"event_type": "usersim.response", "payload": {"turn": 1, "answer": "a"}}))
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(yaml.safe_dump({
        "persona_id": "p", "task_id": "order_to_cash", "level": "L1",
        "role": "r", "domain_knowledge": {}, "adversarial_pressures": ["pushback"],
    }))
    orch = BenchmarkOrchestrator(tasks_dir=Path("tasks"), runs_dir=tmp_path / "runs")

    def fake_failing_run(cmd, **kw):
        # No verdict.json written, mirroring score_redteam_trace.py's real
        # sys.exit(f"no trace dir at {args.trace_dir}") path -- exits non-zero, no exception raised.
        result = MagicMock()
        result.returncode = 1
        result.stdout, result.stderr = "", "no trace dir at ...\n"
        return result

    with patch("appsci_bench.eval.redteam_trace.llm_judge_call") as mock_judge, \
         patch("appsci_bench.orchestrator.run.subprocess.run", side_effect=fake_failing_run):
        mock_judge.side_effect = lambda **kw: kw["output_schema"](reasoning="stub", fired=[])
        with caplog.at_level(logging.INFO, logger="appsci_bench.orchestrator.run"):
            orch._maybe_build_redteam_trace("trace2b", "order_to_cash", persona_path)

    assert not (run_dir / "redteam" / "verdict.json").exists()
    assert not any("assembled and scored" in r.message for r in caplog.records)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a clean non-zero scorer exit must be logged at WARNING, not silently succeed"
    assert "exit 1" in warnings[0].message


def test_maybe_build_redteam_trace_swallows_assembly_failure(tmp_path):
    """A broken run dir (e.g. an unreadable persona path) must not fail the run -- same best-effort
    posture as _persist_eval_report."""
    orch = BenchmarkOrchestrator(tasks_dir=Path("tasks"), runs_dir=tmp_path / "runs")
    orch._maybe_build_redteam_trace("trace3", "order_to_cash", tmp_path / "no-such-persona.yaml")


def test_maybe_build_redteam_trace_swallows_subprocess_failure(tmp_path):
    import yaml
    run_dir = tmp_path / "runs" / "trace4"
    run_dir.mkdir(parents=True)
    (run_dir / "events-usersim.jsonl").write_text("")
    persona_path = tmp_path / "persona.yaml"
    persona_path.write_text(yaml.safe_dump({
        "persona_id": "p", "task_id": "order_to_cash", "level": "L1",
        "role": "r", "domain_knowledge": {}, "adversarial_pressures": ["pushback"],
    }))
    orch = BenchmarkOrchestrator(tasks_dir=Path("tasks"), runs_dir=tmp_path / "runs")
    with patch("appsci_bench.orchestrator.run.subprocess.run", side_effect=OSError("boom")):
        orch._maybe_build_redteam_trace("trace4", "order_to_cash", persona_path)  # must not raise


# --- BenchmarkOrchestrator._persist_optimization_diagnostic -----------------------------------
#
# Written in the `finally:` so it exists for a run that never reaches an eval -- a `--skip-eval` build
# cell (the build IS where the optimization loop ran) and a run that died inside the loop.

def _write_optimize_call(run_dir: Path) -> None:
    """One call that measured the candidate and found the scores flat -- which is the shape a
    SATURATED stop actually has. A cycle that changed and redeployed leaves a score nobody has taken,
    and the tool reports that as `awaiting_verification` instead."""
    events = [
        {"source": "appsci_agent", "event_type": "agent.tool_invocation", "span_id": "s1",
         "timestamp": "2026-09-01T12:01:00+00:00", "task_id": "order_to_cash",
         "payload": {"tool": "optimize_agent"}},
        {"source": "appsci_agent", "event_type": "agent.tool_result", "span_id": "s1",
         "timestamp": "2026-09-01T12:02:00+00:00", "task_id": "order_to_cash",
         "payload": {"success": True, "status": "saturated", "iteration": 1,
                     "headline_metric": 0.6, "saturated": True, "budget_exhausted": False,
                     "cycles_used": 1, "cycles_allowed": 5,
                     "cycle": {"changed": False}}},
    ]
    (run_dir / "events-appsci_agent.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events))
    (run_dir / "events-orchestrator.jsonl").write_text(json.dumps(
        {"source": "orchestrator", "event_type": "build.start",
         "timestamp": "2026-09-01T12:00:00+00:00", "task_id": "order_to_cash",
         "payload": {"optimization_enabled": True, "optimize_cycles": 5}}) + "\n")


def test_persist_optimization_diagnostic_writes_the_counts_beside_the_run(tmp_path):
    run_dir = tmp_path / "runs" / "trace5"
    run_dir.mkdir(parents=True)
    _write_optimize_call(run_dir)
    orch = BenchmarkOrchestrator(tasks_dir=Path("tasks"), runs_dir=tmp_path / "runs")

    orch._persist_optimization_diagnostic("trace5", had_build_phase=True)

    payload = json.loads((run_dir / "optimization.json").read_text())
    assert payload["status"] == "scored"
    assert payload["calls"] == 1 and payload["cycles_used"] == 1
    assert payload["stop_reason"] == "saturated"
    assert payload["early_stopped_on_saturation"] is True


def test_persist_optimization_diagnostic_records_a_run_it_cannot_measure(tmp_path):
    """An unmeasurable run still gets the file: "we could not see the loop" is the finding, and a
    missing file is indistinguishable from a run nobody looked at."""
    run_dir = tmp_path / "runs" / "trace6"
    run_dir.mkdir(parents=True)
    orch = BenchmarkOrchestrator(tasks_dir=Path("tasks"), runs_dir=tmp_path / "runs")

    orch._persist_optimization_diagnostic("trace6", had_build_phase=True)

    assert json.loads((run_dir / "optimization.json").read_text())["status"] == "unmeasurable"


def test_an_eval_only_trial_gets_no_optimization_file_of_its_own(tmp_path):
    """A trial deploys an artifact some earlier run built, so its own run directory holds no agent
    event log and counting over it says `unmeasurable` -- "we could not see the loop" about a run that
    had no loop to see. That file would land beside the trial's own eval/optimization.json, which
    carries the BUILD's real figures: one run, two files of the same name, opposite answers."""
    run_dir = tmp_path / "runs" / "trace8"
    run_dir.mkdir(parents=True)
    orch = BenchmarkOrchestrator(tasks_dir=Path("tasks"), runs_dir=tmp_path / "runs")

    orch._persist_optimization_diagnostic("trace8", had_build_phase=False)

    assert not (run_dir / "optimization.json").exists()


def test_persist_optimization_diagnostic_swallows_a_write_failure(tmp_path):
    """Best-effort, same posture as _persist_eval_report: there is no run directory to write into
    here, and that must not raise out of the cleanup block."""
    orch = BenchmarkOrchestrator(tasks_dir=Path("tasks"), runs_dir=tmp_path / "runs")
    orch._persist_optimization_diagnostic("trace7", had_build_phase=True)  # must not raise
