"""The coding sub-agent's shell runner: a command's timeout error always comes back.

slides_generator_claude-opus-5_b1_l3_20260918_044432 sat idle for 2h38m until the 3-hour build limit
killed it. The sub-agent ran ``setsid nohup sh -c '... > log' &`` with the redirect inside the quoted
string, so the detached ``sh`` kept the command's output pipe. Upstream mini-swe's runner reads that
pipe to end-of-file, and after its timeout kill reads it again with no limit; the kill could not reach
a process in another session, so end-of-file never came.

The hang tests reproduce that shape with ``os.setsid()`` from Python, since macOS has no ``setsid``
command. The rest pin that nothing else changed: output, exit codes, background servers that redirect
their output and must outlive the step, and children in the command's own group that the timeout kill
must still stop.
"""
from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import time

import pytest

from appsci_agent.tools import shell_runner
from appsci_agent.tools.mini_swe_agent import SubAgentEnvironment, command_error_report
from appsci_agent.tools.shell_runner import OUTPUT_HELD_OPEN_NOTE, CommandTimedOut, run_bounded

PY = shlex.quote(sys.executable)

#: A child that moves to its own session (out of reach of the timeout's process-group kill) and keeps
#: the inherited stdout open for a minute. The command prints its pid so the test can clean it up.
ESCAPING_CHILD = f"{PY} -c 'import os, time; os.setsid(); time.sleep(60)' & echo $!"

#: The same, with the shell still running when the timeout fires, so the group kill lands. This is
#: the shape that hangs upstream's runner on macOS too; with the shell already gone, macOS refuses
#: the group kill and upstream escapes by accident, while Linux (the jobs boxes) hangs on both.
ESCAPING_CHILD_SHELL_ALIVE = ESCAPING_CHILD + "; sleep 30"


@pytest.fixture
def short_grace(monkeypatch):
    monkeypatch.setattr(shell_runner, "KILL_GRACE_S", 0.5)


@pytest.fixture
def reap():
    """Kill any pid a test hands it, on every path out, so no sleeper outlives the test."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed child of ours that nobody has reaped yet still answers signal 0; it is not running.
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        return waited == 0
    except ChildProcessError:
        return True


def _first_pid(output: str) -> int:
    return int(output.strip().splitlines()[0])


def test_output_and_exit_code(tmp_path):
    result = run_bounded("echo out; echo err >&2; exit 3", str(tmp_path), dict(os.environ), 10)
    assert result.returncode == 3
    assert result.stdout == "out\nerr\n"


def test_crlf_is_normalized_as_upstream_text_mode_did(tmp_path):
    result = run_bounded("printf 'a\\r\\nb\\rc'", str(tmp_path), dict(os.environ), 10)
    assert result.stdout == "a\nb\nc"


def test_a_slow_command_times_out_on_time(tmp_path, short_grace):
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as info:
        run_bounded("echo begun; sleep 30", str(tmp_path), dict(os.environ), 1)
    assert time.monotonic() - start < 5
    assert "begun" in info.value.output


@pytest.mark.parametrize("command", [ESCAPING_CHILD, ESCAPING_CHILD_SHELL_ALIVE],
                         ids=["shell-exited", "shell-alive"])
def test_a_child_in_another_session_holding_the_pipe_no_longer_hangs(tmp_path, short_grace, reap, command):
    """The slides b1 hang. Upstream's runner never returns from this command on Linux."""
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as info:
        run_bounded(command, str(tmp_path), dict(os.environ), 1)
    elapsed = time.monotonic() - start
    reap.append(_first_pid(info.value.output))

    assert elapsed < 1 + 0.5 + 2, f"took {elapsed:.1f}s; the wait after the kill is not bounded"
    assert isinstance(info.value, CommandTimedOut) and info.value.output_held_open is True
    assert OUTPUT_HELD_OPEN_NOTE in info.value.output, "the model must be told why its output stopped"


def test_the_escaped_child_is_left_running(tmp_path, short_grace, reap):
    """Background processes outlive a step on purpose; the runner lets go of the pipe, nothing more."""
    with pytest.raises(subprocess.TimeoutExpired) as info:
        run_bounded(ESCAPING_CHILD, str(tmp_path), dict(os.environ), 1)
    pid = _first_pid(info.value.output)
    reap.append(pid)
    assert _alive(pid)


