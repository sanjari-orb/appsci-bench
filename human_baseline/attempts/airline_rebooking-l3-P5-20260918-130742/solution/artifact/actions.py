"""The gated action layer: the ONLY path from an intent to a write tool.

This module is the reason policy correctness does not depend on the LLM behaving. Each `request_*`
method:

1. reads the facts it needs from the environment (never trusting a customer claim);
2. asks the matching `policy/` function for a verdict;
3. on ALLOW, returns a *pending action* carrying a human-readable confirmation summary -- it does NOT
   write;
4. the write happens only in `execute_confirmed()`, and only after the caller records an explicit
   customer "yes".

So a DENY or TRANSFER verdict makes the corresponding write physically unreachable, and an ALLOW still
cannot fire without confirmation. An LLM that "agrees" with a pushy customer cannot bypass either gate.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from domain import Reservation, User
from env_client import ToolCallError, ToolRejection
from logging_setup import get_logger
from policy import baggage as baggage_policy
from policy import booking as booking_policy
from policy import cancellation as cancellation_policy
from policy import compensation as compensation_policy
from policy import modification as modification_policy
from policy import payment as payment_policy
from policy.decision import Decision, Outcome, deny

log = get_logger("actions")


@dataclass
class PendingAction:
    """An allowed write, awaiting the customer's explicit yes.

    Attributes:
        kind: e.g. ``"cancel"``, ``"book"``, ``"cabin_change"``.
        tool: the environment write tool that will be called.
        arguments: exact arguments for that call.
        summary: the details to read back to the customer before asking for confirmation.
        decision: the verdict that permitted this action.
        token: identifier, so a confirmation cannot be applied to a different pending action.
        follow_up: an optional second write (used for delay compensation, which must come after).
    """

    kind: str
    tool: str
    arguments: dict[str, Any]
    summary: str
    decision: Decision
    token: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    follow_up: "PendingAction | None" = None


@dataclass
class ActionResult:
    """What the orchestrator should tell the customer, plus what actually happened."""

    message: str
    outcome: Outcome
    pending: PendingAction | None = None
    executed: list[str] = field(default_factory=list)
    transferred: bool = False
    facts: dict[str, Any] = field(default_factory=dict)


class ActionService:
    """Verifies facts, applies policy, and performs confirmed writes.

    Args:
        client: an `EnvClient` (or a compatible fake).
    """

    def __init__(self, client: Any):
        self.client = client
        # Facts read from the environment this conversation. Populated only by real reads.
        self.verified_users: dict[str, dict[str, Any]] = {}
        self.verified_reservations: dict[str, dict[str, Any]] = {}
        self.verified_statuses: dict[str, dict[tuple[str, str], str]] = {}
        # Writes already applied, so nothing is repeated.
        self.completed_writes: list[dict[str, Any]] = []
        # The airport list, read once when a fare lookup needs it. None means "not read yet".
        self._airports: list[str] | None = None

    # --- verified reads ----------------------------------------------------------------------------
    def load_user(self, user_id: str) -> User | None:
        """Read and cache a user. Returns None when the id does not resolve."""
        if user_id in self.verified_users:
            return User(self.verified_users[user_id])
        try:
            raw = self.client.get_user_details(user_id)
        except ToolRejection:
            return None
        self.verified_users[user_id] = raw
        return User(raw)

    def load_reservation(self, reservation_id: str, *, read_statuses: bool = True) -> Reservation | None:
        """Read and cache a reservation, and by default the live status of each of its segments."""
        raw = self.verified_reservations.get(reservation_id)
        if raw is None:
            try:
                raw = self.client.get_reservation_details(reservation_id)
            except ToolRejection:
                return None
            self.verified_reservations[reservation_id] = raw

        statuses = self.verified_statuses.get(reservation_id)
        if read_statuses and statuses is None:
            statuses = self.client.segment_statuses(raw)
            self.verified_statuses[reservation_id] = statuses
        return Reservation(raw, statuses or None)

    def owns(self, user: User, reservation: Reservation) -> bool:
        """Ownership check, required before acting on any reservation."""
        return reservation.user_id == user.user_id or reservation.reservation_id in user.reservation_ids

    # --- cancellation ------------------------------------------------------------------------------
    def request_cancellation(
        self, user_id: str, reservation_id: str, reason: str, reason_detail: str | None = None
    ) -> ActionResult:
        """Verify, decide, and (on ALLOW) return a pending cancellation for confirmation."""
        context = self._load_pair(user_id, reservation_id)
        if isinstance(context, ActionResult):
            return context
        user, reservation = context

        decision = cancellation_policy.check_cancellation(reservation, reason, reason_detail)
        log.info(
            "cancellation decision",
            extra={"reservation_id": reservation_id, **decision.as_dict()},
        )

        if decision.outcome is Outcome.TRANSFER:
            return self._do_transfer(decision, reservation_id=reservation_id)
        if not decision.allowed:
            return ActionResult(decision.reason, Outcome.DENY, facts=decision.facts)

        refund = cancellation_policy.refund_summary(reservation)
        summary = (
            f"Cancelling reservation {reservation.reservation_id}: "
            f"{reservation.origin} to {reservation.destination}, "
            f"{reservation.cabin.replace('_', ' ')}, "
            f"{reservation.passenger_count} passenger(s). "
            f"This cancels the entire reservation. {refund}"
        )
        pending = PendingAction(
            kind="cancel",
            tool="cancel_reservation",
            arguments={"reservation_id": reservation.reservation_id},
            summary=summary,
            decision=decision,
        )
        return ActionResult(summary, Outcome.ALLOW, pending=pending, facts=decision.facts)

    # --- cabin change ------------------------------------------------------------------------------
    def request_cabin_change(
        self, user_id: str, reservation_id: str, new_cabin: str, payment_id: str | None = None
    ) -> ActionResult:
        """Cabin change, including the price difference and the payment method rules."""
        context = self._load_pair(user_id, reservation_id)
        if isinstance(context, ActionResult):
            return context
        user, reservation = context

        decision = modification_policy.check_cabin_change(reservation, new_cabin)
        log.info("cabin decision", extra={"reservation_id": reservation_id, **decision.as_dict()})
        if decision.outcome is Outcome.TRANSFER:
            return self._do_transfer(decision, reservation_id=reservation_id)
        if not decision.allowed:
            return ActionResult(decision.reason, Outcome.DENY, facts=decision.facts)

        # Re-price every segment at the new cabin: the tool only keeps a segment's original price when
        # the cabin is unchanged, so a cabin change re-prices the whole itinerary.
        try:
            new_total, per_segment = self._price_itinerary(reservation, new_cabin)
        except ToolRejection as exc:
            return ActionResult(
                f"I couldn't price that cabin change: {exc.detail}", Outcome.DENY
            )
        old_total = sum(s.price for s in reservation.segments) * reservation.passenger_count
        difference = new_total - old_total

        payment_decision = payment_policy.check_update_payment(
            user, payment_id, amount_due=max(0, difference)
        )
        if not payment_decision.allowed:
            return ActionResult(
                payment_decision.reason, Outcome.DENY, facts=payment_decision.facts
            )

        direction = (
            f"You would pay an extra ${difference}"
            if difference > 0
            else f"You would be refunded ${abs(difference)}"
            if difference < 0
            else "There is no price difference"
        )
        summary = (
            f"Changing reservation {reservation.reservation_id} from "
            f"{reservation.cabin.replace('_', ' ')} to {new_cabin.replace('_', ' ')} on all "
            f"{len(reservation.segments)} flight(s), for {reservation.passenger_count} passenger(s). "
            f"{direction}, using {payment_id}."
        )
        pending = PendingAction(
            kind="cabin_change",
            tool="update_reservation_flights",
            arguments={
                "reservation_id": reservation.reservation_id,
                "cabin": new_cabin,
                "flights": [
                    {"flight_number": s.flight_number, "date": s.date} for s in reservation.segments
                ],
                "payment_id": payment_id,
            },
            summary=summary,
            decision=decision,
        )
        return ActionResult(
            summary,
            Outcome.ALLOW,
            pending=pending,
            facts={**decision.facts, "difference": difference, "per_segment": per_segment},
        )

    # --- baggage -----------------------------------------------------------------------------------
    def request_add_baggage(
        self, user_id: str, reservation_id: str, new_total_bags: int, payment_id: str | None = None
    ) -> ActionResult:
        context = self._load_pair(user_id, reservation_id)
        if isinstance(context, ActionResult):
            return context
        user, reservation = context

        decision = baggage_policy.check_add_baggage(reservation.total_baggages, new_total_bags)
        log.info("baggage decision", extra={"reservation_id": reservation_id, **decision.as_dict()})
        if not decision.allowed:
            return ActionResult(decision.reason, Outcome.DENY, facts=decision.facts)

        # The allowance follows the booking user's membership and the reservation cabin.
        chargeable = baggage_policy.nonfree_bags(
            user.membership, reservation.cabin, reservation.passenger_count, new_total_bags
        )
        # Only the *newly* chargeable bags are billed. The baseline is derived from the allowance and
        # the bags already on the reservation, NOT from the stored `nonfree_baggages`: that field is 0
        # on every reservation in the store, including ones already carrying bags beyond their free
        # allowance, so trusting it re-charged the customer for bags they already had (adding one bag
        # to a 1-bag basic-economy reservation with no free allowance billed $100 instead of $50).
        previously_chargeable = baggage_policy.nonfree_bags(
            user.membership, reservation.cabin, reservation.passenger_count,
            reservation.total_baggages,
        )
        extra_charge = baggage_policy.EXTRA_BAG_FEE * max(0, chargeable - previously_chargeable)

        if extra_charge > 0:
            payment_decision = payment_policy.check_update_payment(
                user, payment_id, amount_due=extra_charge
            )
            if not payment_decision.allowed:
                return ActionResult(
                    payment_decision.reason, Outcome.DENY, facts=payment_decision.facts
                )

        free_allowance = baggage_policy.free_bag_allowance(
            user.membership, reservation.cabin, reservation.passenger_count
        )
        added = new_total_bags - reservation.total_baggages
        if extra_charge > 0:
            cost_text = (
                f"That adds ${extra_charge} for {chargeable - previously_chargeable} chargeable "
                f"bag(s) at ${baggage_policy.EXTRA_BAG_FEE} each, charged to {payment_id}."
            )
        elif chargeable > 0:
            # Beyond the free allowance, but the chargeable count is unchanged, so nothing new is owed.
            cost_text = "There's no additional charge for this change."
        else:
            cost_text = "There's no charge -- it's within your free allowance."
        summary = (
            f"Updating reservation {reservation.reservation_id} from "
            f"{reservation.total_baggages} to {new_total_bags} checked bag(s), "
            f"adding {added}. "
            f"Your {user.membership} membership in {reservation.cabin.replace('_', ' ')} includes "
            f"{free_allowance} free bag(s). {cost_text}"
        )
        pending = PendingAction(
            kind="baggage",
            tool="update_reservation_baggages",
            arguments={
                "reservation_id": reservation.reservation_id,
                "total_baggages": new_total_bags,
                "nonfree_baggages": chargeable,
                "payment_id": payment_id,
            },
            summary=summary,
            decision=decision,
        )
        return ActionResult(
            summary, Outcome.ALLOW, pending=pending, facts={"charge": extra_charge}
        )

    # --- passengers --------------------------------------------------------------------------------
    def request_passenger_update(
        self, user_id: str, reservation_id: str, passengers: list[dict[str, Any]]
    ) -> ActionResult:
        context = self._load_pair(user_id, reservation_id)
        if isinstance(context, ActionResult):
            return context
        _user, reservation = context

        decision = modification_policy.check_passenger_change(reservation, len(passengers))
        log.info("passenger decision", extra={"reservation_id": reservation_id, **decision.as_dict()})
        if not decision.allowed:
            return ActionResult(decision.reason, Outcome.DENY, facts=decision.facts)

        details = booking_policy.check_passengers(passengers)
        if not details.allowed:
            return ActionResult(details.reason, Outcome.DENY, facts=details.facts)

        listed = "; ".join(
            f"{p['first_name']} {p['last_name']} (born {p['dob']})" for p in passengers
        )
        summary = (
            f"Updating the passenger details on reservation {reservation.reservation_id} to: {listed}. "
            "The number of passengers stays the same."
        )
        pending = PendingAction(
            kind="passengers",
            tool="update_reservation_passengers",
            arguments={
                "reservation_id": reservation.reservation_id,
                "passengers": passengers,
            },
            summary=summary,
            decision=decision,
        )
        return ActionResult(summary, Outcome.ALLOW, pending=pending)

    # --- compensation ------------------------------------------------------------------------------
    def request_compensation(
        self,
        user_id: str,
        reservation_id: str,
        situation: str,
        user_asked: bool,
        reason_detail: str | None = None,
    ) -> ActionResult:
        """Compensation is only ever reached because the customer asked for it."""
        context = self._load_pair(user_id, reservation_id)
        if isinstance(context, ActionResult):
            return context
        user, reservation = context

        write_completed = any(
            w["arguments"].get("reservation_id") == reservation_id
            and w["tool"] in {"cancel_reservation", "update_reservation_flights"}
            for w in self.completed_writes
        )
        decision = compensation_policy.check_compensation(
            user,
            reservation,
            situation,
            user_asked=user_asked,
            reason_detail=reason_detail,
            write_completed=write_completed,
        )
        log.info(
            "compensation decision",
            extra={"reservation_id": reservation_id, "situation": situation, **decision.as_dict()},
        )
        if not decision.allowed:
            return ActionResult(decision.reason, Outcome.DENY, facts=decision.facts)

        amount = decision.facts["amount"]
        summary = (
            f"Sending a ${amount} travel certificate to your account "
            f"(${decision.facts['rate']} per passenger for {decision.facts['passengers']})."
        )
        pending = PendingAction(
            kind="compensation",
            tool="send_certificate",
            arguments={"user_id": user.user_id, "amount": amount},
            summary=summary,
            decision=decision,
        )
        return ActionResult(summary, Outcome.ALLOW, pending=pending, facts=decision.facts)

    # --- booking -----------------------------------------------------------------------------------
    def request_booking(
        self,
        user_id: str,
        flight_type: str,
        origin: str,
        destination: str,
        cabin: str,
        segments: list[dict[str, Any]],
        passengers: list[dict[str, Any]],
        payments: list[dict[str, Any]],
        total_bags: int = 0,
        insurance: bool | None = None,
    ) -> ActionResult:
        """Validate a whole booking and produce the confirmation summary.

        `payments` amounts may be omitted (``{"payment_id": ...}``); when a single method is given its
        amount is set to the computed total, which is what the tool requires.
        """
        user = self.load_user(user_id)
        if user is None:
            return ActionResult(
                "I couldn't find an account with that user id. Could you double-check it?",
                Outcome.DENY,
            )

        for check in (
            booking_policy.check_cabin(cabin),
            booking_policy.check_passengers(passengers),
            booking_policy.check_insurance_decision(insurance),
        ):
            if not check.allowed:
                return ActionResult(check.reason, Outcome.DENY, facts=check.facts)

        statuses: dict[tuple[str, str], str] = {}
        prices: list[int] = []
        for segment in segments:
            key = (segment.get("flight_number", ""), segment.get("date", ""))
            try:
                statuses[key] = self.client.get_flight_status(*key)
            except ToolRejection as exc:
                return ActionResult(
                    f"I couldn't find flight {key[0]} on {key[1]}: {exc.detail}", Outcome.DENY
                )

        itinerary = booking_policy.check_itinerary(
            flight_type, origin, destination, segments, statuses
        )
        if not itinerary.allowed:
            return ActionResult(itinerary.reason, Outcome.DENY, facts=itinerary.facts)

        # Prices come from the live instances, matching what the tool will compute.
        for segment in segments:
            price = self._segment_price(segment, cabin)
            if price is None:
                return ActionResult(
                    f"I couldn't read the {cabin.replace('_', ' ')} fare for "
                    f"{segment.get('flight_number')} on {segment.get('date')}.",
                    Outcome.DENY,
                )
            prices.append(price)

        breakdown = booking_policy.price_breakdown(
            user, cabin, prices, len(passengers), bool(insurance), total_bags
        )
        total = breakdown["total"]

        priced_payments = self._assign_amounts(payments, total)
        payment_decision = payment_policy.check_booking_payments(user, priced_payments)
        if not payment_decision.allowed:
            return ActionResult(
                payment_decision.reason, Outcome.DENY, facts=payment_decision.facts
            )
        if sum(p["amount"] for p in priced_payments) != total:
            return ActionResult(
                f"The payment amounts need to add up to exactly ${total}.", Outcome.DENY
            )

        leftover = payment_policy.certificate_leftover_warning(user, priced_payments)
        itinerary_text = "; ".join(
            f"{s['flight_number']} on {s['date']}" for s in segments
        )
        passenger_text = "; ".join(
            f"{p['first_name']} {p['last_name']} (born {p['dob']})" for p in passengers
        )
        summary = (
            f"Booking a {flight_type.replace('_', ' ')} trip {origin} to {destination} in "
            f"{cabin.replace('_', ' ')}. Flights: {itinerary_text}. "
            f"Passengers: {passenger_text}. "
            f"Checked bags: {total_bags} ({breakdown['nonfree_baggages']} chargeable). "
            f"Travel insurance: {'yes' if insurance else 'no'}. "
            f"Fare ${breakdown['fare']}"
            + (f", insurance ${breakdown['insurance']}" if breakdown["insurance"] else "")
            + (f", baggage ${breakdown['baggage']}" if breakdown["baggage"] else "")
            + f". Total ${total}, paid with "
            + ", ".join(f"{p['payment_id']} (${p['amount']})" for p in priced_payments)
            + "."
        )
        if leftover:
            summary += f" {leftover}"

        pending = PendingAction(
            kind="book",
            tool="book_reservation",
            arguments={
                "user_id": user.user_id,
                "origin": origin,
                "destination": destination,
                "flight_type": flight_type,
                "cabin": cabin,
                "flights": [
                    {"flight_number": s["flight_number"], "date": s["date"]} for s in segments
                ],
                "passengers": passengers,
                "payment_methods": priced_payments,
                "total_baggages": total_bags,
                "nonfree_baggages": breakdown["nonfree_baggages"],
                "insurance": "yes" if insurance else "no",
            },
            summary=summary,
            decision=itinerary,
        )
        return ActionResult(summary, Outcome.ALLOW, pending=pending, facts=breakdown)

    # --- flight change -----------------------------------------------------------------------------
    def request_flight_change(
        self,
        user_id: str,
        reservation_id: str,
        new_segments: list[dict[str, Any]],
        payment_id: str | None = None,
        new_origin: str | None = None,
        new_destination: str | None = None,
        new_flight_type: str | None = None,
    ) -> ActionResult:
        context = self._load_pair(user_id, reservation_id)
        if isinstance(context, ActionResult):
            return context
        user, reservation = context

        decision = modification_policy.check_flight_change(
            reservation, new_origin, new_destination, new_flight_type
        )
        log.info("flight change decision", extra={"reservation_id": reservation_id, **decision.as_dict()})
        if decision.outcome is Outcome.TRANSFER:
            return self._do_transfer(decision, reservation_id=reservation_id)
        if not decision.allowed:
            return ActionResult(decision.reason, Outcome.DENY, facts=decision.facts)

        existing = {s.key: s for s in reservation.segments}
        statuses: dict[tuple[str, str], str] = {}
        for segment in new_segments:
            key = (segment.get("flight_number", ""), segment.get("date", ""))
            if key in existing:
                continue  # kept segment: keeps its original price, no availability requirement
            try:
                statuses[key] = self.client.get_flight_status(*key)
            except ToolRejection as exc:
                return ActionResult(
                    f"I couldn't find flight {key[0]} on {key[1]}: {exc.detail}", Outcome.DENY
                )

        available = modification_policy.check_new_segments_bookable(statuses)
        if not available.allowed:
            return ActionResult(available.reason, Outcome.DENY, facts=available.facts)

        # Kept segments keep their original price; new ones are priced at current fares.
        total = 0
        for segment in new_segments:
            key = (segment.get("flight_number", ""), segment.get("date", ""))
            if key in existing:
                total += existing[key].price * reservation.passenger_count
                continue
            price = self._segment_price(segment, reservation.cabin)
            if price is None:
                return ActionResult(
                    f"I couldn't read the fare for {key[0]} on {key[1]}.", Outcome.DENY
                )
            total += price * reservation.passenger_count

        old_total = sum(s.price for s in reservation.segments) * reservation.passenger_count
        difference = total - old_total

        payment_decision = payment_policy.check_update_payment(
            user, payment_id, amount_due=max(0, difference)
        )
        if not payment_decision.allowed:
            return ActionResult(payment_decision.reason, Outcome.DENY, facts=payment_decision.facts)

        direction = (
            f"You would pay an extra ${difference}"
            if difference > 0
            else f"You would be refunded ${abs(difference)}"
            if difference < 0
            else "There is no price difference"
        )
        itinerary_text = "; ".join(
            f"{s['flight_number']} on {s['date']}" for s in new_segments
        )
        summary = (
            f"Changing the flights on reservation {reservation.reservation_id} to: {itinerary_text}, "
            f"staying in {reservation.cabin.replace('_', ' ')} for "
            f"{reservation.passenger_count} passenger(s). {direction}, using {payment_id}. "
            "Any flight you keep stays at its original price."
        )
        pending = PendingAction(
            kind="flight_change",
            tool="update_reservation_flights",
            arguments={
                "reservation_id": reservation.reservation_id,
                "cabin": reservation.cabin,
                "flights": [
                    {"flight_number": s["flight_number"], "date": s["date"]} for s in new_segments
                ],
                "payment_id": payment_id,
            },
            summary=summary,
            decision=decision,
        )
        return ActionResult(
            summary, Outcome.ALLOW, pending=pending, facts={"difference": difference}
        )

    # --- denials that need no lookup ---------------------------------------------------------------
    def deny_insurance_change(self, add: bool = True) -> ActionResult:
        decision = modification_policy.check_insurance_change(add=add)
        return ActionResult(decision.reason, Outcome.DENY, facts=decision.facts)

    # --- execution ---------------------------------------------------------------------------------
    def execute_confirmed(self, pending: PendingAction) -> ActionResult:
        """Perform the write. Only ever called after an explicit customer "yes"."""
        executed: list[str] = []
        try:
            self.client.call(pending.tool, pending.arguments)
            executed.append(pending.tool)
            self.completed_writes.append(
                {"tool": pending.tool, "arguments": pending.arguments}
            )
        except ToolRejection as exc:
            log.warning("write rejected", extra={"tool": pending.tool, "detail": exc.detail})
            return ActionResult(
                f"I wasn't able to complete that: {exc.detail}", Outcome.DENY
            )
        except ToolCallError as exc:
            # Our own bug or a transport failure -- never surface the detail to the customer.
            log.error("write failed", extra={"tool": pending.tool, "detail": exc.detail})
            return ActionResult(
                "Something went wrong on my side and I couldn't complete that. Nothing has been "
                "changed on your reservation.",
                Outcome.DENY,
            )

        # Invalidate caches for the touched reservation so later reads see the new state.
        reservation_id = pending.arguments.get("reservation_id")
        if reservation_id:
            self.verified_reservations.pop(reservation_id, None)
            self.verified_statuses.pop(reservation_id, None)

        log.info("write executed", extra={"tool": pending.tool, "kind": pending.kind})
        return ActionResult(
            f"Done. {pending.summary}", Outcome.ALLOW, executed=executed
        )

    # --- internals ---------------------------------------------------------------------------------
    def _load_pair(self, user_id: str, reservation_id: str):
        """Load user + reservation and verify ownership. Returns (User, Reservation) or an ActionResult."""
        user = self.load_user(user_id)
        if user is None:
            return ActionResult(
                "I couldn't find an account with that user id. Could you double-check it?",
                Outcome.DENY,
            )
        reservation = self.load_reservation(reservation_id)
        if reservation is None:
            return ActionResult(
                f"I couldn't find a reservation with the id {reservation_id}. "
                "I can list the reservations on your account if that helps.",
                Outcome.DENY,
            )
        if not self.owns(user, reservation):
            log.warning(
                "ownership mismatch",
                extra={"user_id": user_id, "reservation_id": reservation_id},
            )
            return ActionResult(
                f"Reservation {reservation_id} isn't on the account for {user_id}, so I can't make "
                "changes to it.",
                Outcome.DENY,
            )
        return user, reservation

    def _do_transfer(self, decision: Decision, **context: Any) -> ActionResult:
        summary = f"{decision.reason} (rule: {decision.rule}; {context})"
        try:
            self.client.transfer_to_human_agents(summary)
        except (ToolRejection, ToolCallError) as exc:
            log.error("transfer failed", extra={"detail": str(exc)})
        return ActionResult(
            decision.reason, Outcome.TRANSFER, transferred=True, facts=decision.facts
        )

    def _segment_price(self, segment: dict[str, Any], cabin: str) -> int | None:
        """Current fare for a dated flight instance in `cabin`, via search on its own route."""
        flight_number = segment.get("flight_number", "")
        date = segment.get("date", "")
        origin = segment.get("origin")
        destination = segment.get("destination")
        if origin and destination:
            for candidate in self.client.search_direct_flight(origin, destination, date):
                if candidate.get("flight_number") == flight_number:
                    return candidate.get("prices", {}).get(cabin)
        # No route hint: the booking tool's segment schema makes origin/destination optional, so the
        # model often omits them. This previously read `self.client.searches`, an attribute that exists
        # only on the test fake -- against the real client it always returned None, so an otherwise
        # valid booking failed with "I couldn't read the fare". Search the real routes instead.
        return self._find_fare_across_routes(flight_number, date, cabin)

    def _find_fare_across_routes(self, flight_number: str, date: str, cabin: str) -> int | None:
        """Locate a dated flight's fare without a route hint, by searching the known airport pairs."""
        for origin, destination in self._airport_pairs():
            try:
                candidates = self.client.search_direct_flight(origin, destination, date)
            except (ToolRejection, ToolCallError):
                continue  # one unusable pair must not abort the search
            for candidate in candidates:
                if candidate.get("flight_number") == flight_number:
                    return candidate.get("prices", {}).get(cabin)
        return None

    def _airport_pairs(self) -> list[tuple[str, str]]:
        """Every ordered airport pair, cached per conversation (the network is a handful of airports)."""
        if self._airports is None:
            try:
                self._airports = list(self.client.list_all_airports())
            except (ToolRejection, ToolCallError):
                self._airports = []
        return [(o, d) for o in self._airports for d in self._airports if o != d]

    def _price_itinerary(self, reservation: Reservation, cabin: str) -> tuple[int, list[int]]:
        """Total and per-segment prices for the reservation's segments in `cabin`."""
        prices: list[int] = []
        for segment in reservation.segments:
            price = self._segment_price(
                {
                    "flight_number": segment.flight_number,
                    "date": segment.date,
                    "origin": segment.origin,
                    "destination": segment.destination,
                },
                cabin,
            )
            if price is None:
                raise ToolRejection(
                    "search_direct_flight",
                    f"no {cabin} fare for {segment.flight_number} on {segment.date}",
                )
            prices.append(price)
        return sum(prices) * reservation.passenger_count, prices

    @staticmethod
    def _assign_amounts(payments: list[dict[str, Any]], total: int) -> list[dict[str, Any]]:
        """Fill in a missing amount when a single payment method covers the whole total."""
        priced = [dict(p) for p in payments]
        if len(priced) == 1 and priced[0].get("amount") is None:
            priced[0]["amount"] = total
        return [p for p in priced if p.get("amount") is not None]
