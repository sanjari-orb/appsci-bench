"""Anthropic Messages client, pointed at whatever endpoint the run provides.

Deliberately a thin client over httpx rather than the vendor SDK: the endpoint here is usually a
gateway that requires an identity header and Bearer auth, and hand-rolling the request keeps those
under our control instead of depending on how a particular SDK version forwards them. Nothing is read
at import time -- the deployer injects these per run.
"""
from __future__ import annotations

import os
import time

import httpx

_TIMEOUT = httpx.Timeout(120.0, connect=15.0)
_MAX_ATTEMPTS = 4
_RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class ModelError(RuntimeError):
    pass


def _base_url() -> str:
    for var in ("ANTHROPIC_BASE_URL", "APPSCI_AGENT_BASE_URL"):
        value = (os.environ.get(var) or "").strip()
        if value:
            return value.rstrip("/")
    return "https://api.anthropic.com"


def _headers() -> dict[str, str]:
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}

    # A gateway token goes in Authorization; a first-party key goes in x-api-key. Check the gateway
    # pair first, since when both are present the gateway is the one that will actually work.
    token = (os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("APPSCI_AGENT_API_KEY") or "").strip()
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif api_key:
        headers["x-api-key"] = api_key

    # Gateway identity headers, "Header: value" separated by "|" or newlines. Omitting them can get
    # the request rejected outright.
    raw = (
        os.environ.get("APPSCI_AGENT_CUSTOM_HEADERS")
        or os.environ.get("ANTHROPIC_CUSTOM_HEADERS")
        or ""
    )
    for part in raw.replace("|", "\n").split("\n"):
        if ":" in part:
            name, value = part.split(":", 1)
            if name.strip():
                headers[name.strip()] = value.strip()
    return headers


def model_name() -> str:
    return (os.environ.get("ARTIFACT_MODEL") or "claude-opus-4-8").strip()


def create(
    messages: list[dict],
    system: str,
    tool_schemas: list[dict] | None = None,
    max_tokens: int = 2048,
) -> dict:
    """One Messages call. Returns the parsed response body.

    `disable_parallel_tool_use` asks the model for a single tool call per turn. It is honoured by the
    endpoints used here, but the caller enforces the rule regardless -- policy requires one call per
    turn and some gateways reject a turn carrying several, so this flag is a courtesy, not the
    guarantee.
    """
    body: dict = {
        "model": model_name(),
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    if tool_schemas:
        body["tools"] = tool_schemas
        body["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}

    url = f"{_base_url()}/v1/messages"
    last_error = ""
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = httpx.post(url, headers=_headers(), json=body, timeout=_TIMEOUT)
        except httpx.HTTPError as exc:
            last_error = f"transport error: {exc}"
        else:
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ModelError(f"model returned a non-JSON body: {exc}") from exc
            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            if response.status_code not in _RETRY_STATUS:
                raise ModelError(last_error)
        if attempt < _MAX_ATTEMPTS - 1:
            time.sleep(1.5 * (2**attempt))
    raise ModelError(f"model call failed after {_MAX_ATTEMPTS} attempts -- {last_error}")


def text_of(response: dict) -> str:
    """Concatenate the text blocks of a response, ignoring tool_use and thinking blocks."""
    parts = [
        block.get("text", "")
        for block in response.get("content") or []
        if block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part.strip()).strip()


def tool_uses(response: dict) -> list[dict]:
    return [b for b in response.get("content") or [] if b.get("type") == "tool_use"]
