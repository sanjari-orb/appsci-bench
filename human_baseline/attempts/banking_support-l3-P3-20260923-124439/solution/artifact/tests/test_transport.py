"""Transport-level tests for ToolClient: wire shape, bearer header, faithful failure classes."""
from __future__ import annotations

import httpx

from tools import (
    KIND_AUTH_ERROR,
    KIND_BAD_RESPONSE,
    KIND_HTTP_ERROR,
    KIND_NETWORK_ERROR,
    KIND_NOT_FOUND,
    KIND_OK,
    KIND_SERVER_ERROR,
    ToolClient,
)
from fake_tools import Recorder, make_client, raising_handler, static_handler

TOKEN = "test-token-do-not-log"


def _client(handler, recorder=None) -> ToolClient:
    return ToolClient("http://tools.test", TOKEN, client=make_client(handler, recorder))


def test_call_success_shape_and_bearer():
    rec = Recorder()
    client = _client(static_handler(200, {"result": {"hits": [1, 2]}}), rec)
    res = client.call("KB_search_bm25", {"query": "overdraft fee"})

    assert res.ok is True
    assert res.kind == KIND_OK
    assert res.result == {"hits": [1, 2]}

    # One recorded call with the documented wire shape and a bearer header.
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/KB_search_bm25"
    assert call["body"] == {"arguments": {"query": "overdraft fee"}}
    assert call["authorization"] == f"Bearer {TOKEN}"


def test_call_empty_arguments_defaults_to_empty_dict():
    rec = Recorder()
    client = _client(static_handler(200, {"result": None}), rec)
    client.call("shell", None)
    assert rec.calls[0]["body"] == {"arguments": {}}


def test_auth_error_is_distinct():
    client = _client(static_handler(401, {"detail": "no"}))
    res = client.call("whatever", {})
    assert res.ok is False
    assert res.kind == KIND_AUTH_ERROR
    assert res.status_code == 401


def test_forbidden_is_auth_error():
    client = _client(static_handler(403))
    res = client.call("whatever", {})
    assert res.kind == KIND_AUTH_ERROR


def test_not_found_is_distinct():
    client = _client(static_handler(404))
    res = client.call("nope", {})
    assert res.ok is False
    assert res.kind == KIND_NOT_FOUND
    assert res.status_code == 404


def test_server_error_is_distinct():
    client = _client(static_handler(503))
    res = client.call("whatever", {})
    assert res.ok is False
    assert res.kind == KIND_SERVER_ERROR
    assert res.status_code == 503


def test_other_status_is_http_error():
    client = _client(static_handler(418))
    res = client.call("whatever", {})
    assert res.kind == KIND_HTTP_ERROR
    assert res.status_code == 418


def test_missing_result_field_is_bad_response():
    client = _client(static_handler(200, {"not_result": 1}))
    res = client.call("whatever", {})
    assert res.ok is False
    assert res.kind == KIND_BAD_RESPONSE


def test_non_json_body_is_bad_response():
    client = _client(static_handler(200, text_body="not json"))
    res = client.call("whatever", {})
    assert res.ok is False
    assert res.kind == KIND_BAD_RESPONSE


def test_timeout_is_network_error():
    client = _client(raising_handler(httpx.ReadTimeout("slow")))
    res = client.call("whatever", {})
    assert res.ok is False
    assert res.kind == KIND_NETWORK_ERROR


def test_connect_error_is_network_error():
    client = _client(raising_handler(httpx.ConnectError("refused")))
    res = client.call("whatever", {})
    assert res.kind == KIND_NETWORK_ERROR


def test_token_not_present_in_any_failure_result():
    # Across failure classes, the token must never appear in the surfaced result object.
    for handler in (
        static_handler(401),
        static_handler(404),
        static_handler(503),
        static_handler(200, {"no_result": 1}),
        raising_handler(httpx.ConnectError("refused")),
    ):
        client = _client(handler)
        res = client.call("whatever", {"query": "x"})
        assert TOKEN not in repr(res)
        assert TOKEN not in (res.error or "")


def test_http_400_validation_detail_is_captured():
    # An empty-argument call that a tool rejects with a validation error must surface the body, which
    # names the required field(s) -- this is how retrieval arg names are learned, not guessed.
    body = {"detail": [{"loc": ["arguments", "query"], "msg": "field required"}]}
    client = _client(static_handler(400, body))
    res = client.call("KB_search_bm25", {})
    assert res.ok is False
    assert res.kind == KIND_HTTP_ERROR
    assert res.status_code == 400
    assert res.detail is not None
    assert "query" in res.detail  # the required field name is preserved, not discarded


def test_success_result_has_no_detail():
    client = _client(static_handler(200, {"result": {"hits": []}}))
    res = client.call("KB_search_bm25", {"query": "x"})
    assert res.ok is True
    assert res.detail is None


def test_detail_is_redacted_of_banking_token(monkeypatch):
    # If a body ever echoed the banking token, the captured detail must redact it (defense in depth).
    monkeypatch.setenv("BANKING_TOOLS_TOKEN", TOKEN)
    client = _client(static_handler(400, {"echo": f"saw {TOKEN} here"}))
    res = client.call("whatever", {})
    assert res.detail is not None
    assert TOKEN not in res.detail
    assert "<redacted>" in res.detail
