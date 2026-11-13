"""Deterministic airline policy rules -- pure functions, no network and no LLM.

Why this module exists: the environment's tools enforce *no* policy at all. `cancel_reservation`
will cancel a fully-flown basic-economy trip without complaint, `update_reservation_baggages` will
happily decrease the bag count, `send_certificate` checks no eligibility, and `book_reservation`
ignores the five-passenger and payment-composition limits. policy.md says as much in as many
words ("The API does not check these for the agent"). Any rule left purely to a prompt is therefore
a coin flip on every write, so every rule that is a pure function of
(tool, arguments, records, clock) is decided here instead.

`check()` is the single entry point: the agent calls it for every WRITE tool before the call is
proxied to the environment. A returned Violation means the write is suppressed; its message is fed
back to the model so it can compose an accurate customer-facing reply (see agent.py).

Deliberately NOT enforced here, because they cannot be:
  * "no proactive compensation" -- not decidable from a tool call's arguments; prompt-owned.
  * cabin uniformity across segments -- structurally impossible to violate, since both
    book_reservation and update_reservation_flights take a single `cabin` argument.
  * "insurance cannot be added after booking" -- structurally impossible, as no tool mutates the
    insurance field; prompt-owned so the agent says no rather than transferring.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import config

# The environment's reference clock is fixed; every time-relative rule is judged against it.
# Sourced from config.REFERENCE_TIME so policy.py, prompt.py and config.py cannot drift apart.
NOW = datetime.fromisoformat(config.REFERENCE_TIME)
CANCELLATION_WINDOW = timedelta(hours=24)

# A segment in one of these states has departed, which blocks cancellation and cabin changes.
FLOWN_STATUSES = {"landed", "flying"}

# Free checked bags per passenger, by membership and cabin (policy.md "Checked bag allowance").
FREE_BAGS: dict[str, dict[str, int]] = {
    "regular": {"basic_economy": 0, "economy": 1, "business": 2},
    "silver": {"basic_economy": 1, "economy": 2, "business": 3},
    "gold": {"basic_economy": 2, "economy": 3, "business": 4},
}

EXTRA_BAG_FEE = 50
INSURANCE_FEE_PER_PASSENGER = 30
CANCELLED_FLIGHT_COMPENSATION_PER_PASSENGER = 100
DELAYED_FLIGHT_COMPENSATION_PER_PASSENGER = 50

MAX_PASSENGERS = 5
MAX_GIFT_CARDS = 3
MAX_CREDIT_CARDS = 1
MAX_CERTIFICATES = 1

TRANSFER_INSTRUCTION = (
    "Policy requires a human agent for this. Call transfer_to_human_agents with a summary, then "
    "send exactly: 'YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON.'"
)

WRITE_TOOLS = {
    "book_reservation", "update_reservation_flights", "update_reservation_baggages",
    "update_reservation_passengers", "cancel_reservation", "send_certificate",
}


@dataclass
class Violation:
    """A blocked write: `code` identifies the rule, `message` is fed back to the model."""

    code: str
    message: str
    transfer: bool = False

    def as_feedback(self) -> str:
        """The text handed back to the model in place of a tool result."""
        text = f"BLOCKED ({self.code}): {self.message}"
        if self.transfer:
            text = f"{text} {TRANSFER_INSTRUCTION}"
        else:
            text = (f"{text} Explain this to the customer accurately; do not retry this call "
                    f"and do not work around it.")
        return text


@dataclass
class Context:
    """Records the guard needs to judge a write. Plain dicts, so tests need no network.

    `reservation` / `user` are the current records; `flight_statuses` maps
    (flight_number, date) -> status; `available_flights` maps the same key to a bookable
    instance carrying `prices` and `available_seats`. `cancellation_reason` and `complaint`
    carry the conversational facts the agent established.

    `reservation_resolved` records whether this turn actually changed or cancelled the
    reservation. The delay certificate is only payable afterwards, so the guard needs evidence
    the work happened rather than an intention to do it; agent.py sets this from the writes it
    has observed succeed, never from anything the customer or the model asserts.
    """

    reservation: dict | None = None
    user: dict | None = None
    flight_statuses: dict[tuple[str, str], str] = field(default_factory=dict)
    available_flights: dict[tuple[str, str], dict] = field(default_factory=dict)
    cancellation_reason: str | None = None
    complaint: str | None = None
    reservation_resolved: bool = False

    def status_of(self, flight_number: str, date: str) -> str | None:
        return self.flight_statuses.get((flight_number, date))


# --- shared computations ------------------------------------------------------------------------
def free_bag_allowance(membership: str, cabin: str, passengers: int) -> int:
    """Total free checked bags for a reservation (per-passenger allowance x passenger count)."""
    try:
        return FREE_BAGS[membership][cabin] * passengers
    except KeyError as exc:
        raise ValueError(f"unknown membership/cabin: {membership}/{cabin}") from exc


def booking_total(flight_prices: list[int], passengers: int, insurance: str,
                  nonfree_baggages: int) -> int:
    """Total charge for a new booking, mirroring book_reservation's own arithmetic."""
    total = sum(flight_prices) * passengers
    if insurance == "yes":
        total += INSURANCE_FEE_PER_PASSENGER * passengers
    return total + EXTRA_BAG_FEE * nonfree_baggages


