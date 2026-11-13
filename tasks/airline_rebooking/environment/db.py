"""In-memory airline database (tau2 FlightDB) backing the airline task environment.

Loads a materialized fixture into plain dicts and provides per-case isolation (fresh deepcopy) + a
canonical state hash for the DB-state oracle. The records are the tau2 schema (flights / users /
reservations); we operate on dicts directly rather than pydantic models so the hash is a faithful
function of the on-disk fixture.

WHICH FIXTURE IS THE DEFAULT, AND WHY IT MATTERS MORE THAN IT LOOKS. There are now two stores -- the
carved build share and the full eval store -- and this module's default resolves to the **eval** one.
The scoring oracle (`tasks/airline_rebooking/eval.py`) replays golden actions against a base env
loaded with NO path argument, on the HOST, while the container loads whatever its phase bundled. If
this default pointed at the build share, the oracle would compare hashes computed over different data
than the container held, and every database check would fail -- with no error, no warning, and nothing
in the score to say why. That is the failure `docs/task_env_carve_spec.md` calls the worst in the whole
change, and this constant is the thing that prevents it.
"""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib

#: The EVAL store: the full fixture the eval cases are drawn from and the oracle replays against.
#: Resolved relative to __file__ so there is no dependency on appsci_bench for a repo-root anchor.
#: Nothing bakes this into the container image any more -- each phase bundles its own data at setup
#: time -- so this path is read on the HOST only (the oracle, and tests).
DEFAULT_DB_PATH = pathlib.Path(__file__).resolve().parent / "eval" / "data" / "db.json"

#: The carved build share, for tests and tooling that need to name it explicitly. Never the default:
#: see the module docstring.
BUILD_DB_PATH = pathlib.Path(__file__).resolve().parent / "build" / "data" / "db.json"


class AirlineDB:
    """Holds the three collections (flights, users, reservations) as dicts keyed by id."""

    def __init__(self, flights: dict, users: dict, reservations: dict):
        self.flights = flights
        self.users = users
        self.reservations = reservations

    @classmethod
    def load(cls, path: str | pathlib.Path | None = None) -> "AirlineDB":
        data = json.loads(pathlib.Path(path or DEFAULT_DB_PATH).read_text())
        return cls(data.get("flights", {}), data.get("users", {}), data.get("reservations", {}))

    @classmethod
    def empty(cls) -> "AirlineDB":
        """No records. What the container boots on before its phase's data is loaded."""
        return cls({}, {}, {})

    def counts(self) -> dict[str, int]:
        """Records per collection. What a setup script checks to prove its data actually landed --
        the service answers `/health` while holding nothing, so readiness cannot show this."""
        return {
            "flights": len(self.flights),
            "users": len(self.users),
            "reservations": len(self.reservations),
        }

    def fresh(self) -> "AirlineDB":
        """A deepcopy, so each eval case mutates an isolated DB."""
        return AirlineDB(copy.deepcopy(self.flights), copy.deepcopy(self.users),
                         copy.deepcopy(self.reservations))

    def as_dict(self) -> dict:
        return {"flights": self.flights, "users": self.users, "reservations": self.reservations}

    def state_hash(self) -> str:
        """Canonical sha256 over the three collections (order-independent, sort_keys).

        This is the DB-state fingerprint the oracle compares: applying the golden actions vs the
        agent's actions to identical fresh DBs and comparing this hash IS the tau2 DB check.
        """
        blob = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()
