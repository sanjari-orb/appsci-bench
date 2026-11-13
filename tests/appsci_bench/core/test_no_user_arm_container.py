"""The NO-USER arm's half of the agent container: what `docker run` is given, and which
combinations `start()` refuses before it builds a command at all.

The arm swaps one thing for another -- the `talk_to_user` route to a stakeholder for a read-only
mount of the documents that stakeholder would have held. Every test here is about that swap being
ALL-OR-NOTHING, because both half-states are silent corruptions of a result rather than crashes: a
control run that also gets the documents scores elicitation it never had to do, and an ablation with
neither reads as a harder task. Neither leaves a mark in the run record, so the guards are what keep
them out of a paper.

subprocess is stubbed throughout; no real docker involved.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from appsci_bench.core.agent_container import (
    REQUIREMENTS_DOCS_MOUNT,
    REQUIREMENTS_MOUNT,
    REQUIREMENTS_POLICIES_MOUNT,
    AppSciAgentContainerConfig,
    AppSciAgentContainerManager,
)


class _R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _config(**overrides):
    defaults = dict(
        task_id="airline_rebooking",
        workspace_container="airline-workspace-1",
        appsci_usersim_url="http://usersim-airline_rebooking:8000",
        appsci_usersim_token="tok",
        orchestrator_submit_url="http://host.docker.internal:9000/submit",
        spec_path="/tmp/spec.yaml",
    )
    defaults.update(overrides)
    return AppSciAgentContainerConfig(**defaults)


#: Host path of a task's per-level policy extracts, as the orchestrator resolves it.
_POLICIES_HOST = "/repo/tasks/airline_rebooking/user_personas/domain_knowledge"


def _no_user_config(**overrides):
    defaults = dict(
        appsci_usersim_url=None,
        appsci_usersim_token=None,
        user_sim_enabled=False,
        requirements_dir="/repo/tasks/airline_rebooking/user_sim_docs",
    )
    defaults.update(overrides)
    return _config(**defaults)


def _run_argv(config, monkeypatch):
    """The `docker run` argv `start()` builds for `config`."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _R()

    with patch("appsci_bench.core.agent_container.subprocess.run", side_effect=fake_run):
        AppSciAgentContainerManager(config).start()
    return calls[1]  # calls[0] is the `docker rm -f` of any stale container


def _env_value(argv, name):
    """The value of `-e NAME=...` in a docker argv, or None if the variable is not passed."""
    for i, arg in enumerate(argv):
        if arg == "-e" and argv[i + 1].startswith(f"{name}="):
            return argv[i + 1].split("=", 1)[1]
    return None


# --- the mount ------------------------------------------------------------------------------

def test_no_user_run_mounts_the_documents_read_only(monkeypatch):
    argv = _run_argv(_no_user_config(), monkeypatch)
    expected = f"/repo/tasks/airline_rebooking/user_sim_docs:{REQUIREMENTS_DOCS_MOUNT}:ro"
    assert expected in argv, f"requirement documents not mounted: {argv}"
    assert argv[argv.index(expected) - 1] == "-v"


def test_no_user_run_mounts_the_policy_extracts_read_only(monkeypatch):
    """The other half of what the stakeholder knew: the rules a persona holds inline in its YAML,
    extracted per level. A task can have documents and no extracts (the state order_to_cash is in
    today), so this mount is independent of the one above rather than implied by it."""
    argv = _run_argv(_no_user_config(policies_dir=_POLICIES_HOST), monkeypatch)
    expected = f"{_POLICIES_HOST}:{REQUIREMENTS_POLICIES_MOUNT}:ro"
    assert expected in argv, f"policy extracts not mounted: {argv}"
    assert argv[argv.index(expected) - 1] == "-v"


def test_the_two_halves_are_siblings_not_nested(monkeypatch):
    """Both bind targets sit UNDER /requirements and neither IS /requirements.

    Binding one half at the parent and the other inside it would make Docker create the nested mount
    point in the host source directory -- writing `policies/` into the repo's own user_sim_docs tree
    on every run. The parent stays a directory the image owns.
    """
    argv = _run_argv(_no_user_config(policies_dir=_POLICIES_HOST), monkeypatch)
    targets = [arg.split(":")[1] for i, arg in enumerate(argv)
               if argv[i - 1] == "-v" and arg.split(":")[1].startswith(REQUIREMENTS_MOUNT)]
    assert sorted(targets) == sorted([REQUIREMENTS_DOCS_MOUNT, REQUIREMENTS_POLICIES_MOUNT])
    assert REQUIREMENTS_MOUNT not in targets