def quote_flight_update(reservation: dict, cabin: str, flights: list[dict],
                        available: dict[tuple[str, str], dict]) -> int:
    """Price delta for a flight/cabin change, mirroring update_reservation_flights exactly.

    The tool's kept-segment rule is genuinely counterintuitive and worth stating plainly: a
    requested segment keeps its originally-paid price only when its flight number AND date match
    an existing segment AND the requested cabin equals the reservation's *current* cabin.

    The consequence is that a cabin change re-prices *every* segment at today's fare -- it is not
    the difference between cabins on the fare originally paid. An agent quoting "just the upgrade
    difference" would state a wrong total, which is exactly the accuracy failure the KPIs target.

    Raises ValueError if a newly-priced segment is not bookable, since no honest quote exists then.
    """
    passengers = len(reservation["passengers"])
    total = 0
    for requested in flights:
        key = (requested["flight_number"], requested["date"])
        kept = next(
            (f for f in reservation["flights"]
             if f["flight_number"] == requested["flight_number"]
             and f["date"] == requested["date"]
             and cabin == reservation["cabin"]),
            None,
        )
        if kept is not None:
            total += kept["price"] * passengers
            continue
        instance = available.get(key)
        if not instance or cabin not in instance.get("prices", {}):
            raise ValueError(f"flight {key[0]} on {key[1]} is not bookable in {cabin}")
        total += instance["prices"][cabin] * passengers
    paid = sum(f["price"] for f in reservation["flights"]) * passengers
    return total - paid


def _parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO timestamp from the environment, or None if it is missing/malformed.

    A malformed `created_at` must not crash the guard mid-turn: the 24h-window ground simply
    does not apply, and the caller falls through to the other cancellation grounds.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_cancellable(reservation: dict, statuses: dict[tuple[str, str], str],
                   reason: str | None) -> tuple[bool, str]:
    """Whether a reservation may be cancelled, with the ground (or the blocking reason).

    Grounds, per policy.md: booked within the last 24h, a flight cancelled by the airline, a
    business-cabin reservation, or insurance covering the stated reason. The insurance ground
    depends on an unverifiable claim, so it is gated on config.TRUST_STATED_REASON.
    """
    created = _parse_timestamp(reservation.get("created_at"))
    if created is not None and NOW - created <= CANCELLATION_WINDOW:
        return True, "the booking was made within the last 24 hours"
    if any(statuses.get((f["flight_number"], f["date"])) == "cancelled"
           for f in reservation["flights"]):
        return True, "a flight in this reservation was cancelled by the airline"
    if reservation["cabin"] == "business":
        return True, "this is a business-cabin reservation"
    if reservation.get("insurance") == "yes":
        if config.TRUST_STATED_REASON and _insurance_covers(reason):
            return True, "travel insurance covers the stated reason"
        return False, "insurance"
    return False, "none"


