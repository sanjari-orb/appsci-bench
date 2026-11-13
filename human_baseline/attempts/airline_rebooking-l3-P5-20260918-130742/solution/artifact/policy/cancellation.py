"""Cancellation eligibility.

Precedence matters and is deliberate:

1. **A flown segment wins over everything.** If any portion has already been flown, the request is a
   TRANSFER even when another condition (business cabin, 24-hour window) would otherwise qualify it.
   Confirmed with the product manager: "flown wins".
2. Otherwise the reservation is cancellable if ANY of the four conditions holds:
   booked within 24 hours / airline cancelled a flight / business cabin / insurance AND a covered
   reason (health or weather).
3. Otherwise DENY.

Every condition except the *reason* is verified against the record, never taken from the customer. The
reason is the one input we cannot verify, so it is accepted as stated -- but it only ever matters on the
insurance path.
"""
from __future__ import annotations

from datetime import datetime

from domain import Reservation
from policy.decision import Decision, allow, deny, transfer

# Cancellation reasons the agent recognises.
REASON_CHANGE_OF_PLAN = "change_of_plan"
REASON_AIRLINE_CANCELLED = "airline_cancelled"
REASON_OTHER = "other"
VALID_REASONS = frozenset({REASON_CHANGE_OF_PLAN, REASON_AIRLINE_CANCELLED, REASON_OTHER})

# Travel insurance covers health and weather reasons; those are the only reasons that unlock the
# insurance path. They arrive as free text from the customer, so they are matched on keywords.
INSURANCE_COVERED_KEYWORDS = (
    "health", "medical", "illness", "ill", "sick", "injury", "hospital", "doctor",
    "weather", "storm", "hurricane", "snow", "flood",
)

REFUND_TIMEFRAME = "5 to 7 business days"


def reason_is_insurance_covered(reason_detail: str | None) -> bool:
    """True when the customer's stated reason is a health or weather reason.

    The reason itself cannot be verified against any record, so it is taken as stated. Whether
    insurance *exists* is always checked against the reservation.
    """
    text = (reason_detail or "").lower()
    return any(keyword in text for keyword in INSURANCE_COVERED_KEYWORDS)


def check_cancellation(
    reservation: Reservation,
    reason: str,
    reason_detail: str | None = None,
    reference_time: datetime | None = None,
) -> Decision:
    """Decide whether a reservation may be cancelled.

    Args:
        reservation: the reservation, with every segment status already read from the environment.
        reason: one of ``VALID_REASONS``.
        reason_detail: the customer's own words, used only to test insurance coverage.
        reference_time: overrides the fixed clock (tests only).

    Returns:
        ALLOW to cancel, DENY if no condition qualifies, TRANSFER if a portion has been flown.
    """
    if reservation.cancelled:
        return deny(
            "That reservation is already cancelled, so there's nothing further for me to cancel.",
            rule="cancel.already_cancelled",
            reservation_id=reservation.reservation_id,
        )

    # Guard: never decide on unverified facts. The orchestrator must read every segment status first.
    if not reservation.statuses_known:
        return deny(
            "I still need to check the current status of each flight on this reservation before I can "
            "act on it.",
            rule="cancel.statuses_unverified",
            reservation_id=reservation.reservation_id,
        )

    # 1. Flown beats every qualifying condition.
    if reservation.has_flown_segment:
        return transfer(
            "Part of this itinerary has already been flown, so I can't cancel it myself -- I'll pass "
            "you to a human colleague who can look at it.",
            rule="cancel.flown_transfer",
            reservation_id=reservation.reservation_id,
            flown_segments=[s.key for s in reservation.segments if s.flown],
        )

    if reason not in VALID_REASONS:
        return deny(
            "I need to record why you're cancelling: a change of plan, the airline cancelling your "
            "flight, or another reason.",
            rule="cancel.reason_missing",
            reason=reason,
        )

    # 2. Any one qualifying condition is enough. Each is checked against the record.
    if reservation.booked_within_hours(24, reference_time):
        return allow(
            "This booking was made within the last 24 hours, so it can be cancelled.",
            rule="cancel.within_24h",
            created_at=reservation.raw.get("created_at"),
        )

    if reservation.has_airline_cancelled_segment:
        return allow(
            "The airline cancelled a flight on this reservation, so it can be cancelled.",
            rule="cancel.airline_cancelled",
            cancelled_segments=[s.key for s in reservation.segments if s.cancelled_by_airline],
        )

    if reservation.cabin == "business":
        return allow(
            "This is a business-cabin reservation, so it can be cancelled.",
            rule="cancel.business_cabin",
            cabin=reservation.cabin,
        )

    if reservation.insurance:
        if reason_is_insurance_covered(reason_detail):
            return allow(
                "This reservation has travel insurance and the reason is covered by it, so it can be "
                "cancelled for a full refund.",
                rule="cancel.insurance_covered",
                reason_detail=reason_detail,
            )
        return deny(
            "This reservation has travel insurance, but that covers cancellations for health or "
            "weather reasons. The reason given isn't one of those, and none of the other conditions "
            "for cancelling apply here.",
            rule="cancel.insurance_reason_not_covered",
            reason=reason,
            reason_detail=reason_detail,
        )

    # 3. Nothing qualifies.
    return deny(
        "I'm not able to cancel this reservation. It wasn't booked in the last 24 hours, the airline "
        "hasn't cancelled any of these flights, it isn't a business-cabin booking, and there's no "
        "travel insurance on it.",
        rule="cancel.no_qualifying_condition",
        cabin=reservation.cabin,
        insurance=reservation.insurance,
        created_at=reservation.raw.get("created_at"),
    )


def refund_summary(reservation: Reservation) -> str:
    """Plain description of what happens to the money after an allowed cancellation."""
    text = (
        f"The refund goes back to your original payment method within {REFUND_TIMEFRAME}."
    )
    if reservation.paid_with_certificate:
        text += (
            " One note: any unused amount on a travel certificate isn't refundable, so that portion "
            "can't come back."
        )
    return text
