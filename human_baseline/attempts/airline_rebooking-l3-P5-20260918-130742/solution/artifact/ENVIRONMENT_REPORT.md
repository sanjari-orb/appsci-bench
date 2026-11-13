# Airline Workspace Environment — Exploration Report

Written against the live container `airline-workspace-1` on 2026-09-18. All findings below were
verified against the running service, not read off source alone.

## 0. How to reach it

| | |
|---|---|
| Container | `airline-workspace-1` (image `airline-workspace`), up, `bench-network` |
| Listener | `0.0.0.0:8080` inside the container, **not published to the host** |
| Process | `uvicorn tasks.airline_rebooking.environment.service:create_app --factory` |
| Reach it | `docker exec airline-workspace-1 curl ... http://localhost:8080/...` |
| Code | `/app/tasks/airline_rebooking/environment/{service,tools,db,__init__}.py` (551 lines total) |
| Python | 3.11.16; only fastapi/uvicorn/pydantic installed |

Two bearer tokens, both in the container's environment:

- `AIRLINE_ENV_TOKEN=airline-tool-token-local` — `/health`, `/tools/{name}`. This is what the agent gets.
- `AIRLINE_ENV_ADMIN_TOKEN=airline-admin-token-local` — `/admin/reset`, `/admin/load`, `/admin/state_hash`. Evaluator/setup only.

Verified: no token → 401; tool token on an `/admin` route → 401.

### Routes

| Route | Auth | Purpose |
|---|---|---|
| `GET /health` | tool | `{"status":"ok"}` — answers even when the store is **empty**, by design |
| `POST /tools/{name}` `{arguments}` | tool | The 14 airline tools. `ToolError` → HTTP 400 `{detail}` |
| `POST /admin/reset` | admin | Re-copy pristine base over the live DB |
| `POST /admin/load` `{path}` | admin | Load a fixture by in-container path; returns per-collection counts |
| `GET /admin/state_hash` | admin | sha256 of the three collections — the scoring oracle's fingerprint |

The service boots holding **no records**; the phase's setup script (`/scripts/setup.sh`) loads the
fixture via `/admin/load` after the container is up, asserts every collection is non-empty, then
`rm -rf /fixture`. So there is no fixture file left in the container — the data exists only in the
uvicorn process's memory, reachable through the tool API.

---

## 1. Available data

### Which store is loaded — the important part

`db.json` canonical hash of the live environment:

```
8f0b535fba3b6a813d94c8197f37146cecae9dff0c95e03ae98092117c18debd
```

That is an exact match for the **carved build share**, not the eval store:

| Store | flights | users | reservations | size | hash |
|---|---|---|---|---|---|
| **build** (loaded) | **10** | **5** | **10** | 91 KB | `8f0b535f…` |
| eval (not loaded) | 300 | 500 | 2000 | 5.1 MB | `44b4b7da…` |

So anything built against the live container sees 25 records total, while grading runs against a
store two orders of magnitude larger with **entirely different IDs**. Do not hardcode IDs, and do not
assume any ID that works here exists at eval time.

Environment clock is fixed at **2024-05-15T15:00:00** (`NOW` in `tools.py`), matching the policy.

### Airports (5)

`DEN, JFK, LAX, ORD, SFO` — ORD is the hub.

### Flights (10) — all `HAT901`–`HAT910`, 30 dated instances each (2024-05-01 → 2024-05-30)

| Flight | Route | Dep (EST) | Arr | Notable dated statuses |
|---|---|---|---|---|
| HAT901 | SFO→ORD | 08:00 | 14:15 | — |
| HAT902 | ORD→SFO | 16:00 | 18:30 | **05-04 cancelled**, **05-15 on time** |
| HAT903 | ORD→JFK | 07:30 | 10:45 | — |
| HAT904 | JFK→ORD | 12:00 | 13:50 | — |
| HAT905 | SFO→JFK | 11:00 | 19:20 | **05-15 flying** |
| HAT906 | ORD→DEN | 09:15 | 10:40 | **05-20 cancelled** |
| HAT907 | DEN→ORD | 11:30 | 14:45 | — |
| HAT908 | LAX→ORD | 07:00 | 13:10 | **05-11 cancelled** |
| HAT909 | ORD→LAX | 15:30 | 17:55 | — |
| HAT910 | DEN→LAX | 08:45 | 10:05 | — |