def _insurance_covers(reason: str | None) -> bool:
    """Whether a stated cancellation reason is a health or weather reason.

    Keyword matching over free text is inherently approximate: it can miss a genuine reason
    phrased unusually, or fire on an unrelated turn of phrase. This is acceptable only because
    the caller (is_cancellable) gates it behind config.TRUST_STATED_REASON, which defaults to
    False -- an uncorroborated reason is transferred to a human, never acted on automatically.
    """
    if not value_present(reason):
        return False
    text = reason.lower()
    return any(word in text for word in (
        "health", "ill", "illness", "sick", "sickness", "medical", "medication", "hospital",
        "doctor", "injury", "injured", "emergency",
        "weather", "storm", "hurricane", "tornado", "blizzard", "snow", "flood",
    ))


def value_present(reason: str | None) -> bool:
    """Whether a stated reason is non-empty, ignoring surrounding whitespace."""
    return bool(reason and reason.strip())


def compensation_amount(complaint: str | None, passengers: int) -> int | None:
    """Certificate amount owed for a complaint, or None if the complaint warrants none."""
    if complaint == "cancelled_flight":
        return CANCELLED_FLIGHT_COMPENSATION_PER_PASSENGER * passengers
    if complaint == "delayed_flight":
        return DELAYED_FLIGHT_COMPENSATION_PER_PASSENGER * passengers
    return None


def is_compensation_eligible(user: dict, reservation: dict) -> bool:
    """Whether the customer may be compensated at all (silver/gold, insured, or business)."""
    return (user.get("membership") in {"silver", "gold"}
            or reservation.get("insurance") == "yes"
            or reservation.get("cabin") == "business")


# --- per-tool checks ----------------------------------------------------------------------------
def _check_not_cancelled(reservation: dict, action: str) -> Violation | None:
    """No write may touch a reservation that has already been cancelled.

    Only check_cancel tests this today via its own ALREADY_CANCELLED branch; the other writes
    (flights, baggage, passengers) had no equivalent guard, so a cancelled reservation could
    still be "modified" -- corrupting a record the KPIs require to be in the right state.
    """
    if reservation.get("status") == "cancelled":
        return Violation(
            "RESERVATION_CANCELLED",
            f"Reservation {reservation['reservation_id']} is already cancelled, so {action} is "
            f"not possible.",
        )
    return None


def check_cancel(args: dict, ctx: Context) -> Violation | None:
    """Cancellation: nothing flown, not already cancelled, and an eligibility ground applies."""
    reservation = ctx.reservation
    if reservation is None:
        return Violation("UNKNOWN_RESERVATION",
                         f"Reservation {args.get('reservation_id')} could not be read; "
                         f"confirm the id before cancelling.")
    if reservation.get("status") == "cancelled":
        return Violation("ALREADY_CANCELLED",
                         f"Reservation {reservation['reservation_id']} is already cancelled; "
                         f"cancelling again would duplicate the refund.")

    flown = [f for f in reservation["flights"]
             if ctx.status_of(f["flight_number"], f["date"]) in FLOWN_STATUSES]
    if flown:
        segments = ", ".join(f"{f['flight_number']} on {f['date']}" for f in flown)
        return Violation(
            "FLOWN_SEGMENT",
            f"Part of this trip has already been flown ({segments}), so it cannot be cancelled "
            f"here.",
            transfer=True,
        )

    ok, ground = is_cancellable(reservation, ctx.flight_statuses, ctx.cancellation_reason)
    if ok:
        return None
    if ground == "insurance":
        return Violation(
            "INSURANCE_REASON_UNVERIFIED",
            "The only possible ground is travel insurance covering the stated reason, which "
            "cannot be verified with the available tools.",
            transfer=True,
        )
    return Violation(
        "NOT_CANCELLABLE",
        f"Reservation {reservation['reservation_id']} does not meet any cancellation ground: it "
        f"was booked on {reservation['created_at']} (more than 24 hours ago), the cabin is "
        f"{reservation['cabin']} (not business), no flight was cancelled by the airline, and "
        f"there is no travel insurance"
        + ("." if reservation.get("insurance") != "yes" else " ground that applies."),
    )