def test_policy_extracts_alone_are_a_valid_arm(monkeypatch):
    """A task whose stakeholder knowledge is ALL inline domain_knowledge has no user_sim_docs/ tree
    and is still a runnable ablation: there is material to hand over, so the swap is real."""
    argv = _run_argv(
        _no_user_config(requirements_dir=None, policies_dir=_POLICIES_HOST), monkeypatch
    )
    assert any(REQUIREMENTS_POLICIES_MOUNT in arg for arg in argv)
    assert not any(REQUIREMENTS_DOCS_MOUNT in arg for arg in argv)
    assert _env_value(argv, "APPSCI_REQUIREMENTS_DOCS_DIR") is None


def test_each_half_is_advertised_only_when_it_was_mounted(monkeypatch):
    """The prompt names a directory per half. An agent told to read `/requirements/policies` on a
    task that has no extracts spends turns on an empty `ls` and, worse, may conclude the rules it
    cannot find do not exist."""
    argv = _run_argv(_no_user_config(), monkeypatch)  # documents only
    assert _env_value(argv, "APPSCI_REQUIREMENTS_DOCS_DIR") == REQUIREMENTS_DOCS_MOUNT
    assert _env_value(argv, "APPSCI_REQUIREMENTS_POLICIES_DIR") is None


def test_control_run_gets_no_requirements_mount(monkeypatch):
    """The asymmetry the benchmark is built on: on an ordinary run the agent has no path to the
    simulator's documents, which is what makes them elicitable rather than readable."""
    argv = _run_argv(_config(), monkeypatch)
    assert not any(REQUIREMENTS_MOUNT in arg for arg in argv), argv


def test_mount_target_and_advertised_path_are_the_same_value(monkeypatch):
    """The prompt tells the agent its requirements are at APPSCI_REQUIREMENTS_DIR. If that were a
    second copy of the path it could name a directory that was never mounted, and the agent would
    spend its turns on an empty `ls` instead of reading the policy."""
    argv = _run_argv(_no_user_config(policies_dir=_POLICIES_HOST), monkeypatch)
    # Every advertised path is a bind target, for both halves and for the parent that contains them.
    targets = {arg.split(":")[1] for i, arg in enumerate(argv)
               if argv[i - 1] == "-v" and ":ro" in arg}
    for var, expected in (
        ("APPSCI_REQUIREMENTS_DOCS_DIR", REQUIREMENTS_DOCS_MOUNT),
        ("APPSCI_REQUIREMENTS_POLICIES_DIR", REQUIREMENTS_POLICIES_MOUNT),
    ):
        assert _env_value(argv, var) == expected
        assert expected in targets, f"{var} names a path that was never mounted"
    # The parent is not itself mounted; it is the directory the two halves hang off, and the agent is
    # pointed at it as the place to start reading.
    assert _env_value(argv, "APPSCI_REQUIREMENTS_DIR") == REQUIREMENTS_MOUNT
    assert all(t.startswith(f"{REQUIREMENTS_MOUNT}/") for t in targets if t.startswith(REQUIREMENTS_MOUNT))


# --- the environment ------------------------------------------------------------------------

def test_no_user_run_disables_the_tool_in_the_container(monkeypatch):
    argv = _run_argv(_no_user_config(), monkeypatch)
    assert _env_value(argv, "AGENT_USER_SIM_ENABLED") == "0"


def test_no_user_run_passes_no_user_sim_coordinates(monkeypatch):
    """Omitted, not blanked. An empty `APPSCI_USERSIM_URL` left in the environment is a live-looking
    handle to a service that was never started -- and if anything ever reached for it, the failure
    would read as a broken user-sim rather than as an arm that deliberately has none."""
    argv = _run_argv(_no_user_config(), monkeypatch)
    assert _env_value(argv, "APPSCI_USERSIM_URL") is None
    assert _env_value(argv, "APPSCI_USERSIM_TOKEN") is None


