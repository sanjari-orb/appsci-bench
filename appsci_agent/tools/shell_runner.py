"""Run one coding sub-agent shell command so that its timeout error always comes back.

mini-swe's own runner (``minisweagent.environments.local._run``) already enforces a per-command
timeout: it kills the command's process group and raises ``TimeoutExpired``. But between the kill and
the raise it reads the output pipe to end-of-file **with no time limit**. A process the command started
in a new session (``setsid``, a daemonizing server) is outside that group, survives the kill, and still
holds the pipe, so end-of-file never comes, the error is never raised, and nothing above the runner
hears about it. That is what ended slides_generator_claude-opus-5_b1_l3_20260918_044432: a
``setsid nohup sh -c '... > log' &`` whose redirect sat inside the quoted string, so the detached
``sh`` kept the pipe, and the build sat silent for 2h38m until the orchestrator's limit killed it.

This runner adds no limit of its own and changes nothing about which processes are killed. It differs
from upstream's only where upstream never returns:

* The timeout covers waiting for the shell as well as reading its output, so a command that closes
  its output and keeps running times out like any other instead of blocking in the final wait.
* After the same process-group kill, output is read for at most ``KILL_GRACE_S``. If the pipe is
  still held then, it is let go and the timeout is raised as ``CommandTimedOut`` with
  ``output_held_open=True``, so the sub-agent and the harness are told what happened.
* The process holding the pipe is left alone. It is outside the command's group, which is the same
  boundary upstream's kill already respects.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time

#: After the timeout kill, how long to keep reading for output still in flight. Long enough for the
#: killed processes' last writes to arrive; short because nothing the group kill reached can write more.
KILL_GRACE_S = 5.0

_READ_CHUNK = 64 * 1024

OUTPUT_HELD_OPEN_NOTE = (
    "\n[harness] The command timed out and its process group was killed, but a process it started "
    "outside that group still holds the command's output open. Output after this point was not "
    "collected, and that process may still be running."
)


class CommandTimedOut(subprocess.TimeoutExpired):
    """``TimeoutExpired``, plus whether a process outside the command's group still held its output."""

    def __init__(self, cmd, timeout, output: str, output_held_open: bool):
        super().__init__(cmd, timeout, output=output)
        self.output_held_open = output_held_open


def _drain(stream, chunks: list[bytes], deadline: float) -> bool:
    """Read ``stream`` into ``chunks`` until end-of-file (True) or ``deadline`` passes (False)."""
    with selectors.DefaultSelector() as sel:
        sel.register(stream, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if not sel.select(timeout=remaining):
                continue  # the loop head re-checks the deadline
            data = os.read(stream.fileno(), _READ_CHUNK)
            if not data:
                return True
            chunks.append(data)


def _wait(process: subprocess.Popen, deadline: float) -> bool:
    """Wait for the shell itself to exit before ``deadline``. True if it did."""
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
        return True
    except subprocess.TimeoutExpired:
        return False


def _kill_group(process: subprocess.Popen) -> None:
    """Upstream's timeout kill: SIGKILL to the command's process group."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # The group is already gone, or holds only the shell's unreaped exit (macOS refuses the group
        # kill then). Kill the shell directly so the wait below cannot outlive it.
        try:
            process.kill()
        except ProcessLookupError:
            pass


def run_bounded(command: str, cwd: str, env: dict[str, str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run ``command`` under ``sh`` and return its combined output.

    Raises ``CommandTimedOut`` (a ``subprocess.TimeoutExpired``, carrying the output gathered so far)
    on timeout, as upstream's runner does, so ``LocalEnvironment.execute``'s error formatting applies
    unchanged. Returns or raises within ``timeout`` + ``KILL_GRACE_S``.
    """
    if timeout is None or timeout <= 0:
        raise ValueError(f"timeout must be a positive number of seconds, got {timeout!r}")

    process = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    chunks: list[bytes] = []
    output_held_open = False
    try:
        deadline = time.monotonic() + timeout
        finished = _drain(process.stdout, chunks, deadline) and _wait(process, deadline)
        if not finished:
            _kill_group(process)
            grace_deadline = time.monotonic() + KILL_GRACE_S
            output_held_open = not _drain(process.stdout, chunks, grace_deadline)
            _wait(process, grace_deadline)
    finally:
        process.stdout.close()
        if process.poll() is None:
            # Reached when something raised mid-read, or SIGKILL to our own child had not landed
            # within the grace period. Either way the shell must not be left behind.
            process.kill()
            process.wait()

    # Decoded the way upstream's text-mode Popen decodes, universal newlines included, so the model
    # sees the same text it saw before for every command that did not hang.
    output = b"".join(chunks).decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    if not finished:
        if output_held_open:
            output += OUTPUT_HELD_OPEN_NOTE
        raise CommandTimedOut(command, timeout, output=output, output_held_open=output_held_open)
    return subprocess.CompletedProcess(command, process.returncode, stdout=output)
