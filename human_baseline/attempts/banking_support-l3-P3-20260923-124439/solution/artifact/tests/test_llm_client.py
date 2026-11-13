"""Tests for llm_client.AnthropicMessagesClient: request shape, parsing, and non-secret failures.

These exercise the ONE confirmed runtime interface (Anthropic Messages at {base}/v1/messages) with a
MockTransport, asserting: correct endpoint + bearer + version + merged custom headers, payload shape,
tool_use parsing, tools included only when supplied, and that every failure class raises LLMError
with no credential value in the message.
"""
from __future__ import annotations

import json

import httpx
import pytest

import config
from llm_client import AnthropicMessagesClient, LLMError

TOKEN = "anthropic-tok-secret-value"
HEADER_VALUE = "routing-secret-value"


def _capture_client(handler):
    captured: dict = {}

    def wrapped(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content) if request.content else None
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(wrapped)), captured


def _client(handler, **kwargs):
    transport, captured = _capture_client(handler)
    defaults = dict(base_url="https://proxy.test", token=TOKEN, model="claude-opus-4-8")
    defaults.update(kwargs)
    return AnthropicMessagesClient(client=transport, **defaults), captured


def _text_response(text="Hello", stop_reason="end_turn"):
    return lambda r: httpx.Response(
        200,
        json={
            "id": "msg_1",
            "model": "claude-opus-4-8",
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 3, "output_tokens": 2},
            "content": [{"type": "text", "text": text}],
        },
    )


def test_happy_path_text_only_request_shape():
    client, captured = _client(_text_response("Hi there"), custom_headers={"x-route": HEADER_VALUE})
    resp = client.create_message(system="SYS", messages=[{"role": "user", "content": "hello"}])

    assert resp.text == "Hi there"
    assert resp.tool_calls == []
    assert resp.stop_reason == "end_turn"

    assert captured["path"] == "/v1/messages"
    assert captured["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["headers"]["x-route"] == HEADER_VALUE
    assert captured["body"]["model"] == "claude-opus-4-8"
    assert captured["body"]["system"] == "SYS"
    assert captured["body"]["messages"] == [{"role": "user", "content": "hello"}]
    # No tools supplied -> no 'tools' key on the request.
    assert "tools" not in captured["body"]


def test_tools_included_only_when_supplied():
    tools = [{"name": "KB_search_bm25", "description": "d", "input_schema": {"type": "object"}}]
    client, captured = _client(_text_response())
    client.create_message(system="S", messages=[{"role": "user", "content": "x"}], tools=tools)
    assert captured["body"]["tools"] == tools


def test_default_max_tokens_used_when_unspecified():
    client, captured = _client(_text_response())
    client.create_message(system="S", messages=[{"role": "user", "content": "x"}])
    assert captured["body"]["max_tokens"] == 2048


def test_explicit_max_tokens_overrides_default():
    client, captured = _client(_text_response())
    client.create_message(system="S", messages=[{"role": "user", "content": "x"}], max_tokens=64)
    assert captured["body"]["max_tokens"] == 64


def test_parses_tool_use_blocks():
    def handler(r):
        return httpx.Response(
            200,
            json={
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "let me look"},
                    {"type": "tool_use", "id": "tu_1", "name": "KB_search_bm25",
                     "input": {"query": "overdraft fee"}},
                ],
            },
        )

    client, _ = _client(handler)
    resp = client.create_message(system="S", messages=[{"role": "user", "content": "x"}])
    assert resp.text == "let me look"
    assert len(resp.tool_calls) == 1
    call = resp.tool_calls[0]
    assert (call.id, call.name, call.input) == ("tu_1", "KB_search_bm25", {"query": "overdraft fee"})
    # raw_content is preserved verbatim so the agent loop can replay the assistant turn.
    assert resp.raw_content[1]["type"] == "tool_use"


def test_http_error_raises_llmerror_and_redacts_secrets(monkeypatch):
    # config.redact() scrubs env secrets; set them so the echoed body is scrubbed.
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", f"x-route: {HEADER_VALUE}")

    def handler(r):
        return httpx.Response(401, json={"error": {"message": f"bad key {TOKEN} via {HEADER_VALUE}"}})

    client, _ = _client(handler, custom_headers={"x-route": HEADER_VALUE})
    with pytest.raises(LLMError) as exc:
        client.create_message(system="S", messages=[{"role": "user", "content": "x"}])
    assert exc.value.kind == "http_error"
    assert exc.value.status_code == 401
    assert TOKEN not in str(exc.value)
    assert HEADER_VALUE not in str(exc.value)
    assert "<redacted>" in str(exc.value)


def test_network_error_raises_llmerror(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)

    def boom(r):
        raise httpx.ConnectError("connection refused")

    client, _ = _client(boom)
    with pytest.raises(LLMError) as exc:
        client.create_message(system="S", messages=[{"role": "user", "content": "x"}])
    assert exc.value.kind == "network_error"
    assert TOKEN not in str(exc.value)


def test_non_json_body_is_bad_response():
    client, _ = _client(lambda r: httpx.Response(200, text="not json"))
    with pytest.raises(LLMError) as exc:
        client.create_message(system="S", messages=[{"role": "user", "content": "x"}])
    assert exc.value.kind == "bad_response"


def test_missing_content_is_bad_response():
    client, _ = _client(lambda r: httpx.Response(200, json={"stop_reason": "end_turn"}))
    with pytest.raises(LLMError) as exc:
        client.create_message(system="S", messages=[{"role": "user", "content": "x"}])
    assert exc.value.kind == "bad_response"


def test_from_config_uses_runtime_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env-proxy.test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", f"x-route: {HEADER_VALUE}")
    for var in config._AGENT_MODEL_VARS:
        monkeypatch.delenv(var, raising=False)

    transport, captured = _capture_client(_text_response("ok"))
    client = AnthropicMessagesClient.from_config(client=transport)
    assert client.model == config.DEFAULT_AGENT_MODEL
    client.create_message(system="S", messages=[{"role": "user", "content": "x"}])
    # Uses the env base URL, env bearer, and merges the env custom header.
    assert captured["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert captured["headers"]["x-route"] == HEADER_VALUE
    assert captured["body"]["model"] == config.DEFAULT_AGENT_MODEL


def test_from_config_raises_when_env_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        AnthropicMessagesClient.from_config()
