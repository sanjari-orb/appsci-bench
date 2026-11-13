"""What the coding sub-agent can see: the uid drop, the env scrub, and command fidelity.

The sub-agent (mini-swe) runs in-process inside the agent container and used to run as root with the
agent's whole environment, while the harness's own code sat readable at /app. So the subject under
test could read the meta-agent's LLM client, its gateway wiring, and the user-sim client -- none of
which is the task it was asked to implement, and all of which it was observed exploring.

Two things fix that and both are guarded here: commands run as an unprivileged user (so /app, mode
0700, is unreadable) with the harness's own variables stripped, and the model-access facts the
sub-agent actually needed are stated in its prompt instead of being discoverable.

The fidelity tests matter as much as the isolation ones: the wrapper re-quotes every command the model
writes, so a heredoc, a nested quote, or an exit code that stopped surviving that trip would break the
sub-agent in a way no isolation assertion would catch.
"""

import os
import subprocess

import pytest
from minisweagent.exceptions import Submitted

import appsci_agent.tools.mini_swe_agent as msa
from appsci_agent.core.config import AgentConfig
from appsci_agent.core.model_access import model_access_brief, present_credential_env_vars
from appsci_agent.tools.mini_swe_agent import (
    SUB_AGENT_HOME,
    SUB_AGENT_USER,
    InstrumentedEnvironment,
    SubAgentEnvironment,
    build_sandboxed_command,
    can_drop_privileges,
)


@pytest.fixture
def dropping(monkeypatch):
    """Pretend we are root in the agent image, where the drop actually happens."""
    monkeypatch.setattr(msa, "can_drop_privileges", lambda: True)


class TestPrivilegeDrop:
    def test_command_runs_as_the_unprivileged_user(self, dropping):
        wrapped = build_sandboxed_command("cat /app/appsci_agent/core/llm_client.py")
        assert f"setpriv --reuid={SUB_AGENT_USER} --regid={SUB_AGENT_USER} --init-groups" in wrapped

    def test_home_follows_the_uid(self, dropping):
        """Left at root's HOME, `pip install` has nowhere writable and fails outright."""
        assert f"HOME={SUB_AGENT_HOME}" in build_sandboxed_command("pip install requests")

    def test_no_drop_outside_the_container(self):
        """A developer laptop and CI are not root: commands must still run, unchanged."""
        wrapped = build_sandboxed_command("echo hi")
        assert "setpriv" not in wrapped
        assert "HOME=" not in wrapped

    def test_non_root_cannot_drop(self):
        # The test process is not root, which is the whole precondition.
        assert os.geteuid() != 0
        assert can_drop_privileges() is False

    def test_missing_user_disables_the_drop(self, monkeypatch):
        """No `swe` in /etc/passwd => setpriv would fail every command. Run unwrapped instead."""
        monkeypatch.setattr(msa.os, "geteuid", lambda: 0)
        monkeypatch.setattr(msa.shutil, "which", lambda _name: "/usr/bin/setpriv")

        def _no_such_user(_name):
            raise KeyError(_name)

        monkeypatch.setattr(msa.pwd, "getpwnam", _no_such_user)
        assert can_drop_privileges() is False

    def test_missing_setpriv_disables_the_drop(self, monkeypatch):
        monkeypatch.setattr(msa.os, "geteuid", lambda: 0)
        monkeypatch.setattr(msa.shutil, "which", lambda _name: None)
        assert can_drop_privileges() is False


