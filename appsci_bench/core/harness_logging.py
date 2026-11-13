"""Host-side, bounded live capture of native CLI stdout/stderr from Docker.

No agent-provided paths or extra mounts. The CLI remains PID 1; this observer does
not change its stdin, exit code, signals, or benchmark submission lifecycle.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path

from shared.observability.redact import scrub, secret_values

logger = logging.getLogger(__name__)
MAX_LOG_BYTES = 50 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024


class HarnessLogCapture:
    def __init__(self, container: str, path: Path, environ: dict[str, str]):
        self.container = container
        self.path = path
        self.secrets = secret_values(environ)
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            ["docker", "logs", "--follow", "--timestamps", self.container],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        self.thread = threading.Thread(target=self._copy, name="native-harness-log", daemon=True)
        self.thread.start()

    def _copy(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        size = 0
        skipping = False
        truncated = False
        try:
            with self.path.open("wb") as output:
                while chunk := self.process.stdout.readline(MAX_LINE_BYTES + 1):
                    if truncated:
                        continue  # Drain the observer, never back-pressure the agent.
                    if skipping:
                        skipping = not chunk.endswith(b"\n")
                        continue
                    if len(chunk) > MAX_LINE_BYTES:
                        skipping = not chunk.endswith(b"\n")
                        chunk = (json.dumps({"type": "benchmark.log_warning",
                                             "message": "Oversized native log record omitted."}) + "\n").encode()
                    text = scrub(chunk.decode("utf-8", errors="replace"), self.secrets)
                    if not text.endswith("\n"):
                        text += "\n"  # EOF without a newline is still a complete captured record.
                    data = text.encode("utf-8")
                    if size + len(data) > MAX_LOG_BYTES:
                        data = b'{"type":"benchmark.log_warning","message":"Native log size limit reached; remaining output omitted."}\n'
                        truncated = True
                    output.write(data)
                    output.flush()
                    size += len(data)
        except OSError:
            logger.exception("Could not capture native harness output")
        finally:
            self.process.stdout.close()

    def stop(self) -> None:
        if self.process is not None:
            try:
                # After docker stop/CLI exit the follower normally drains and exits itself.
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        if self.thread is not None:
            self.thread.join(timeout=3)
