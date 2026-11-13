#!/usr/bin/env python
"""Invoice review artifact for the order-to-cash task.

Contract (docs/task_runner.md "Artifact Interface"):
  GET  /health   -> 200 {"status": "ok"}
  POST /respond  -> {"messages": [...]} => {"choices": [{"message": {"role": "assistant",
                                                                    "content": <json text>}}]}

The assistant content is a bare JSON object -- first char '{', last char '}', no prose, no fence --
with exactly three keys: flagged (bool), fault_type (str|None), reason (str).

Request shape: one plain-English sentence naming an invoice number, supplier and PO number. Only the
invoice number is used; the invoice, its PO line and the supplier contract are read from the OLTP db.

Fault ladder -- first failing check wins, secondary findings are described in `reason`:
  1 DUPLICATE          invoices.duplicate_of is populated
  2 PRICE_DELTA        (invoice_unit_price - po_unit_price) * invoiced_qty > 500
  3 QTY_MISMATCH       invoiced_qty != po_quantity (either direction, exact match)
  4 TOTAL_MATH         |amount - (invoiced_qty * unit_price + fees)| > 500
  5 OFF_CONTRACT_ITEM  no contract line for (supplier_id, item_id)
  6 OFF_CONTRACT_FEE   a fee label absent from that contract's allowed_fees
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from decimal import Decimal
from typing import Any

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# --- thresholds -----------------------------------------------------------------------------------
PRICE_DELTA_THRESHOLD = Decimal("500")   # strictly greater than, in dollars of total impact
TOTAL_MATH_THRESHOLD = Decimal("500")    # strictly greater than, absolute dollars

# --- db -------------------------------------------------------------------------------------------
# Only $PORT is injected into the artifact container; the db coordinates are the task environment's
# published defaults, overridable by env for other deployments.
DSN_CANDIDATES = [
    os.environ.get("DATABASE_URL"),
    "postgres://{u}:{p}@{h}:{port}/{db}".format(
        u=os.environ.get("POSTGRES_USER", "sandbox"),
        p=os.environ.get("POSTGRES_PASSWORD", "sandbox"),
        h=os.environ.get("POSTGRES_HOST", "db"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        db=os.environ.get("POSTGRES_DB", "oltp"),
    ),
    "postgres://sandbox:sandbox@o2c-db-1:5432/oltp",
    "postgres://sandbox:sandbox@localhost:5432/oltp",
]

# One connection per worker thread. `asyncio.to_thread` uses a bounded executor, so the connection
# count is bounded too -- and because nothing is shared between threads, a failure on one connection
# can never disturb a request in flight on another.
_local = threading.local()
_dsn_lock = threading.Lock()
_dsn_in_use: str | None = None


def _open_connection() -> Any:
    """Open a connection, trying each candidate DSN once and remembering the one that worked."""
    global _dsn_in_use
    with _dsn_lock:
        ordered = ([_dsn_in_use] if _dsn_in_use else []) + [
            d for d in DSN_CANDIDATES if d and d != _dsn_in_use
        ]
    errors = []
    for dsn in ordered:
        try:
            conn = psycopg2.connect(dsn, connect_timeout=5)
            conn.autocommit = True
            with _dsn_lock:
                _dsn_in_use = dsn
            return conn
        except Exception as exc:  # try the next candidate
            errors.append(f"{dsn.rsplit('@', 1)[-1]}: {exc}")
    raise RuntimeError("no reachable database; tried " + " | ".join(errors))


def _thread_connection() -> Any:
    """This thread's connection, reopened if it was never made or has since died."""
    conn = getattr(_local, "conn", None)
    if conn is not None and not conn.closed:
        return conn
    conn = _open_connection()
    _local.conn = conn
    return conn


def _drop_thread_connection() -> None:
    conn = getattr(_local, "conn", None)
    _local.conn = None
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