The status pattern is uniform and clock-aligned: **05-01 → 05-15 are `landed`** (past), **05-16 →
05-30 are `available`** (bookable), with the six exceptions bolded above. Only `available` instances
are bookable or returned by search. Each instance carries `prices` and `available_seats` per cabin
(`basic_economy`, `economy`, `business`).

Note there is **no direct JFK↔LAX or SFO↔LAX flight** — those routes require a connection through ORD.

### Users (5)

| user_id | Name | Membership | Payment methods | Reservations |
|---|---|---|---|---|
| `nora_castillo_9412` | Nora Castillo | **gold** | visa ·4417; gift card $500; **certificate $250** | TRN001, TRN002 |
| `owen_delgado_9137` | Owen Delgado | **silver** | mastercard ·8823; gift card $300 | TRN003, TRN004 |
| `priya_venkat_9268` | Priya Venkat | **regular** | visa ·1902 *(only method)* | TRN005, TRN006 |
| `quinn_okafor_9503` | Quinn Okafor | **silver** | gift card $150; **certificate $100** | TRN007, TRN008 |
| `rafael_moreau_9821` | Rafael Moreau | **gold** | mastercard ·6650; gift card $800; **certificate $500** | TRN009, TRN010 |

All three membership tiers are represented, and the set spans "has a certificate", "gift card only",
and "single credit card" — enough to exercise the payment-limit rules.

### Reservations (10) — `TRN001`–`TRN010`

Two per user. None carries a `status` field (i.e. none is pre-cancelled).

| id | user | route | type | cabin | created_at | ins. | pax | bags t/nf | paid |
|---|---|---|---|---|---|---|---|---|---|
| TRN001 | nora | SFO→ORD | round | business | 05-02 10:24 | **yes** | 1 | 2/0 | $890 cc |
| TRN002 | nora | ORD→JFK | one_way | **basic_economy** | 05-06 18:03 | no | 1 | 0/0 | $72 gift |
| TRN003 | owen | LAX→ORD | round | economy | 05-03 08:47 | **yes** | 2 | 3/0 | $744 cc |
| TRN004 | owen | SFO→JFK | one_way | economy | 05-01 14:12 | no | 1 | 1/0 | $142 gift |
| TRN005 | priya | SFO→JFK | one_way | business | 05-05 21:35 | no | **3** | 4/0 | $2580 cc |
| TRN006 | priya | ORD→DEN | round | **basic_economy** | 05-07 11:58 | **yes** | 1 | 1/0 | $205 cc |
| TRN007 | quinn | DEN→LAX | one_way | economy | 05-08 16:41 | no | **4** | 4/0 | $728 gift |
| TRN008 | quinn | ORD→JFK | round | business | **05-15 09:12** | no | 2 | 2/0 | $1850 **certificate** |
| TRN009 | rafael | ORD→DEN | one_way | economy | 05-04 13:26 | **yes** | 1 | 1/0 | $185 cc |
| TRN010 | rafael | ORD→LAX | one_way | **basic_economy** | 05-09 07:19 | no | 1 | 0/0 | $95 gift |

### The fixture is deliberately built around the policy edges

Cross-referencing each reservation's segments against live flight status makes the intent obvious —
these are the test cases:

- **TRN004** — segment HAT905 on 2024-05-10 is **`landed`**. Already flown → a cancel request here is
  the README's canonical **transfer**, and a cabin change is a **denial**.
- **TRN009** — segment HAT906 on 2024-05-20 is **`cancelled`** by the airline. Owner is **gold** with
  insurance → cancellable, *and* compensation-eligible at $100 × 1 pax if he asks.
- **TRN008** — `created_at` 2024-05-15 09:12 vs the fixed clock of 15:00 the same day → **inside the
  24-hour window** (and business cabin, so it qualifies twice over). Also the only reservation paid by
  **certificate**, which is where the "leftover is never refundable" wrinkle bites.
- **TRN002 / TRN006 / TRN010** — **basic economy**: flights not modifiable, but cabin still changeable.
  TRN006 also has insurance, so the insurance-path cancel (health/weather only) is testable.
