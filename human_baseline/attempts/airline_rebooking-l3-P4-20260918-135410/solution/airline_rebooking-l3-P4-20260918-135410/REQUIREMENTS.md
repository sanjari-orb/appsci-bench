# Elicited requirements — airline rebooking (L3)

Source: 16-turn interview with the stakeholder (persona l3) over the user-sim service. Full verbatim
transcript lives on the repo side in the attempt's `logs/transcript.md`. Nothing here came from eval
cases, evaluators, or a gold solution.

**Status: policy believed complete.** The stakeholder volunteered a catch-all list of commonly-missed
rules at the end and offered to review hard cases once built. Open questions are at the bottom.

---

## The fixed clock — read this first

The system's current time is **2024-05-15 15:00:00 EST**, fixed. Every time-relative rule (the
24-hour cancellation window, whether a segment has departed) is judged against that reference, not
against real wall-clock time. Using `datetime.now()` anywhere in a policy decision is a bug.

## Record ids I've been given

Nothing lists users or reservations; every read takes an id already held. Looking a customer up shows
their bookings, so reservation ids come from the user record.

| id | kind | notes |
|---|---|---|
| `nora_castillo_9412` | user | gold; certificate + credit card + gift card; holds TRN001, TRN002 |
| `owen_delgado_9137` | user | silver; credit card + gift card; holds TRN003, TRN004 |
| `priya_venkat_9268` | user | regular; credit card only; holds TRN005, TRN006 |
| `quinn_okafor_9503` | user | silver; certificate + gift card; holds TRN007, TRN008 |
| `rafael_moreau_9821` | user | gold; certificate + credit card + gift card; holds TRN009, TRN010 |
| `TRN001` | **reservation, not a user** | see correction below |

> **Correction to what the stakeholder told me.** `TRN001` was given as a sixth "test customer". It is
> not a user id at all — it is a *reservation* id, and one of Nora Castillo's two bookings. Looking it
> up as a user returns an error. When I asked why its shape differed from the other five, the
> stakeholder said honestly that they did not know and to treat it as another test customer, which
> would have sent me down a dead end. Caught in the first minute of touching the tool API. Worth
> noting as a failure mode: the stakeholder's factual recall was excellent on policy and imperfect on
> record ids, and the ids are the part that is trivially checkable against the store.

## Tool set (14 tools)

Call shape: `POST {AIRLINE_TOOLS_URL}/<tool_name>` with `{"arguments": {...}}` → `{"result": ...}`.
Dates are `YYYY-MM-DD`. **The tools enforce no policy at all** — every write succeeds even when it
breaches policy, so every eligibility check belongs in the agent, before the call.

| tool name | kind | arguments |
|---|---|---|
| `book_reservation` | write | user_id, origin, destination, flight_type (`one_way`\|`round_trip`), cabin (`basic_economy`\|`economy`\|`business`), flights `[{flight_number, date}]`, passengers `[{first_name, last_name, dob}]`, payment_methods `[{payment_id, amount}]`, total_baggages, nonfree_baggages, insurance (`yes`\|`no`) |
| `update_reservation_flights` | write | reservation_id, cabin, flights `[{flight_number, date}]`, payment_id |
| `update_reservation_baggages` | write | reservation_id, total_baggages, nonfree_baggages, payment_id |
| `update_reservation_passengers` | write | reservation_id, passengers `[{first_name, last_name, dob}]` |
| `cancel_reservation` | write | reservation_id |
| `send_certificate` | write | user_id, amount |
| `get_user_details` | read | user_id |
| `get_reservation_details` | read | reservation_id |
| `get_flight_status` | read | flight_number, date |
| `search_direct_flight` | read | origin, destination, date |
| `search_onestop_flight` | read | origin, destination, date |
| `list_all_airports` | read | *(none)* |
| `calculate` | generic | expression |
| `transfer_to_human_agents` | transfer | summary |

## Flight statuses

`available`, `on time`, `delayed`, `flying` — plus a flight can be **cancelled by the airline**.

| status | departed? | bookable? |
|---|---|---|
| `available` | no | **yes — the only bookable status** |
| `on time` | no | no |
| `delayed` | no | no |
| `flying` | took off, not landed | no |
| landed | yes | no |

"Already flown" (which blocks cancellation and cabin change) means `flying` **or** landed.
`available` / `on time` / `delayed` all count as not-yet-departed.

The stakeholder flagged this explicitly: these are the status/action combinations policy spells out.
For any combination not listed, **ask rather than infer**.