INVOICE_SQL = """
SELECT i.invoice_number, i.supplier_id, i.supplier_name, i.po_number, i.item_id, i.item_number,
       i.invoiced_qty, i.unit_price, i.amount, i.extra_fees, i.duplicate_of,
       pl.po_line_id AS matched_po_line_id, pl.quantity AS po_qty, pl.unit_price AS po_unit_price,
       c.contract_id, c.agreed_unit_price, c.allowed_fees
FROM invoices i
LEFT JOIN po_lines pl
       ON pl.po_line_id = i.po_line_id
       OR (i.po_line_id IS NULL AND pl.po_number = i.po_number AND pl.item_id = i.item_id)
LEFT JOIN contracts c
       ON c.supplier_id = i.supplier_id AND c.item_id = i.item_id
WHERE upper(i.invoice_number) = upper(%s)
ORDER BY pl.po_line_id NULLS LAST
LIMIT 1
"""


def fetch_invoice(invoice_number: str) -> dict | None:
    """Read the invoice plus its PO line and supplier contract.

    Retries once on its own connection so a link dropped between requests (idle timeout, db restart)
    costs a reconnect rather than a failed review.
    """
    last_exc: Exception | None = None
    for _attempt in (1, 2):
        try:
            conn = _thread_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(INVOICE_SQL, (invoice_number,))
                row = cur.fetchone()
            return dict(row) if row else None
        except Exception as exc:
            last_exc = exc
            _drop_thread_connection()
    raise last_exc if last_exc else RuntimeError("invoice lookup failed")


# --- request parsing ------------------------------------------------------------------------------
TOKEN_RE = re.compile(r"\b([A-Za-z]{2,6})-(\d{2,})\b")


def extract_invoice_number(messages: list[dict]) -> str | None:
    """Pull the invoice identifier out of the newest message that carries one.

    The sentence also names a PO ("PO PO-000498"), so PO-prefixed tokens are never candidates and an
    INV- token always wins over any other identifier shape.
    """
    for msg in reversed(messages or []):
        text = msg.get("content")
        if isinstance(text, list):  # content parts
            text = " ".join(
                part.get("text", "") for part in text if isinstance(part, dict)
            )
        if not isinstance(text, str) or not text:
            continue
        candidates = [
            f"{prefix.upper()}-{digits}"
            for prefix, digits in TOKEN_RE.findall(text)
            if prefix.upper() != "PO"
        ]
        if not candidates:
            continue
        for cand in candidates:
            if cand.startswith("INV-"):
                return cand
        return candidates[0]
    return None