def check_update_flights(args: dict, ctx: Context) -> Violation | None:
    """Flight/cabin change: basic-economy, route, trip type, flown segments, payment, pricing."""
    reservation = ctx.reservation
    if reservation is None:
        return Violation("UNKNOWN_RESERVATION",
                         f"Reservation {args.get('reservation_id')} could not be read; "
                         f"confirm the id before changing it.")
    if violation := _check_not_cancelled(reservation, "changing its flights or cabin"):
        return violation

    requested = args.get("flights") or []
    cabin = args.get("cabin")
    current = [(f["flight_number"], f["date"]) for f in reservation["flights"]]
    requested_keys = [(f["flight_number"], f["date"]) for f in requested]
    flights_changed = requested_keys != current

    if len(set(requested_keys)) != len(requested_keys):
        return Violation(
            "DUPLICATE_SEGMENT",
            "The requested flights list repeats the same flight number and date more than once; "
            "each segment must be distinct.",
        )

    if reservation["cabin"] == "basic_economy" and flights_changed:
        return Violation(
            "BASIC_ECONOMY_NOT_MODIFIABLE",
            "Basic economy flights cannot be changed. (A cabin upgrade on the same flights is "
            "still possible.)",
        )

    # Any segment the reservation currently holds that has already flown blocks the write --
    # whether the request would drop that segment (a flight change) or keep it under a new
    # cabin. Unlike cancellation, policy.md does not call for a transfer here: a modification
    # is simply refused. Checked against the CURRENT segments deliberately: a flown segment
    # cannot be legitimised by asking to swap it out, so it must gate flight changes too, not
    # only cabin changes.
    flown = [f for f in reservation["flights"]
             if ctx.status_of(f["flight_number"], f["date"]) in FLOWN_STATUSES]
    if flown and (cabin != reservation["cabin"] or flights_changed):
        segments = ", ".join(f"{f['flight_number']} on {f['date']}" for f in flown)
        code = ("CABIN_CHANGE_AFTER_FLOWN" if cabin != reservation["cabin"]
                else "FLOWN_SEGMENT_NOT_MODIFIABLE")
        return Violation(
            code,
            f"This reservation cannot be modified: part of the trip has already been flown "
            f"({segments}).",
        )

    if flights_changed:
        violation = _check_route_preserved(reservation, requested, ctx)
        if violation:
            return violation

    violation = _check_update_payment(args.get("payment_id"), ctx)
    if violation:
        return violation

    try:
        quote_flight_update(reservation, cabin, requested, ctx.available_flights)
    except ValueError as exc:
        return Violation("NOT_BOOKABLE", f"{exc}. Offer the customer an available alternative.")
    return None


def _check_route_preserved(reservation: dict, requested: list[dict],
                           ctx: Context) -> Violation | None:
    """Origin, destination and trip type must survive a modification unchanged."""
    routes = [ctx.available_flights.get((f["flight_number"], f["date"])) for f in requested]
    if any(r is None for r in routes):
        return Violation(
            "NOT_BOOKABLE",
            "At least one requested flight is not available on that date, so the route cannot be "
            "confirmed. Offer the customer an available alternative.",
        )
    if routes[0].get("origin") != reservation["origin"]:
        return Violation(
            "ORIGIN_CHANGED",
            f"A change may not alter the origin: this reservation departs from "
            f"{reservation['origin']}, but the requested itinerary starts at "
            f"{routes[0].get('origin')}.",
        )
    if routes[-1].get("destination") != reservation["destination"]:
        return Violation(
            "DESTINATION_CHANGED",
            f"A change may not alter the destination: this reservation arrives at "
            f"{reservation['destination']}, but the requested itinerary ends at "
            f"{routes[-1].get('destination')}.",
        )
    if len(requested) != len(reservation["flights"]):
        return Violation(
            "TRIP_TYPE_CHANGED",
            f"A change may not alter the trip type: this reservation has "
            f"{len(reservation['flights'])} segment(s), the request has {len(requested)}.",
        )
    return None


