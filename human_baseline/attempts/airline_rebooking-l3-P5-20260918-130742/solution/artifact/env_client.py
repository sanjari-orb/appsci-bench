"""HTTP client for the task environment's 14 tools.

Three behaviours matter beyond plain HTTP:

1. **400 vs 500.** The service maps a `ToolError` (a real precondition failure, e.g. "Flight not
   available") to HTTP 400. A *missing argument* raises `TypeError` inside the service and surfaces as
   HTTP 500. A 400 is business information worth relaying; a 500 is our own malformed call and must
   never be shown to a customer. They are therefore distinct exception types.
2. **Write-once guard on cancellation.** `cancel_reservation` is not idempotent -- each call appends
   another set of negated refund rows. The client refuses a second cancel of the same reservation
   within a conversation.
3. **Every call is logged** with the correlation id, so a conversation's tool trace can be replayed.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from logging_setup import get_logger

log = get_logger("env_client")

# Tools that mutate the DB. These are the only calls the scoring oracle can see.
WRITE_TOOLS = frozenset({
    "book_reservation",
    "update_reservation_flights",
    "update_reservation_baggages",
    "update_reservation_passengers",
    "cancel_reservation",
    "send_certificate",
})

READ_TOOLS = frozenset({
    "get_user_details",
    "get_reservation_details",
    "search_direct_flight",
    "search_onestop_flight",
    "list_all_airports",
    "get_flight_status",
    "calculate",
})

TRANSFER_TOOL = "transfer_to_human_agents"
ALL_TOOLS = WRITE_TOOLS | READ_TOOLS | {TRANSFER_TOOL}


class ToolRejection(Exception):
    """HTTP 400 -- the environment refused on a precondition. Safe to relay in substance."""

    def __init__(self, tool: str, detail: str):
        super().__init__(detail)
        self.tool = tool
        self.detail = detail


class ToolCallError(Exception):
    """A malformed call or transport failure (HTTP 500, timeout, bad JSON). Our bug, not the customer's."""

    def __init__(self, tool: str, detail: str, status: int | None = None):
        super().__init__(detail)
        self.tool = tool
        self.detail = detail
        self.status = status


class EnvClient:
    """Thin, synchronous client over `POST /tools/{name}`.

    Args:
        base_url: e.g. ``http://airline-workspace-1:8080``.
        token: the tool bearer token.
        timeout: per-request timeout in seconds.
    """

    def __init__(self, base_url: str, token: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # Reservations already cancelled in this conversation, to keep cancel_reservation write-once.
        self._cancelled: set[str] = set()
        self.call_log: list[dict[str, Any]] = []

    # --- transport ---------------------------------------------------------------------------------
    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read() or "null")

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Invoke a tool and return its result.

        Raises:
            ToolRejection: the environment refused (HTTP 400).
            ToolCallError: unknown tool, malformed call, or transport failure.
        """
        if name not in ALL_TOOLS:
            raise ToolCallError(name, f"Unknown tool {name}")

        arguments = arguments or {}

        # Write-once guard: a second cancel would corrupt payment_history.
        if name == "cancel_reservation":
            reservation_id = arguments.get("reservation_id", "")
            if reservation_id in self._cancelled:
                raise ToolCallError(
                    name, f"Reservation {reservation_id} was already cancelled in this conversation"
                )

        try:
            body = self._post(f"/tools/{name}", {"arguments": arguments})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            detail = raw
            try:
                detail = json.loads(raw).get("detail", raw)
            except json.JSONDecodeError:
                pass
            self._record(name, arguments, error=detail, status=exc.code)
            if exc.code == 400:
                # A genuine precondition failure from the tool.
                raise ToolRejection(name, str(detail)) from exc
            raise ToolCallError(name, str(detail), status=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            self._record(name, arguments, error=str(exc))
            raise ToolCallError(name, str(exc)) from exc

        result = body.get("result") if isinstance(body, dict) else body
        if name == "cancel_reservation":
            self._cancelled.add(arguments.get("reservation_id", ""))
        self._record(name, arguments, result=result)
        return result

    def _record(
        self,
        tool: str,
        arguments: dict[str, Any],
        result: Any = None,
        error: str | None = None,
        status: int | None = None,
    ) -> None:
        entry = {
            "tool": tool,
            "arguments": arguments,
            "is_write": tool in WRITE_TOOLS,
            "error": error,
            "status": status,
        }
        self.call_log.append(entry)
        if error:
            log.warning("tool call failed", extra={**entry, "result": None})
        else:
            log.info("tool call ok", extra=entry)

    # --- typed convenience wrappers ---------------------------------------------------------------
    def get_user_details(self, user_id: str) -> dict[str, Any]:
        return self.call("get_user_details", {"user_id": user_id})

    def get_reservation_details(self, reservation_id: str) -> dict[str, Any]:
        return self.call("get_reservation_details", {"reservation_id": reservation_id})

    def get_flight_status(self, flight_number: str, date: str) -> str:
        return self.call("get_flight_status", {"flight_number": flight_number, "date": date})

    def search_direct_flight(self, origin: str, destination: str, date: str) -> list[dict[str, Any]]:
        return self.call(
            "search_direct_flight", {"origin": origin, "destination": destination, "date": date}
        )

    def list_all_airports(self) -> list[str]:
        return self.call("list_all_airports", {})

    def calculate(self, expression: str) -> str:
        return self.call("calculate", {"expression": expression})

    def transfer_to_human_agents(self, summary: str) -> str:
        return self.call(TRANSFER_TOOL, {"summary": summary})

    def segment_statuses(self, reservation: dict[str, Any]) -> dict[tuple[str, str], str]:
        """Read the live status of every segment on a reservation.

        This is the call that turns a customer's claim into a verified fact. A segment whose dated
        instance no longer exists is recorded as ``"unknown"`` rather than silently omitted, so the
        caller can still see that it was checked.
        """
        statuses: dict[tuple[str, str], str] = {}
        for flight in reservation.get("flights") or []:
            key = (flight.get("flight_number", ""), flight.get("date", ""))
            try:
                statuses[key] = self.get_flight_status(*key)
            except ToolRejection:
                statuses[key] = "unknown"
        return statuses

    def health(self) -> bool:
        try:
            request = urllib.request.Request(
                f"{self.base_url}/health", headers={"Authorization": f"Bearer {self.token}"}
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read()).get("status") == "ok"
        except Exception:
            return False
