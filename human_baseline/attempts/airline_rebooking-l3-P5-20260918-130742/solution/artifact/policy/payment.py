"""Payment-method rules for booking and for updates.

Two distinct rule sets:

* **Booking** -- at most one travel certificate, at most one credit card, at most three gift cards, and
  every method must already exist in the user's profile.
* **Update** (flight change / cabin difference / extra bags) -- exactly ONE method, which must be a
  gift card or credit card already in the profile. A certificate can never fund an update; the
  environment tool rejects it outright.

No new payment method can be added during an interaction, so "not in the profile" is always a denial.
"""
from __future__ import annotations

from typing import Any, Iterable

from domain import User
from policy.decision import Decision, allow, deny

MAX_CERTIFICATES = 1
MAX_CREDIT_CARDS = 1
MAX_GIFT_CARDS = 3


def _source_counts(user: User, payment_ids: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pid in payment_ids:
        source = user.payment_source(pid) or "unknown"
        counts[source] = counts.get(source, 0) + 1
    return counts


def check_booking_payments(user: User, payments: list[dict[str, Any]]) -> Decision:
    """Validate the payment methods for a new booking.

    Args:
        user: the booking user, as read from the environment.
        payments: ``[{"payment_id": ..., "amount": ...}, ...]`` as it will be sent to the tool.
    """
    if not payments:
        return deny("I need at least one payment method from your profile to book.",
                    rule="payment.none_supplied")

    ids = [p.get("payment_id", "") for p in payments]

    missing = [pid for pid in ids if pid not in user.payment_methods]
    if missing:
        return deny(
            "For security I can only use a payment method already saved on your profile, and I can't "
            f"add a new one during our conversation. These aren't on file: {', '.join(missing)}.",
            rule="payment.not_in_profile",
            missing=missing,
        )

    duplicates = sorted({pid for pid in ids if ids.count(pid) > 1})
    if duplicates:
        return deny(
            f"Each payment method can only be used once per reservation: {', '.join(duplicates)}.",
            rule="payment.duplicate_method",
            duplicates=duplicates,
        )

    counts = _source_counts(user, ids)
    for source, limit, label in (
        ("certificate", MAX_CERTIFICATES, "travel certificate"),
        ("credit_card", MAX_CREDIT_CARDS, "credit card"),
        ("gift_card", MAX_GIFT_CARDS, "gift cards"),
    ):
        if counts.get(source, 0) > limit:
            return deny(
                f"A reservation can use at most {limit} {label}, and this would use "
                f"{counts[source]}.",
                rule=f"payment.too_many_{source}",
                used=counts[source],
                limit=limit,
            )

    # Gift cards and certificates must actually hold the amount being drawn from them.
    for payment in payments:
        pid = payment.get("payment_id", "")
        amount = payment.get("amount")
        balance = user.payment_balance(pid)
        if balance is not None and amount is not None and amount > balance:
            return deny(
                f"{pid} only has ${balance} available, which doesn't cover the ${amount} assigned to "
                "it.",
                rule="payment.insufficient_balance",
                payment_id=pid,
                balance=balance,
                requested=amount,
            )

    return allow("Payment methods are valid for booking.", rule="payment.booking_ok",
                 sources=counts)


def check_update_payment(user: User, payment_id: str | None, amount_due: int = 0) -> Decision:
    """Validate the single payment method used for a change (charge or refund).

    Args:
        user: the reservation's owner.
        payment_id: the method the customer nominated.
        amount_due: positive when the customer owes money; the balance check only applies then.
    """
    if not payment_id:
        return deny(
            "For a change I need one gift card or credit card from your profile to settle the "
            "difference.",
            rule="payment.update_missing",
        )

    if payment_id not in user.payment_methods:
        return deny(
            "For security that has to be a payment method already saved on your profile, and I can't "
            "add a new one during our conversation.",
            rule="payment.not_in_profile",
            payment_id=payment_id,
        )

    source = user.payment_source(payment_id)
    if source == "certificate":
        return deny(
            "A travel certificate can't be used for a change -- it needs to be a gift card or credit "
            "card on your profile.",
            rule="payment.certificate_on_update",
            payment_id=payment_id,
        )
    if source not in {"gift_card", "credit_card"}:
        return deny(
            "A change has to be settled with a single gift card or credit card from your profile.",
            rule="payment.unsupported_source_on_update",
            payment_id=payment_id,
            source=source,
        )

    balance = user.payment_balance(payment_id)
    if amount_due > 0 and balance is not None and amount_due > balance:
        return deny(
            f"{payment_id} has ${balance} available, which doesn't cover the ${amount_due} difference.",
            rule="payment.insufficient_balance",
            payment_id=payment_id,
            balance=balance,
            requested=amount_due,
        )

    return allow("Payment method is valid for an update.", rule="payment.update_ok",
                 payment_id=payment_id, source=source)


def certificate_leftover_warning(user: User, payments: list[dict[str, Any]]) -> str | None:
    """Warn when a certificate's unused balance will be forfeited (it is never refundable)."""
    for payment in payments:
        pid = payment.get("payment_id", "")
        if user.payment_source(pid) != "certificate":
            continue
        balance = user.payment_balance(pid) or 0
        used = payment.get("amount") or 0
        if balance > used:
            return (
                f"Note: {pid} is worth ${balance} and only ${used} will be used. The remaining "
                f"${balance - used} is not refundable and would be lost."
            )
    return None
