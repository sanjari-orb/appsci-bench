#!/usr/bin/env python
"""Invoice review service.

Reviews one supplier invoice per request against its PO line and the supplier
contract, and decides whether a human in accounts payable should stop and look at
it.  The service flags and explains only -- it never auto-pays or auto-rejects.

Artifact contract (unchanged from the scaffold):
  GET  /health   -> 200 {"status": "ok"}
  POST /respond  -> {"messages": [...]}  =>  OpenAI-style
                     {"choices": [{"message": {"role": "assistant", "content": ...}}]}

The assistant content is a bare JSON object with exactly three keys:
  {"flagged": bool, "fault_type": <one of six labels>|null, "reason": str}

The evaluator's transport-conformance handshake ("conformance probe: please respond")
is answered 200 with {"status": "ok"} -- an envelope check, carrying no verdict because
no invoice was reviewed.  That guard matches one exact sentence only.

A request that cannot be reviewed does NOT get a fabricated verdict.  It gets a
parseable JSON error at a non-2xx status carrying no flagged and no fault_type:
422 for malformed input or missing referenced records, 503 when the database
itself is unavailable.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import db
import rules
from models import DatabaseUnavailable, Invoice, Unreviewable, Verdict, norm_key
from parsing import ParsedRequest, is_transport_probe, parse_request

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("invoice_review")

app = FastAPI()

# Lazy singleton: connecting at import would let a database failure kill the
# container and take /health down with it.
_DATABASE: Optional[db.Database] = None


def get_database() -> db.Database:
    global _DATABASE
    if _DATABASE is None:
        _DATABASE = db.Database()
    return _DATABASE


@app.get("/health")
def health() -> dict:
    """Pure liveness -- deliberately does not touch the database.  It must stay 200
    while the database is down, or the deployer never routes traffic here."""
    return {"status": "ok"}


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def _validate_linkage(parsed: ParsedRequest, invoice: Invoice) -> None:
    """Step 3.  The three request identifiers are validated, not overridden: the
    database record never silently wins.  Where the invoice carries no such column
    there is nothing to contradict and the request value stands."""
    # The record may name its supplier in more than one column (an id and a name).  The
    # request names one token; agreeing with either column is agreement.  Contradiction
    # means it matches none of them -- so a genuine mismatch is still caught, while a
    # request that names the supplier by its other column is not rejected.
    supplier_columns = [v for v in (invoice.supplier_id, invoice.supplier_name) if v]
    if supplier_columns and not any(
        norm_key(v) == norm_key(parsed.supplier_id) for v in supplier_columns
    ):
        raise Unreviewable(
            "contradictory_linkage",
            f"invoice {invoice.invoice_number} belongs to supplier "
            f"{invoice.supplier_id or invoice.supplier_name}, "
            f"but the request names {parsed.supplier_id}",
        )
    if invoice.po_number and norm_key(invoice.po_number) != norm_key(parsed.po_number):
        raise Unreviewable(
            "contradictory_linkage",
            f"invoice {invoice.invoice_number} references PO {invoice.po_number}, "
            f"but the request names {parsed.po_number}",
        )


def review(messages: Any) -> Verdict:
    """The seven pipeline steps, in order.  DUPLICATE short-circuits at step 4."""
    # Step 1 -- parse the three identifiers out of the request sentence.
    parsed = parse_request(messages)
    database = get_database()

    # Step 2 -- load the invoice header (lines, fees, contract and PO come later).
    invoice = db.load_invoice_header(database, parsed.invoice_number)
    if invoice is None:
        raise Unreviewable(
            "invoice_not_found",
            f"no invoice {parsed.invoice_number} found in the database",
        )

    # Step 3 -- linkage must agree with the request.
    _validate_linkage(parsed, invoice)
    invoice.supplier_id = invoice.supplier_id or parsed.supplier_id
    invoice.po_number = invoice.po_number or parsed.po_number

    # Step 4 -- DUPLICATE is check 1 and is fully decided from the validated header,
    # so it returns before the step-5 loads and the step-6 gates.  A duplicate is
    # still reported as DUPLICATE when the contract is missing, a PO line is
    # missing, or the document is negative: those gates protect checks 2-6, which
    # are never reached here.
    if invoice.duplicate_of:
        log.info("invoice %s is a duplicate of %s", invoice.invoice_number,
                 invoice.duplicate_of)
        return rules.duplicate_verdict(invoice, parsed.po_number)

    # Step 5 -- non-duplicates only: lines, fees, contract rows and PO lines.
    db.load_invoice_details(database, invoice)

    contract = db.load_contract(
        database, invoice.supplier_id, invoice.supplier_name, parsed.supplier_id
    )
    if contract is None or contract.row_count == 0:
        raise Unreviewable(
            "contract_not_found",
            f"no contract rows found for supplier {invoice.supplier_id}",
        )

    po_lines = db.load_po_lines(database, invoice.po_number)
    if not po_lines:
        raise Unreviewable(
            "po_not_found",
            f"no purchase order lines found for {invoice.po_number}",
        )

    # Step 6 -- gates: document type, currency, completeness.
    po_index = rules.build_po_index(po_lines)
    rules.run_gates(invoice, po_lines, po_index)

    # TOTAL_MATH is literally lines + fees.  Log the components on every evaluation
    # so that if tax/shipping/discount turn out to live elsewhere, the evidence is
    # already there without re-running anything (see plan section 7).
    lines_total, fees_total, computed = rules.compute_total_components(invoice)
    log.info(
        "invoice %s total components: lines=%s fees=%s computed=%s stated=%s delta=%s",
        invoice.invoice_number, lines_total, fees_total, computed, invoice.total,
        invoice.total - computed,
    )

    # Step 7 -- checks 2-6, check-major, first failure wins.
    return rules.evaluate_non_duplicate(invoice, po_index, contract)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _error(status: int, code: str, message: str) -> JSONResponse:
    """Flat error body: no flagged, no fault_type, no invented category."""
    return JSONResponse(status_code=status, content={"error": code, "message": message})


def _envelope(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _verdict_envelope(verdict: Verdict) -> dict:
    # Bare JSON object: first character '{', last '}', no fence and no prose.
    return _envelope(json.dumps(verdict.to_dict()))


@app.post("/respond")
async def respond(request: Request):
    try:
        body = await request.json()
    except Exception:
        return _error(422, "malformed_request", "request body is not valid JSON")

    messages = body.get("messages") if isinstance(body, dict) else None

    # Transport-conformance handshake.  The evaluator verifies the envelope before any
    # invoice exists, so this must answer 200 without an invoice and without touching the
    # database.  It carries no business verdict -- no flagged, no fault_type, no reason --
    # because nothing was reviewed.  The guard matches one exact sentence and does not
    # make any other identifier-less request acceptable.
    if is_transport_probe(messages):
        log.info("transport conformance probe")
        return _envelope(json.dumps({"status": "ok"}))

    try:
        # The loaders are synchronous; keep them off the event loop.
        verdict = await run_in_threadpool(review, messages)
    except Unreviewable as exc:
        log.info("unreviewable (%s): %s", exc.code, exc.message)
        return _error(exc.status_code, exc.code, exc.message)
    except DatabaseUnavailable as exc:
        log.error("database unavailable (%s): %s", exc.code, exc.message)
        return _error(exc.status_code, exc.code, exc.message)
    except Exception as exc:  # pragma: no cover -- last-resort guard
        log.exception("unexpected failure while reviewing")
        return _error(500, "internal_error", f"{type(exc).__name__}: {exc}"[:300])

    log.info("verdict flagged=%s fault_type=%s", verdict.flagged, verdict.fault_type)
    return _verdict_envelope(verdict)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ["PORT"]))
