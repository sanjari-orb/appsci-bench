"""Unit tests for the eval-only pipeline CLI (appsci_bench.orchestrator.eval_artifact).

Caller B of the shared deploy capability. Docker / TaskRunner / the real evaluator are all patched
out; these assert the orchestration contract: deploy under the spec-declared name/port/network with
the task agent_env, score the host-translated endpoint with the SAME evaluate_deployed the
orchestrator uses, and always tear the artifact container down -- stopping the task-env only when we
brought it up (not under --attach).
"""
from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import appsci_bench.orchestrator.eval_artifact as ea
import appsci_bench.orchestrator.run as run_mod
from appsci_bench.orchestrator.deployer import DeploymentResult


def _fake_report(results=None):
    return SimpleNamespace(
        task_id="t",
        final_score=0.5,
        views={"per_metric": [{"objective": "m", "score": 0.5, "weight": 1.0, "positive": True}]},
        warnings=[],
        store_root="/tmp/store",
        # Per-case rows; an `error` naming an infrastructure failure invalidates the run. The default
        # is one CLEAN row rather than none: a report carrying final_score 0.5 over zero measurements
        # cannot occur, and an empty result set is now itself an invalidating condition (an absence, not
        # a zero), so an empty default would test a state the real system never reaches.
        results=results if results is not None else [
            SimpleNamespace(data_point_id="case_1", objective="m", score=0.5, error=None),
        ],
    )


def _fake_config(agent_env=None):
    entries = agent_env if agent_env is not None else [{"name": "K", "value": "V"}]
    return SimpleNamespace(
        task_id="t",
        artifact_container="appsci-artifact-t",
        artifact_port=8080,
        # Drives the workspace-container name for the env-ownership preflight ("t-workspace-1").
        workspace_name="t-workspace",
        default_workspace_container="t-workspace-1",
        # Needed to tear down an ORPHANED env we never started (compose only needs file + project).
        docker_compose="tasks/t/environment/docker-compose.yml",
        # The deployer reads the network from HERE rather than from the running env, so a task whose
        # eval phase needs no environment still deploys (see the phaseless-task test below).
        network_name="bench-network",
        agent_env=entries,
        # No per-copy token var: this stub task runs one environment, so there is nothing to vary and
        # `agent_env` resolution never has to read a token off a container.
        lane_token_var=None,
        resolved_agent_env=lambda **_: {e["name"]: e["value"] for e in entries},
    )


_TASK_ENV = {"network_name": "bench-network", "workspace_container": "c", "env_admin_token_var": None}


