"""The Docker log observer is bounded and does not change the native CLI lifecycle."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import threading
import time
from unittest.mock import Mock

from appsci_bench.core import harness_logging
from appsci_bench.core.agent_container import AppSciAgentContainerConfig, AppSciAgentContainerManager
from appsci_bench.core.harness import HarnessConfig


def capture(tmp_path, monkeypatch, data, **environ):
    process = Mock(stdout=io.BytesIO(data))
    process.wait.return_value = 0
    popen = Mock(return_value=process)
    monkeypatch.setattr(harness_logging.subprocess, "Popen", popen)
    observer = harness_logging.HarnessLogCapture("test-container", tmp_path / "native.log", environ)
    observer.start()
    observer.stop()
    assert popen.call_args.args[0] == ["docker", "logs", "--follow", "--timestamps", "test-container"]
    assert popen.call_args.kwargs["stderr"] == subprocess.STDOUT
    return observer.path.read_text(), process


def test_capture_scrubs_known_credentials_before_writing(tmp_path, monkeypatch):
    secret = "test-private-token-value-0123456789"
    data = ('2026-09-16T12:00:00Z ' + json.dumps({"type": "result", "result": secret}) + "\n").encode()
    output, process = capture(tmp_path, monkeypatch, data, ANTHROPIC_AUTH_TOKEN=secret)
    assert secret not in output
    assert "[REDACTED:ANTHROPIC_AUTH_TOKEN]" in output
    assert output.startswith("2026-09-16T12:00:00Z ")
    process.terminate.assert_not_called()


def test_oversized_line_is_discarded_without_losing_next_event(tmp_path, monkeypatch):
    monkeypatch.setattr(harness_logging, "MAX_LINE_BYTES", 64)
    output, _ = capture(tmp_path, monkeypatch, b"x" * 200 + b'\n{"type":"turn.completed"}\n')
    lines = [json.loads(line) for line in output.splitlines()]
    assert lines[0]["type"] == "benchmark.log_warning"
    assert lines[1]["type"] == "turn.completed"
    assert "xxxx" not in output


def test_capture_limits_disk_usage_and_marks_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr(harness_logging, "MAX_LOG_BYTES", 50)
    output, _ = capture(tmp_path, monkeypatch, b'{"type":"turn.started"}\n' * 100)
    assert len(output) < 250
    assert "size limit reached" in output


def test_output_is_visible_before_native_process_exits(tmp_path, monkeypatch):
    real_popen = subprocess.Popen
    child = real_popen([sys.executable, "-u", "-c",
                        "import sys; print('live event', flush=True); sys.stdin.read()"],
                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    monkeypatch.setattr(harness_logging.subprocess, "Popen", lambda *args, **kwargs: child)
    observer = harness_logging.HarnessLogCapture("container", tmp_path / "native.log", {})
    try:
        observer.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if observer.path.exists() and "live event" in observer.path.read_text():
                break
            threading.Event().wait(0.01)
        assert "live event" in observer.path.read_text()
        assert child.poll() is None
    finally:
        observer.stop()
        child.stdin.close()
    assert not observer.thread.is_alive()
    assert child.poll() is not None


def test_manager_capture_is_native_only_and_stops_before_removal(tmp_path, monkeypatch):
    config = AppSciAgentContainerConfig(
        task_id="task", workspace_container="workspace", appsci_usersim_url="http://usersim",
        appsci_usersim_token="secret-stakeholder-token-long-enough", orchestrator_submit_url="http://submit",
        spec_path="spec.yaml",
    )
    manager = AppSciAgentContainerManager(config)
    manager._container_name = "container"
    observer = Mock()
    factory = Mock(return_value=observer)
    monkeypatch.setattr(harness_logging, "HarnessLogCapture", factory)
    manager.start_harness_log_capture(tmp_path / "native.log")
    factory.assert_not_called()
    config.harness = HarnessConfig("codex", "image", adapter="codex", auth="api")
    manager.start_harness_log_capture(tmp_path / "native.log")
    observer.start.assert_called_once()
    assert factory.call_args.args[2]["APPSCI_USERSIM_TOKEN"] == config.appsci_usersim_token
    manager.start_harness_log_capture(tmp_path / "native.log")
    factory.assert_called_once()
    order = []
    observer.stop.side_effect = lambda: order.append("observer stopped")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: order.append("container removed"))
    manager.remove()
    assert order == ["observer stopped", "container removed"]


def test_log_observer_failure_does_not_fail_the_agent_cleanup(tmp_path):
    config = AppSciAgentContainerConfig(
        task_id="task", workspace_container="workspace", appsci_usersim_url="http://usersim",
        appsci_usersim_token="token", orchestrator_submit_url="http://submit", spec_path="spec.yaml",
    )
    manager = AppSciAgentContainerManager(config)
    manager._harness_log_capture = Mock()
    manager._harness_log_capture.stop.side_effect = OSError("observer unavailable")
    manager.stop_harness_log_capture()
    assert manager._harness_log_capture is None