class TestEnvScrub:
    def test_harness_variables_are_stripped(self):
        wrapped = build_sandboxed_command("env")
        for name in ("APPSCI_USERSIM_URL", "APPSCI_USERSIM_TOKEN", "ORCHESTRATOR_SUBMIT_URL",
                     "OBSERVABILITY_RUNS_DIR", "OBSERVABILITY_TRACE_ID"):
            assert f"-u {name}" in wrapped

    def test_model_credentials_are_kept(self):
        """The sub-agent has to smoke-test the artifact's model client, so it keeps the credentials."""
        wrapped = build_sandboxed_command("env")
        for name in ("APPSCI_AGENT_API_KEY", "APPSCI_AGENT_BASE_URL", "ANTHROPIC_API_KEY",
                     "APPSCI_AGENT_CUSTOM_HEADERS"):
            assert name not in wrapped

    def test_scrub_applies_without_the_drop(self):
        """Isolation and scrubbing are independent: outside the container the scrub still happens."""
        assert "-u APPSCI_USERSIM_TOKEN" in build_sandboxed_command("env")

    def test_pythonpath_is_dropped(self):
        """PYTHONPATH names /app, which the sub-agent cannot read. Left set, `pip install` and
        anything else that walks sys.path dies with EACCES instead of working."""
        assert "-u PYTHONPATH" in build_sandboxed_command("pip install requests")

    def test_scrubbed_variable_is_gone_from_the_child(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APPSCI_USERSIM_TOKEN", "sim-token")
        monkeypatch.setenv("APPSCI_AGENT_API_KEY", "gateway-token")
        env = SubAgentEnvironment(cwd=str(tmp_path), env={})
        out = env.execute({"command": "echo usersim=${APPSCI_USERSIM_TOKEN:-MISSING} "
                                      "gateway=${APPSCI_AGENT_API_KEY:-MISSING}"})
        assert "usersim=MISSING" in out["output"]
        assert "gateway=gateway-token" in out["output"]


class TestCommandFidelity:
    """The wrapper re-quotes the model's command; nothing about running it may change."""

    def _both(self, tmp_path, command):
        """Run `command` through the sandboxed env and through a plain shell, for comparison.

        stderr is merged into stdout on both sides because that is what mini-swe's environment does,
        so a command whose output goes to stderr (or that fails to parse) still compares like for
        like.
        """
        env = SubAgentEnvironment(cwd=str(tmp_path), env={})
        sandboxed = env.execute({"command": command})
        plain = subprocess.run(command, shell=True, cwd=str(tmp_path), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
        return sandboxed, plain

    @pytest.mark.parametrize("command", [
        "echo hello",
        "echo 'single quoted'",
        'echo "double quoted"',
        "echo it's got an apostrophe",
        "printf '%s\\n' one two",
        "echo $((2 + 3))",
        "cat <<'EOF'\nline one\nline 'two'\nEOF",
        "echo a && echo b | tr a-z A-Z",
    ])
    def test_output_matches_an_unwrapped_shell(self, tmp_path, command):
        sandboxed, plain = self._both(tmp_path, command)
        assert sandboxed["output"] == plain.stdout
        assert sandboxed["returncode"] == plain.returncode

    def test_heredoc_writes_the_file_it_would_have_written(self, tmp_path):
        env = SubAgentEnvironment(cwd=str(tmp_path), env={})
        env.execute({"command": "cat <<'EOF' > server.py\nprint('hi $NOT_EXPANDED')\nEOF"})
        assert (tmp_path / "server.py").read_text() == "print('hi $NOT_EXPANDED')\n"

    def test_failure_exit_code_survives(self, tmp_path):
        env = SubAgentEnvironment(cwd=str(tmp_path), env={})
        assert env.execute({"command": "exit 42"})["returncode"] == 42

    def test_submission_sentinel_still_ends_the_task(self, tmp_path):
        """Upstream detects submission by reading stdout, which is why the wrapper wraps rather
        than reimplements `execute`. If that broke, the sub-agent could never finish."""
        env = SubAgentEnvironment(cwd=str(tmp_path), env={})
        with pytest.raises(Submitted):
            env.execute({"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && echo done"})


class TestBothEnvironmentsAreSandboxed:
    def test_instrumented_environment_inherits_the_sandbox(self):
        """Observability on or off, the sub-agent gets the same boundary."""
        assert issubclass(InstrumentedEnvironment, SubAgentEnvironment)

    def test_the_trace_records_the_model_written_command(self, monkeypatch, tmp_path):
        """The event has to show what the model wrote; a `setpriv ... sh -c '...'` line in every
        bash_command payload would make the timeline unreadable."""
        emitted: list[dict] = []

        class _Publisher:
            def emit(self, **kwargs):
                emitted.append(kwargs)
                return "span-1"

        monkeypatch.setattr(msa, "get_publisher", lambda: _Publisher())
        env = InstrumentedEnvironment(cwd=str(tmp_path), env={})
        env.execute({"command": "echo hi"})
        commands = [e["payload"]["command"] for e in emitted if "command" in e["payload"]]
        assert commands == ["echo hi"]


class TestModelAccessBrief:
    """Stating model access is the other half of the fix: with /app unreadable, an agent that was
    never told how its artifact reaches a model has nowhere left to look."""

    ENV = {
        "APPSCI_AGENT_API_KEY": "sk-do-not-print",
        "APPSCI_AGENT_BASE_URL": "https://gateway.internal/v1",
        "APPSCI_AGENT_CUSTOM_HEADERS": "X-Gateway-User: someone@example.com",
    }

    def test_names_the_model(self):
        brief = model_access_brief(AgentConfig(model="claude-haiku-4-5"), self.ENV)
        assert "claude-haiku-4-5" in brief

    def test_names_variables_not_values(self):
        brief = model_access_brief(AgentConfig(), self.ENV)
        assert "APPSCI_AGENT_API_KEY" in brief
        for value in self.ENV.values():
            assert value not in brief

    def test_says_to_read_them_at_runtime(self):
        brief = model_access_brief(AgentConfig(), self.ENV)
        assert "os.environ" in brief
        assert "hardcode" in brief.lower()

    def test_blank_variable_is_not_advertised(self):
        """An exported-but-empty key is absent to every client library; listing it sends the
        artifact author after a credential that is not there."""
        assert present_credential_env_vars({"OPENAI_API_KEY": "   "}) == []

    def test_empty_when_there_is_nothing_to_say(self):
        """No credentials at all: a section describing no way to reach a model would only invite
        the agent to go looking for one."""
        assert model_access_brief(AgentConfig(), {}) == ""

    def test_reaches_the_sub_agent_task(self, monkeypatch):
        """The brief has to land in mini-swe's own prompt, not only the meta-agent's."""
        from appsci_agent.tools.base import ToolContext

        seen: dict = {}

        class _FakeAgent:
            def __init__(self, **kwargs):
                pass

            def run(self, task):
                seen["task"] = task
                return {"exit_status": "Submitted", "submission": "done"}

        monkeypatch.setattr(msa, "get_publisher", lambda: None)
        monkeypatch.setattr(msa, "LitellmModel", lambda **kwargs: object())
        monkeypatch.setattr(msa, "DefaultAgent", _FakeAgent)
        monkeypatch.setattr(msa, "SubAgentEnvironment",
                            lambda **kwargs: type("_Env", (), {"command_errors": []})())
        for name, value in self.ENV.items():
            monkeypatch.setenv(name, value)

        result = msa.MiniSweAgentTool().execute({"task": "write server.py"},
                                                ToolContext(config=AgentConfig()))
        assert result.success, result.error
        assert "# Model Access" in seen["task"]
        assert "APPSCI_AGENT_API_KEY" in seen["task"]
        assert self.ENV["APPSCI_AGENT_API_KEY"] not in seen["task"]


class TestTheSharedSubAgentConfigNamesNoTaskSpecifics:
    """One config serves every task, so anything concrete in it is wrong for most of them.

    It shipped `postgres://sandbox:sandbox@db:5432/oltp` as the "Database access" example from the
    commit that introduced it, when order_to_cash was effectively the only task. That is order_to_cash's
    own DSN, verbatim from its spec and compose file. Two of the eleven real tasks run Postgres at all
    (the repo's own guards enumerate by excluding underscore-prefixed directories) and one has that
    string, so nine of eleven sub-agents were handed a connection string for a database their task does
    not have -- and airline's agent, whose store is reachable only over an HTTP tool API, went looking
    for a DSN twice.
    """

    def _instance_template(self) -> str:
        import yaml

        with open(msa.APPSCI_CONFIG_PATH) as f:
            return yaml.safe_load(f)["agent"]["instance_template"]

    def test_it_names_no_connection_string(self):
        template = self._instance_template()
        for token in ("postgres://", "postgresql://", "mysql://", "mongodb://", "redis://"):
            assert token not in template, (
                f"the shared sub-agent config names a {token} endpoint; it is read by every task, so "
                "a concrete one is false for all the tasks that do not have it")

    def test_it_names_no_task_owned_identifier(self):
        """Not just DSNs: any database name, table or host from one task's fixtures."""
        template = self._instance_template()
        for token in ("oltp", "sandbox:sandbox", "@db:", "airline_db", "banking_db"):
            assert token not in template, (
                f"the shared sub-agent config names {token!r}, which belongs to one task's environment")

    def test_it_still_tells_the_sub_agent_where_to_look_instead(self):
        """Removing the false specifics must not leave the sub-agent with nothing.

        What has to survive is the substance, not any particular wording: that a task may expose a
        database or only a tool API, and that the endpoints come from the task rather than from here.
        """
        template = self._instance_template().lower()
        assert "tool api" in template and "database" in template, (
            "the config no longer says a task may expose a tool API instead of a database, which is "
            "the fact that stops a sub-agent assuming a DSN")
        assert "the task above" in template, (
            "the config no longer points the sub-agent at the task for its actual endpoints")
        assert "connect to it directly" in template, (
            "the config no longer tells the sub-agent to explore a database that does exist -- which "
            "is the half of this guidance that is true for the Postgres tasks")


class TestTheWorkspaceCannotBeRenamedOutFromUnderTheTool:
    """A root-owned state directory is not enough on its own, and this PR claimed it was.

    Renaming an entry needs write permission on the PARENT, not on the entry. `/workspace` used to be
    chowned to the sub-agent in the image and is baked in rather than bind-mounted, so a root-owned
    `.appsci` inside it was still one `mv` away -- the same hole the tree was meant to close.

    What closes it is the /tmp model: `/workspace` root-owned, world-writable, sticky. Root ownership
    is load-bearing, because the sticky bit exempts a directory's own owner -- a sub-agent-owned
    `/workspace` with the sticky bit set would still let the sub-agent remove root's entries. Asserted
    against the Dockerfile because that is where the property lives; the container is not built here.
    """

    def _dockerfile(self) -> str:
        import pathlib

        return (pathlib.Path(msa.__file__).resolve().parents[1] / "Dockerfile").read_text()

    def test_the_workspace_is_root_owned_and_sticky(self):
        text = self._dockerfile()
        assert "chown root:root /workspace" in text, (
            "/workspace is not root-owned, so the sticky bit below would exempt its owner and the "
            "sub-agent could still rename the tool's state directory")
        assert "chmod 1777 /workspace" in text, (
            "/workspace is not sticky, so the sub-agent can rename or delete entries it does not own")

    def test_the_workspace_is_not_chowned_to_the_sub_agent(self):
        """The line this replaced was `chown -R swe:swe /work /workspace`."""
        text = self._dockerfile()
        assert "chown -R swe:swe /work /workspace" not in text
        assert f"chown -R {SUB_AGENT_USER}:{SUB_AGENT_USER} /work\n" in text, (
            "/work must stay sub-agent-owned -- it is the WORKDIR and holds the artifact output dir")


class _FakePwEntry:
    """Stands in for a pwd entry, so these run identically on a machine with no `swe` user."""

    def __init__(self, uid, gid):
        self.pw_uid = uid
        self.pw_gid = gid


class TestDirectoriesTheSubAgentMustWriteTo:
    """The uid drop cuts both ways: a directory the AGENT makes is not one the sub-agent can use.

    The agent process is root and the sub-agent's commands are not, so a directory created at run time
    lands root-owned and, under the default umask, read-only to everyone else. The image's build-time
    chown of /workspace cannot cover it, because it does not exist yet. That is a silent failure mode:
    a brief that requires the sub-agent to write a file there asks for something the filesystem
    forbids, and the caller reads the resulting absence as work not done rather than work not
    permitted -- which is how an optimization cycle came back with no score at all.
    """

    def test_ownership_is_transferred_when_privileges_can_be_dropped(self, monkeypatch, tmp_path):
        monkeypatch.setattr(msa, "can_drop_privileges", lambda: True)
        monkeypatch.setattr(msa.pwd, "getpwnam", lambda _name: _FakePwEntry(4242, 4343))
        chowned = {}
        monkeypatch.setattr(msa.os, "chown",
                            lambda path, uid, gid: chowned.update(path=path, uid=uid, gid=gid))

        target = tmp_path / "state"
        msa.makedirs_writable_by_sub_agent(str(target))

        assert target.is_dir()
        assert chowned == {"path": str(target), "uid": 4242, "gid": 4343}

    def test_directory_still_exists_when_the_chown_fails(self, monkeypatch, tmp_path):
        """Never raise: a failed chown leaves the caller's own error handling to cover the write."""
        monkeypatch.setattr(msa, "can_drop_privileges", lambda: True)
        monkeypatch.setattr(msa.pwd, "getpwnam", lambda _name: _FakePwEntry(4242, 4343))

        def _denied(*_args):
            raise PermissionError("not permitted")

        monkeypatch.setattr(msa.os, "chown", _denied)

        target = tmp_path / "state"
        msa.makedirs_writable_by_sub_agent(str(target))

        assert target.is_dir()

    def test_no_chown_off_the_image(self, monkeypatch, tmp_path):
        """Off the image there is no second user, so there is nothing to transfer to."""
        monkeypatch.setattr(msa, "can_drop_privileges", lambda: False)

        def _unexpected(*_args):
            raise AssertionError("chown attempted with no privilege drop in effect")

        monkeypatch.setattr(msa.os, "chown", _unexpected)

        target = tmp_path / "state"
        msa.makedirs_writable_by_sub_agent(str(target))

        assert target.is_dir()

    def test_existing_directory_is_accepted(self, monkeypatch, tmp_path):
        """Called once per cycle, so a second call must not fail on the directory already existing."""
        monkeypatch.setattr(msa, "can_drop_privileges", lambda: False)
        target = tmp_path / "state"
        target.mkdir()

        msa.makedirs_writable_by_sub_agent(str(target))

        assert target.is_dir()