@contextlib.contextmanager
def _patched(deploy_result, *, env_state=None, translate="http://localhost:55000/respond",
             env_running=False, report_results=None):
    """Patch every external seam the eval subcommand touches; yield the mocks + captured eval args.

    `env_running` drives the env-ownership preflight: True simulates a task-env already up for this
    task (another run owns it), which `eval` without --attach must refuse rather than adopt-and-destroy.
    """
    runner = MagicMock()
    runner.start.return_value = dict(_TASK_ENV)
    # Mirror TaskRunner's real project derivation so the preflight computes the true container name.
    runner._derive_project_name.side_effect = lambda name: name.rsplit("-", 1)[0]
    runner._container_manager.exists.return_value = env_running
    deployer = MagicMock()
    deployer.deploy.return_value = deploy_result
    captured: dict = {}

    def fake_evaluate(spec, endpoint, task_env, *, limit=None, workdir=None, **kwargs):
        captured["endpoint"] = endpoint
        captured["task_env"] = task_env
        captured["limit"] = limit
        captured["workdir"] = workdir
        # `**kwargs` rather than one more named parameter: this double stands in for a real signature
        # that grows, and every growth breaking nine tests on a TypeError teaches nothing. What the
        # double must still do is RECORD what it was passed, so a test can assert on it -- see
        # `captured["kwargs"]` and the sample-seed test below.
        captured["kwargs"] = kwargs
        return _fake_report(report_results)

    # The deploy contract now lives in ONE place -- `run.deploy_artifact_dir`, shared by this CLI, the
    # orchestrator's eval-only cell path and (via the same deployer) the agent's /deploy route -- so
    # these three are patched on `run`, where the construction happens, rather than on `ea`, which only
    # calls the helper. The assertions below are unchanged: it is the same constructor with the same
    # keyword names.
    with patch.object(ea, "load_task_spec", return_value=SimpleNamespace()), \
         patch.object(ea, "TaskConfig") as TC, \
         patch.object(ea, "TaskRunner", return_value=runner) as TR, \
         patch.object(run_mod, "DockerArtifactDeployer", return_value=deployer) as DC, \
         patch.object(run_mod, "llm_credential_env",
                      return_value={"APPSCI_AGENT_API_KEY": "proxy-key"}), \
         patch.object(run_mod, "_translate_docker_url_to_host", return_value=translate), \
         patch.object(ea, "evaluate_deployed", side_effect=fake_evaluate) as EV, \
         patch.object(ea, "_cleanup_artifact_containers") as CL, \
         patch.object(ea, "_STATE_DIR", Path(tempfile.mkdtemp())), \
         patch.object(ea, "_read_env_state", return_value=env_state):
        TC.from_spec.return_value = _fake_config()
        yield SimpleNamespace(runner=runner, deployer=deployer, TR=TR, DC=DC, EV=EV, CL=CL,
                              captured=captured)


_OK = DeploymentResult(success=True, endpoint_url="http://appsci-artifact-t:8080/respond",
                       container_name="appsci-artifact-t")


def test_eval_happy_path_deploys_scores_and_tears_down(tmp_path):
    with _patched(_OK) as m:
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path), "--limit", "3"])
    assert rc == 0
    # deployed under the spec-declared name/port/network, injecting the task agent_env
    kw = m.DC.call_args.kwargs
    assert kw["artifact_dir"] == tmp_path.resolve()
    assert kw["container_name"] == "appsci-artifact-t"
    assert kw["port"] == 8080
    assert kw["network"] == "bench-network"
    # env merges the LLM/proxy creds (so the artifact -- itself an agent -- can reach its model
    # provider) with the task agent_env; task agent_env wins on any clash.
    assert kw["env"] == {"APPSCI_AGENT_API_KEY": "proxy-key", "K": "V"}
    # scored the HOST-translated endpoint (not the container-DNS URL), with the task_env + limit
    assert m.captured["endpoint"] == "http://localhost:55000/respond"
    assert m.captured["task_env"] == _TASK_ENV
    assert m.captured["limit"] == 3
    # eval writes straight into the durable dir next to the artifact (runs/<trace>/eval/ when
    # --artifact-dir is a run's artifact/) -- no /tmp round-trip
    assert m.captured["workdir"] == str(tmp_path.resolve().parent / "eval")
    # teardown: artifact container removed by its known name; task-env stopped (we started it)
    m.CL.assert_called_once_with("appsci-artifact-t", None, None)
    m.runner.stop.assert_called_once()


def test_eval_attach_missing_state_errors_without_starting_env(tmp_path):
    with _patched(_OK, env_state=None) as m:
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path), "--attach"])
    assert rc == 1
    m.runner.start.assert_not_called()      # --attach must not silently spin up a new env
    m.DC.assert_not_called()                 # and nothing was deployed
    m.EV.assert_not_called()


def test_eval_attach_uses_state_and_leaves_env_running(tmp_path):
    with _patched(_OK, env_state=dict(_TASK_ENV)) as m:
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path), "--attach"])
    assert rc == 0
    m.runner.start.assert_not_called()       # reused the running env
    m.runner.stop.assert_not_called()        # ... and left it up for the next rerun
    m.CL.assert_called_once_with("appsci-artifact-t", None, None)  # artifact still torn down
    assert m.captured["task_env"] == _TASK_ENV