def _check_update_payment(payment_id: str | None, ctx: Context) -> Violation | None:
    """A payment method must be a stored gift card or credit card, never a certificate.

    policy.md states this explicitly for flight/cabin changes ("Payment" under Modify flight).
    It is reused here for baggage charges too (check_update_baggages): policy.md is silent on
    baggage payment composition specifically, but a certificate's remaining amount is stated to
    be non-refundable and certificates are never listed among the allowed reservation-change
    payment types anywhere in the document, so the same restriction is applied by extension
    rather than left unenforced.
    """
    methods = (ctx.user or {}).get("payment_methods", {})
    if payment_id not in methods:
        return Violation(
            "PAYMENT_NOT_IN_PROFILE",
            f"Payment method {payment_id} is not in the customer's profile. For safety, only "
            f"stored payment methods may be used.",
        )
    if methods[payment_id].get("source") == "certificate":
        return Violation(
            "CERTIFICATE_NOT_ALLOWED",
            "A travel certificate cannot be used to pay for this change; a gift card or credit "
            "card is required.",
        )
    return None


def check_update_baggages(args: dict, ctx: Context) -> Violation | None:
    """Baggage: bags may be added but never removed, and the free allowance must be applied."""
    reservation = ctx.reservation
    if reservation is None:
        return Violation("UNKNOWN_RESERVATION",
                         f"Reservation {args.get('reservation_id')} could not be read.")
    if violation := _check_not_cancelled(reservation, "changing its baggage"):
        return violation

    total = args.get("total_baggages")
    nonfree = args.get("nonfree_baggages")
    if total is None or nonfree is None or total < 0 or nonfree < 0:
        return Violation(
            "BAGGAGE_COUNT_INVALID",
            f"Baggage counts must be non-negative (got total_baggages={total}, "
            f"nonfree_baggages={nonfree}).",
        )
    if nonfree > total:
        return Violation(
            "BAGGAGE_COUNT_INVALID",
            f"nonfree_baggages ({nonfree}) cannot exceed total_baggages ({total}).",
        )
    if total < reservation["total_baggages"]:
        return Violation(
            "BAGGAGE_DECREASE",
            f"Checked bags can be added but not removed: the reservation already has "
            f"{reservation['total_baggages']} and the request would reduce it to {total}.",
        )

    free = free_bag_allowance((ctx.user or {}).get("membership", "regular"),
                              reservation["cabin"], len(reservation["passengers"]))
    expected = max(0, total - free)
    if nonfree != expected:
        return Violation(
            "BAGGAGE_ALLOWANCE",
            f"This customer is {(ctx.user or {}).get('membership')} in {reservation['cabin']} with "
            f"{len(reservation['passengers'])} passenger(s), so {free} checked bag(s) are free. "
            f"For {total} total bags that means {expected} paid bag(s), not {nonfree}.",
        )
    # A payment method only has to be valid when there is actually something to charge.
    if expected > reservation["nonfree_baggages"]:
        return _check_update_payment(args.get("payment_id"), ctx)
    return None


def check_update_passengers(args: dict, ctx: Context) -> Violation | None:
    """Passenger details may change; the passenger count may not -- and that is a refusal."""
    reservation = ctx.reservation
    if reservation is None:
        return Violation("UNKNOWN_RESERVATION",
                         f"Reservation {args.get('reservation_id')} could not be read.")
    if violation := _check_not_cancelled(reservation, "changing its passengers"):
        return violation
    requested = args.get("passengers") or []
    if len(requested) != len(reservation["passengers"]):
        return Violation(
            "PASSENGER_COUNT_IMMUTABLE",
            f"The number of passengers cannot be changed (this reservation has "
            f"{len(reservation['passengers'])}, the request has {len(requested)}). Not even a "
            f"human agent can do this, so do not transfer -- explain that a new booking is needed.",
        )
    return None


