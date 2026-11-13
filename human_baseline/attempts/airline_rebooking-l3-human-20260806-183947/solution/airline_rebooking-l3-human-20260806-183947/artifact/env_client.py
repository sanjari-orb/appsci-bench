"""HTTP client for the airline task environment's tool API.

The environment exposes every tool at `POST {base_url}/tools/{name}` with a bearer token, and
surfaces a failed precondition as HTTP 400 with a `detail` message. A 400 is *information for
the agent* ("that flight is not available on that date"), not a server fault, so `call` returns
it as a normal error result rather than raising.

The read helpers exist so the policy guard can fetch the records it needs to judge a write,
independently of whether the model happened to look them up itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

import config


@dataclass
class ToolResult:
    """Outcome of one tool call: `ok` False carries the message the agent should see."""

    ok: bool
    payload: Any


class EnvClient:
    """Calls the airline tools over HTTP. Pass `client` to target an in-process ASGI app."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.base_url = (base_url or config.ENV_BASE_URL).rstrip("/")
        self.token = token or config.ENV_TOOL_TOKEN
        self._client = client or httpx.Client(timeout=config.ENV_TIMEOUT_SECONDS)

    def call(self, name: str, arguments: dict) -> ToolResult:
        """Invoke a tool. Returns ok=False with a readable message on any failure."""
        try:
            resp = self._client.post(
                f"{self.base_url}/tools/{name}",
                json={"arguments": arguments or {}},
                headers={"Authorization": f"Bearer {self.token}"},
            )
        except httpx.HTTPError as exc:
            return ToolResult(False, f"Could not reach the reservation system: {exc}")

        if resp.status_code == 200:
            return ToolResult(True, resp.json().get("result"))
        if resp.status_code == 400:
            # A tool precondition failed; the message is meant for the agent to relay.
            return ToolResult(False, _detail(resp))
        return ToolResult(
            False, f"Reservation system error (HTTP {resp.status_code}): {_detail(resp)}"
        )

    # --- read helpers used by the policy guard ----------------------------------------------
    def get_reservation(self, reservation_id: str) -> dict | None:
        """Fetch a reservation record, or None if it does not exist."""
        res = self.call("get_reservation_details", {"reservation_id": reservation_id})
        return res.payload if res.ok and isinstance(res.payload, dict) else None

    def get_user(self, user_id: str) -> dict | None:
        """Fetch a user profile, or None if it does not exist."""
        res = self.call("get_user_details", {"user_id": user_id})
        return res.payload if res.ok and isinstance(res.payload, dict) else None

    def get_flight_status(self, flight_number: str, date: str) -> str | None:
        """Return a flight instance's status on a date, or None if there is no such instance."""
        res = self.call("get_flight_status", {"flight_number": flight_number, "date": date})
        return res.payload if res.ok else None

    def find_available_flight(self, origin: str, destination: str, date: str,
                              flight_number: str) -> dict | None:
        """Find one bookable flight on a known route, with its seats and prices.

        `search_direct_flight` is the only tool that returns seats and prices, and it only lists
        instances whose status is "available" -- so a None result means "not bookable", which is
        exactly what the guard needs to know before promising a change or an upgrade.
        """
        res = self.call(
            "search_direct_flight",
            {"origin": origin, "destination": destination, "date": date},
        )
        if not res.ok:
            return None
        for flight in res.payload or []:
            if flight.get("flight_number") == flight_number:
                return flight
        return None


def _detail(resp: httpx.Response) -> str:
    """Pull the `detail` message out of an error response, falling back to the raw body."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip()
    return str(body.get("detail", body)) if isinstance(body, dict) else str(body)
