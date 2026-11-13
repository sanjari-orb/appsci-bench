"""Discovery-probe tests: 2xx candidate vs. class-distinguished non-2xx (never collapse to 'none')."""
from __future__ import annotations

import httpx

from tools import (
    KIND_AUTH_ERROR,
    KIND_NETWORK_ERROR,
    KIND_NOT_FOUND,
    KIND_OK,
    KIND_SERVER_ERROR,
    ToolClient,
)
from fake_tools import make_client, static_handler

TOKEN = "probe-token"


def _client(handler) -> ToolClient:
    return ToolClient("http://tools.test", TOKEN, client=make_client(handler))


def test_all_404_means_no_candidate_but_records_not_found():
    probe = _client(static_handler(404)).probe_discovery()
    assert probe.manifest_candidate is False
    assert probe.findings
    assert all(f.kind == KIND_NOT_FOUND for f in probe.findings)


def test_2xx_marks_candidate():
    probe = _client(static_handler(200, {"tools": []})).probe_discovery()
    assert probe.manifest_candidate is True
    assert any(f.kind == KIND_OK for f in probe.findings)


def test_401_is_auth_not_missing_manifest():
    probe = _client(static_handler(401)).probe_discovery()
    # An auth failure must NOT be reported as 'no manifest'.
    assert probe.manifest_candidate is False
    assert all(f.kind == KIND_AUTH_ERROR for f in probe.findings)
    assert all(f.kind != KIND_NOT_FOUND for f in probe.findings)


def test_5xx_is_transient_not_missing_manifest():
    probe = _client(static_handler(502)).probe_discovery()
    assert probe.manifest_candidate is False
    assert all(f.kind == KIND_SERVER_ERROR for f in probe.findings)


def test_probe_uses_get_and_bearer():
    from fake_tools import Recorder, make_client as mk

    rec = Recorder()
    client = ToolClient("http://tools.test", TOKEN, client=mk(static_handler(404), rec))
    client.probe_discovery(["", "tools"])
    assert rec.calls
    assert all(c["method"] == "GET" for c in rec.calls)
    assert all(c["authorization"] == f"Bearer {TOKEN}" for c in rec.calls)


def test_probe_network_error_is_surfaced():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = ToolClient("http://tools.test", TOKEN, client=make_client(boom))
    probe = client.probe_discovery([""])
    assert probe.manifest_candidate is False
    assert probe.findings[0].kind == KIND_NETWORK_ERROR