- **TRN005** — 3 pax, **regular** member, **no** insurance. Business cabin, so cancellable — but note
  priya's *other* reservation TRN006 is basic economy, giving the exact
  regular + no-insurance + basic-economy combination that gets **no compensation**.
- **TRN007** — 4 pax, largest party; economy, no insurance, future flight → a change-of-plan cancel
  here should be **denied** (no qualifying condition).
- **HAT902 on 05-15 `on time`** and **HAT909 on 05-15 `delayed`** exist as statuses no reservation uses
  — available for the "customer claims a delay/cancellation that isn't real" checks the README stresses.

---

## 2. Available tools

14 tools, dispatched by name via `POST /tools/{name}`. `execute()` refuses unknown names and anything
starting with `_` (verified: `/tools/_user` → `Unknown tool _user`), so the internal helpers are not
reachable.

### Writes (6) — these are the only things the scoring oracle sees

| Tool | Arguments | Mutation |
|---|---|---|
| `book_reservation` | `user_id, origin, destination, flight_type, cabin, flights, passengers, payment_methods, total_baggages, nonfree_baggages, insurance` | Creates a reservation; decrements seats and gift-card/certificate balances |
| `update_reservation_flights` | `reservation_id, cabin, flights, payment_id` | Replaces segments + cabin; appends a payment delta |
| `update_reservation_baggages` | `reservation_id, total_baggages, nonfree_baggages, payment_id` | Sets bag counts; charges $50 × increase |
| `update_reservation_passengers` | `reservation_id, passengers` | Replaces passenger list |
| `cancel_reservation` | `reservation_id` | Sets `status="cancelled"`, appends negated refund rows |
| `send_certificate` | `user_id, amount` | Adds a `certificate_*` payment method to the user |

### Reads (7) + transfer (1) — non-mutating, invisible to the oracle

| Tool | Arguments | Returns |
|---|---|---|
| `get_user_details` | `user_id` | Full user record (membership, payment methods, reservation ids) |
| `get_reservation_details` | `reservation_id` | Full reservation record |
| `search_direct_flight` | `origin, destination, date` | `available` direct flights only |
| `search_onestop_flight` | `origin, destination, date` | **stub — see below** |
| `list_all_airports` | *(none)* | `["DEN","JFK","LAX","ORD","SFO"]` |
| `get_flight_status` | `flight_number, date` | Status string for that instance |
| `calculate` | `expression` | Arithmetic, 2dp (verified `420*2+30` → `"870.0"`) |
| `transfer_to_human_agents` | `summary` | `"Transfer successful"` |

### Tool-surface findings worth acting on

1. **`search_onestop_flight` is a stub.** Its body is `return self.search_direct_flight(...)` — the
   comment says one-stop isn't needed for the DB-state oracle. Verified live: `JFK→LAX` on 2024-05-20
   returns `[]`, even though `HAT904 JFK→ORD` + `HAT909 ORD→LAX` both exist and are `available` that
   day. **Consequence:** connections cannot be discovered through the intended tool. For any itinerary
   needing a connection (JFK↔LAX, SFO↔LAX), the agent must call `search_direct_flight` twice against
   ORD. Booking a multi-segment trip still works — `book_reservation` accepts a list of segments.

2. **The API enforces none of the policy** — exactly as the README warns. `cancel_reservation` takes
   only a reservation id and will cancel a flown reservation (TRN004) without complaint;
   `update_reservation_flights` will happily modify a basic-economy reservation; nothing checks
   insurance, membership, the 24-hour window, or cabin uniformity. What the tools *do* enforce is
   narrow and mechanical: flight must be `available` to book, enough seats, payment method must exist
   in the profile, gift-card/certificate balance must cover the amount, booking totals must add up
   exactly, passenger **count** cannot change, and a certificate cannot be used for an *update*
   (`"Certificate cannot be used to update reservation"`). Every policy rule in the README is the
   agent's responsibility.

3. **Missing arguments produce HTTP 500, not 400.** `execute()` does `fn(**arguments)`, so an omitted
   required argument raises `TypeError`, which isn't caught alongside `ToolError` — verified:
   `/tools/get_user_details` with `{}` → `500 Internal Server Error` with an ASGI traceback in the
   container log. Only `ToolError` maps to a clean 400. The agent should treat a 500 as its own bug
   (malformed call) rather than as a business-rule rejection, and never surface it to the customer.

