"""Compensation: a discretionary goodwill certificate, never offered proactively.

Two gates, both of which must pass:

* **Eligibility** -- the user is silver/gold, OR the reservation has travel insurance, OR the cabin is
  business. The excluded combination is exactly: regular member AND no insurance AND (basic) economy.
  Per the product manager, the *insurance* route additionally requires the reason to be one insurance
  covers (health or weather), mirroring the insurance cancellation rule.
* **Situation** -- only two qualify:
    - a cancelled flight in the reservation -> $100 x passengers, after confirming the facts;
    - a delayed flight where the customer wants to change or cancel -> $50 x passengers, after
      confirming the facts AND after the change/cancel has actually been carried out.

No other grounds, ever. And the claim is always verified against flight status -- a customer saying
their flight was cancelled is not evidence that it was.
"""
from __future__ import annotations

from domain import Reservation, User
from policy.cancellation import reason_is_insurance_covered
from policy.decision import Decision, allow, deny

CANCELLED_RATE_PER_PASSENGER = 100
DELAYED_RATE_PER_PASSENGER = 50

SITUATION_CANCELLED = "cancelled"
SITUATION_DELAYED = "delayed"


def check_eligibility(user: User, reservation: Reservation, reason_detail: str | None = None) -> Decision:
    """The membership/insurance/cabin gate, independent of the situation."""
    if user.membership in {"silver", "gold"}:
        return allow(
            f"{user.membership.title()} membership qualifies for compensation.",
            rule="comp.eligible_membership",
            membership=user.membership,
        )

    if reservation.cabin == "business":
        return allow(
            "A business-cabin reservation qualifies for compensation.",
            rule="comp.eligible_business",
            cabin=reservation.cabin,
        )

    if reservation.insurance:
        # Ruling from the product manager: the insurance route needs an insurance-covered reason.
        if reason_is_insurance_covered(reason_detail):
            return allow(
                "Travel insurance on this reservation, with a reason it covers, qualifies for "
                "compensation.",
                rule="comp.eligible_insurance",
                reason_detail=reason_detail,
            )
        return deny(
            "This reservation has travel insurance, but that route to compensation only applies to "
            "health or weather reasons, and this isn't one of those.",
            rule="comp.insurance_reason_not_covered",
            reason_detail=reason_detail,
        )

    return deny(
        "I'm not able to offer compensation on this reservation. That's limited to silver and gold "
        "members, reservations with travel insurance, or business-cabin travel.",
        rule="comp.not_eligible",
        membership=user.membership,
        cabin=reservation.cabin,
        insurance=reservation.insurance,
    )


def check_compensation(
    user: User,
    reservation: Reservation,
    situation: str,
    user_asked: bool,
    reason_detail: str | None = None,
    write_completed: bool = False,
) -> Decision:
    """Decide whether a compensation certificate may be offered, and for how much.

    Args:
        user: the reservation's owner, read from the environment.
        reservation: the reservation, with every segment status already read.
        situation: ``SITUATION_CANCELLED`` or ``SITUATION_DELAYED``.
        user_asked: True only if the customer explicitly asked for compensation.
        reason_detail: the customer's own words, used for the insurance eligibility route.
        write_completed: for the delayed case, whether the change/cancel has already been done.

    Returns:
        ALLOW with ``facts["amount"]`` set, or DENY.
    """
    # Never proactive. This is the first gate on purpose.
    if not user_asked:
        return deny(
            "Compensation is only considered when a customer asks for it, so I won't raise it.",
            rule="comp.not_requested",
        )

    if not reservation.statuses_known:
        return deny(
            "I need to check the status of the flights on this reservation before I can talk about "
            "compensation.",
            rule="comp.statuses_unverified",
        )

    if situation not in {SITUATION_CANCELLED, SITUATION_DELAYED}:
        return deny(
            "Compensation only applies to a cancelled flight, or to a delayed flight where we change "
            "or cancel your booking. I can't offer it for anything else.",
            rule="comp.situation_not_qualifying",
            situation=situation,
        )

    # Verify the claimed situation against the record before anything else.
    if situation == SITUATION_CANCELLED and not reservation.has_airline_cancelled_segment:
        return deny(
            "I've checked each flight on this reservation and none of them is showing as cancelled, so "
            "there's no compensation I can offer on that basis.",
            rule="comp.cancellation_not_real",
            statuses={f"{s.flight_number}|{s.date}": s.status for s in reservation.segments},
        )
    if situation == SITUATION_DELAYED and not reservation.has_delayed_segment:
        return deny(
            "I've checked each flight on this reservation and none of them is showing as delayed, so "
            "there's no compensation I can offer on that basis.",
            rule="comp.delay_not_real",
            statuses={f"{s.flight_number}|{s.date}": s.status for s in reservation.segments},
        )

    eligibility = check_eligibility(user, reservation, reason_detail)
    if not eligibility.allowed:
        return eligibility

    # Ordering rule: the delayed case pays only after the change/cancel has actually been done.
    if situation == SITUATION_DELAYED and not write_completed:
        return deny(
            "For a delayed flight, the compensation comes after I've actually changed or cancelled the "
            "reservation for you.",
            rule="comp.delay_write_not_done",
        )

    passengers = reservation.passenger_count
    rate = (
        CANCELLED_RATE_PER_PASSENGER
        if situation == SITUATION_CANCELLED
        else DELAYED_RATE_PER_PASSENGER
    )
    amount = rate * passengers
    return allow(
        f"A ${amount} travel certificate can be offered (${rate} per passenger for {passengers}).",
        rule=f"comp.allowed_{situation}",
        amount=amount,
        rate=rate,
        passengers=passengers,
        eligibility_rule=eligibility.rule,
    )
