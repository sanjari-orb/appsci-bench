"""The orchestrator's half of the NO-USER arm: refusing a task with nothing to hand over, and
naming a run for the arm it actually was.

Both are about a result that would otherwise look fine. A no-user run on a task with no
`user_sim_docs/` is a HARDER task, not an ablation of elicitation, and it would come back with the
same shape as any other run. A no-user run named `..._l3_...` invites a reader to line three of them
up as a persona comparison when they were the same condition three times.
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest

from appsci_bench.orchestrator.run import NO_USER_RUN_SLUG, BenchmarkOrchestrator, compose_run_name

SPEC = """
task_id: demo
name: Demo
description: a demo task
business_goals: ["ship it"]
environment:
  resources: []
artifacts:
  output_dir: /work/out
task_runner:
  network_name: bench-network
  workspace_name: demo-workspace
evaluation_data:
  dataset: tasks/demo/eval_data
  objectives: []
user_personas:
  - tasks/demo/user_personas/l1.yaml
"""

PERSONA = "persona_id: l1\ntask_id: demo\nlevel: L1\nrole: Manager\n"


def _task(tmp_path: pathlib.Path, *, with_docs: bool, with_policies: bool = False,
          policies_extra: dict[str, str] | None = None):
    tasks_dir = tmp_path / "tasks"
    task = tasks_dir / "demo"
    (task / "user_personas").mkdir(parents=True)
    (task / "spec.yaml").write_text(SPEC)
    (task / "user_personas" / "l1.yaml").write_text(PERSONA)
    if with_docs:
        (task / "user_sim_docs" / "common").mkdir(parents=True)
        (task / "user_sim_docs" / "common" / "policy.md").write_text("THE RULES\n")
    if with_policies or policies_extra:
        extracts = task / "user_personas" / "domain_knowledge"
        extracts.mkdir(parents=True)
        (extracts / "l1.md").write_text("# L1\n\nFlag anything over $500.\n")
        for name, body in (policies_extra or {}).items():
            (extracts / name).write_text(body)
    return tasks_dir, task


def _forbid_all_startup(monkeypatch) -> list[str]:
    """Make every startup path explode, so "nothing started" is proven rather than assumed."""
    started: list[str] = []

    def boom(name):
        def _f(*a, **kw):
            started.append(name)
            raise AssertionError(f"{name} must not start")
        return _f

    import appsci_bench.orchestrator.run as run_mod

    monkeypatch.setattr(run_mod, "ArtifactSubmissionServer", boom("submission_server"))
    monkeypatch.setattr(run_mod, "DockerAppsciAgentUserSimService", boom("usersim"))
    monkeypatch.setattr(run_mod, "AppSciAgentContainerManager", boom("agent_container"))
    monkeypatch.setattr(run_mod, "write_agent_spec", boom("write_agent_spec"))
    return started


def _run_no_persona(tasks_dir, tmp_path, **kwargs):
    """Drive the orchestrator the way the CLI now can: no persona at all."""
    orch = BenchmarkOrchestrator(tasks_dir=tasks_dir, runs_dir=tmp_path / "runs")
    return asyncio.run(orch.run(task_id="demo", **kwargs))


def _run(tasks_dir, task, tmp_path, **kwargs):
    orch = BenchmarkOrchestrator(tasks_dir=tasks_dir, runs_dir=tmp_path / "runs")
    return asyncio.run(orch.run(
        task_id="demo", persona_path=task / "user_personas" / "l1.yaml", **kwargs
    ))


# --- a task with nothing to hand over -----------------------------------------------------------

def test_a_task_without_documents_fails_before_anything_starts(tmp_path, monkeypatch):
    tasks_dir, task = _task(tmp_path, with_docs=False)
    started = _forbid_all_startup(monkeypatch)

    result = _run(tasks_dir, task, tmp_path, user_sim_enabled=False)

    assert started == []
    assert result.status == "failed"
    assert "user_sim_docs" in result.error
    assert "domain_knowledge" in result.error
    assert result.metadata["user_sim_enabled"] is False


def test_policy_extracts_alone_are_enough_to_run(tmp_path, monkeypatch):
    """The refusal is about having NOTHING to hand over. A task whose stakeholder knowledge lives
    entirely in the personas' inline domain_knowledge has no user_sim_docs/ tree and is still a real
    ablation once that knowledge is extracted."""
    tasks_dir, task = _task(tmp_path, with_docs=False, with_policies=True)
    _forbid_all_startup(monkeypatch)

    # Proof it got PAST the guard: execution reaches `load_task_spec`, which resolves against the
    # repo-global tasks/ tree rather than this fixture's. Same marker the sibling tests use.
    with pytest.raises(FileNotFoundError, match="no spec for 'demo'"):
        _run(tasks_dir, task, tmp_path, user_sim_enabled=False)


def test_a_persona_yaml_in_the_extracts_tree_is_refused(tmp_path, monkeypatch):
    """That tree is mounted whole into the agent container. A persona YAML copied in for reference
    would hand over the adversarial plants and the withholding design, and the run would come back
    looking like a good score rather than a leak."""
    tasks_dir, task = _task(tmp_path, with_docs=True,
                            policies_extra={"l3.yaml": "persona_id: l3\n"})
    started = _forbid_all_startup(monkeypatch)

    result = _run(tasks_dir, task, tmp_path, user_sim_enabled=False)

    assert started == []
    assert result.status == "failed"
    assert "l3.yaml" in result.error
    assert result.metadata["user_sim_enabled"] is False


def test_the_control_arm_ignores_the_extracts_tree(tmp_path, monkeypatch):
    """Even a malformed extracts tree is irrelevant to a run that has a stakeholder: nothing is
    mounted, so nothing can leak, and refusing the run would block the control arm over a file it
    never reads."""
    tasks_dir, task = _task(tmp_path, with_docs=True,
                            policies_extra={"l3.yaml": "persona_id: l3\n"})
    _forbid_all_startup(monkeypatch)

    with pytest.raises(FileNotFoundError, match="no spec for 'demo'"):
        _run(tasks_dir, task, tmp_path, user_sim_enabled=True)


def test_that_failure_is_per_task_not_an_environment_blocker(tmp_path, monkeypatch):
    """An ordinary failed BenchmarkResult (main exits 1). Exit 3 aborts a whole batch, and one task
    lacking sim-only documents says nothing about the others."""
    tasks_dir, task = _task(tmp_path, with_docs=False)
    _forbid_all_startup(monkeypatch)

    result = _run(tasks_dir, task, tmp_path, user_sim_enabled=False)

    assert result.status == "failed"
    assert result.task_id == "demo"
    assert result.scores is None


def test_the_same_task_is_fine_on_the_control_arm(tmp_path, monkeypatch):
    """A task with no sim-only documents is perfectly valid -- it just has nothing to elicit. The
    refusal must be specific to the arm that needs those documents.

    Proof that it got PAST the check: execution reaches `load_task_spec`, which resolves against the
    repo-global tasks/ tree rather than this fixture's, and so raises for the synthetic task id. Same
    marker the sibling docs-fail-fast tests use.
    """
    tasks_dir, task = _task(tmp_path, with_docs=False)
    _forbid_all_startup(monkeypatch)

    with pytest.raises(FileNotFoundError, match="no spec for 'demo'"):
        _run(tasks_dir, task, tmp_path, user_sim_enabled=True)


def test_an_eval_only_trial_is_not_subject_to_the_check(tmp_path, monkeypatch):
    """A trial runs no agent, so it needs nothing handed over. Refusing one would make an ablation
    sweep's trials fail on a task whose build cell handled it fine. Same past-the-check marker."""
    tasks_dir, task = _task(tmp_path, with_docs=False)
    _forbid_all_startup(monkeypatch)
    artifact = tmp_path / "artifact"
    artifact.mkdir()

    with pytest.raises(FileNotFoundError, match="no spec for 'demo'"):
        _run(tasks_dir, task, tmp_path, user_sim_enabled=False, artifact_dir=artifact)