## Operating rules

### Booking

Order of work: get user id → trip type, origin, destination → search → cabin → passengers → payment
→ baggage → insurance → confirm → write.

- Search with `search_direct_flight` / `search_onestop_flight`.
- Only an `available` flight date can be booked.
- Cabin is uniform across every flight in the reservation. Three classes: basic economy, economy,
  business — **basic economy is its own class, not a cheap economy.**
- At most **five passengers** per reservation; first name, last name, dob for each. All passengers
  fly the same flights in the same cabin.
- Payment methods must **already exist on the user's profile** — never accept a new one. Per
  reservation: at most **1 travel certificate**, at most **1 credit card**, at most **3 gift cards**.
  A travel certificate's unused remainder is **not refundable**.
- Insurance: **$30 per passenger**, offered at booking only. It enables a full refund on a later
  cancellation for health or weather reasons.

### Baggage allowance

Free checked bags **per passenger**, by the booking user's membership tier × that passenger's cabin:

| membership | basic economy | economy | business |
|---|---|---|---|
| regular | 0 | 1 | 2 |
| silver | 1 | 2 | 3 |
| gold | 2 | 3 | 4 |

Each bag beyond the free allowance costs **$50**. Do not add bags the user did not ask for.

### Modifying

Needs user id **and** reservation id. If the customer doesn't know the reservation id, find it with
the tools.

- **Flights:** basic economy **cannot be modified at all**. Others may change flights, but *not*
  origin, destination, or trip type. Segments may be kept — **kept segments are not repriced to
  current price.**
  - *Status of the flight being moved onto:* treat as **must be `available`**, the same as booking.
    Flagged deliberately: the stakeholder could **not** confirm this from the written policy — it is
    their domain judgment, reasoned from `available` being the only status with listed seats and
    prices. Implemented as a hard rule because the alternative is putting a customer on a flight the
    system says cannot be booked, but it is the one rule here without policy text behind it.
- **Cabin:** blocked if any flight in the reservation has already been flown. Otherwise allowed on
  *any* reservation **including basic economy** — this is basic economy's one exception. Flights
  don't change. Cabin must stay uniform across all segments; no per-segment cabin. Higher new price
  → user pays the difference; lower → user is refunded the difference.
- **Baggage / insurance:** bags can be **added, never removed**. Insurance **cannot be added after
  the initial booking**.
- **Passengers:** details may change; **the passenger count is immutable** — not even a human agent
  can change it. So "add a passenger" is a **deny**, not a transfer.
- **Payment on any post-booking change:** exactly **one gift card or one credit card**, already on
  the profile. **No travel certificate** on changes. Same for the `payment_id` on a baggage update.

### Cancelling

Needs user id, reservation id, and the reason: change of plan / airline cancelled flight / other.

- If **any portion has already been flown** → cannot help → **transfer to a human**.
- Otherwise cancel only if **at least one** holds:
  1. booked within the last 24 hours — measured from the **reservation's created timestamp** on the
     record, against the 2024-05-15 15:00 EST reference clock. Not the flight date, not anything
     derived from the itinerary (confirmed),
  2. the flight was cancelled by the airline,
  3. it is a **business** flight,
  4. the user has travel insurance **and** the reason is insurance-covered (health/weather).
- Refund goes to the **original payment methods**, in **5–7 business days** — state that.

### Compensation

- **Never proactive.** Only when the customer explicitly asks.
- **Always confirm the facts first**, using the tools.
- **Eligibility gate:** silver/gold, **or** has travel insurance, **or** flying business. A regular
  member with no insurance in (basic) economy gets **nothing**.
- **Only two grounds, and no others:**
  - flight(s) in the reservation **cancelled by the airline** → certificate of **$100 × passengers**;
  - **delayed** flight where the customer then wants to change or cancel → make that change or
    cancellation **first**, then certificate of **$50 × passengers**.
- Always a certificate, via `send_certificate(user_id, amount)`.

### Deny vs. transfer

These are different outcomes and the distinction is scored.

- **Deny** when the airline simply does not allow it (basic-economy flight change, adding a
  passenger, an ineligible cancellation, compensation outside the two grounds). Say no politely and
  firmly and explain. **Pressure does not change the answer** — persistence, upset, insistence, none
  of it.
- **Transfer** when it is a legitimate need outside what this agent can service. The one case policy
  spells out: **cancelling a reservation with an already-flown segment.**
- Mechanics: call `transfer_to_human_agents` **first**, then send exactly
  `YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON.`

