"""The user-sim docs mount, and the separation it creates (issue #76, phase C).

`tasks/<id>/user_sim_docs/` is mounted into the user-sim container and NOWHERE ELSE. That asymmetry IS
the feature: the agent under test cannot read this material, so its only route to it is asking the sim.
These tests assert both halves -- the sim gets it read-only, and the agent's own mount set and snapshot
set never mention it.
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

from appsci_bench.core.agent_container import _AGENT_SNAPSHOT_DIRS
from appsci_bench.core.appsci_agent_usersim import (
    _USERSIM_SNAPSHOT_DIRS,
    DockerAppsciAgentUserSimService,
)

DOCS_MOUNT = DockerAppsciAgentUserSimService.DOCS_MOUNT


def _service(**kw) -> DockerAppsciAgentUserSimService:
    defaults = dict(task_id="t1", spec_path="/a/spec.yaml", persona_path="/a/persona.yaml")
    return DockerAppsciAgentUserSimService(**{**defaults, **kw})


def _docker_run_argv(monkeypatch, svc) -> list[str]:
    """Start the service with subprocess + health-wait stubbed; return the `docker run` argv."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(DockerAppsciAgentUserSimService, "_wait_for_health", lambda self: None)
    svc.start()
    # calls[0] is the stale-container `docker rm -f`; calls[1] is the run.
    return calls[1]


