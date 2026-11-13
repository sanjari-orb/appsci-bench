"""HTTP client for the banking + retrieval tool API.

The artifact reaches every tool through the task-env service (EXPLORATION.md §2.2):

    POST {BANKING_TOOLS_URL}/tools/<name>   body {"arguments": {...}}   -> {"result": ...}

The artifact acts as the **agent**, so it never sets ``requestor: "user"`` -- that value routes to
the customer toolkit. Customer-side actions are handed over with ``give_discoverable_user_tool``, not
by impersonation.

The exact base path is unconfirmed (EXPLORATION.md §8 items 1-2): BANKING_TOOLS_URL may or may not
already include ``/tools``. The client tries ``/tools/<name>`` first, falls back to ``/<name>`` on a
404, and caches whichever shape worked so subsequent calls skip the probe.
"""
from __future__ import annotations

from typing import Any

import httpx

from .config import Config


class BankingToolClient:
    def __init__(self, config: Config, timeout: float = 30.0) -> None:
        self._config = config
        self._client = httpx.Client(
            base_url=config.tools_url,
            headers={"Authorization": f"Bearer {config.tools_token}"},
            timeout=timeout,
        )
        # None until the first successful call reveals which path shape the deployer uses.
        self._path_prefix: str | None = None

    def _candidate_prefixes(self) -> list[str]:
        if self._path_prefix is not None:
            return [self._path_prefix]
        return ["/tools", ""]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a tool. Returns the parsed ``result`` on success.

        On a tool-level or transport error, returns a structured ``{"error": ...}`` dict so the
        caller (the model loop) can feed it back as a tool result rather than crashing the turn.
        """
        body = {"arguments": arguments or {}}
        last_error: str | None = None
        for prefix in self._candidate_prefixes():
            path = f"{prefix}/{name}"
            try:
                resp = self._client.post(path, json=body)
            except httpx.HTTPError as exc:  # transport failure -- try next prefix
                last_error = f"transport error calling {path}: {exc}"
                continue
            if resp.status_code == 404 and self._path_prefix is None:
                # Wrong prefix guess; remember nothing and try the next candidate.
                last_error = f"404 at {path}"
                continue
            self._path_prefix = prefix  # lock in the shape that reached the service
            return self._parse(resp)
        return {"error": last_error or f"tool {name!r} unreachable"}

    @staticmethod
    def _parse(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            return {"error": f"non-JSON response ({resp.status_code}): {resp.text[:500]}"}
        if resp.status_code >= 400:
            return {"error": data.get("detail", data)}
        # Success envelope is {"result": ...}; surface the payload directly.
        return {"result": data.get("result", data)}

    def close(self) -> None:
        self._client.close()