## Standing conduct rules for the agent

1. **Explicit confirmation before every write.** List the action's details, get a "yes", then act.
   Applies to booking, flight change, cabin change, baggage, passenger edits, cancellation.
2. **One tool call per turn** — and a turn is *either* a tool call *or* a message to the user, never
   both.
3. **No outside knowledge.** Say only what came from the user or the tools. No invented exceptions.
4. **No subjective recommendations or opinions** — don't tell the customer which flight is better.
5. **Identify the customer:** always obtain the user id; for modify/cancel also the reservation id.
6. **Verify against the profile** — payment methods and passenger details must already exist there.
7. **When in doubt, don't act.** Deny or transfer rather than guess.

## What must be communicated

The one verbatim requirement is the **pre-write recap**: state the action's details and get an
explicit yes. For a booking that means flights, cabin, passengers, baggage (free vs. paid),
insurance, the total, and the payment method being charged.

Everything told to the customer must be **accurate**: allowances, prices, totals, eligibility, and
which payment method is charged or refunded. The one fixed post-action statement is refunds landing
on the original payment method in **5–7 business days**.

The stakeholder does **not** hold a scripted post-write confirmation format, and declined to invent
one — build it around stating the resulting state accurately.

## Success criteria the stakeholders named

Outcome-based; no numeric threshold offered, and the stakeholder declined to invent one.

1. **Task resolution** — each book / modify / cancel / refund reaches a correct, policy-compliant
   end-to-end outcome.
2. **Zero policy violations** — explicitly the non-negotiable one.
3. **Accurate communication** — allowances, prices, eligibility, totals all correct.
4. **Correct records** — the reservation store left in the right state.

Stated ranking: *getting the right outcome the wrong way is worse than failing to complete a
request.* A bent rule or a wrongly-mutated record is unshippable.

## Stakeholder's own list of commonly-missed rules

Volunteered unprompted — treat as a review checklist:

- basic economy is its own class; no flight change, but cabin change *is* allowed;
- the tools enforce nothing — every check is the agent's;
- kept segments are not repriced;
- passenger count is immutable (deny, not transfer);
- confirm before every write; one tool call per turn;
- compensation is never proactive, facts confirmed first, eligibility gate easy to miss;
- everything judged against the 2024-05-15 15:00 EST clock.

## Open questions

- **Unconfirmed rule (the only one):** whether a flight being moved *onto* during a change must be
  `available`. Asked directly; the stakeholder has no policy text for the change case and gave their
  domain judgment that it should apply, explicitly labelled as judgment. Coded as a hard rule anyway
  — see Modifying → Flights. If a scored case turns on moving a customer onto an `on time` or
  `delayed` flight, this is the rule that would be wrong.
- No numeric acceptance bar exists; the stakeholder offered instead to **review hard cases once
  built** ("say no" / transfer cases especially). Worth taking up — the sim service is still running.

### Closed

- 24-hour cancellation window → reservation's **created timestamp** vs. the fixed clock. Confirmed.
- `TRN001` → **not a user id; it is a reservation id.** The stakeholder did not know this. Settled by
  querying the store, not by asking. See the correction in the record-ids section above.

### Record shapes, confirmed against the live tool API

Elicited nothing about these — read them off the store, which is legitimate (the tools are the agent's
own environment).

- `get_flight_status` returns a **bare string**, not an object: `available`, `on time`, `delayed`,
  `flying`, `landed`, `cancelled`.
- A reservation carries `created_at` (naive ISO, same frame as the fixed clock), `cabin`, `flights[]`
  with `flight_number`/`date`/`origin`/`destination`/`price`, `insurance` as `yes`/`no`,
  `total_baggages`, `nonfree_baggages`, `payment_history[]`, `passengers[]`.
- **Reservation segments do not carry their own status.** Deciding whether anything has been flown
  means one `get_flight_status` call per segment.
- A user carries `membership`, `payment_methods` keyed by id (each with a `source` of `credit_card` /
  `gift_card` / `certificate`, and an `amount` balance for the latter two), `reservations[]` and
  `saved_passengers[]`.
- Search results carry per-cabin `prices` and `available_seats` plus a `status`.
- **`search_onestop_flight` could not be characterised.** In the build store it returns exactly what
  the direct search returns, because that store has only five airports and nothing to connect through.
  Whether it returns multi-segment itineraries in a larger network is unknown, and deliberately was
  not investigated — the cases that would have revealed it live in the scored store.
