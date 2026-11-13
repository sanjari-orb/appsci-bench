"""Free checked-bag allowance and extra-bag pricing.

The allowance is a pure function of the booking user's membership level and the reservation cabin.
Anything beyond the allowance costs $50 per bag. Travel insurance is $30 per passenger (priced here
because booking totals need both numbers in one place).
"""
from __future__ import annotations

from policy.decision import Decision, allow, deny

EXTRA_BAG_FEE = 50
INSURANCE_FEE_PER_PASSENGER = 30

# (membership, cabin) -> free checked bags PER PASSENGER. Straight from the policy table.
FREE_BAGS: dict[tuple[str, str], int] = {
    ("regular", "basic_economy"): 0,
    ("regular", "economy"): 1,
    ("regular", "business"): 2,
    ("silver", "basic_economy"): 1,
    ("silver", "economy"): 2,
    ("silver", "business"): 3,
    ("gold", "basic_economy"): 2,
    ("gold", "economy"): 3,
    ("gold", "business"): 4,
}


def free_bag_allowance(membership: str, cabin: str, passengers: int) -> int:
    """Total free checked bags for the whole reservation.

    Raises:
        KeyError: if membership/cabin is not a known combination (caller bug, not a policy denial).
    """
    per_passenger = FREE_BAGS[(membership.lower(), cabin.lower())]
    return per_passenger * passengers


def nonfree_bags(membership: str, cabin: str, passengers: int, total_bags: int) -> int:
    """How many of `total_bags` are chargeable, i.e. beyond the free allowance."""
    return max(0, total_bags - free_bag_allowance(membership, cabin, passengers))


def baggage_cost(membership: str, cabin: str, passengers: int, total_bags: int) -> int:
    return EXTRA_BAG_FEE * nonfree_bags(membership, cabin, passengers, total_bags)


def check_add_baggage(current_total: int, new_total: int) -> Decision:
    """Baggage modifications are add-only; removal is never permitted.

    Args:
        current_total: `total_baggages` currently on the reservation.
        new_total: requested new total.
    """
    if new_total < current_total:
        return deny(
            "Checked bags can only be added, never removed, so I can't reduce the bag count on this "
            "reservation.",
            rule="baggage.remove_forbidden",
            current_total=current_total,
            requested_total=new_total,
        )
    if new_total == current_total:
        return deny(
            "That reservation already has that number of checked bags, so there's nothing to change.",
            rule="baggage.no_change",
            current_total=current_total,
        )
    return allow(
        "Adding checked bags is permitted.",
        rule="baggage.add_allowed",
        current_total=current_total,
        requested_total=new_total,
    )