def test_eval_deploy_failure_returns_1_skips_eval_but_still_cleans_up(tmp_path):
    bad = DeploymentResult(success=False, error="docker build failed: bad instruction")
    with _patched(bad) as m:
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path)])
    assert rc == 1
    m.EV.assert_not_called()                 # never scored a broken deploy
    m.CL.assert_called_once_with("appsci-artifact-t", None, None)  # cleanup still runs
    m.runner.stop.assert_called_once()       # env we started is stopped


def test_eval_missing_artifact_dir_returns_1(tmp_path):
    missing = tmp_path / "nope"
    with _patched(_OK) as m:
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(missing)])
    assert rc == 1
    m.runner.start.assert_not_called()
    m.DC.assert_not_called()


# --- env ownership: refuse only for an env a LIVE run owns; recover orphans ----------------------
#
# `runner.start()` is `docker compose up -d --build`, which SILENTLY ATTACHES to an already-running
# project. Left unchecked, `eval` marked that env as its own and destroyed it in `finally`, pulling
# containers out from under a concurrent run: the victim's artifact died mid-request, then every later
# case failed its per-sample reset with "No such container" and was scored 0.0.
#
# But "a container is running" alone must NOT block: a leftover from a crashed run is an orphan nobody
# owns, and refusing on it would break the documented self-contained command.


def test_eval_recovers_orphaned_env_and_proceeds(tmp_path):
    """Env up, but no live lock and no `up` state file => orphan from a crashed run. Remove + restart."""
    with _patched(_OK, env_running=True, env_state=None) as m:
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path)])
    assert rc == 0
    # tore the orphan down (by compose file + project, which is all `stop` needs) THEN started fresh.
    # The descriptor comes from the runner itself, since teardown needs nothing a start() produced.
    m.runner.teardown_descriptor.assert_called_once_with("t")
    assert m.runner.stop.call_args_list[0].args[0] is m.runner.teardown_descriptor.return_value
    # And the fresh env is the EVAL phase's: this pipeline exists to score, so it needs the records
    # the eval cases are drawn from, not the build phase's share (docs/task_env_split.md).
    m.runner.start.assert_called_once_with("t", phase="eval")
    m.EV.assert_called_once()                # and went on to actually score


def test_eval_refuses_when_env_was_deliberately_brought_up(tmp_path):
    """Env up AND an `up` state file => the operator asked to keep it; don't destroy it."""
    with _patched(_OK, env_running=True, env_state=dict(_TASK_ENV)) as m:
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path)])
    assert rc == 2
    m.runner.start.assert_not_called()
    m.runner.stop.assert_not_called()        # left the operator's env alone
    m.DC.assert_not_called()
    m.EV.assert_not_called()
    m.CL.assert_not_called()                 # and destroyed nothing


def test_eval_refuses_while_another_live_run_holds_the_task(tmp_path):
    with _patched(_OK) as m:
        ea._acquire_lock("t")                # simulate a live run holding the task (this pid)
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path)])
        ea._release_lock("t")
    assert rc == 2
    m.runner.start.assert_not_called()
    m.DC.assert_not_called()
    m.CL.assert_not_called()


def test_eval_clears_stale_lock_from_crashed_run(tmp_path):
    """A lock left by a dead process must never permanently block the next run."""
    with _patched(_OK) as m:
        ea._STATE_DIR.mkdir(parents=True, exist_ok=True)
        ea._lock_path("t").write_text("999999")   # pid that does not exist
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path)])
    assert rc == 0                                # recovered, ran normally
    m.EV.assert_called_once()


def test_lock_is_released_after_a_run(tmp_path):
    with _patched(_OK) as m:
        assert ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path)]) == 0
        assert not ea._lock_path("t").exists()    # released in finally
        assert ea._live_lock_holder("t") is None
    assert m.EV.call_count == 1