# --- the trial label -----------------------------------------------------------------------------

def test_a_trial_of_a_no_user_build_reports_no_stakeholder_not_unmeasurable(tmp_path, monkeypatch):
    """A no-user build recorded no conversation, so its run dir reads as `unreadable` and the trial
    would otherwise report `unmeasurable` -- "we could not find the conversation", which describes a
    plumbing fault rather than the arm. This is the only place a `builds`/`trials` sweep computes
    elicitation at all, so getting it wrong here gets the whole arm's column wrong."""
    import appsci_bench.orchestrator.run as run_mod
    from appsci_bench.eval.build_transcript import STATUS_NO_STAKEHOLDER

    captured = {}

    def fake_evaluate_deployed(*a, **kw):
        captured["build_transcript"] = kw.get("build_transcript")
        raise RuntimeError("stop here -- the transcript argument is what this test is about")

    monkeypatch.setattr(run_mod, "evaluate_deployed", fake_evaluate_deployed)
    monkeypatch.setattr(run_mod, "deploy_artifact_dir",
                        lambda *a, **kw: ("http://artifact:8080", "c", None))

    orch = BenchmarkOrchestrator(tasks_dir=tmp_path / "tasks", runs_dir=tmp_path / "runs")
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(orch._run_eval_only(
            task_id="demo", spec=object(), task_config=object(),
            artifact_dir=tmp_path, task_env=None, trace_id=None, eval_limit=None,
            env_reused=False, build_run_dir=tmp_path / "build", user_sim_enabled=False,
        ))

    assert captured["build_transcript"].status == STATUS_NO_STAKEHOLDER


