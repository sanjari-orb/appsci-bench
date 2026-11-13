"""Tests for AppsciAgentUserSimClient -- the agent-side HTTP client. No network: httpx is given a MockTransport."""
from __future__ import annotations

import httpx
import pytest

from appsci_agent.clients import AppsciAgentUserSimClient, AppsciAgentUserSimClientError


def _client_with(handler, monkeypatch) -> AppsciAgentUserSimClient:
    """Build a AppsciAgentUserSimClient whose internal httpx.Client routes through a MockTransport."""
    monkeypatch.setenv("APPSCI_USERSIM_URL", "http://usersim:8000")
    monkeypatch.setenv("APPSCI_USERSIM_TOKEN", "run_tok")
    c = AppsciAgentUserSimClient()
    # Swap the real transport for a mock one, preserving base_url + auth headers.
    c._client = httpx.Client(
        base_url="http://usersim:8000",
        headers={"Authorization": "Bearer run_tok"},
        transport=httpx.MockTransport(handler),
    )
    return c


class TestConstruction:
    def test_missing_url_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("APPSCI_USERSIM_URL", raising=False)
        monkeypatch.setenv("APPSCI_USERSIM_TOKEN", "t")
        with pytest.raises(AppsciAgentUserSimClientError, match="APPSCI_USERSIM_URL"):
            AppsciAgentUserSimClient()

    def test_missing_token_raises(self, monkeypatch) -> None:
        monkeypatch.setenv("APPSCI_USERSIM_URL", "http://usersim:8000")
        monkeypatch.delenv("APPSCI_USERSIM_TOKEN", raising=False)
        with pytest.raises(AppsciAgentUserSimClientError, match="APPSCI_USERSIM_TOKEN"):
            AppsciAgentUserSimClient()

    def test_explicit_args_override_env(self, monkeypatch) -> None:
        monkeypatch.delenv("APPSCI_USERSIM_URL", raising=False)
        monkeypatch.delenv("APPSCI_USERSIM_TOKEN", raising=False)
        c = AppsciAgentUserSimClient(base_url="http://x:9000", token="tok")
        assert c._client.headers["Authorization"] == "Bearer tok"

    def test_sends_bearer_header(self, monkeypatch) -> None:
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"status": "ok"})

        c = _client_with(handler, monkeypatch)
        c.health()
        assert seen["auth"] == "Bearer run_tok"


class TestHealth:
    def test_health_true_on_ok(self, monkeypatch) -> None:
        c = _client_with(lambda r: httpx.Response(200, json={"status": "ok"}), monkeypatch)
        assert c.health() is True

    def test_health_false_on_non_ok(self, monkeypatch) -> None:
        c = _client_with(lambda r: httpx.Response(200, json={"status": "warming"}), monkeypatch)
        assert c.health() is False


class TestAsk:
    def test_ask_returns_message(self, monkeypatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/ask"
            return httpx.Response(200, json={"message": "the reply", "turn": 1})

        c = _client_with(handler, monkeypatch)
        assert c.ask("what is success?") == "the reply"

    def test_ask_raises_on_http_error(self, monkeypatch) -> None:
        """Raises the client's OWN error, not httpx's, so the caller gets the service's reason.

        `talk_to_user` already catches `AppsciAgentUserSimClientError`, and httpx's message is only
        "Server error '500 Internal Server Error' for url ..." -- which is what a real stalled build
        surfaced while the actual cause sat unread in the response body.
        """
        c = _client_with(lambda r: httpx.Response(409, json={"detail": "no active session"}), monkeypatch)
        with pytest.raises(AppsciAgentUserSimClientError, match="no active session"):
            c.ask("q")

    def test_ask_error_names_the_status_and_the_service_detail(self, monkeypatch) -> None:
        c = _client_with(
            lambda r: httpx.Response(500, json={"detail": "could not parse a {message} turn"}),
            monkeypatch,
        )
        with pytest.raises(AppsciAgentUserSimClientError) as caught:
            c.ask("q")

        assert "500" in str(caught.value)
        assert "could not parse a {message} turn" in str(caught.value)

    def test_ask_error_survives_a_non_json_body(self, monkeypatch) -> None:
        """A proxy or crash can return HTML. Still report the status and whatever text there was,
        rather than raising a second error while reporting the first."""
        c = _client_with(lambda r: httpx.Response(502, text="<html>bad gateway</html>"), monkeypatch)
        with pytest.raises(AppsciAgentUserSimClientError, match="502"):
            c.ask("q")

    def test_ask_error_says_so_when_the_body_carries_no_detail(self, monkeypatch) -> None:
        c = _client_with(lambda r: httpx.Response(500, json={}), monkeypatch)
        with pytest.raises(AppsciAgentUserSimClientError, match="no detail in the response body"):
            c.ask("q")