def test_eval_does_not_remove_artifact_container_it_never_deployed(tmp_path):
    """The artifact container name is a global per-task constant, so the finally must not remove it
    unless this run actually invoked the deployer."""
    with _patched(_OK, env_running=True, env_state=dict(_TASK_ENV)) as m:
        ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path)])
    m.CL.assert_not_called()







# --- infrastructure failure must not be presented as a score ------------------------------------


def test_eval_flags_infra_failures_and_returns_1(tmp_path):
    """Cases that never reached the artifact are graded 0.0 by the evaluator, which makes a broken
    harness look like a bad artifact. The CLI must refuse to call that a score."""
    infra_rows = [
        SimpleNamespace(error="RuntimeError('env admin exec failed (w-1): No such container: w-1')"),
        SimpleNamespace(error="ArtifactQueryError('... Connection refused')"),
        SimpleNamespace(error=None),
    ]
    with _patched(_OK, report_results=infra_rows) as m:
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path)])
    assert rc == 1                           # non-zero: scripted callers can't mistake it for a result
    m.EV.assert_called_once()                # it did score (report stays on disk for diagnosis)
    m.runner.stop.assert_called_once()       # teardown still happens


def test_eval_clean_run_still_returns_0(tmp_path):
    """A run whose only failures are genuine artifact misses (no infra markers) is a valid score."""
    rows = [SimpleNamespace(error=None), SimpleNamespace(error="AssertionError('wrong answer')")]
    with _patched(_OK, report_results=rows) as m:
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path)])
    assert rc == 0
    m.EV.assert_called_once()


def test_infra_failures_counts_by_marker_and_ignores_clean_rows():
    report = _fake_report([
        SimpleNamespace(error="No such container: w-1"),
        SimpleNamespace(error="No such container: w-1"),
        SimpleNamespace(error="litellm.AuthenticationError: invalid or expired token"),
        SimpleNamespace(error=None),
        SimpleNamespace(error="ValueError('bad answer')"),   # a real artifact failure, not infra
    ])
    counts = ea._infra_failures(report)
    assert counts["No such container"] == 2
    # each row is counted once, under its first matching marker
    assert sum(counts.values()) == 3


# --- up / down state lifecycle ------------------------------------------------------------------

@contextlib.contextmanager
def _patched_env_cli(tmp_path):
    runner = MagicMock()
    runner.start.return_value = dict(_TASK_ENV)
    with patch.object(ea, "TaskRunner", return_value=runner), \
         patch.object(ea, "_STATE_DIR", tmp_path / "state"):
        yield runner


def test_up_persists_state_then_down_stops_and_clears(tmp_path):
    with _patched_env_cli(tmp_path) as runner:
        assert ea.main(["up", "--phase", "eval", "--task", "t"]) == 0
        runner.start.assert_called_once_with("t", phase="eval")
        assert ea._state_path("t").exists()          # env dict persisted for --attach / down

        assert ea.main(["down", "--task", "t"]) == 0
        runner.stop.assert_called_once_with(_TASK_ENV)
        assert not ea._state_path("t").exists()      # state cleared after teardown


# --- which PHASE `up` brings up ------------------------------------------------------------------
#
# `up` serves two workflows with OPPOSITE needs and no safe default. A human baseline attempt authors
# against the build environment (holding only the records no eval case touches, which is all an agent
# under test ever sees); an operator re-scoring an artifact needs the eval one. Getting it wrong is
# silent both ways: build-to-score measures the artifact on a store its cases are missing from, and
# eval-to-author hands a human the scored data. See docs/task_env_split.md.

def test_up_requires_an_explicit_phase(tmp_path):
    """No default, because either default is silently wrong for one of the two callers."""
    with _patched_env_cli(tmp_path) as runner:
        with pytest.raises(SystemExit) as exc:
            ea.main(["up", "--task", "t"])
        assert exc.value.code == 2
        runner.start.assert_not_called()