def check_book(args: dict, ctx: Context) -> Violation | None:
    """Booking: passenger cap, payment composition, availability, allowance, and total."""
    passengers = args.get("passengers") or []
    if not passengers:
        return Violation("NO_PASSENGERS", "A reservation needs at least one passenger.")
    if len(passengers) > MAX_PASSENGERS:
        return Violation(
            "TOO_MANY_PASSENGERS",
            f"A reservation may hold at most {MAX_PASSENGERS} passengers; {len(passengers)} were "
            f"requested. The customer needs a separate reservation for the rest.",
        )

    cabin = args.get("cabin")
    flights = args.get("flights") or []
    flight_type = args.get("flight_type")

    if not flights:
        return Violation("NO_FLIGHTS", "A reservation needs at least one flight.")

    keys = [(f["flight_number"], f["date"]) for f in flights]
    if len(set(keys)) != len(keys):
        return Violation(
            "DUPLICATE_SEGMENT",
            "The requested flights list repeats the same flight number and date more than once; "
            "each segment must be distinct.",
        )

    # One way is exactly one segment, round trip is exactly two (out and back); the schema has
    # no third trip type or multi-city concept, so any other segment count for either type is a
    # request that cannot correspond to a real trip_type value once written.
    expected_segments = {"one_way": 1, "round_trip": 2}.get(flight_type)
    if expected_segments is not None and len(flights) != expected_segments:
        return Violation(
            "SEGMENT_COUNT_MISMATCH",
            f"A {flight_type.replace('_', ' ')} reservation must have {expected_segments} "
            f"flight segment(s); {len(flights)} were requested.",
        )

    prices = []
    for f in flights:
        instance = ctx.available_flights.get((f["flight_number"], f["date"]))
        if not instance or cabin not in instance.get("prices", {}):
            return Violation(
                "NOT_BOOKABLE",
                f"Flight {f['flight_number']} is not available in {cabin} on {f['date']}.",
            )
        if instance.get("available_seats", {}).get(cabin, 0) < len(passengers):
            return Violation(
                "NOT_ENOUGH_SEATS",
                f"Flight {f['flight_number']} on {f['date']} does not have {len(passengers)} "
                f"{cabin} seats available.",
            )
        prices.append(instance["prices"][cabin])

    if violation := _check_booking_route(args, flights, ctx):
        return violation

    if violation := _check_booking_payments(args, ctx):
        return violation

    free = free_bag_allowance((ctx.user or {}).get("membership", "regular"), cabin, len(passengers))
    total_bags = args.get("total_baggages", 0)
    nonfree_bags = args.get("nonfree_baggages", 0)
    if total_bags < 0 or nonfree_bags < 0:
        return Violation(
            "BAGGAGE_COUNT_INVALID",
            f"Baggage counts must be non-negative (got total_baggages={total_bags}, "
            f"nonfree_baggages={nonfree_bags}).",
        )
    if nonfree_bags > total_bags:
        return Violation(
            "BAGGAGE_COUNT_INVALID",
            f"nonfree_baggages ({nonfree_bags}) cannot exceed total_baggages ({total_bags}).",
        )
    expected_nonfree = max(0, total_bags - free)
    if nonfree_bags != expected_nonfree:
        return Violation(
            "BAGGAGE_ALLOWANCE",
            f"A {(ctx.user or {}).get('membership')} member gets {free} free checked bag(s) for "
            f"{len(passengers)} {cabin} passenger(s), so {total_bags} total bags means "
            f"{expected_nonfree} paid bag(s), not {nonfree_bags}.",
        )

    expected_total = booking_total(prices, len(passengers), args.get("insurance"),
                                   expected_nonfree)
    charged = sum(p.get("amount", 0) for p in args.get("payment_methods") or [])
    if charged != expected_total:
        return Violation(
            "TOTAL_MISMATCH",
            f"The payment total does not match the price: flights "
            f"{'+'.join(str(p) for p in prices)} x {len(passengers)} passenger(s)"
            + (f", insurance {INSURANCE_FEE_PER_PASSENGER} x {len(passengers)}"
               if args.get("insurance") == "yes" else "")
            + (f", {expected_nonfree} extra bag(s) x {EXTRA_BAG_FEE}"
               if expected_nonfree else "")
            + f" = {expected_total}, but {charged} was submitted.",
        )
    return None


