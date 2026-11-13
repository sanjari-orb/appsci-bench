"""The verdict type every policy function returns.

Three outcomes, mapping directly onto the stakeholder's three buckets:

* ``ALLOW``    -- in policy and within our tools: do it (after explicit user confirmation for writes).
* ``DENY``     -- against policy: say no. Never dress a denial up as a transfer.
* ``TRANSFER`` -- a legitimate request our actions genuinely cannot carry out.

``Decision`` is deliberately inert data: it carries the outcome, a customer-safe reason, and the facts
the verdict was based on. Producing it involves no I/O, so every rule is unit-testable in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Outcome(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    TRANSFER = "transfer"


@dataclass(frozen=True)
class Decision:
    """A policy verdict.

    Args:
        outcome: ALLOW, DENY or TRANSFER.
        reason: Customer-safe explanation of why. Always populated, including for ALLOW.
        rule: Short stable identifier of the deciding rule, for logs and tests.
        facts: The record-derived facts the verdict rests on (never customer claims).
    """

    outcome: Outcome
    reason: str
    rule: str = ""
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.outcome is Outcome.ALLOW

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "rule": self.rule,
            "facts": self.facts,
        }


# `message` is positional-only (the `/`) so a *fact* may be named "message" without colliding.
# `rule` stays a keyword because every call site reads better as `rule="..."`. A fact that would
# collide with `rule` must be named something else (e.g. `deciding_rule`).
def allow(message: str, /, rule: str = "", **facts: Any) -> Decision:
    return Decision(Outcome.ALLOW, message, rule, facts)


def deny(message: str, /, rule: str = "", **facts: Any) -> Decision:
    return Decision(Outcome.DENY, message, rule, facts)


def transfer(message: str, /, rule: str = "", **facts: Any) -> Decision:
    return Decision(Outcome.TRANSFER, message, rule, facts)