def test_control_run_env_keeps_its_pre_arm_ORDER(monkeypatch):
    """Not just the same SET of variables -- the same SEQUENCE.

    Docker does not care: `-e` is a set and nothing here is repeated, so any order runs the same
    container. A HUMAN cares. The claim this arm rests on is "the control is untouched", and the way
    anyone checks it is by diffing a control `docker run` against one from before the arm existed. If
    the user-sim pair drifts to a different position in the list, that diff comes back non-empty and
    every future reviewer has to re-derive that the difference is cosmetic. Pinning the order keeps
    the check a one-line diff instead of an argument.
    """
    argv = _run_argv(_config(), monkeypatch)
    env_order = [argv[i + 1].split("=", 1)[0] for i, a in enumerate(argv) if a == "-e"]
    assert env_order[:6] == [
        "APPSCI_USERSIM_URL",
        "APPSCI_USERSIM_TOKEN",
        "TASK_WORKSPACE",
        "ORCHESTRATOR_SUBMIT_URL",
        "TASK_SPEC_PATH",
        "APPSCI_AGENT_ARTIFACT_NETWORK",
    ], env_order


def test_control_run_environment_is_unchanged(monkeypatch):
    """The control arm must still be the run it was before this knob existed."""
    argv = _run_argv(_config(), monkeypatch)
    assert _env_value(argv, "APPSCI_USERSIM_URL") == "http://usersim-airline_rebooking:8000"
    assert _env_value(argv, "APPSCI_USERSIM_TOKEN") == "tok"
    assert _env_value(argv, "AGENT_USER_SIM_ENABLED") is None
    assert _env_value(argv, "APPSCI_REQUIREMENTS_DIR") is None


# --- the refused half-states ------------------------------------------------------------------

def test_documents_mounted_while_the_stakeholder_is_still_reachable_is_refused(monkeypatch):
    """A control run that ALSO gets the documents is the arm nobody wants: the agent can read what it
    was supposed to ask for, so the elicitation score measures nothing while still looking valid."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    config = _config(user_sim_enabled=True, requirements_dir="/repo/tasks/x/user_sim_docs")
    with pytest.raises(ValueError, match="neither arm"):
        AppSciAgentContainerManager(config).start()


def test_policy_extracts_mounted_while_the_stakeholder_is_still_reachable_is_refused(monkeypatch):
    """Same corruption as the documents case, by the other half. Handing a control run the extracted
    decision rules leaves the stakeholder holding facts the agent already has, and the elicitation
    score it produces is a number about nothing."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    config = _config(user_sim_enabled=True, policies_dir=_POLICIES_HOST)
    with pytest.raises(ValueError, match="neither arm"):
        AppSciAgentContainerManager(config).start()


def test_no_stakeholder_and_neither_half_is_refused(monkeypatch):
    """An agent with nothing to ask and nothing to read is a HARDER TASK, not an ablation of
    elicitation -- and it would be reported as if it were the same task. Only the state with BOTH
    halves missing is refused: either one alone is material to hand over."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    config = _no_user_config(requirements_dir=None, policies_dir=None)
    with pytest.raises(ValueError, match="harder task"):
        AppSciAgentContainerManager(config).start()


@pytest.mark.parametrize("missing", ["appsci_usersim_url", "appsci_usersim_token"])
def test_stakeholder_enabled_without_coordinates_is_refused(monkeypatch, missing):
    """The mirror of the case above: `talk_to_user` in the registry with nothing behind it. The agent
    would burn its turn budget on connection errors and the trace would look like a broken
    user-sim."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with pytest.raises(ValueError, match="user-sim URL/token are missing"):
        AppSciAgentContainerManager(_config(**{missing: None})).start()


def test_refusals_happen_before_any_docker_command_runs(monkeypatch):
    """These are configuration errors, not runtime ones. Reaching `docker rm -f` first would leave a
    half-torn-down container behind for a mistake that was knowable up front."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    with patch("appsci_bench.core.agent_container.subprocess.run",
               side_effect=AssertionError("docker must not be called")):
        with pytest.raises(ValueError):
            AppSciAgentContainerManager(
                _no_user_config(requirements_dir=None, policies_dir=None)
            ).start()


# --- the snapshot ---------------------------------------------------------------------------

def test_requirements_mount_is_in_the_snapshot_set():
    """In this arm the documents are an INPUT the agent was handed. A trace that does not show what
    it was given cannot be read against one where it had to ask for the same thing."""
    from appsci_bench.core.agent_container import _AGENT_SNAPSHOT_DIRS

    assert REQUIREMENTS_MOUNT in _AGENT_SNAPSHOT_DIRS