def test_a_control_trial_still_reads_the_build_run_dir(tmp_path, monkeypatch):
    import appsci_bench.orchestrator.run as run_mod

    captured = {}

    def fake_evaluate_deployed(*a, **kw):
        captured["build_transcript"] = kw.get("build_transcript")
        raise RuntimeError("stop here")

    monkeypatch.setattr(run_mod, "evaluate_deployed", fake_evaluate_deployed)
    monkeypatch.setattr(run_mod, "deploy_artifact_dir",
                        lambda *a, **kw: ("http://artifact:8080", "c", None))

    orch = BenchmarkOrchestrator(tasks_dir=tmp_path / "tasks", runs_dir=tmp_path / "runs")
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(orch._run_eval_only(
            task_id="demo", spec=object(), task_config=object(),
            artifact_dir=tmp_path, task_env=None, trace_id=None, eval_limit=None,
            env_reused=False, build_run_dir=tmp_path / "build",
        ))

    assert captured["build_transcript"] == tmp_path / "build"


# --- the build -> eval handover -----------------------------------------------------------------
#
# Every test above either refuses before anything starts or drives the eval-only path, so none of
# them reaches the handover. That is where the arm actually broke: `usersim.stop()` was called
# unguarded, `usersim` is only ever assigned under `if user_sim_enabled`, and the AttributeError was
# swallowed by the outer handler into `status="failed"` with no scores -- after the whole build had
# been paid for. The identical call in the cleanup `finally` was already guarded, so the suite stayed
# green.


class _FakeAgentContainer:
    container_name = "appsci-agent-demo"

    def __init__(self, *a, **kw):
        self.halted = False

    def start(self):
        return None

    def halt(self):
        self.halted = True

    def stop(self):
        return None

    def get_logs(self):
        return "agent logs"

    def snapshot_files(self, dest):
        return None

    def snapshot_artifact(self, *a, **kw):
        return None


class _FakeSubmissionServer:
    def __init__(self, *a, **kw):
        self.url = "http://submit:9000"

    async def start(self):
        return None

    async def stop(self):
        return None

    def configure_deploy(self, *a, **kw):
        return None

    def redeploy_before_eval(self, *a, **kw):
        return None


class _FakeUserSim:
    url = "http://usersim:8300"
    token = "t0ken"

    def __init__(self, *a, **kw):
        self.stopped = False

    def start(self):
        return None

    def stop(self):
        self.stopped = True

    def snapshot_files(self, dest):
        return None


class _FakeSpec:
    artifacts = {"output_dir": "/work/out"}