def test_a_redirected_background_server_returns_at_once_and_survives(tmp_path, reap):
    """The shape the sub-agent is told to use, and the one it relies on across steps."""
    start = time.monotonic()
    result = run_bounded("sleep 60 > /dev/null 2>&1 < /dev/null & echo $!", str(tmp_path),
                         dict(os.environ), 10)
    pid = _first_pid(result.stdout)
    reap.append(pid)

    assert time.monotonic() - start < 3
    assert result.returncode == 0
    assert _alive(pid)


def test_a_same_group_child_holding_the_pipe_is_still_killed_at_the_timeout(tmp_path, short_grace, reap):
    """Upstream's behavior, kept: the group kill reaches it, so it does not survive the timeout."""
    with pytest.raises(subprocess.TimeoutExpired) as info:
        run_bounded("sleep 60 & echo $!", str(tmp_path), dict(os.environ), 1)
    pid = _first_pid(info.value.output)
    reap.append(pid)

    assert info.value.output_held_open is False, "the kill reached it, so the pipe closed"
    assert OUTPUT_HELD_OPEN_NOTE not in info.value.output
    time.sleep(0.2)
    assert not _alive(pid)


def test_a_shell_that_closes_its_output_and_keeps_running_is_bounded(tmp_path, short_grace):
    """End-of-file arrives early here, so the deadline has to cover the wait for the shell too."""
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded("exec >&- 2>&-; sleep 30", str(tmp_path), dict(os.environ), 1)
    assert time.monotonic() - start < 4


def test_large_output_is_returned_whole(tmp_path):
    """No cap of the runner's own: upstream had none, and the observation template already trims."""
    result = run_bounded(f"{PY} -c 'print(\"x\" * 1000000)'", str(tmp_path), dict(os.environ), 10)
    assert result.returncode == 0
    assert result.stdout == "x" * 1000000 + "\n"


def test_no_output_at_all(tmp_path):
    result = run_bounded("true", str(tmp_path), dict(os.environ), 10)
    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize("timeout", [0, -1, None])
def test_a_missing_or_nonpositive_timeout_is_rejected(tmp_path, timeout):
    """No timeout would bring the unbounded wait straight back."""
    with pytest.raises(ValueError):
        run_bounded("true", str(tmp_path), dict(os.environ), timeout)


def test_the_sub_agent_environment_reports_the_hang_as_a_failed_step(tmp_path, short_grace, reap):
    """Through the environment mini-swe actually calls: a -1 step the model can read, not a hang."""
    env = SubAgentEnvironment(cwd=str(tmp_path), env={}, timeout=1)
    start = time.monotonic()
    out = env.execute({"command": ESCAPING_CHILD})
    reap.append(_first_pid(out["output"]))

    assert time.monotonic() - start < 4
    assert out["returncode"] == -1
    assert "timed out" in out["exception_info"]
    assert OUTPUT_HELD_OPEN_NOTE in out["output"]
    assert out["extra"]["timed_out"] is True and out["extra"]["output_held_open"] is True


# --- What reaches the harness -------------------------------------------------------------------------

def test_the_environment_records_a_timed_out_command(tmp_path, short_grace, reap):
    env = SubAgentEnvironment(cwd=str(tmp_path), env={}, timeout=1)
    out = env.execute({"command": ESCAPING_CHILD})
    reap.append(_first_pid(out["output"]))

    assert env.command_errors == [{
        "command": ESCAPING_CHILD,      # the command the model wrote, not the sandbox wrapper
        "exception_type": "CommandTimedOut",
        "timed_out": True,
        "timeout_s": 1,
        "output_held_open": True,
    }]


def test_a_nonzero_exit_is_not_a_command_error(tmp_path):
    """A command that ran and failed is the command's result, which the sub-agent already sees."""
    env = SubAgentEnvironment(cwd=str(tmp_path), env={}, timeout=10)
    out = env.execute({"command": "exit 7"})
    assert out["returncode"] == 7
    assert env.command_errors == []