4. **`book_reservation` prices are computed from live instance prices, not passed in.** Total is
   `Σ(segment price × pax) + 30×pax if insurance + 50×nonfree_baggages`, and `payment_methods` amounts
   must sum to it **exactly** or the call fails with `"Payment amount does not add up"`. The free-bag
   allowance is *not* computed by the tool — the agent derives `nonfree_baggages` from membership +
   cabin itself.

5. **`cancel_reservation` is not idempotent.** It appends another full set of negated refund rows each
   time it's called, so a double-cancel leaves a corrupted `payment_history` and a different state
   hash. Call it at most once per reservation.

6. **`update_reservation_flights` re-prices only *new* segments.** A kept segment is matched on
   `(flight_number, date)` **and** unchanged cabin, and retains its original price — consistent with
   the policy. But because the match requires `cabin == reservation["cabin"]`, a cabin change forces
   *every* segment to be re-priced at current prices, which can move the total more than a naive
   "difference between cabins" calculation suggests. Use `calculate` on the actual numbers.

7. **New IDs are from a tiny fixed pool.** `_new_reservation_id()` only ever yields `HATHAT`,
   `HATHAU`, `HATHAV` (then `"Too many reservations"`), and `send_certificate` only
   `certificate_3221322/3/4`. Fine for a scored case, but it means at most 3 bookings per case.

8. **`calculate` uses `eval`** with a character allowlist (`0-9 + - * / ( ) . space`) and empty
   builtins. It's constrained, but note it will accept `1/0` and raise, and returns a float string
   (`"870.0"`) — so amounts need care when echoed to a customer.

9. **Reads are genuinely read-only.** Confirmed: after this whole exploration session (~1000 probe
   calls including brute-force enumeration) `/admin/state_hash` still returns the pristine
   `8f0b535f…`. Nothing I did perturbed the environment.

---

## 3. Practical implications for the artifact

The artifact at [artifact/server.py](artifact/server.py) is still the untouched stub — `/health` plus a
`/respond` that returns `"TODO: not yet implemented"`. It must return an OpenAI-shaped
`{"choices":[{"message":{"role":"assistant","content":...}}]}` and bind `0.0.0.0:$PORT`.

Carrying over from the above:

- **Enumeration is not available.** There is no "list users" or "list reservations" tool. The agent
  gets a `user_id` from the customer, then `get_user_details` → `reservations[]` →
  `get_reservation_details` for each. That is the only path from a person to their bookings, and it's
  how the README's "help them find the reservation id" has to work.
- **Verification is cheap and always possible.** Every fact the README says to confirm has a read
  behind it: membership and payment methods from `get_user_details`; cabin, insurance, `created_at`,
  and passenger count from `get_reservation_details`; real cancellation/delay from `get_flight_status`
  on each segment's `(flight_number, date)`. There is no excuse for taking a customer's claim at face
  value.
- **"Already flown" is a per-segment status check.** A reservation is flown if *any* segment's instance
  status is `landed` (or `flying`) — TRN004 is the example. Given the clock, any segment dated
  ≤ 2024-05-15 is in the past.
- **Build against shapes, not IDs.** The eval store has 500 users and 2000 reservations with different
  IDs; the 25 records here are a development sample only.

## Appendix — commands used

```bash
# reach the API (port is not published to the host)
docker exec airline-workspace-1 curl -s -X POST \
  -H "Authorization: Bearer airline-tool-token-local" \
  -H "Content-Type: application/json" \
  -d '{"arguments":{"user_id":"nora_castillo_9412"}}' \
  http://localhost:8080/tools/get_user_details

# confirm which store is loaded
docker exec airline-workspace-1 curl -s \
  -H "Authorization: Bearer airline-admin-token-local" \
  http://localhost:8080/admin/state_hash
# => 8f0b535f... == build share (10 flights / 5 users / 10 reservations)
```

Flight numbers were enumerated by probing `get_flight_status` for `HAT001`–`HAT999` against an
impossible date and separating `"not found on date"` (flight exists) from `"not found"` (it doesn't).
Reservation IDs came from the users' own `reservations[]` arrays and were confirmed live as
`TRN001`–`TRN010`, with `TRN011` returning not-found.