def _check_booking_route(args: dict, flights: list[dict], ctx: Context) -> Violation | None:
    """Booked segments must actually connect the stated origin to the stated destination.

    A one-way books a single origin->destination leg; a round trip books that leg followed by
    its mirror, destination->origin. Nothing else in check_book cross-references `origin` and
    `destination` against the segments actually being purchased, so a mismatched itinerary
    would otherwise write a reservation whose stated route and actual flights disagree.
    """
    origin, destination = args.get("origin"), args.get("destination")
    legs = [(origin, destination)]
    if args.get("flight_type") == "round_trip" and len(flights) > 1:
        legs.append((destination, origin))
    for f, (want_origin, want_destination) in zip(flights, legs):
        instance = ctx.available_flights.get((f["flight_number"], f["date"]))
        if instance is None:
            continue  # already reported as NOT_BOOKABLE by the caller
        if instance.get("origin") != want_origin or instance.get("destination") != want_destination:
            return Violation(
                "ROUTE_MISMATCH",
                f"Flight {f['flight_number']} on {f['date']} flies "
                f"{instance.get('origin')} -> {instance.get('destination')}, not "
                f"{want_origin} -> {want_destination} as this booking requires.",
            )
    return None


def _check_booking_payments(args: dict, ctx: Context) -> Violation | None:
    """At most one certificate, one credit card and three gift cards, all already stored."""
    methods = (ctx.user or {}).get("payment_methods", {})
    counts = {"certificate": 0, "credit_card": 0, "gift_card": 0}
    for payment in args.get("payment_methods") or []:
        pid = payment.get("payment_id")
        if pid not in methods:
            return Violation(
                "PAYMENT_NOT_IN_PROFILE",
                f"Payment method {pid} is not in the customer's profile. For safety, only stored "
                f"payment methods may be used.",
            )
        source = methods[pid].get("source")
        if source in counts:
            counts[source] += 1
    limits = (("certificate", MAX_CERTIFICATES), ("credit_card", MAX_CREDIT_CARDS),
              ("gift_card", MAX_GIFT_CARDS))
    for source, limit in limits:
        if counts[source] > limit:
            return Violation(
                "PAYMENT_COMPOSITION",
                f"A reservation may use at most {limit} {source.replace('_', ' ')}(s); "
                f"{counts[source]} were supplied.",
            )
    return None


def check_send_certificate(args: dict, ctx: Context) -> Violation | None:
    """Compensation: only for eligible customers, only for the two listed reasons, exact amount."""
    reservation, user = ctx.reservation, ctx.user
    if user is None:
        return Violation("UNKNOWN_USER",
                         f"User {args.get('user_id')} could not be read; confirm the facts before "
                         f"offering compensation.")
    if reservation is None:
        return Violation(
            "FACTS_NOT_CONFIRMED",
            "Compensation requires confirming the facts against the reservation first; read the "
            "reservation before sending a certificate.",
        )
    if not is_compensation_eligible(user, reservation):
        return Violation(
            "COMPENSATION_NOT_ELIGIBLE",
            f"This customer is a {user.get('membership')} member in {reservation['cabin']} with no "
            f"travel insurance, so no compensation is due.",
        )

    expected = compensation_amount(ctx.complaint, len(reservation["passengers"]))
    if expected is None:
        return Violation(
            "COMPENSATION_REASON",
            "Compensation is only available for a flight the airline cancelled, or for a delay "
            "where the reservation has just been changed or cancelled. No other reason qualifies.",
        )
    if ctx.complaint == "delayed_flight" and not ctx.reservation_resolved:
        return Violation(
            "COMPENSATION_PREMATURE",
            "A delay certificate is only offered after the reservation has actually been changed "
            "or cancelled. Complete that first.",
        )
    if args.get("amount") != expected:
        return Violation(
            "COMPENSATION_AMOUNT",
            f"The certificate must be {expected} for {len(reservation['passengers'])} "
            f"passenger(s), not {args.get('amount')}.",
        )
    return None


_CHECKS = {
    "cancel_reservation": check_cancel,
    "update_reservation_flights": check_update_flights,
    "update_reservation_baggages": check_update_baggages,
    "update_reservation_passengers": check_update_passengers,
    "book_reservation": check_book,
    "send_certificate": check_send_certificate,
}


def check(tool_name: str, arguments: dict, ctx: Context) -> Violation | None:
    """Judge one write call. Returns None to allow it, or the Violation that blocks it.

    Read-only tools are never blocked: they cannot damage the records, and the agent needs them
    freely to establish the facts.
    """
    checker = _CHECKS.get(tool_name)
    if checker is None:
        return None
    return checker(arguments or {}, ctx)
