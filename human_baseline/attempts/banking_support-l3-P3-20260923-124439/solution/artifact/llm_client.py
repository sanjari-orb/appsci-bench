"""Anthropic Messages client for the Rho-Bank agent core.

This wraps the ONE interface confirmed by the runtime connectivity probe (`diagnostic.py llm`,
HTTP 200): the deployer injects ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / APPSCI_AGENT_CUSTOM_HEADERS,
and a POST to `{base}/v1/messages` with model `claude-opus-4-8` over the Anthropic Messages protocol
succeeds.

Responsibilities are deliberately narrow: turn a normalized (system, messages, tools) request into
that HTTP call and parse the response into a normalized `LLMResponse`. It contains NO agent logic
(that is agent_core.py) and hard-codes no tool. Credentials live only in headers and are never logged;
failures are raised as `LLMError` with redacted, non-secret messages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

import config

_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 2048
_DEFAULT_TIMEOUT = 60.0
_ERR_LIMIT = 800


@dataclass
class ToolCall:
    """A single tool_use block the model emitted."""

    id: str
    name: str
    input: Dict[str, Any]


@dataclass
class LLMResponse:
    """Normalized model turn: concatenated text, any tool calls, and the raw content blocks.

    `raw_content` is the model's original `content` array; the agent loop echoes it back verbatim as
    the assistant turn when continuing a tool-use exchange (Anthropic requires the tool_use blocks to
    be replayed alongside their tool_result blocks).
    """

    text: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    stop_reason: Optional[str] = None
    raw_content: List[Any] = field(default_factory=list)


class LLMError(RuntimeError):
    """A faithful, non-secret LLM failure. Never carries a credential value."""

    def __init__(self, kind: str, message: str, status_code: Optional[int] = None) -> None:
        self.kind = kind
        self.status_code = status_code
        super().__init__(message)


class AnthropicMessagesClient:
    """Minimal Anthropic Messages transport over the confirmed runtime interface."""

    def __init__(
        self,
        base_url: str,
        token: str,
        custom_headers: Optional[Dict[str, str]] = None,
        model: Optional[str] = None,
        client: Optional[httpx.Client] = None,
        timeout: float = _DEFAULT_TIMEOUT,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._custom = dict(custom_headers or {})
        self._model = model or config.agent_model()
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._max_tokens = max_tokens

    @classmethod
    def from_config(cls, client: Optional[httpx.Client] = None) -> "AnthropicMessagesClient":
        """Build from the runtime env (raises if the Anthropic vars are missing)."""
        base_url, token = config.require_anthropic()
        return cls(
            base_url,
            token,
            custom_headers=config.anthropic_custom_headers(),
            model=config.agent_model(),
            client=client,
        )

    @property
    def model(self) -> str:
        return self._model

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "AnthropicMessagesClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _headers(self) -> Dict[str, str]:
        # The token appears only here. Never log this dict.
        headers = {
            "Authorization": f"Bearer {self._token}",
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        headers.update(self._custom)  # proxy/routing headers; values never printed
        return headers

    def create_message(
        self,
        *,
        system: str,
        messages: List[Any],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
    ) -> LLMResponse:
        """POST one Messages request and return the normalized response."""
        payload: Dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens or self._max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
        url = f"{self._base_url}/v1/messages"

        try:
            resp = self._client.post(url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            # Connection/timeout: no HTTP response. Redact and never leak the token.
            raise LLMError("network_error", config.redact(f"{type(exc).__name__}: {exc}")) from exc

        if not (200 <= resp.status_code < 300):
            snippet = config.redact((resp.text or "")[:_ERR_LIMIT])
            raise LLMError("http_error", f"LLM HTTP {resp.status_code}: {snippet}", resp.status_code)

        try:
            body = resp.json()
        except ValueError as exc:
            raise LLMError("bad_response", "LLM response body was not JSON") from exc
        return self._parse(body)

    @staticmethod
    def _parse(body: Any) -> LLMResponse:
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, list):
            raise LLMError("bad_response", "LLM response missing 'content' blocks")
        texts: List[str] = []
        calls: List[ToolCall] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(str(block.get("text", "")))
            elif btype == "tool_use":
                raw_input = block.get("input")
                calls.append(
                    ToolCall(
                        id=str(block.get("id", "")),
                        name=str(block.get("name", "")),
                        input=raw_input if isinstance(raw_input, dict) else {},
                    )
                )
        return LLMResponse(
            text="".join(texts),
            tool_calls=calls,
            stop_reason=body.get("stop_reason") if isinstance(body, dict) else None,
            raw_content=content,
        )