@pytest.mark.parametrize("phase", ["build", "eval"])
def test_up_starts_the_phase_it_was_asked_for(tmp_path, phase):
    with _patched_env_cli(tmp_path) as runner:
        assert ea.main(["up", "--phase", phase, "--task", "t"]) == 0
        runner.start.assert_called_once_with("t", phase=phase)


def test_up_on_a_phaseless_task_reports_rather_than_crashing(tmp_path):
    """A task declaring `eval: none` has nothing to bring up; that is a clean exit, not a failure."""
    with _patched_env_cli(tmp_path) as runner:
        runner.start.return_value = None
        assert ea.main(["up", "--phase", "eval", "--task", "t"]) == 0
        assert not ea._state_path("t").exists()      # nothing running, so nothing recorded


def test_scoring_refuses_to_attach_to_the_build_environment(tmp_path):
    """The half of the mistake `up --phase` cannot prevent: attaching to a build env and scoring it.

    It has to REFUSE rather than warn. Every case would be measured against a store its records were
    deliberately carved out of, which yields a plausible, entirely wrong number instead of an error --
    the exact failure shape this whole feature exists to remove.
    """
    build_env = {**_TASK_ENV, "phase": "build"}
    with _patched(_OK, env_state=build_env) as m:
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path), "--attach"])
    assert rc == 2
    m.EV.assert_not_called()                          # nothing was scored


def test_scoring_attaches_to_the_eval_environment(tmp_path):
    build_free = {**_TASK_ENV, "phase": "eval"}
    with _patched(_OK, env_state=build_free) as m:
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path), "--attach"])
    assert rc == 0
    m.EV.assert_called_once()


def test_down_without_state_is_noop(tmp_path):
    with _patched_env_cli(tmp_path) as runner:
        assert ea.main(["down", "--task", "t"]) == 0
        runner.stop.assert_not_called()


def test_scoring_a_task_with_no_eval_environment_still_deploys(tmp_path):
    """`eval: none` must not break the rerun path -- it is what score_final.py drives.

    The command works out that the task has no eval environment and sets it aside, then used to read
    the network name out of that same nothing two lines later: a TypeError after the run lock was
    already taken, killing the whole path for slides_generator. It comes off the spec instead, which
    is the authoritative source anyway since both phases share one network.
    """
    with _patched(_OK) as m:
        m.runner.start.return_value = None            # the task declares eval: none
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path)])

    assert rc == 0
    m.EV.assert_called_once()                          # it scored rather than crashing
    assert m.captured["task_env"] is None              # and scored without an environment
    assert m.DC.call_args.kwargs["network"] == "bench-network"


def test_sample_seed_reaches_the_evaluator(tmp_path):
    """`--sample-seed` decides WHICH samples a `--limit` keeps, and it has to travel the whole way.

    Taking the first N is not a neutral way to take fewer: on order_to_cash the first 20 of 60 cases
    over-represent one fault type fourfold and contain no OFF_CONTRACT_ITEM case at all, so a first-N cap
    cannot see an improvement in the categories it never exercises. A flag that parsed but was dropped
    before reaching the evaluator would silently restore that bias.
    """
    with _patched(_OK) as m:
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path),
                      "--limit", "20", "--sample-seed", "4242"])

    assert rc == 0
    assert m.captured["limit"] == 20
    assert m.captured["kwargs"].get("sample_seed") == 4242


def test_no_sample_seed_means_the_deterministic_first_n(tmp_path):
    """Omitting it keeps the first-N cap, which is what a smoke test wants."""
    with _patched(_OK) as m:
        rc = ea.main(["eval", "--task", "t", "--artifact-dir", str(tmp_path), "--limit", "5"])

    assert rc == 0
    assert m.captured["limit"] == 5
    assert m.captured["kwargs"].get("sample_seed") is None
