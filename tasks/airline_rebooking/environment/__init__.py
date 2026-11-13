"""Airline task environment: an in-memory FlightDB + the 14 tools the artifact drives.

This is the "real resource" the airline artifact acts against at eval time. Semantics mirror
tau2-bench's airline domain (see provenance/airline/AIRLINE_PROVENANCE.md).
A fresh environment is bound per eval case (isolated DB copy); the DB-state oracle compares the
post-run state_hash() against a golden replay.

    env = AirlineEnvironment.from_fixture()      # load the materialized fixture once
    case = env.fresh()                           # isolated copy for one case
    case.tools.execute("cancel_reservation", {"reservation_id": "EHGLP3"})
    case.state_hash()                            # fingerprint for scoring
"""
from __future__ import annotations

import pathlib

from .db import AirlineDB, DEFAULT_DB_PATH
from .tools import AirlineTools, NON_WRITE, READ_ONLY, ToolError

TOOL_NAMES = [
    "book_reservation", "update_reservation_flights", "update_reservation_baggages",
    "update_reservation_passengers", "cancel_reservation", "send_certificate",
    "get_user_details", "get_reservation_details", "search_direct_flight", "search_onestop_flight",
    "list_all_airports", "get_flight_status", "calculate", "transfer_to_human_agents",
]


class AirlineEnvironment:
    """A bound (DB + tools) airline environment for one eval case."""

    def __init__(self, db: AirlineDB):
        self.db = db
        self.tools = AirlineTools(db)

    @classmethod
    def from_fixture(cls, path: str | pathlib.Path | None = None) -> "AirlineEnvironment":
        return cls(AirlineDB.load(path))

    @classmethod
    def empty(cls) -> "AirlineEnvironment":
        """An environment holding no records, for a service that loads its store at setup time.

        The container boots on this. Both phases run from one image and the harness copies a phase's
        data in only AFTER the container is running, so a service that read its fixture at start-up
        could only ever serve whatever was baked into the image -- which is exactly how a carved build
        share ends up decorative. Booting empty is what makes the load a per-phase decision.

        The service answers `/health` while empty, deliberately: readiness says the process is up, not
        that it holds data. Each phase's setup script is what asserts the records actually landed.
        """
        return cls(AirlineDB.empty())

    def fresh(self) -> "AirlineEnvironment":
        """A new environment over an isolated deepcopy of this DB (per-case isolation)."""
        return AirlineEnvironment(self.db.fresh())

    def apply_actions(self, actions) -> list[dict]:
        """Apply a list of {name, arguments} actions, skipping read-only/transfer tools (they don't
        change DB state). Returns per-action {name, ok, error?}; a failing write leaves the DB
        unchanged for that action (mirrors an agent's tool call erroring out)."""
        results = []
        for a in actions or []:
            name = a.get("name")
            if name in NON_WRITE:
                results.append({"name": name, "ok": True, "skipped": "non-write"})
                continue
            try:
                self.tools.execute(name, a.get("arguments") or {})
                results.append({"name": name, "ok": True})
            except Exception as e:  # noqa: BLE001 - a failed write is a graded outcome, not a crash
                results.append({"name": name, "ok": False, "error": repr(e)})
        return results

    def state_hash(self) -> str:
        return self.db.state_hash()


def _smoke() -> None:
    env = AirlineEnvironment.from_fixture()
    base = env.fresh()
    h0 = base.state_hash()
    # a read doesn't change state; a cancel does
    rid = next(iter(base.db.reservations))
    base.tools.execute("get_reservation_details", {"reservation_id": rid})
    assert base.state_hash() == h0, "read must not mutate state"
    base.tools.execute("cancel_reservation", {"reservation_id": rid})
    assert base.state_hash() != h0, "cancel must mutate state"
    print(f"airline env OK: {len(base.db.flights)} flights, {len(base.db.users)} users, "
          f"{len(base.db.reservations)} reservations; hash changed on cancel")


if __name__ == "__main__":
    _smoke()