# --- fee helpers ----------------------------------------------------------------------------------
def parse_fees(raw: Any) -> list[tuple[str, Decimal]]:
    """Normalise extra_fees into (label, amount) pairs; tolerant of shape drift in the column."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, dict):
        raw = [raw]
    fees: list[tuple[str, Decimal]] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or entry.get("type") or entry.get("name") or "").strip()
        try:
            amount = Decimal(str(entry.get("amount", 0) or 0))
        except Exception:
            amount = Decimal("0")
        if label:
            fees.append((label, amount))
    return fees


def parse_allowed_fees(raw: Any) -> set[str]:
    """Contract-permitted fee labels, lowercased for comparison. Differs per supplier by design."""
    if raw is None:
        return set()
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return set()
    if isinstance(raw, dict):
        raw = list(raw.keys())
    return {str(x).strip().lower() for x in raw if str(x).strip()} if isinstance(raw, list) else set()


def money(value: Decimal) -> str:
    return f"${value:,.2f}"


def dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


# --- the review -----------------------------------------------------------------------------------
def review(row: dict) -> tuple[bool, str | None, str]:
    """Run the fault ladder. Returns (flagged, fault_type, reason)."""
    inv_no = row.get("invoice_number")
    qty = dec(row.get("invoiced_qty"))
    price = dec(row.get("unit_price"))
    amount = dec(row.get("amount"))
    po_qty = dec(row.get("po_qty"))
    po_price = dec(row.get("po_unit_price"))
    has_po_line = row.get("matched_po_line_id") is not None
    fees = parse_fees(row.get("extra_fees"))
    fee_total = sum((amt for _, amt in fees), Decimal("0"))

    findings: list[tuple[str, str]] = []  # (fault_type, sentence) in ladder order

    # 1. duplicate submission -- the record marks it, no cross-invoice matching needed
    if row.get("duplicate_of"):
        findings.append((
            "DUPLICATE",
            f"Duplicate submission: this invoice is a resubmission of {row['duplicate_of']}.",
        ))

    # 2. price overrun against the PO line price, measured as total dollar impact
    if has_po_line and None not in (price, po_price, qty):
        delta_total = (price - po_price) * qty
        if delta_total > PRICE_DELTA_THRESHOLD:
            findings.append((
                "PRICE_DELTA",
                f"Billed {money(price)} per unit against a PO price of {money(po_price)} on "
                f"{qty:g} units, so {money(delta_total)} more than the PO allows.",
            ))

    # 3. quantity mismatch -- exact-match check, either direction
    if has_po_line and None not in (qty, po_qty) and qty != po_qty:
        findings.append((
            "QTY_MISMATCH",
            f"Invoiced quantity {qty:g} does not match the PO quantity {po_qty:g}.",
        ))

    # 4. invoice total vs line amount plus fees
    if None not in (amount, qty, price):
        expected = qty * price + fee_total
        gap = amount - expected
        if abs(gap) > TOTAL_MATH_THRESHOLD:
            direction = "over" if gap > 0 else "under"
            findings.append((
                "TOTAL_MATH",
                f"Invoice total {money(amount)} is {money(abs(gap))} {direction} the line amount "
                f"plus fees ({money(expected)}).",
            ))

    # 5. item not covered by the supplier's contract
    covered = row.get("contract_id") is not None
    if not covered:
        findings.append((
            "OFF_CONTRACT_ITEM",
            f"Item {row.get('item_number') or row.get('item_id')} is not covered by the contract "
            f"for {row.get('supplier_name') or row.get('supplier_id')}.",
        ))

    # 6. fee label the supplier's contract does not authorise
    if covered and fees:
        allowed = parse_allowed_fees(row.get("allowed_fees"))
        bad = [(label, amt) for label, amt in fees if label.strip().lower() not in allowed]
        if bad:
            listed = ", ".join(f"{label} {money(amt)}" for label, amt in bad)
            findings.append((
                "OFF_CONTRACT_FEE",
                f"Contract does not authorise this charge: {listed}.",
            ))

    if not findings:
        # Nothing material. A PO line we could not locate is still worth a human look.
        if not has_po_line:
            return (
                True,
                None,
                f"Could not locate the PO line for {inv_no} on PO {row.get('po_number')}, so price "
                f"and quantity could not be checked against the order.",
            )
        return (
            False,
            None,
            "Matches the PO on price and quantity with no unauthorised charges; no review needed.",
        )

    primary_type, primary_reason = findings[0]
    if len(findings) > 1:
        extras = " ".join(sentence for _, sentence in findings[1:])
        primary_reason = f"{primary_reason} Also: {extras}"
    return True, primary_type, primary_reason


# --- endpoints ------------------------------------------------------------------------------------
app = FastAPI()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def as_content(payload: dict) -> JSONResponse:
    """Wrap the decision as the assistant turn; content is bare JSON text, nothing around it."""
    return JSONResponse(
        {"choices": [{"message": {"role": "assistant", "content": json.dumps(payload)}}]}
    )


@app.post("/respond")
async def respond(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    messages = body.get("messages") or []

    invoice_number = extract_invoice_number(messages)
    if not invoice_number:
        return as_content({
            "flagged": True,
            "fault_type": None,
            "reason": "No invoice number found in the request, so the invoice could not be checked.",
        })

    try:
        row = await asyncio.to_thread(fetch_invoice, invoice_number)
    except Exception as exc:
        return as_content({
            "flagged": True,
            "fault_type": None,
            "reason": f"Could not read {invoice_number} from the invoice records ({exc}); "
                      f"needs a manual look.",
        })

    if row is None:
        return as_content({
            "flagged": True,
            "fault_type": None,
            "reason": f"No invoice record found for {invoice_number}; cannot match it to a PO or "
                      f"contract, so a human should confirm it exists.",
        })

    flagged, fault_type, reason = review(row)
    return as_content({"flagged": flagged, "fault_type": fault_type, "reason": reason})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]), log_level="warning")
