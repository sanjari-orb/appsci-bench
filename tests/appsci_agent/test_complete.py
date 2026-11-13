"""Submission failures must tell the agent how to recover without ending the build."""
from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, Mock
from urllib.error import HTTPError

import pytest

from appsci_agent.tools.base import ToolContext
from appsci_agent.tools.complete import CompleteTool

SUBMIT_URL = "http://orchestrator:9000/submit"
ENDPOINT = "http://artifact:8080/respond"
STALE_DETAIL = "Artifact source changed after deployment; deploy and test it again"


@pytest.fixture
def submission(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_SUBMIT_URL", SUBMIT_URL)
    tool = CompleteTool()
    monkeypatch.setattr(tool, "_check_endpoint", lambda url: (True, None))
    context = ToolContext(artifact_endpoint=ENDPOINT)
    urlopen = Mock()
    monkeypatch.setattr("appsci_agent.tools.complete.urllib.request.urlopen", urlopen)
    return tool, context, urlopen


def test_submission_conflict_exposes_recovery_instruction(submission, caplog):
    tool, context, urlopen = submission
    urlopen.side_effect = HTTPError(
        SUBMIT_URL, 409, "Conflict", {},
        io.BytesIO(json.dumps({"detail": STALE_DETAIL}).encode()),
    )

    result = tool.execute({}, context)

    expected = f"HTTP error: 409 Conflict -- {STALE_DETAIL}"
    assert result.success is False
    assert result.to_dict()["error"] == f"Failed to notify orchestrator: {expected}"
    assert expected in caplog.text
    assert context.build_complete is False
    assert context.artifact_endpoint == ENDPOINT

    # A rejected submission leaves the tool usable once the server accepts the artifact.
    response = MagicMock()
    response.__enter__.return_value.status = 200
    urlopen.side_effect = None
    urlopen.return_value = response
    assert tool.execute({}, context).success is True
    assert context.build_complete is True


@pytest.mark.parametrize("body,detail", [
    (b"upstream unavailable", "upstream unavailable"),
    (b'{"detail":', '{"detail":'),
    (b'{"error":"unavailable"}', '{"error":"unavailable"}'),
    (b'{"detail":[{"msg":"field required"}]}', '{"detail":[{"msg":"field required"}]}'),
    (b'"unavailable"', '"unavailable"'),
    (b"unavailable: \xff", "unavailable: \ufffd"),
    (b"", ""),
    (b" \n ", ""),
])
def test_submission_error_body_fallbacks(submission, body, detail):
    tool, context, urlopen = submission
    urlopen.side_effect = HTTPError(
        SUBMIT_URL, 502, "Bad Gateway", {}, io.BytesIO(body),
    )

    result = tool.execute({}, context)

    expected = "Failed to notify orchestrator: HTTP error: 502 Bad Gateway"
    if detail:
        expected += f" -- {detail}"
    assert result.success is False
    assert result.error == expected
    assert context.build_complete is False


def test_unreadable_error_body_preserves_http_status(submission):
    tool, context, urlopen = submission
    body = Mock()
    body.read.side_effect = OSError("response stream closed")
    urlopen.side_effect = HTTPError(SUBMIT_URL, 409, "Conflict", {}, body)

    result = tool.execute({}, context)

    assert result.success is False
    assert result.error == "Failed to notify orchestrator: HTTP error: 409 Conflict"
    assert context.build_complete is False
