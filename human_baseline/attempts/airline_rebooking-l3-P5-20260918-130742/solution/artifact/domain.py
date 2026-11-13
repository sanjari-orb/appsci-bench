"""Typed read-only views over the environment's raw user / reservation / flight records.

These wrappers exist so policy code never indexes raw dicts and never has to re-derive the same fact
two different ways. They are pure: constructing one performs no I/O.

Key domain vocabulary:

* A segment is *flown* when its dated flight instance status is ``landed`` or ``flying``.
* A reservation is *flown* when **any** segment is flown -- that is what blocks a cabin change and
  makes a cancellation a transfer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from config import NOW_ISO

CABINS = ("basic_economy", "economy", "business")
MEMBERSHIPS = ("regular", "silver", "gold")

# A dated flight instance in one of these states has departed, so the segment counts as flown.
FLOWN_STATUSES = frozenset({"landed", "flying"})
# Only an `available` instance can be booked or used as a new segment.
BOOKABLE_STATUS = "available"


def parse_ts(value: str) -> datetime:
    """Parse an environment timestamp (``YYYY-MM-DDTHH:MM:SS`` or a bare date)."""
    return datetime.fromisoformat(value.strip().replace(" ", "T"))


def now() -> datetime:
    """The fixed environment clock."""
    return parse_ts(NOW_ISO)


@dataclass(frozen=True)
class Segment:
    """One flight segment of a reservation, paired with its live instance status when known."""

    flight_number: str
    date: str
    origin: str = ""
    destination: str = ""
    price: int = 0
    status: str | None = None

    @property
    def flown(self) -> bool:
        return (self.status or "") in FLOWN_STATUSES

    @property
    def cancelled_by_airline(self) -> bool:
        return (self.status or "") == "cancelled"

    @property
    def delayed(self) -> bool:
        return (self.status or "") == "delayed"

    @property
    def key(self) -> tuple[str, str]:
        return (self.flight_number, self.date)


@dataclass(frozen=True)
class User:
    raw: dict[str, Any]

    @property
    def user_id(self) -> str:
        return self.raw.get("user_id", "")

    @property
    def membership(self) -> str:
        return (self.raw.get("membership") or "regular").lower()

    @property
    def payment_methods(self) -> dict[str, dict[str, Any]]:
        return self.raw.get("payment_methods") or {}

    @property
    def reservation_ids(self) -> list[str]:
        return list(self.raw.get("reservations") or [])

    def payment_source(self, payment_id: str) -> str | None:
        method = self.payment_methods.get(payment_id)
        return method.get("source") if method else None

    def payment_balance(self, payment_id: str) -> int | None:
        """Remaining balance for a gift card / certificate; None for a credit card (no limit here)."""
        method = self.payment_methods.get(payment_id)
        if not method:
            return None
        return method.get("amount") if method.get("source") in {"gift_card", "certificate"} else None


@dataclass(frozen=True)
class Reservation:
    raw: dict[str, Any]
    segment_statuses: dict[tuple[str, str], str] | None = None

    @property
    def reservation_id(self) -> str:
        return self.raw.get("reservation_id", "")

    @property
    def user_id(self) -> str:
        return self.raw.get("user_id", "")

    @property
    def cabin(self) -> str:
        return self.raw.get("cabin", "")

    @property
    def flight_type(self) -> str:
        return self.raw.get("flight_type", "")

    @property
    def origin(self) -> str:
        return self.raw.get("origin", "")

    @property
    def destination(self) -> str:
        return self.raw.get("destination", "")

    @property
    def insurance(self) -> bool:
        return str(self.raw.get("insurance", "")).lower() == "yes"

    @property
    def passenger_count(self) -> int:
        return len(self.raw.get("passengers") or [])

    @property
    def total_baggages(self) -> int:
        return int(self.raw.get("total_baggages") or 0)

    @property
    def nonfree_baggages(self) -> int:
        return int(self.raw.get("nonfree_baggages") or 0)

    @property
    def created_at(self) -> datetime:
        return parse_ts(self.raw["created_at"])

    @property
    def cancelled(self) -> bool:
        return str(self.raw.get("status", "")).lower() == "cancelled"

    @property
    def is_basic_economy(self) -> bool:
        return self.cabin == "basic_economy"

    @property
    def payment_history(self) -> list[dict[str, Any]]:
        return list(self.raw.get("payment_history") or [])

    @property
    def paid_with_certificate(self) -> bool:
        """True when any original payment was a travel certificate (leftover is never refundable)."""
        return any(str(p.get("payment_id", "")).startswith("certificate") for p in self.payment_history)

    @property
    def segments(self) -> list[Segment]:
        statuses = self.segment_statuses or {}
        out: list[Segment] = []
        for f in self.raw.get("flights") or []:
            key = (f.get("flight_number", ""), f.get("date", ""))
            out.append(
                Segment(
                    flight_number=key[0],
                    date=key[1],
                    origin=f.get("origin", ""),
                    destination=f.get("destination", ""),
                    price=int(f.get("price") or 0),
                    status=statuses.get(key),
                )
            )
        return out

    @property
    def statuses_known(self) -> bool:
        """True only when every segment has a status read from the environment.

        Policy that depends on flown/cancelled/delayed must refuse to decide until this is True --
        that is the structural guard against acting on an unverified customer claim.
        """
        segs = self.segments
        return bool(segs) and all(s.status is not None for s in segs)

    @property
    def has_flown_segment(self) -> bool:
        return any(s.flown for s in self.segments)

    @property
    def has_airline_cancelled_segment(self) -> bool:
        return any(s.cancelled_by_airline for s in self.segments)

    @property
    def has_delayed_segment(self) -> bool:
        return any(s.delayed for s in self.segments)

    def booked_within_hours(self, hours: int = 24, reference: datetime | None = None) -> bool:
        delta = (reference or now()) - self.created_at
        return 0 <= delta.total_seconds() <= hours * 3600

    def with_statuses(self, statuses: dict[tuple[str, str], str]) -> "Reservation":
        merged = dict(self.segment_statuses or {})
        merged.update(statuses)
        return Reservation(self.raw, merged)


def total_segment_price(segments: Iterable[Segment]) -> int:
    return sum(s.price for s in segments)
