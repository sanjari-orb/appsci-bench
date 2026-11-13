"""Unit tests for TracerServer's preflight port-conflict check.

Regression: TracerServer.start() used to spawn uvicorn's bind inside a background
daemon thread with no exception handling, so a port-already-in-use error was silently
swallowed there -- start() would still sleep, return a URL, and log success as if
the server were actually listening. TracerPortInUseError is now raised synchronously,
before that thread is even spawned, so the orchestrator can hard-stop instead of
continuing without a working dashboard.
"""
from __future__ import annotations

import asyncio
import socket

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from appsci_bench.tracer.server import (
    TracerPortInUseError, TracerPortRangeExhaustedError, TracerServer, url_base,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_start_raises_when_port_already_bound(tmp_path):
    port = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Same interface the server binds (TracerServer.host defaults to loopback), or the two do not
    # actually contend for the port and this asserts nothing.
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    try:
        server = TracerServer(runs_dir=tmp_path, port=port)
        with pytest.raises(TracerPortInUseError) as exc_info:
            asyncio.run(server.start())
        assert str(port) in str(exc_info.value)
    finally:
        blocker.close()


def test_start_succeeds_when_port_is_free(tmp_path):
    port = _free_port()
    server = TracerServer(runs_dir=tmp_path, port=port)
    try:
        url = asyncio.run(server.start())
        assert url == f"http://localhost:{port}"
    finally:
        asyncio.run(server.stop())


# --- TracerPortRangeExhaustedError -----------------------------------------------------------
# Raised by the orchestrator's _start_tracer_with_retry once every port in its retry window is
# occupied; it's a subclass of TracerPortInUseError so callers that only handle the single-port
# case (e.g. the CLI's hard-stop exit code) still catch this too.

def test_range_exhausted_error_is_a_port_in_use_error():
    assert isinstance(TracerPortRangeExhaustedError(8080, 8099), TracerPortInUseError)


def test_range_exhausted_error_message_names_the_range():
    e = TracerPortRangeExhaustedError(8080, 8099)
    assert "8080" in str(e) and "8099" in str(e)


# --- url_base ---------------------------------------------------------------------------------
# Every URL in the tracer's templates is written `{{ base }}/...` so this app works both on its own
# port and mounted inside another one (the experiments server mounts it at /tracer to avoid a
# second port + SSH tunnel). `url_base` is the whole mechanism: Starlette sets `root_path` to the
# mount path when mounted and leaves it empty otherwise. The empty case is the important one --
# it is what makes the refactor a no-op for a standalone tracer.


def _request_with_root_path(root_path):
    scope = {"type": "http", "method": "GET", "path": "/", "headers": []}
    if root_path is not None:
        scope["root_path"] = root_path
    return Request(scope)


@pytest.mark.parametrize(
    ("root_path", "expected"),
    [
        (None, ""),        # no root_path in scope at all
        ("", ""),          # standalone: rendered URLs stay absolute, as before
        ("/tracer", "/tracer"),
        ("/tracer/", "/tracer"),   # trailing slash would double up against "{{ base }}/api/..."
        ("/a/b", "/a/b"),          # nested mounts / a reverse proxy sub-path
    ],
)
def test_url_base_reads_root_path(root_path, expected):
    assert url_base(_request_with_root_path(root_path)) == expected


def test_standalone_dashboard_keeps_absolute_urls(tmp_path):
    """The regression guard: with an empty root_path the page is what it always was."""
    (tmp_path / "tr").mkdir(parents=True)
    html = TestClient(TracerServer(runs_dir=tmp_path, port=0)._create_app()).get(
        "/?trace_id=tr"
    ).text
    assert "/api/events/tr?since_id=" in html
    assert "/static/styles.css?v=" in html
    assert "{{ base }}" not in html
