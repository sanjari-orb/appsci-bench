"""Test doubles for the banking tool API.

We drive `ToolClient` through an in-memory `httpx.MockTransport`, so no real port or network is used.
A `Recorder` captures each request the client makes -- method, path, Authorization header, and parsed
body -- so tests can assert the wire shape (documented {"arguments": ...} payload, bearer header) and
confirm the token is never exposed anywhere but that header.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

import httpx


class Recorder:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def record(self, request: httpx.Request) -> None:
        raw = request.content
        try:
            body: Any = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode(errors="replace")
        self.calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "authorization": request.headers.get("authorization"),
                "body": body,
            }
        )


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    recorder: Optional[Recorder] = None,
) -> httpx.Client:
    """Build an httpx.Client whose transport is the given handler, optionally recording every call."""

    def wrapped(request: httpx.Request) -> httpx.Response:
        if recorder is not None:
            recorder.record(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(wrapped), base_url="http://tools.test")


def static_handler(
    status_code: int,
    json_body: Any = None,
    text_body: Optional[str] = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """A handler that always returns the same response, for a whole request class."""

    def handler(request: httpx.Request) -> httpx.Response:
        if text_body is not None:
            return httpx.Response(status_code, text=text_body)
        return httpx.Response(status_code, json=json_body)

    return handler


def raising_handler(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    """A handler that raises, to simulate connection/timeout failures with no HTTP response."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler
