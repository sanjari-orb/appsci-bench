"""Pull the three identifiers out of the request sentence.

The wording is fixed and carries no invoice values -- only three names vary:

    "Review invoice INV-1877 (supplier Supplier_7, PO PO-000498) against its PO
     line and the supplier contract; decide whether to flag it for human review
     and give a short reason."

All three identifiers are required.  Any one missing means the request cannot be
reviewed, which is a 422 rather than a guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from models import Unreviewable

# Identifier-shaped token: starts alphanumeric, then word chars, hyphens, slashes.
_TOKEN = r"([A-Za-z0-9][\w\-/]*)"
# Tolerate "invoice no. X", "invoice #X", "invoice: X" as well as "invoice X".
_FILLER = r"[\s:#]*(?:(?:number|no\.?|id)[\s:#]*)?"


def _pattern(*words: str) -> re.Pattern[str]:
    alternation = "|".join(words)
    return re.compile(rf"(?:{alternation})\b{_FILLER}{_TOKEN}", re.IGNORECASE)


INVOICE_RE = _pattern(r"\binvoice")
SUPPLIER_RE = _pattern(r"\bsupplier", r"\bvendor")
PO_RE = _pattern(r"\bPO", r"\bpurchase\s+order")

# Words that follow the keyword in the fixed wording but are prose, not identifiers
# ("against its PO line", "the supplier contract").
_STOPWORDS = frozenset(
    """
    a an and against are as at be by decide flag for from give human id in invoice invoices is
    it its item items line lines no number numbers of on or order orders po pos price prices
    purchase quantity reason review short should supplier suppliers terms that the their this
    to total totals unit value values vendor vendors was whether with
    """.split()
)


def _has_digit(text: str) -> bool:
    return any(character.isdigit() for character in text)


def _extract(pattern: re.Pattern[str], text: str) -> Optional[str]:
    """Best identifier-shaped candidate for one keyword.

    The sentence mentions each keyword more than once ("PO PO-000498 ... its PO
    line"), so candidates are scored rather than taking the first blindly: a token
    containing a digit wins, then any non-prose token.
    """
    candidates = [match.group(1) for match in pattern.finditer(text)]
    plausible = [c for c in candidates if c.lower() not in _STOPWORDS]
    for candidate in plausible:
        if _has_digit(candidate):
            return candidate
    return plausible[0] if plausible else None


def _message_text(message: Any) -> str:
    """Flatten a message's content, which may be a string or content blocks."""
    if isinstance(message, str):
        return message
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def last_user_text(messages: Any) -> str:
    """The last user turn, falling back to the last turn carrying any text."""
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and str(message.get("role", "")).lower() == "user":
            text = _message_text(message)
            if text.strip():
                return text
    for message in reversed(messages):
        text = _message_text(message)
        if text.strip():
            return text
    return ""


# The evaluator's transport-conformance handshake.  It checks that the service returns a
# well-formed envelope before any invoice exists, so it carries no identifiers.
PROBE_TEXT = "conformance probe: please respond"


def is_transport_probe(messages: Any) -> bool:
    """True only for the transport handshake, and deliberately nothing else.

    The normalised last user turn must equal the probe sentence and carry no other
    content.  A request that pairs the probe wording with invoice identifiers, or with
    any additional text, is not a probe: it falls through to normal invoice review, and
    is rejected there if it is malformed.  Normalisation is limited to surrounding and
    repeated whitespace plus letter case -- it never drops tokens.
    """
    text = last_user_text(messages)
    if not text:
        return False
    return " ".join(text.split()).casefold() == PROBE_TEXT


@dataclass
class ParsedRequest:
    invoice_number: str
    supplier_id: str
    po_number: str
    source_text: str


def parse_request(messages: Any) -> ParsedRequest:
    text = last_user_text(messages)
    if not text.strip():
        raise Unreviewable(
            "malformed_request", "request carries no user message to review"
        )

    found = {
        "invoice": _extract(INVOICE_RE, text),
        "supplier": _extract(SUPPLIER_RE, text),
        "PO": _extract(PO_RE, text),
    }
    missing = [name for name, value in found.items() if not value]
    if missing:
        raise Unreviewable(
            "malformed_request",
            "could not identify the " + ", ".join(missing)
            + (" in the request" if len(missing) == 1 else " identifiers in the request"),
        )

    return ParsedRequest(
        invoice_number=found["invoice"],
        supplier_id=found["supplier"],
        po_number=found["PO"],
        source_text=text,
    )
