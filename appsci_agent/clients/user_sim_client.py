"""HTTP client for the appsci-agent-user-sim service -- the agent's downstream view of the API.

The applied-science agent under test runs in an ISOLATED container and reaches the appsci agent user sim
ONLY over HTTP. This client is the entire surface it needs: it reads the connection info the
orchestrator injects (APPSCI_USERSIM_URL + APPSCI_USERSIM_TOKEN) and talks to the service's `/health` and `/ask`
endpoints. See docs/user_simulator_api.md for the full contract.

Deliberately depends on stdlib + httpx ONLY -- never on `appsci_bench` (including its appsci_agent_user_sim
subpackage). That is the trust boundary: the agent cannot see the benchmark internals or the
simulator's hidden persona, it can only ask questions and read answers.

SKELETON: shape + contract are fixed; exercised once a live service exists.
"""
from __future__ import annotations

import os

import httpx


class AppsciAgentUserSimClientError(RuntimeError):
    """Raised when the appsci-agent-user-sim service is unreachable or misconfigured."""


class AppsciAgentUserSimClient:
    """Thin client over the appsci-agent-user-sim HTTP API (one implicit session per run)."""

    def __init__(self, base_url: str | None = None, token: str | None = None, *, timeout: float = 60.0) -> None:
        base_url = base_url or os.environ.get("APPSCI_USERSIM_URL")
        token = token or os.environ.get("APPSCI_USERSIM_TOKEN")
        if not base_url:
            raise AppsciAgentUserSimClientError("APPSCI_USERSIM_URL not set (orchestrator injects it for a run)")
        if not token:
            raise AppsciAgentUserSimClientError("APPSCI_USERSIM_TOKEN not set (orchestrator injects it for a run)")
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def health(self) -> bool:
        """Return True once the service is ready (GET /health -> 200)."""
        resp = self._client.get("/health")
        return resp.status_code == 200 and resp.json().get("status") == "ok"

    def ask(self, question: str, parent_span_id: str | None = None) -> str:
        """Ask the stakeholder one question; return the reply text (POST /ask -> {message, turn}).

        The conversation is implicit and server-side: send only the question, the service remembers
        prior turns. The simulator never ends the conversation -- stopping is the agent's call.

        ``parent_span_id`` (optional) is forwarded as the ``X-Obs-Parent-Span`` header so the service
        can parent its observability events under the agent's talk_to_user tool call. It is trace
        metadata only -- it never reaches the persona or influences the reply.
        """
        headers = {"X-Obs-Parent-Span": parent_span_id} if parent_span_id else None
        resp = self._client.post("/ask", json={"question": question}, headers=headers)
        if resp.is_error:
            # Carry the service's OWN reason, which it puts in the JSON body's `detail`. Bare
            # `raise_for_status()` produced only httpx's "Server error '500 Internal Server Error' for
            # url ..." -- and since the service logged nothing either, the actual cause (e.g. the
            # persona model answering in prose instead of the {message} JSON) existed nowhere at all:
            # not in the agent's tool result, not in either container's log, not in the run dir. A
            # build stalled on exactly this and the reason had to be reconstructed by hand.
            detail = ""
            try:
                body = resp.json()
                detail = body.get("detail") or body.get("message") or ""
            except Exception:  # noqa: BLE001 -- a non-JSON error body is still worth reporting
                detail = (resp.text or "").strip()[:500]
            raise AppsciAgentUserSimClientError(
                f"user-sim returned HTTP {resp.status_code} for POST /ask"
                + (f": {detail}" if detail else " (no detail in the response body)")
            )
        return resp.json()["message"]

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AppsciAgentUserSimClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
