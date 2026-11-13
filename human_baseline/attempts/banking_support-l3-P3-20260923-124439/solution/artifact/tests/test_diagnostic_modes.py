"""Tests for the read-only `llm` connectivity probe and `readdoc` mode of diagnostic.py.

Guarantees asserted here:
  * `llm` mode sends exactly ONE Anthropic Messages request to {base_url}/v1/messages with a bearer
    auth header, anthropic-version, and any custom headers merged in -- and reports ONLY non-secret
    facts, never the auth token or a custom-header value. It fails closed (available False) when the
    Anthropic runtime config is missing, before any network call.
  * `readdoc` mode does a read-only `cat` of a *safe*, human-supplied corpus path only; unsafe paths
    and missing config are refused without touching the tool server.
Both are diagnostics, not the agent core.
"""
from __future__ import annotations

import json

import httpx
import pytest

import config
import diagnostic
import tools as tools_mod
from fake_tools import Recorder, make_client

TOKEN = "anthropic-tok-secret"
HEADER_VALUE = "route-secret-value"


# --- llm connectivity probe -----------------------------------------------------------------------

def _anthropic_env(monkeypatch, *, base="https://proxy.test", token=TOKEN,
                   headers='{"x-route": "route-secret-value"}'):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", base)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", token)
    if headers is None:
        monkeypatch.delenv("APPSCI_AGENT_CUSTOM_HEADERS", raising=False)
    else:
        monkeypatch.setenv("APPSCI_AGENT_CUSTOM_HEADERS", headers)
    for var in config._AGENT_MODEL_VARS:
        monkeypatch.delenv(var, raising=False)


def _capture_client(handler):
    """An httpx.Client over MockTransport that records the outgoing request for assertions."""
    captured: dict = {}

    def wrapped(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content) if request.content else None
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(wrapped)), captured


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_1",
            "model": "claude-opus-4-8",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 2},
            "content": [{"type": "text", "text": "OK"}],
        },
    )


