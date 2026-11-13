"""The one source of "now" for this service.

The airline store runs on a fixed reference clock, not wall-clock time: the stakeholder was explicit
that every time-relative rule is judged against 2024-05-15 15:00:00 EST. Real `datetime.now()` is
never correct for a policy decision here -- a cancellation that is inside the 24-hour window against
the reference clock is two years outside it against the real one. Nothing else in this service may
read the system clock for a policy purpose; it all comes through here.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# The store's fixed "current time". Naive on purpose: every timestamp the tool API returns
# (reservation `created_at`) is naive and already expressed in this same frame, so comparing them
# directly is correct and converting either side would introduce an offset that is not really there.
REFERENCE_NOW = datetime(2024, 5, 15, 15, 0, 0)

CANCELLATION_WINDOW = timedelta(hours=24)

# Flight status vocabulary, confirmed against the live tool API rather than assumed.
STATUS_AVAILABLE = "available"
STATUS_ON_TIME = "on time"
STATUS_DELAYED = "delayed"
STATUS_FLYING = "flying"
STATUS_LANDED = "landed"
STATUS_CANCELLED = "cancelled"

# "Already flown" in the policy sense: took off (whether or not it has landed yet). This is the set
# that blocks a cancellation (-> transfer) and blocks a cabin change.
DEPARTED_STATUSES = frozenset({STATUS_FLYING, STATUS_LANDED})

# The only status a new booking may be made on.
BOOKABLE_STATUSES = frozenset({STATUS_AVAILABLE})


def parse_timestamp(value: str) -> datetime | None:
    """Parse a tool-API timestamp. Returns None rather than raising on anything unexpected."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", ""))
    except ValueError:
        return None


def booked_within_24h(created_at: str) -> bool:
    """Was the reservation created within 24h of the reference clock?

    An unparseable or future-dated timestamp returns False: the 24-hour window is one of four
    conditions that can *permit* a cancellation, so failing to read it must withhold permission, not
    grant it.
    """
    created = parse_timestamp(created_at)
    if created is None:
        return False
    delta = REFERENCE_NOW - created
    return timedelta(0) <= delta <= CANCELLATION_WINDOW


def has_departed(status: str) -> bool:
    return str(status).strip().lower() in DEPARTED_STATUSES


def is_bookable(status: str) -> bool:
    return str(status).strip().lower() in BOOKABLE_STATUSES


def is_cancelled(status: str) -> bool:
    return str(status).strip().lower() == STATUS_CANCELLED


def is_delayed(status: str) -> bool:
    return str(status).strip().lower() == STATUS_DELAYED