def test_the_report_names_each_failed_command():
    report = command_error_report([
        {"command": "sleep 99", "exception_type": "CommandTimedOut", "timed_out": True,
         "timeout_s": 30, "output_held_open": False},
        {"command": "setsid server &", "exception_type": "CommandTimedOut", "timed_out": True,
         "timeout_s": 30, "output_held_open": True},
        {"command": "ls", "exception_type": "FileNotFoundError", "timed_out": False,
         "timeout_s": None, "output_held_open": False},
    ])
    assert report.splitlines()[0] == "3 of the sub-agent's shell commands did not run to completion:"
    assert "- timed out after 30s and was killed: sleep 99" in report
    assert "still held its output open and may still be running: setsid server &" in report
    assert "- failed to run (FileNotFoundError): ls" in report


def test_the_report_is_empty_when_nothing_failed():
    assert command_error_report([]) == ""


def test_a_long_list_is_summarised():
    errors = [{"command": f"cmd{i}", "exception_type": "CommandTimedOut", "timed_out": True,
               "timeout_s": 30, "output_held_open": False} for i in range(8)]
    report = command_error_report(errors)
    assert "cmd4" in report and "cmd5" not in report
    assert report.splitlines()[-1] == "- and 3 more"


def _run_tool(monkeypatch, tmp_path, command, exit_status, reap):
    """Run the real tool and environment with a stand-in sub-agent that issues one ``command``."""
    import appsci_agent.tools.mini_swe_agent as msa
    from appsci_agent.core.config import AgentConfig
    from appsci_agent.tools.base import ToolContext

    class _FakeModel:
        def __init__(self, **kwargs):
            pass

    class _FakeAgent:
        def __init__(self, model, env, **kwargs):
            self.env = env

        def run(self, task):
            out = self.env.execute({"command": command}, timeout=1)
            if command == ESCAPING_CHILD:
                reap.append(_first_pid(out["output"]))
            return {"exit_status": exit_status, "submission": "done"}

    monkeypatch.setattr(msa, "get_publisher", lambda: None)
    monkeypatch.setattr(msa, "LitellmModel", _FakeModel)
    monkeypatch.setattr(msa, "DefaultAgent", _FakeAgent)
    return msa.MiniSweAgentTool().execute(
        {"task": "do the thing"}, ToolContext(config=AgentConfig(), workspace_dir=str(tmp_path)))


def test_a_submitted_task_still_reports_its_timed_out_command(monkeypatch, tmp_path, short_grace, reap):
    """The sub-agent can submit without mentioning a command that timed out; the main agent is told."""
    result = _run_tool(monkeypatch, tmp_path, ESCAPING_CHILD, "Submitted", reap)

    assert result.success, result.error
    assert result.output["result"] == "done"
    assert "1 of the sub-agent's shell commands did not run to completion" in result.output["command_errors"]
    assert "still held its output open" in result.output["command_errors"]


def test_a_failed_task_reports_its_timed_out_command(monkeypatch, tmp_path, short_grace, reap):
    result = _run_tool(monkeypatch, tmp_path, ESCAPING_CHILD, "LimitsExceeded", reap)

    assert not result.success
    assert "exit_status: LimitsExceeded" in result.error
    assert "timed out after 1s and was killed" in result.error


def test_a_clean_task_adds_nothing(monkeypatch, tmp_path, reap):
    result = _run_tool(monkeypatch, tmp_path, "echo fine", "Submitted", reap)
    assert result.success, result.error
    assert "command_errors" not in result.output


def test_the_trace_records_how_each_command_ended(monkeypatch, tmp_path, short_grace, reap):
    """swe.bash_command says only what ran; swe.bash_result says how it ended."""
    import appsci_agent.tools.mini_swe_agent as msa
    from appsci_agent.tools.mini_swe_agent import InstrumentedEnvironment

    emitted: list[dict] = []

    class _Publisher:
        def emit(self, **kwargs):
            emitted.append(kwargs)
            return "span-1"

    monkeypatch.setattr(msa, "get_publisher", lambda: _Publisher())
    env = InstrumentedEnvironment(cwd=str(tmp_path), env={})
    env.execute({"command": "exit 3"})
    out = env.execute({"command": ESCAPING_CHILD}, timeout=1)
    reap.append(_first_pid(out["output"]))

    results = [e["payload"] for e in emitted if str(e["event_type"]) == "swe.bash_result"]
    assert [r["step_num"] for r in results] == [1, 2]
    assert results[0]["returncode"] == 3 and results[0]["timed_out"] is False
    assert results[1]["returncode"] == -1
    assert results[1]["timed_out"] is True and results[1]["output_held_open"] is True