def test_llm_connectivity_happy_path(monkeypatch):
    _anthropic_env(monkeypatch)
    client, captured = _capture_client(_ok_handler)

    report = diagnostic.build_llm_connectivity_report(client=client)

    assert report["available"] is True
    assert report["ok"] is True
    assert report["status_code"] == 200
    assert report["response_model"] == "claude-opus-4-8"
    assert report["stop_reason"] == "end_turn"
    assert report["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert report["content_text_snippet"] == "OK"
    assert report["model"] == config.DEFAULT_AGENT_MODEL == "claude-opus-4-8"
    assert report["model_source"] == f"default:{config.DEFAULT_AGENT_MODEL}"
    # NAMES of custom headers only -- never their values.
    assert report["custom_header_names"] == ["x-route"]

    # Request shape: correct endpoint, bearer auth, version pin, merged custom header, trivial payload.
    assert captured["path"] == "/v1/messages"
    assert captured["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert captured["headers"]["anthropic-version"] == diagnostic._ANTHROPIC_VERSION
    assert captured["headers"]["x-route"] == HEADER_VALUE
    assert captured["body"]["model"] == "claude-opus-4-8"
    assert captured["body"]["max_tokens"] == 16
    assert captured["body"]["messages"][0]["role"] == "user"


def test_llm_report_never_leaks_secrets(monkeypatch):
    _anthropic_env(monkeypatch)
    client, _ = _capture_client(_ok_handler)
    report = diagnostic.build_llm_connectivity_report(client=client)
    blob = json.dumps(report)
    assert TOKEN not in blob
    assert HEADER_VALUE not in blob


def test_llm_http_error_is_classified_and_redacted(monkeypatch):
    _anthropic_env(monkeypatch)

    def err_handler(request: httpx.Request) -> httpx.Response:
        # An error envelope that (pathologically) echoes both secrets -- must be redacted.
        return httpx.Response(
            401,
            json={
                "type": "error",
                "error": {
                    "type": "authentication_error",
                    "message": f"bad key {TOKEN} routed via {HEADER_VALUE}",
                },
            },
        )

    client, _ = _capture_client(err_handler)
    report = diagnostic.build_llm_connectivity_report(client=client)

    assert report["available"] is True
    assert report["ok"] is False
    assert report["kind"] == "http_error"
    assert report["status_code"] == 401
    assert report["error_type"] == "authentication_error"
    assert TOKEN not in report["detail"]
    assert HEADER_VALUE not in report["detail"]
    assert "<redacted>" in report["detail"]


def test_llm_non_json_body_is_bad_response(monkeypatch):
    _anthropic_env(monkeypatch)
    client, _ = _capture_client(lambda r: httpx.Response(200, text="not json"))
    report = diagnostic.build_llm_connectivity_report(client=client)
    assert report["ok"] is False
    assert report["kind"] == "bad_response"


def test_llm_network_error_is_classified(monkeypatch):
    _anthropic_env(monkeypatch)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client, _ = _capture_client(boom)
    report = diagnostic.build_llm_connectivity_report(client=client)
    assert report["available"] is True
    assert report["ok"] is False
    assert report["kind"] == "network_error"
    assert TOKEN not in report["error"]


def test_llm_missing_config_fails_closed_without_network(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    for var in config._AGENT_MODEL_VARS:
        monkeypatch.delenv(var, raising=False)

    # No client passed: if it tried to hit the network the test would fail; it must return first.
    report = diagnostic.build_llm_connectivity_report()

    assert report["available"] is False
    assert "ANTHROPIC_BASE_URL" in report["reason"]
    # Non-secret base facts are still reported so P3 sees the intended model/env presence.
    assert report["model"] == config.DEFAULT_AGENT_MODEL
    assert "anthropic_env_presence" in report


def test_llm_report_surfaces_malformed_custom_headers_without_leaking(monkeypatch):
    # Present-but-malformed header value (empty name) must be surfaced, not silently dropped, and its
    # value must never appear in the report.
    _anthropic_env(monkeypatch, headers=": route-secret-value")
    client, captured = _capture_client(_ok_handler)

    report = diagnostic.build_llm_connectivity_report(client=client)

    assert report["custom_headers_error"] is not None
    assert report["custom_header_names"] == []          # nothing parsed -> no header attached
    assert "route-secret-value" not in json.dumps(report)
    # And the malformed header was NOT smuggled onto the outbound request.
    assert "route-secret-value" not in json.dumps(dict(captured["headers"]))


# --- readdoc mode ---------------------------------------------------------------------------------

def _cat_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"result": "SIGNATURE: unlock_discoverable_agent_tool(name: str)"})


def _patch_toolclient(monkeypatch, recorder: Recorder) -> None:
    monkeypatch.setattr(config, "require_banking_tools", lambda: ("http://tools.test", "tok"))
    shared = make_client(_cat_handler, recorder)
    real_cls = tools_mod.ToolClient

    def factory(url, token, **kwargs):
        return real_cls(url, token, client=shared)

    monkeypatch.setattr(tools_mod, "ToolClient", factory)


def test_readdoc_reads_safe_path_via_readonly_cat(monkeypatch):
    rec = Recorder()
    _patch_toolclient(monkeypatch, rec)

    report = diagnostic.build_readdoc_report("kb/tools/reference.md")

    assert report["available"] is True
    assert report["path"] == "kb/tools/reference.md"
    assert report["read"]["tool"] == "shell"
    assert report["read"]["arguments"] == {"command": "cat kb/tools/reference.md"}
    assert report["read"]["ok"] is True

    # Exactly one read-only shell call was made, to /shell, with a bearer header.
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["path"] == "/shell"
    assert call["authorization"] == "Bearer tok"
    assert call["body"] == {"arguments": {"command": "cat kb/tools/reference.md"}}


@pytest.mark.parametrize(
    "path",
    [
        "",                       # empty
        "/etc/passwd",            # absolute
        "../secrets.md",          # parent traversal
        "kb/../../etc/passwd",    # embedded traversal
        "kb/x; rm -rf /",         # shell metachars (space + ';')
        "kb/$(rm x).md",          # command substitution chars
        "kb/a|b.md",              # pipe char
    ],
)
def test_readdoc_refuses_unsafe_path_without_network(monkeypatch, path):
    # If it reached the tool server the missing patch would surface a config/network path; instead it
    # must refuse purely on the path. We also assert it never required banking config.
    called = {"require": False}

    def spy():
        called["require"] = True
        return ("http://tools.test", "tok")

    monkeypatch.setattr(config, "require_banking_tools", spy)

    report = diagnostic.build_readdoc_report(path)

    assert report["available"] is False
    assert called["require"] is False  # rejected before any config/tool access


def test_readdoc_reports_missing_config_for_safe_path(monkeypatch):
    def raise_missing():
        raise RuntimeError("BANKING_TOOLS_URL and BANKING_TOOLS_TOKEN must be set")

    monkeypatch.setattr(config, "require_banking_tools", raise_missing)
    report = diagnostic.build_readdoc_report("kb/tools.md")
    assert report["available"] is False
    assert "BANKING_TOOLS_URL" in report["reason"]


# --- main() dispatch for the new modes ------------------------------------------------------------

def test_main_readdoc_requires_a_path(capsys):
    rc = diagnostic.main(["readdoc"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "error" in out and "path" in out["error"]
