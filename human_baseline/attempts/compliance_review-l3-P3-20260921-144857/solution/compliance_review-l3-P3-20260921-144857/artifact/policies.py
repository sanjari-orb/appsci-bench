#!/usr/bin/env python
"""Policy catalog for compliance review.

Single source of truth for policy names, category spelling, and rule assignment.
Four named policies, 22 rule assignments: 8 must_have + 10 must_not_have + 4 needs_review.

Policy resolution recognizes canonical names and the explicit aliases listed below -- nothing
else. No fuzzy/approximate matching, no deal-type inference, no default policy: an unrecognized
standard is an error, never a guess.
"""
from __future__ import annotations

import re

MUST_HAVE = "must_have"
MUST_NOT_HAVE = "must_not_have"
NEEDS_REVIEW = "needs_review"

RULE_KINDS = (MUST_HAVE, MUST_NOT_HAVE, NEEDS_REVIEW)

# Category strings are the exact wire spelling ("Ip Ownership Assignment", "Joint Ip Ownership").
# Declaration order here is the output order of findings -- it is the artifact's determinism.
POLICIES: dict[str, dict[str, tuple[str, ...]]] = {
    "Risk-Averse Standard": {
        MUST_HAVE: ("Cap On Liability", "Audit Rights", "Insurance"),
        MUST_NOT_HAVE: ("Uncapped Liability", "Liquidated Damages"),
        NEEDS_REVIEW: ("Covenant Not To Sue",),
    },
    "Vendor Contract Standard": {
        MUST_HAVE: ("Cap On Liability", "Governing Law"),
        MUST_NOT_HAVE: ("Uncapped Liability", "Most Favored Nation"),
        NEEDS_REVIEW: ("Change Of Control",),
    },
    "Procurement Clean Standard": {
        MUST_HAVE: ("Audit Rights",),
        MUST_NOT_HAVE: (
            "Most Favored Nation",
            "Non-Compete",
            "Exclusivity",
            "Minimum Commitment",
        ),
        NEEDS_REVIEW: ("Volume Restriction",),
    },
    "IP-Protective Standard": {
        MUST_HAVE: ("License Grant", "Ip Ownership Assignment"),
        MUST_NOT_HAVE: ("Joint Ip Ownership", "Irrevocable Or Perpetual License"),
        NEEDS_REVIEW: ("Source Code Escrow",),
    },
}

# Normalized alias -> canonical policy name. Canonical names are added programmatically below.
_EXTRA_ALIASES: dict[str, str] = {
    "risk averse": "Risk-Averse Standard",
    "risk averse policy": "Risk-Averse Standard",
    "vendor contract": "Vendor Contract Standard",
    "vendor contract policy": "Vendor Contract Standard",
    "procurement clean": "Procurement Clean Standard",
    "procurement clean policy": "Procurement Clean Standard",
    "ip protective": "IP-Protective Standard",
    "ip protective policy": "IP-Protective Standard",
}


def normalize(text: str) -> str:
    """Lowercase and collapse every non-alphanumeric run to a single space."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


ALIASES: dict[str, str] = {normalize(name): name for name in POLICIES}
ALIASES.update(_EXTRA_ALIASES)

# Longest first, so "risk averse standard" is preferred over "risk averse" when both fit.
_ALIAS_ORDER: tuple[str, ...] = tuple(sorted(ALIASES, key=len, reverse=True))

# A capitalized multi-word name ending in Standard/Policy: used only to tell "no policy named"
# apart from "a standard was named but is not in the catalog".
NAMED_STANDARD_RE = re.compile(r"\b((?:[A-Z][A-Za-z0-9/&-]*\s+){1,4}(?:Standard|Policy))\b")


def known_policies() -> list[str]:
    return sorted(POLICIES)


def categories(policy: str, kind: str) -> tuple[str, ...]:
    return POLICIES[policy][kind]


def assignments() -> list[tuple[str, str, str]]:
    """Every (policy, kind, category) assignment in declaration order. 22 of them."""
    out: list[tuple[str, str, str]] = []
    for policy, rules in POLICIES.items():
        for kind in RULE_KINDS:
            for category in rules[kind]:
                out.append((policy, kind, category))
    return out


def resolve_policy_name(value: str) -> str | None:
    """Resolve an explicitly supplied policy name. Canonical names and defined aliases only."""
    if not isinstance(value, str):
        return None
    return ALIASES.get(normalize(value))


def find_policy_mentions(text: str) -> list[str]:
    """Distinct canonical policies named in `text`, in order of first appearance.

    Substring match on the normalized text against the alias table only -- no edit distance,
    no prefix guessing, no inference from deal type.
    """
    padded = " " + normalize(text) + " "
    hits: list[tuple[int, str]] = []
    seen: set[str] = set()
    for alias in _ALIAS_ORDER:
        idx = padded.find(" " + alias + " ")
        if idx == -1:
            continue
        canonical = ALIASES[alias]
        if canonical in seen:
            continue
        seen.add(canonical)
        hits.append((idx, canonical))
    return [canonical for _, canonical in sorted(hits)]


def looks_like_named_standard(text: str) -> str | None:
    """A capitalized '... Standard'/'... Policy' phrase that the alias table does not know."""
    for match in NAMED_STANDARD_RE.finditer(text):
        phrase = match.group(1).strip()
        if resolve_policy_name(phrase) is None:
            return phrase
    return None
