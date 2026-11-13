"""Test fixtures built from a real snapshot of the task environment.

`db_snapshot.json` was captured from the live container through the tool API (5 users, 10 reservations,
every referenced flight instance status, plus flight searches). Keeping it as captured data rather than
hand-transcribed literals means the tests exercise the real record shapes.

The reservation ids here are development-sample ids only -- the eval store has entirely different ones,
so no agent code may reference them. They appear in tests exclusively.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

from domain import Reservation, User

SNAPSHOT_PATH = pathlib.Path(__file__).with_name("db_snapshot.json")
_SNAPSHOT: dict[str, Any] = json.loads(SNAPSHOT_PATH.read_text())

USERS: dict[str, dict[str, Any]] = _SNAPSHOT["users"]
RESERVATIONS: dict[str, dict[str, Any]] = _SNAPSHOT["reservations"]
# Keyed "FLIGHT|DATE" in the JSON; converted to tuple keys for domain code.
FLIGHT_STATUS: dict[tuple[str, str], str] = {
    tuple(key.split("|")): value for key, value in _SNAPSHOT["flight_status"].items()
}
FLIGHT_SEARCH: dict[str, list[dict[str, Any]]] = _SNAPSHOT["flight_search"]

# Convenience ids, named by what they exercise rather than by customer.
NORA = "nora_castillo_9412"        # gold,    cert + gift + visa
OWEN = "owen_delgado_9137"         # silver,  mastercard + gift
PRIYA = "priya_venkat_9268"        # regular, single visa
QUINN = "quinn_okafor_9503"        # silver,  gift + cert
RAFAEL = "rafael_moreau_9821"      # gold,    mastercard + gift + cert


def user(user_id: str) -> User:
    return User(USERS[user_id])


def reservation(reservation_id: str, *, with_statuses: bool = True) -> Reservation:
    """Build a Reservation view.

    Args:
        reservation_id: e.g. ``"TRN004"``.
        with_statuses: when True (default) every segment carries its real status from the snapshot,
            which is the state policy code requires before it will decide anything. Pass False to
            simulate "statuses not yet read" and assert the verification guards fire.
    """
    raw = RESERVATIONS[reservation_id]
    if not with_statuses:
        return Reservation(raw)
    keys = {(f["flight_number"], f["date"]) for f in raw.get("flights", [])}
    return Reservation(raw, {k: FLIGHT_STATUS[k] for k in keys if k in FLIGHT_STATUS})


def status(flight_number: str, date: str) -> str:
    return FLIGHT_STATUS[(flight_number, date)]


def mutated_reservation(reservation_id: str, **overrides: Any) -> Reservation:
    """A reservation with fields overridden, for cases the fixture doesn't contain naturally."""
    raw = dict(RESERVATIONS[reservation_id])
    raw.update(overrides)
    keys = {(f["flight_number"], f["date"]) for f in raw.get("flights", [])}
    return Reservation(raw, {k: FLIGHT_STATUS[k] for k in keys if k in FLIGHT_STATUS})


def reservation_with_status(reservation_id: str, forced_status: str) -> Reservation:
    """A reservation whose every segment is forced to `forced_status`.

    Used to build cases the 10-record sample doesn't contain, e.g. a delayed segment on a reservation
    owned by a regular member.
    """
    raw = RESERVATIONS[reservation_id]
    keys = {(f["flight_number"], f["date"]) for f in raw.get("flights", [])}
    return Reservation(raw, {k: forced_status for k in keys})