class TestDocsMount:
    def test_mounted_read_only_at_the_docs_mount_point(self, monkeypatch, tmp_path) -> None:
        docs = tmp_path / "user_sim_docs"
        docs.mkdir()

        argv = _docker_run_argv(monkeypatch, _service(docs_dir=str(docs)))

        assert f"{docs}:{DOCS_MOUNT}:ro" in argv

    def test_mount_is_read_only_not_writable(self, monkeypatch, tmp_path) -> None:
        """Explicit: the sim must not be able to modify the task's ground-truth-adjacent documents."""
        docs = tmp_path / "user_sim_docs"
        docs.mkdir()

        argv = _docker_run_argv(monkeypatch, _service(docs_dir=str(docs)))

        mount = next(a for a in argv if DOCS_MOUNT in a and a.count(":") == 2)
        assert mount.endswith(":ro")

    def test_relative_docs_dir_is_made_absolute(self, monkeypatch, tmp_path) -> None:
        """Docker requires an absolute host path; a relative one would be read as a named volume."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "user_sim_docs").mkdir()

        argv = _docker_run_argv(monkeypatch, _service(docs_dir="user_sim_docs"))

        assert f"{tmp_path / 'user_sim_docs'}:{DOCS_MOUNT}:ro" in argv

    def test_no_mount_when_no_docs_dir(self, monkeypatch) -> None:
        """Tasks that have not adopted known_docs must start unchanged.

        Checks for an actual bind-mount spec, not for the string anywhere: the env var
        APPSCI_USERSIM_DOCS_DIR legitimately names the mount point even when nothing is mounted there.
        """
        argv = _docker_run_argv(monkeypatch, _service())

        mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
        assert not any(m.endswith(f"{DOCS_MOUNT}:ro") for m in mounts), mounts

    def test_docs_env_var_always_passed(self, monkeypatch) -> None:
        """The service reads the mount point from env; it must be set even with no mount, so the
        default and the actual mount point can never drift apart."""
        argv = _docker_run_argv(monkeypatch, _service())

        assert f"APPSCI_USERSIM_DOCS_DIR={DOCS_MOUNT}" in argv

    def test_spec_and_persona_mounts_still_present(self, monkeypatch, tmp_path) -> None:
        docs = tmp_path / "user_sim_docs"
        docs.mkdir()

        argv = _docker_run_argv(monkeypatch, _service(docs_dir=str(docs)))

        assert "/a/spec.yaml:/spec/spec.yaml:ro" in argv
        assert "/a/persona.yaml:/spec/persona.yaml:ro" in argv


class TestSeparationFromTheAgent:
    def test_usersim_snapshot_includes_docs(self) -> None:
        """Without this the tracer cannot show what the sim was given -- exactly what you need when an
        elicitation run looks wrong."""
        assert "/spec" in _USERSIM_SNAPSHOT_DIRS
        assert DOCS_MOUNT in _USERSIM_SNAPSHOT_DIRS

    def test_agent_snapshot_excludes_docs(self) -> None:
        assert DOCS_MOUNT not in _AGENT_SNAPSHOT_DIRS

    def test_the_two_snapshot_sets_live_with_their_owners(self) -> None:
        """Each container's snapshot set is defined in that container's own module (review on PR #80).

        The usersim set was previously declared in agent_container.py, where it was unused and had to be
        imported back out via a function-level import. Splitting them means the asymmetry -- the whole
        point of the mechanism -- is no longer visible on two adjacent lines, so it is asserted here
        instead: exactly one of the two sets may contain the docs mount.
        """
        import appsci_bench.core.agent_container as agent_mod
        import appsci_bench.core.appsci_agent_usersim as usersim_mod

        assert not hasattr(agent_mod, "_USERSIM_SNAPSHOT_DIRS")
        assert hasattr(usersim_mod, "_USERSIM_SNAPSHOT_DIRS")
        owners = [DOCS_MOUNT in _AGENT_SNAPSHOT_DIRS, DOCS_MOUNT in _USERSIM_SNAPSHOT_DIRS]
        assert owners == [False, True]

    def test_agent_container_never_mounts_the_docs_dir(self, monkeypatch, tmp_path) -> None:
        """THE separation assertion. If the agent container ever gains this path, the whole mechanism
        is void: the agent reads the policy instead of eliciting it."""
        from appsci_bench.core.agent_container import (
            AppSciAgentContainerConfig,
            AppSciAgentContainerManager,
        )

        docs = tmp_path / "user_sim_docs"
        docs.mkdir()
        (docs / "policy.md").write_text("SECRET RULES")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

        calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        monkeypatch.setattr(subprocess, "run", fake_run)

        mgr = AppSciAgentContainerManager(AppSciAgentContainerConfig(
            task_id="t1",
            workspace_container="ws-1",
            appsci_usersim_url="http://usersim:8000",
            appsci_usersim_token="tok",
            orchestrator_submit_url="http://host:9000/submit",
            spec_path=str(tmp_path / "spec.yaml"),
            data_dir=None,
            sop_dir=None,
        ))
        mgr.start()

        run_argv = [c for c in calls if c[:3] == ["docker", "run", "-d"]][0]
        joined = " ".join(run_argv)
        assert DOCS_MOUNT not in joined
        assert str(docs) not in joined
        assert "user_sim_docs" not in joined


class TestOrchestratorSuppliesTheDir:
    """The orchestrator resolves tasks/<id>/user_sim_docs and hands it to the sim service (C3)."""

    def test_returns_path_when_the_dir_exists(self, tmp_path) -> None:
        from appsci_bench.orchestrator.run import usersim_docs_dir

        (tmp_path / "demo" / "user_sim_docs").mkdir(parents=True)

        assert usersim_docs_dir(tmp_path, "demo") == str(tmp_path / "demo" / "user_sim_docs")

    def test_returns_none_when_absent(self, tmp_path) -> None:
        from appsci_bench.orchestrator.run import usersim_docs_dir

        (tmp_path / "demo").mkdir()

        assert usersim_docs_dir(tmp_path, "demo") is None

    def test_returns_none_when_a_file_shadows_the_name(self, tmp_path) -> None:
        """A file named user_sim_docs is not a docs tree; mounting it would fail confusingly later."""
        from appsci_bench.orchestrator.run import usersim_docs_dir

        (tmp_path / "demo").mkdir()
        (tmp_path / "demo" / "user_sim_docs").write_text("oops")

        assert usersim_docs_dir(tmp_path, "demo") is None

    def test_every_real_task_resolves_without_error(self) -> None:
        """None today (no task has adopted known_docs yet); must not raise for any of them."""
        from appsci_bench.core.spec import TASKS_DIR
        from appsci_bench.orchestrator.run import usersim_docs_dir

        for task_dir in sorted(p for p in TASKS_DIR.iterdir() if p.is_dir()):
            result = usersim_docs_dir(TASKS_DIR, task_dir.name)
            assert result is None or pathlib.Path(result).is_dir()

    def test_docs_tree_is_outside_every_agent_mounted_tree(self) -> None:
        """The structural guarantee: user_sim_docs/ is a SIBLING of environment/, whose data/ and sop/
        subdirectories are whole-dir bind-mounted to the agent. If it ever moved inside environment/,
        the agent would read what it is supposed to elicit."""
        from appsci_bench.core.spec import TASKS_DIR
        from appsci_bench.orchestrator.run import usersim_docs_dir

        for task_dir in sorted(p for p in TASKS_DIR.iterdir() if p.is_dir()):
            docs = usersim_docs_dir(TASKS_DIR, task_dir.name)
            if docs is None:
                continue
            for agent_tree in ("environment/data", "environment/sop"):
                mount = (task_dir / agent_tree).resolve()
                assert not pathlib.Path(docs).resolve().is_relative_to(mount), (
                    f"{task_dir.name}: user_sim_docs resolves inside the agent-mounted {agent_tree}"
                )


class TestTheAuthorGuideNamesTheContainers:
    """`tasks/README.md` must say WHICH container each directory is mounted into (review on PR #80).

    A task author's whole decision -- "does the agent get to read this, or must it ask?" -- is a choice
    between two containers, so a table that names the paths but not the containers leaves the one thing
    that matters implicit. Pinned against the real constants so a rename breaks this test rather than
    silently making the guide wrong.

    Also pins the correction that came out of the same review: the guide used to say `environment/data`
    and `environment/sop` were readable by "agent **and** sim". They are not. Only /spec and
    /user_sim_docs are mounted into the sim's container (see _USERSIM_SNAPSHOT_DIRS); a persona listing
    those resources in `known_resources` gets metadata, never contents.
    """

    def _readme(self) -> str:
        return (pathlib.Path(__file__).resolve().parents[3] / "tasks" / "README.md").read_text()

    def test_the_docs_mount_point_is_named(self) -> None:
        assert DOCS_MOUNT in self._readme()

    @pytest.mark.parametrize("source_dir", ["environment/data/", "environment/sop/", "user_sim_docs/"])
    def test_every_source_directory_is_listed(self, source_dir: str) -> None:
        assert source_dir in self._readme()

    @pytest.mark.parametrize("container", ["**agent** container", "**user-sim** container"])
    def test_both_containers_are_named(self, container: str) -> None:
        assert container in self._readme()

    def test_it_says_the_sim_cannot_read_the_agent_mounts(self) -> None:
        """The claim this replaced was actively misleading, and it is the same confusion the reviewer
        flagged on `known_resources` ("speak to" reading as "can open")."""
        readme = self._readme()

        assert "cannot read `/data` or `/sop`" in readme
        assert "agent **and** sim" not in readme
