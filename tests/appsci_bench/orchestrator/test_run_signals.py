"""SIGTERM has to reach `run()`'s `finally:`, because that block is the container teardown.

`BenchmarkOrchestrator.run` stops the agent, the user sim, the task environment and the artifact
container the agent deployed itself in a single `finally:`. Nothing installed a SIGTERM handler, so
the default disposition terminated the process outright and none of that ran -- every container the
run had started was left on the host. On the shared jobs box they stay reachable and keep spending on
the LLM gateway.

Driven through a **real subprocess** rather than an in-process loop for two reasons: it is the only
way to observe what an actual signal does to an actual `asyncio.run`, and if the handler regresses,
the default SIGTERM kills that subprocess rather than the test runner.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

# Mirrors the shape of BenchmarkOrchestrator.run: an `except Exception` sitting in front of the
# `finally:` that removes the containers, and `await`s inside that `finally:` (the real one awaits
# `submission_server.stop()` and `aggregator_server.stop()`). Each stage drops a marker file, so the
# test can see exactly how far the unwind got.
PROGRAM = '''
import asyncio, os, sys
from appsci_bench.orchestrator.run import _install_shutdown_handler

OUT = sys.argv[1]
CLEANUP_SECONDS = float(sys.argv[2])


def mark(name):
    open(os.path.join(OUT, name), "w").write("yes")


async def fake_run():
    try:
        mark("running")
        await asyncio.sleep(300)
    except Exception:
        mark("caught_by_except_exception")
    finally:
        mark("cleanup_started")
        await asyncio.sleep(CLEANUP_SECONDS)
        mark("containers_removed")


async def amain():
    _install_shutdown_handler()
    try:
        await fake_run()
    except asyncio.CancelledError:
        mark("cancelled_reached_main")
        sys.exit(1)


asyncio.run(amain())
'''


#: Every wait in this file is a LIVENESS bound -- "did this happen at all" -- and never a measurement
#: of how fast it happened, so all of them are deliberately generous. A real regression still fails,
#: just later: the handler either runs the teardown or it does not.
#:
#: Two separate bounds, because the two things being waited on fail for completely different reasons.
#: STARTUP is the child importing `appsci_bench.orchestrator.run`, which pulls in litellm and boto3:
#: measured 1.9s of that import alone unloaded, 7s for this whole file unloaded, and 42s for it at 1x
#: CPU oversubscription -- a 6x slowdown that has nothing to do with signals. At 3x oversubscription
#: (33 spinning processes on 11 cores) the file did not finish inside a single 120s bound at all. That
#: is a starved interpreter, and the earlier 20s bound failed here exactly that way: an empty marker
#: directory and a child that was still alive and had not reached its first statement. Startup
#: therefore gets its own large bound, so a slow machine cannot be mistaken for a broken handler.
STARTUP_WAIT_SECONDS = 300.0
#: BEHAVIOUR is everything after the signal is sent -- the unwind, the teardown, the exit. That is
#: bounded work by construction (the fake teardown sleeps a fixed 0.1s or 3s), so it does not need
#: startup's headroom.
GENEROUS_WAIT_SECONDS = 120.0


def _await_marker(
    out: Path, name: str, proc: subprocess.Popen, timeout: float = GENEROUS_WAIT_SECONDS
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (out / name).exists():
            return
        if proc.poll() is not None:
            break
        time.sleep(0.02)
    raise AssertionError(
        f"marker {name!r} never appeared (child rc={proc.returncode}); "
        f"got {sorted(p.name for p in out.iterdir())}"
    )


def _markers(out: Path) -> set[str]:
    return {p.name for p in out.iterdir()}


@pytest.fixture
def child(tmp_path):
    """Spawn the orchestrator-shaped program, once it is genuinely inside its `try:`."""
    out = tmp_path / "markers"
    out.mkdir()
    started: list[subprocess.Popen] = []

    def _start(cleanup_seconds: float = 0.1) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", PROGRAM, str(out), str(cleanup_seconds)],
            cwd=str(REPO_ROOT),
        )
        started.append(proc)
        # The startup bound, not the behavioural one: this wait covers a heavy import, and confusing
        # a starved interpreter with a broken handler is what the two constants exist to prevent.
        _await_marker(out, "running", proc, timeout=STARTUP_WAIT_SECONDS)
        return proc

    _start.out = out  # type: ignore[attr-defined]
    try:
        yield _start
    finally:
        for proc in started:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)


def test_sigterm_runs_the_whole_container_teardown(child):
    proc = child()
    out = child.out

    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=GENEROUS_WAIT_SECONDS) == 1, "killed by the signal instead of unwinding"

    got = _markers(out)
    # The teardown ran, and ran to the end -- including past the `await` inside it, which is where
    # the real one stops the submission server and the tracer.
    assert "cleanup_started" in got
    assert "containers_removed" in got
    # And the cancellation was not swallowed by the `except Exception` that guards the run: if it
    # were, the process would report an ordinary failed run and nobody could tell it was interrupted.
    assert "caught_by_except_exception" not in got
    assert "cancelled_reached_main" in got


def test_a_second_sigterm_does_not_cut_the_teardown_short(child):
    """The handler is one-shot on purpose.

    A second cancellation lands on the `await` inside the `finally:` and abandons the rest of the
    teardown -- i.e. repeating the signal causes the exact container leak the handler exists to
    prevent. An operator holding down the stop button must not be able to do that; the SIGKILL that
    follows from TimeoutStopSec (or from the queue worker's escalation) is the real bound.
    """
    proc = child(cleanup_seconds=3.0)   # a teardown slow enough to interrupt, like a real one
    out = child.out

    proc.send_signal(signal.SIGTERM)
    _await_marker(out, "cleanup_started", proc)
    proc.send_signal(signal.SIGTERM)    # while the teardown is mid-flight

    assert proc.wait(timeout=GENEROUS_WAIT_SECONDS) == 1
    assert "containers_removed" in _markers(out), (
        "the second SIGTERM interrupted the teardown that the first one started"
    )