#: The fixture spec plus the two keys `TaskConfig.from_spec` insists on. Kept separate from `SPEC`
#: so the refusal tests above, which never construct a TaskConfig, stay as minimal as they were.
BUILDABLE_SPEC = SPEC.replace(
    "environment:\n  resources: []",
    "environment:\n  resources: []\n  docker_compose: docker-compose.yml",
).replace(
    "  workspace_name: demo-workspace",
    "  workspace_name: demo-workspace\n"
    "  artifact_container: appsci-artifact-demo\n"
    "  artifact_port: 8080\n"
    # Both phases have to be declared, and `none` is the way a task says "this phase needs no
    # environment" -- which is what this fixture wants, since `task_runner.start` is faked anyway.
    "  environments:\n    build: none\n    eval: none",
)


def _drive_build(tmp_path, monkeypatch, *, user_sim_enabled: bool, wait_result: dict,
                 usersim_cls=None):
    """Run a build with every service faked to succeed, and `_wait_for_agent_completion` scripted.

    `skip_eval=True` so a completed build returns immediately AFTER the handover -- which makes the
    returned status a direct read on whether the handover survived, with nothing else in between to
    muddy it.

    The spec loaders are faked because they resolve against the repo root rather than `tasks_dir`;
    the tests above never reach that line, which is part of why this whole region was untested.
    """
    import appsci_bench.orchestrator.run as run_mod

    tasks_dir, task = _task(tmp_path, with_docs=True)
    spec_file = task / "spec.yaml"
    spec_file.write_text(BUILDABLE_SPEC)

    monkeypatch.setattr(run_mod, "ArtifactSubmissionServer", _FakeSubmissionServer)
    monkeypatch.setattr(run_mod, "AppSciAgentContainerManager", _FakeAgentContainer)
    monkeypatch.setattr(run_mod, "DockerAppsciAgentUserSimService", usersim_cls or _FakeUserSim)
    # `TaskConfig` is NOT faked: it reads `tasks_dir/<task>/spec.yaml`, which the fixture wrote, so
    # the real one works and the container names / ports under test are the real ones.
    monkeypatch.setattr(run_mod, "load_task_spec", lambda task_id: _FakeSpec())
    monkeypatch.setattr(run_mod, "resolve_spec_path", lambda task_id: spec_file)
    monkeypatch.setattr(run_mod, "write_agent_spec",
                        lambda src, dest: (dest.write_text("task_id: demo\n"), dest)[1])

    orch = BenchmarkOrchestrator(tasks_dir=tasks_dir, runs_dir=tmp_path / "runs")
    monkeypatch.setattr(orch.task_runner, "start", lambda task_id, phase=None: {
        "workspace_container": "demo-workspace",
        "network_name": "bench-network",
    })
    monkeypatch.setattr(orch.task_runner, "stop", lambda env: None)
    monkeypatch.setattr(orch.task_runner, "teardown_descriptor", lambda task_id: {"task": task_id})

    async def _scripted(**kw):
        return dict(wait_result)

    monkeypatch.setattr(orch, "_wait_for_agent_completion", _scripted)

    return asyncio.run(orch.run(
        task_id="demo",
        persona_path=task / "user_personas" / "l1.yaml",
        user_sim_enabled=user_sim_enabled,
        skip_eval=True,
    ))


COMPLETED = {"status": "completed", "artifact_endpoint": "http://appsci-artifact-demo:8080",
             "logs": "agent logs", "exit_code": 0}


def test_the_no_user_arm_survives_the_handover(tmp_path, monkeypatch):
    """The blocking one: this returned `failed` with `'NoneType' object has no attribute 'stop'`,
    after the whole build had already been paid for."""
    result = _drive_build(tmp_path, monkeypatch, user_sim_enabled=False, wait_result=COMPLETED)

    assert result.status == "completed", result.error
    assert result.error is None


