"""An in-memory stand-in for `EnvClient`, backed by the captured snapshot.

It mirrors the real environment's *behaviour*, not just its data:

* reads come from the snapshot;
* writes mutate a local copy and are recorded in `writes` for assertions;
* unknown ids raise `ToolRejection`, matching the service's HTTP 400;
* `cancel_reservation` is write-once, matching the real client's guard.

Tests assert on `fake.writes` to prove an out-of-policy conversation performed **no** mutation.
"""
from __future__ import annotations

import copy
from typing import Any

from env_client import TRANSFER_TOOL, WRITE_TOOLS, ToolCallError, ToolRejection
from tests import fixtures as fx


class FakeEnvClient:
    """Snapshot-backed fake with the same surface as `EnvClient`."""

    def __init__(self) -> None:
        self.users: dict[str, Any] = copy.deepcopy(fx.USERS)
        self.reservations: dict[str, Any] = copy.deepcopy(fx.RESERVATIONS)
        self.statuses: dict[tuple[str, str], str] = dict(fx.FLIGHT_STATUS)
        self.searches: dict[str, Any] = copy.deepcopy(fx.FLIGHT_SEARCH)
        self.writes: list[dict[str, Any]] = []
        self.call_log: list[dict[str, Any]] = []
        self.transfer_summaries: list[str] = []
        self._cancelled: set[str] = set()

    # --- dispatch ----------------------------------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        arguments = arguments or {}
        self.call_log.append({"tool": name, "arguments": arguments, "is_write": name in WRITE_TOOLS})
        # Prefer the `_do_*` endpoint when one exists, so the public wrapper of the same name can
        # route through here without recursing.
        handler = getattr(self, f"_do_{name}", None) or getattr(self, name, None)
        if handler is None:
            raise ToolCallError(name, f"Unknown tool {name}")
        result = handler(**arguments)
        if name in WRITE_TOOLS:
            self.writes.append({"tool": name, "arguments": arguments})
        return result

    # --- reads -------------------------------------------------------------------------------------
    # The `_do_*` methods are the endpoints (what the service itself would execute); the public
    # wrappers below route through `call()` so every read lands in `call_log`, exactly as the real
    # EnvClient does. Tests rely on that log to prove the agent really verified a fact.
    def _do_get_user_details(self, user_id: str) -> dict[str, Any]:
        if user_id not in self.users:
            raise ToolRejection("get_user_details", f"User {user_id} not found")
        return self.users[user_id]

    def _do_get_reservation_details(self, reservation_id: str) -> dict[str, Any]:
        if reservation_id not in self.reservations:
            raise ToolRejection(
                "get_reservation_details", f"Reservation {reservation_id} not found"
            )
        return self.reservations[reservation_id]

    def _do_get_flight_status(self, flight_number: str, date: str) -> str:
        key = (flight_number, date)
        if key not in self.statuses:
            raise ToolRejection(
                "get_flight_status", f"Flight {flight_number} not found on date {date}"
            )
        return self.statuses[key]

    def get_user_details(self, user_id: str) -> dict[str, Any]:
        return self.call("get_user_details", {"user_id": user_id})

    def get_reservation_details(self, reservation_id: str) -> dict[str, Any]:
        return self.call("get_reservation_details", {"reservation_id": reservation_id})

    def get_flight_status(self, flight_number: str, date: str) -> str:
        return self.call(
            "get_flight_status", {"flight_number": flight_number, "date": date}
        )

    def search_direct_flight(self, origin: str, destination: str, date: str) -> list[dict[str, Any]]:
        return self.searches.get(f"{origin}->{destination}|{date}", [])

    def search_onestop_flight(self, origin: str, destination: str, date: str) -> list[dict[str, Any]]:
        # Mirrors the environment's stub: direct flights only.
        return self.search_direct_flight(origin, destination, date)

    def list_all_airports(self) -> list[str]:
        return list(fx._SNAPSHOT["airports"])

    def calculate(self, expression: str) -> str:
        allowed = set("0123456789+-*/(). ")
        if not set(expression) <= allowed:
            raise ToolRejection("calculate", "Invalid characters in expression")
        return str(round(float(eval(expression, {"__builtins__": {}}, {})), 2))

    def transfer_to_human_agents(self, summary: str) -> str:
        self.transfer_summaries.append(summary)
        return "Transfer successful"

    # --- writes ------------------------------------------------------------------------------------
    def cancel_reservation(self, reservation_id: str) -> dict[str, Any]:
        if reservation_id in self._cancelled:
            raise ToolCallError(
                "cancel_reservation",
                f"Reservation {reservation_id} was already cancelled in this conversation",
            )
        reservation = self._do_get_reservation_details(reservation_id)
        refunds = [
            {"payment_id": p["payment_id"], "amount": -p["amount"]}
            for p in reservation.get("payment_history", [])
        ]
        reservation["payment_history"] = list(reservation.get("payment_history", [])) + refunds
        reservation["status"] = "cancelled"
        self._cancelled.add(reservation_id)
        return reservation

    def send_certificate(self, user_id: str, amount: int) -> str:
        user = self._do_get_user_details(user_id)
        pid = f"certificate_322132{len(self._cancelled) + 2}"
        user.setdefault("payment_methods", {})[pid] = {
            "id": pid,
            "amount": amount,
            "source": "certificate",
        }
        return f"Certificate {pid} added to user {user_id} with amount {amount}."

    def update_reservation_baggages(
        self, reservation_id: str, total_baggages: int, nonfree_baggages: int, payment_id: str
    ) -> dict[str, Any]:
        reservation = self._do_get_reservation_details(reservation_id)
        reservation["total_baggages"] = total_baggages
        reservation["nonfree_baggages"] = nonfree_baggages
        return reservation

    def update_reservation_passengers(
        self, reservation_id: str, passengers: list[dict[str, Any]]
    ) -> dict[str, Any]:
        reservation = self._do_get_reservation_details(reservation_id)
        if len(passengers) != len(reservation.get("passengers", [])):
            raise ToolRejection(
                "update_reservation_passengers", "Number of passengers does not match"
            )
        reservation["passengers"] = copy.deepcopy(passengers)
        return reservation

    def update_reservation_flights(
        self, reservation_id: str, cabin: str, flights: list[dict[str, Any]], payment_id: str
    ) -> dict[str, Any]:
        reservation = self._do_get_reservation_details(reservation_id)
        reservation["cabin"] = cabin
        reservation["flights"] = copy.deepcopy(flights)
        return reservation

    def book_reservation(self, **kwargs: Any) -> dict[str, Any]:
        rid = "HATHAT" if "HATHAT" not in self.reservations else "HATHAU"
        reservation = {"reservation_id": rid, **copy.deepcopy(kwargs)}
        self.reservations[rid] = reservation
        return reservation

    # --- helpers mirroring EnvClient ----------------------------------------------------------------
    def segment_statuses(self, reservation: dict[str, Any]) -> dict[tuple[str, str], str]:
        """Read each segment's status *through* `call()`, as the real client does.

        Going through `call()` rather than straight to the handler is what makes the verification
        visible in `call_log` -- tests assert that the agent really did check flight status instead of
        taking the customer's word.
        """
        statuses: dict[tuple[str, str], str] = {}
        for flight in reservation.get("flights") or []:
            key = (flight.get("flight_number", ""), flight.get("date", ""))
            try:
                statuses[key] = self.call(
                    "get_flight_status", {"flight_number": key[0], "date": key[1]}
                )
            except ToolRejection:
                statuses[key] = "unknown"
        return statuses

    def health(self) -> bool:
        return True

    @property
    def write_tools_called(self) -> list[str]:
        return [w["tool"] for w in self.writes]
