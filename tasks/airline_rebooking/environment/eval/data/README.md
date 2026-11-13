# Airline DB fixture

The airline reservation database (tau2 FlightDB) materialized as the agent-facing task environment
resource (spec resource `airline_db`). Produced by `scripts/convert_tau2_airline.py --emit-db`.

- `db.json` — full DB: `{flights, users, reservations}`, each a dict keyed by id. This is what the
  environment loads and what the per-case DB-state oracle is replayed against.

`db.json` is byte-equal to the upstream tau2 `data/tau2/domains/airline/db.json` (commit in
`../../../../provenance/airline/TAU2_COMMIT.txt`). Counts: 300 flights, 500 users, 2000 reservations.
