"""Transport for the Rho-Bank tool API.

Everything here is fully documented by TASK_BRIEF.yaml and needs no guessed names:

    POST {BANKING_TOOLS_URL}/<tool_name>   body: {"arguments": {...}}
      -> 200 {"result": ...}               (success envelope)
    Authorization: Bearer {BANKING_TOOLS_TOKEN}

This module provides:
  * `ToolClient.call(name, arguments)` — invoke a named tool, returning a structured `ToolResult`
    that faithfully distinguishes success from each failure class (auth, not-found, server, network,
    bad-envelope). Failures are represented, never masked as success.
  * `ToolClient.probe_discovery()` — a read-only, non-mutating probe that checks whether the base URL
    exposes any listing/manifest endpoint, distinguishing 404 (no manifest there) from 401/403 (auth
    issue) from 5xx/timeout (transient) from 2xx (candidate). It never assumes a manifest exists and
    never guesses tool names.

The bearer token is only ever placed in the Authorization header; it is never logged or returned in
a ToolResult.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

import config

# Longest response-body snippet we keep on a failure, so validation details (e.g. an HTTP 400 that
# names a required field) are surfaced rather than discarded, without dumping unbounded payloads.
_DETAIL_LIMIT = 800

# Result kinds. `ok` is the only success; everything else is a faithfully-surfaced failure class.
KIND_OK = "ok"
KIND_AUTH_ERROR = "auth_error"          # 401 / 403
KIND_NOT_FOUND = "not_found"            # 404
KIND_SERVER_ERROR = "server_error"      # 5xx
KIND_HTTP_ERROR = "http_error"          # other non-2xx
KIND_NETWORK_ERROR = "network_error"    # connection/timeout, no HTTP response
KIND_BAD_RESPONSE = "bad_response"      # 2xx but not the documented {"result": ...} envelope


@dataclass
class ToolResult:
    """Structured outcome of a tool call. `ok` gates success; failures are never dressed as success."""

    ok: bool
    tool: str
    kind: str
    result: Any = None
    error: Optional[str] = None
    status_code: Optional[int] = None
    # Redacted, truncated response-body text for a failure. Carries useful server feedback such as an
    # HTTP 400 validation message naming the required argument fields. None on success.
    detail: Optional[str] = None

    def unwrap(self) -> Any:
        """Return the result on success, else raise — for call sites that must not proceed on failure."""
        if not self.ok:
            raise ToolCallError(self)
        return self.result


class ToolCallError(RuntimeError):
    def __init__(self, result: ToolResult) -> None:
        self.result = result
        super().__init__(
            f"tool {result.tool!r} failed ({result.kind}"
            + (f", status={result.status_code}" if result.status_code is not None else "")
            + f"): {result.error}"
        )


@dataclass
class ProbeFinding:
    """Classification of one read-only probe request against the tool API base URL."""

    path: str
    method: str
    kind: str
    status_code: Optional[int] = None
    note: Optional[str] = None


@dataclass
class DiscoveryProbe:
    """Aggregate read-only discovery result. `manifest_candidate` is True only on a 2xx listing-shaped
    response; otherwise findings record *why* (not-found vs auth vs transient) so we never collapse
    every non-2xx into 'no manifest'."""

    manifest_candidate: bool
    findings: List[ProbeFinding] = field(default_factory=list)


class ToolClient:
    """Thin, faithful proxy over the documented tool API transport."""

    def __init__(
        self,
        base_url: str,
        token: str,
        client: Optional[httpx.Client] = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        # Allow injecting a client (tests use httpx.MockTransport); otherwise create a real one.
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ToolClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _headers(self) -> Dict[str, str]:
        # The token appears only here. Never log this dict.
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _body_snippet(resp: httpx.Response) -> Optional[str]:
        """Return a redacted, truncated snippet of a response body for error surfacing.

        The tool server's error responses (notably HTTP 400 validation errors) name the required
        argument fields; keeping this snippet is how discovery learns those names without guessing.
        We redact the banking token defensively even though a tool response should not echo it, and
        we cap the length so a large body cannot flood a log or response.
        """
        try:
            text = resp.text
        except Exception:  # noqa: BLE001 -- body may be undecodable; treat as absent
            return None
        if not text:
            return None
        text = config.redact(text)
        if len(text) > _DETAIL_LIMIT:
            text = text[:_DETAIL_LIMIT] + "...(truncated)"
        return text

    def call(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> ToolResult:
        """POST {base}/<name> with {"arguments": {...}} and classify the outcome."""
        url = f"{self._base_url}/{name}"
        payload = {"arguments": arguments or {}}
        try:
            resp = self._client.post(url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            return ToolResult(False, name, KIND_NETWORK_ERROR, error=f"timeout: {exc!r}")
        except httpx.HTTPError as exc:
            # Connection errors etc. — no HTTP response at all. Do not leak the token in the message.
            return ToolResult(False, name, KIND_NETWORK_ERROR, error=f"network error: {exc!r}")

        status = resp.status_code
        if 200 <= status < 300:
            # 2xx: expect the documented {"result": ...} envelope.
            try:
                body = resp.json()
            except ValueError:
                return ToolResult(
                    False, name, KIND_BAD_RESPONSE, status_code=status,
                    error="response body was not JSON", detail=self._body_snippet(resp),
                )
            if not isinstance(body, dict) or "result" not in body:
                return ToolResult(
                    False, name, KIND_BAD_RESPONSE, status_code=status,
                    error="response JSON lacked the documented 'result' field",
                    detail=self._body_snippet(resp),
                )
            return ToolResult(True, name, KIND_OK, result=body["result"], status_code=status)

        # Non-2xx: capture the (redacted, truncated) body so validation details are not discarded.
        detail = self._body_snippet(resp)
        if status in (401, 403):
            return ToolResult(
                False, name, KIND_AUTH_ERROR, status_code=status,
                error="authentication/authorization failed", detail=detail,
            )
        if status == 404:
            return ToolResult(
                False, name, KIND_NOT_FOUND, status_code=status,
                error="tool endpoint not found", detail=detail,
            )
        if 500 <= status < 600:
            return ToolResult(
                False, name, KIND_SERVER_ERROR, status_code=status,
                error="tool server error", detail=detail,
            )
        return ToolResult(
            False, name, KIND_HTTP_ERROR, status_code=status,
            error=f"unexpected HTTP status {status}", detail=detail,
        )

    def _probe_get(self, path: str) -> ProbeFinding:
        """One read-only GET, classified. Never mutates anything."""
        url = self._base_url if path == "" else f"{self._base_url}/{path.lstrip('/')}"
        try:
            resp = self._client.get(url, headers=self._headers())
        except httpx.TimeoutException as exc:
            return ProbeFinding(path or "/", "GET", KIND_NETWORK_ERROR, note=f"timeout: {exc!r}")
        except httpx.HTTPError as exc:
            return ProbeFinding(path or "/", "GET", KIND_NETWORK_ERROR, note=f"network error: {exc!r}")

        status = resp.status_code
        if status in (401, 403):
            return ProbeFinding(path or "/", "GET", KIND_AUTH_ERROR, status_code=status,
                                note="auth/permission issue, NOT proof of missing manifest")
        if status == 404:
            return ProbeFinding(path or "/", "GET", KIND_NOT_FOUND, status_code=status,
                                note="no endpoint at this path")
        if 500 <= status < 600:
            return ProbeFinding(path or "/", "GET", KIND_SERVER_ERROR, status_code=status,
                                note="transient server error")
        if 200 <= status < 300:
            return ProbeFinding(path or "/", "GET", KIND_OK, status_code=status,
                                note="2xx — possible manifest/listing, inspect manually")
        return ProbeFinding(path or "/", "GET", KIND_HTTP_ERROR, status_code=status)

    def probe_discovery(self, paths: Optional[List[str]] = None) -> DiscoveryProbe:
        """Read-only probe for a listing/manifest endpoint. Assumes nothing; guesses no tool names.

        We try only the base URL and a few *conventional* read-only listing paths. A 2xx on any of
        them marks a manifest *candidate* (to inspect by hand); everything else is recorded with its
        class so a 404 is not confused with an auth failure or a transient 5xx.
        """
        if paths is None:
            # Conventional, non-mutating listing paths only. Not tool names — just where a manifest
            # might conventionally live if one exists at all.
            paths = ["", "tools", "manifest", "openapi.json", "docs", ".well-known/tools"]
        findings = [self._probe_get(p) for p in paths]
        candidate = any(f.kind == KIND_OK for f in findings)
        return DiscoveryProbe(manifest_candidate=candidate, findings=findings)