def test_the_control_arm_still_stops_its_user_sim_at_the_handover(tmp_path, monkeypatch):
    """The guard must not become "never stop it": a user-sim left running past the handover is a
    reachable stakeholder on the bench network while the eval environment comes up."""
    stopped: list[bool] = []

    class _RecordingUserSim(_FakeUserSim):
        def stop(self):
            stopped.append(True)

    result = _drive_build(tmp_path, monkeypatch, user_sim_enabled=True, wait_result=COMPLETED,
                          usersim_cls=_RecordingUserSim)

    assert result.status == "completed", result.error
    assert stopped == [True], "the control arm's stakeholder must still be stopped, exactly once"


# --- which arm a FAILED run belonged to -----------------------------------------------------------

def test_a_crashed_run_records_which_arm_it_was(tmp_path, monkeypatch):
    """The scored and build-only paths recorded the arm flags; the crash, timeout and no-submission
    paths recorded nothing. An arm whose every run dies on startup showed up as an empty row rather
    than a failing one -- which is how the container bug above stayed invisible."""
    result = _drive_build(
        tmp_path, monkeypatch, user_sim_enabled=False,
        wait_result={"status": "crashed", "error": "container exited 1", "logs": "boom",
                     "exit_code": 1},
    )

    assert result.status == "failed"
    assert result.metadata["user_sim_enabled"] is False
    assert result.metadata["optimization_enabled"] is True


def test_a_timed_out_run_records_which_arm_it_was(tmp_path, monkeypatch):
    result = _drive_build(
        tmp_path, monkeypatch, user_sim_enabled=False,
        wait_result={"status": "timeout", "logs": "slow", "exit_code": None},
    )

    assert result.status == "timeout"
    assert result.metadata["user_sim_enabled"] is False


def test_a_run_that_submitted_nothing_records_which_arm_it_was(tmp_path, monkeypatch):
    result = _drive_build(
        tmp_path, monkeypatch, user_sim_enabled=False,
        wait_result={"status": "no_submission", "logs": "quiet", "exit_code": 0},
    )

    assert result.status == "no_submission"
    assert result.metadata["user_sim_enabled"] is False


# --- naming -------------------------------------------------------------------------------------

def test_the_arm_replaces_the_persona_in_the_run_name():
    """`nouser`, not `l3`. The whole document tree is mounted, so the bound persona decides nothing
    and a name claiming a level would read as a persona-level result it is not."""
    name = compose_run_name(
        "airline_rebooking", agent_model="claude-opus-5",
        persona_path=NO_USER_RUN_SLUG, label="b0",
    )
    assert "_nouser_" in name


def test_the_control_arm_still_names_its_persona():
    name = compose_run_name(
        "airline_rebooking", agent_model="claude-opus-5",
        persona_path="tasks/airline_rebooking/user_personas/l3.yaml", label="b0",
    )
    assert "_l3_" in name
    assert "nouser" not in name


# --- the arm takes no persona --------------------------------------------------------------------

def test_the_arm_runs_with_no_persona_at_all(tmp_path, monkeypatch):
    """The point of the flag is that there is no stakeholder, so there is nothing for a persona to
    describe. Requiring one made the caller name a file the run then ignored, which reads as though
    the level mattered (reported in review against a `slides_generator` e2e).

    Proof it got PAST the argument handling: execution reaches `load_task_spec`, which resolves
    against the repo-global tasks/ tree rather than this fixture's. Same marker the sibling tests use.
    """
    tasks_dir, _ = _task(tmp_path, with_docs=True)
    _forbid_all_startup(monkeypatch)

    with pytest.raises(FileNotFoundError, match="no spec for 'demo'"):
        _run_no_persona(tasks_dir, tmp_path, user_sim_enabled=False)


def test_a_control_run_without_a_persona_is_refused_with_a_sentence(tmp_path, monkeypatch):
    """The other half: with a stakeholder the persona IS the thing being role-played, so its absence
    has to fail loudly rather than start a user-sim with nothing to be."""
    tasks_dir, _ = _task(tmp_path, with_docs=True)
    _forbid_all_startup(monkeypatch)

    with pytest.raises(ValueError, match="persona is required to run with a stakeholder"):
        _run_no_persona(tasks_dir, tmp_path, user_sim_enabled=True)
