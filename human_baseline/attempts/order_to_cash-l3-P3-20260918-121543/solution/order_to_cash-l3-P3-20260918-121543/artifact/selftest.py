#!/usr/bin/env python
"""Local self-test for the invoice review service.  Run inside artifact/:

    python selftest.py

Part 1 exercises the rule engine as pure logic.  Part 2 builds a SQLite fixture
and drives the real loaders and the real /respond pipeline through it.  No live
database, no deployment, no network.
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal

from models import (
    Contract,
    Fee,
    Invoice,
    InvoiceLine,
    POLine,
    Unreviewable,
    norm_key,
)
import rules

D = Decimal

_FAILURES: list[str] = []
_PASSES = 0
_SECTION = ""


def section(title: str) -> None:
    global _SECTION
    _SECTION = title
    print(f"\n-- {title}")


def check(label: str, condition: bool, detail: str = "") -> None:
    global _PASSES
    if condition:
        _PASSES += 1
        print(f"   ok   {label}")
    else:
        _FAILURES.append(f"{_SECTION} :: {label} {detail}".rstrip())
        print(f"   FAIL {label} {detail}".rstrip())


def check_eq(label: str, actual, expected) -> None:
    check(label, actual == expected, f"(got {actual!r}, want {expected!r})")


def check_in(label: str, needle: str, haystack: str) -> None:
    check(label, needle in haystack, f"({needle!r} not in {haystack!r})")


# --------------------------------------------------------------------------
# Fixture builders -- plain dataclasses, no database
# --------------------------------------------------------------------------

def line(item: str, qty: str, price: str, line_no: int | None = None) -> InvoiceLine:
    return InvoiceLine(item_id=item, quantity=D(qty), unit_price=D(price), line_no=line_no)


def po(item: str, qty: str, price: str, line_no: int | None = None,
       po_number: str = "PO-000498", currency: str | None = None) -> POLine:
    return POLine(po_number=po_number, item_id=item, quantity=D(qty),
                  unit_price=D(price), line_no=line_no, currency=currency)


def invoice(lines, fees=(), total: str | None = None, **kw) -> Invoice:
    lines = list(lines)
    fees = list(fees)
    if total is None:
        computed = sum((l.quantity * l.unit_price for l in lines), D(0))
        computed += sum((f.amount for f in fees), D(0))
        total = str(computed)
    return Invoice(
        invoice_number=kw.pop("invoice_number", "INV-1877"),
        supplier_id=kw.pop("supplier_id", "Supplier_7"),
        po_number=kw.pop("po_number", "PO-000498"),
        total=D(total),
        lines=lines,
        fees=fees,
        **kw,
    )


def contract(items, fees, supplier_id: str = "Supplier_7") -> Contract:
    return Contract(
        supplier_id=supplier_id,
        allowed_items=frozenset(norm_key(i) for i in items),
        allowed_fees=frozenset(norm_key(f) for f in fees),
        row_count=len(list(items)),
    )


BASE_CONTRACT = contract(["ITEM_22", "ITEM_23"], ["freight", "handling"])


def verdict_for(inv: Invoice, po_lines, con: Contract = BASE_CONTRACT):
    po_lines = list(po_lines)
    index = rules.build_po_index(po_lines)
    rules.run_gates(inv, po_lines, index)
    return rules.evaluate_non_duplicate(inv, index, con)


def gate_error(inv: Invoice, po_lines, con: Contract = BASE_CONTRACT):
    """Return the Unreviewable raised by the gates, or None."""
    try:
        verdict_for(inv, po_lines, con)
    except Unreviewable as exc:
        return exc
    return None


# ==========================================================================
# 6.1  one case per fault_type, exact label, reason naming the real figures
# ==========================================================================

def test_one_case_per_fault_type() -> None:
    section("6.1 one case per fault_type")

    # PRICE_DELTA: 100 units at $48 vs $41 agreed = $700 overcharge
    v = verdict_for(invoice([line("ITEM_22", "100", "48.00")], total="4800.00"),
                    [po("ITEM_22", "100", "41.00")])
    check_eq("PRICE_DELTA label", v.fault_type, "PRICE_DELTA")
    check("PRICE_DELTA flagged", v.flagged is True)
    for fragment in ("ITEM_22", "$48.00", "$41.00", "PO-000498", "$700.00", "100 units"):
        check_in(f"PRICE_DELTA reason names {fragment}", fragment, v.reason)

    # QTY_MISMATCH: 120 invoiced vs 100 ordered, price identical
    v = verdict_for(invoice([line("ITEM_22", "120", "41.00")], total="4920.00"),
                    [po("ITEM_22", "100", "41.00")])
    check_eq("QTY_MISMATCH label", v.fault_type, "QTY_MISMATCH")
    for fragment in ("ITEM_22", "120 units", "100 ordered", "PO-000498"):
        check_in(f"QTY_MISMATCH reason names {fragment}", fragment, v.reason)

    # TOTAL_MATH: lines+fees = 11,150, invoice says 12,400 -> off by 1,250
    v = verdict_for(
        invoice([line("ITEM_22", "100", "41.00"), line("ITEM_23", "150", "45.00")],
                [Fee("freight", D("300.00"))], total="12400.00"),
        [po("ITEM_22", "100", "41.00"), po("ITEM_23", "150", "45.00")])
    check_eq("TOTAL_MATH label", v.fault_type, "TOTAL_MATH")
    for fragment in ("$12,400.00", "$11,150.00", "$1,250.00"):
        check_in(f"TOTAL_MATH reason names {fragment}", fragment, v.reason)

    # OFF_CONTRACT_ITEM: ITEM_99 is on the PO but not on the contract
    v = verdict_for(invoice([line("ITEM_99", "10", "20.00")], total="200.00"),
                    [po("ITEM_99", "10", "20.00")])
    check_eq("OFF_CONTRACT_ITEM label", v.fault_type, "OFF_CONTRACT_ITEM")
    for fragment in ("ITEM_99", "Supplier_7"):
        check_in(f"OFF_CONTRACT_ITEM reason names {fragment}", fragment, v.reason)

    # OFF_CONTRACT_FEE: expedite_surcharge is not in this supplier's allowed_fees
    v = verdict_for(
        invoice([line("ITEM_22", "100", "41.00")],
                [Fee("expedite_surcharge", D("250.00"))], total="4350.00"),
        [po("ITEM_22", "100", "41.00")])
    check_eq("OFF_CONTRACT_FEE label", v.fault_type, "OFF_CONTRACT_FEE")
    for fragment in ("expedite_surcharge", "$250.00", "Supplier_7"):
        check_in(f"OFF_CONTRACT_FEE reason names {fragment}", fragment, v.reason)

    # DUPLICATE: decided from the invoice record alone
    dup = invoice([line("ITEM_22", "100", "41.00")], total="4100.00")
    dup.duplicate_of = "INV-3434"
    v = rules.duplicate_verdict(dup)
    check_eq("DUPLICATE label", v.fault_type, "DUPLICATE")
    for fragment in ("INV-3434", "PO-000498"):
        check_in(f"DUPLICATE reason names {fragment}", fragment, v.reason)


# ==========================================================================
# 6.2  clean invoice
# ==========================================================================

def test_clean_invoice() -> None:
    section("6.2 clean invoice")
    v = verdict_for(
        invoice([line("ITEM_22", "100", "41.00"), line("ITEM_23", "50", "45.00")],
                [Fee("freight", D("300.00"))]),
        [po("ITEM_22", "100", "41.00"), po("ITEM_23", "50", "45.00")])
    check_eq("clean flagged", v.flagged, False)
    check_eq("clean fault_type is null", v.fault_type, None)
    check("clean reason is non-empty", bool(v.reason.strip()))
    check_eq("clean serialises to three keys", sorted(v.to_dict()),
             ["fault_type", "flagged", "reason"])


# ==========================================================================
# 6.3  order precedence -- earliest failing check wins
# ==========================================================================

def test_order_precedence() -> None:
    section("6.3 order precedence")

    # Every one of checks 2-6 fails at once.  PRICE_DELTA is check 2, so it wins.
    all_bad = invoice(
        [line("ITEM_99", "120", "60.00")],          # off-contract item, qty and price wrong
        [Fee("expedite_surcharge", D("250.00"))],   # unauthorised fee
        total="99999.00",                           # total nowhere near lines+fees
    )
    v = verdict_for(all_bad, [po("ITEM_99", "100", "41.00")])
    check_eq("all five failing -> PRICE_DELTA", v.fault_type, "PRICE_DELTA")
    check_in("reason mentions secondary issues", "also", v.reason)

    # Drop the price problem: QTY_MISMATCH (check 3) is now first.
    v = verdict_for(invoice([line("ITEM_99", "120", "41.00")],
                            [Fee("expedite_surcharge", D("250.00"))], total="99999.00"),
                    [po("ITEM_99", "100", "41.00")])
    check_eq("no price fault -> QTY_MISMATCH", v.fault_type, "QTY_MISMATCH")

    # Drop quantity too: TOTAL_MATH (check 4) is next.
    v = verdict_for(invoice([line("ITEM_99", "100", "41.00")],
                            [Fee("expedite_surcharge", D("250.00"))], total="99999.00"),
                    [po("ITEM_99", "100", "41.00")])
    check_eq("no qty fault -> TOTAL_MATH", v.fault_type, "TOTAL_MATH")

    # Fix the total: OFF_CONTRACT_ITEM (check 5) outranks the fee.
    v = verdict_for(invoice([line("ITEM_99", "100", "41.00")],
                            [Fee("expedite_surcharge", D("250.00"))]),
                    [po("ITEM_99", "100", "41.00")])
    check_eq("item outranks fee", v.fault_type, "OFF_CONTRACT_ITEM")


# ==========================================================================
# 6.4  check-major across lines, stable under row reordering
# ==========================================================================

def test_check_major_across_lines() -> None:
    section("6.4 check-major across lines")

    # Quantity problem on line 1, price problem on line 3.  Check-major means the
    # price check sweeps every line before the quantity check runs at all.
    lines = [
        line("ITEM_23", "150", "45.00", line_no=1),   # qty wrong (PO says 100)
        line("ITEM_24", "10", "12.00", line_no=2),    # clean
        line("ITEM_22", "100", "48.00", line_no=3),   # price wrong by $700
    ]
    po_lines = [
        po("ITEM_23", "100", "45.00", line_no=1),
        po("ITEM_24", "10", "12.00", line_no=2),
        po("ITEM_22", "100", "41.00", line_no=3),
    ]
    con = contract(["ITEM_22", "ITEM_23", "ITEM_24"], ["freight"])
    inv = invoice(lines)
    v = verdict_for(inv, po_lines, con)
    check_eq("price on later line still beats qty on earlier line",
             v.fault_type, "PRICE_DELTA")
    check_in("reason names the overcharged item", "ITEM_22", v.reason)

    # Reorder both sides; the label must not move.
    for permutation in ([2, 0, 1], [1, 2, 0], [2, 1, 0]):
        shuffled_inv = invoice([lines[i] for i in permutation], total=str(inv.total))
        shuffled_po = [po_lines[i] for i in permutation]
        w = verdict_for(shuffled_inv, shuffled_po, con)
        check_eq(f"label stable under reordering {permutation}", w.fault_type, v.fault_type)
        check_eq(f"reason stable under reordering {permutation}", w.reason, v.reason)


# ==========================================================================
# 6.5  boundaries -- strictly greater than $500
# ==========================================================================

def test_boundaries() -> None:
    section("6.5 boundaries at exactly $500")

    # PRICE_DELTA: 100 units x $5.00 over = exactly $500.00 impact.
    v = verdict_for(invoice([line("ITEM_22", "100", "46.00")], total="4600.00"),
                    [po("ITEM_22", "100", "41.00")])
    check_eq("price impact of exactly $500.00 does not flag", v.flagged, False)

    # One cent more.
    v = verdict_for(invoice([line("ITEM_22", "100", "46.0001")], total="4600.01"),
                    [po("ITEM_22", "100", "41.00")])
    check_eq("price impact of $500.01 flags", v.fault_type, "PRICE_DELTA")

    # TOTAL_MATH: delta of exactly $500.00 either way.
    for direction, total in (("over", "4600.00"), ("under", "3600.00")):
        v = verdict_for(invoice([line("ITEM_22", "100", "41.00")], total=total),
                        [po("ITEM_22", "100", "41.00")])
        check_eq(f"total delta of exactly $500.00 ({direction}) does not flag",
                 v.flagged, False)
    for direction, total in (("over", "4600.01"), ("under", "3599.99")):
        v = verdict_for(invoice([line("ITEM_22", "100", "41.00")], total=total),
                        [po("ITEM_22", "100", "41.00")])
        check_eq(f"total delta of $500.01 ({direction}) flags", v.fault_type, "TOTAL_MATH")

    # A one-cent quantity difference still flags: quantity is exact, no tolerance.
    v = verdict_for(invoice([line("ITEM_22", "100.5", "41.00")], total="4120.50"),
                    [po("ITEM_22", "100", "41.00")])
    check_eq("quantity has no tolerance", v.fault_type, "QTY_MISMATCH")

    # Decimal arithmetic must not manufacture a cent-level delta out of thin air.
    v = verdict_for(invoice([line("ITEM_22", "3", "0.10")], total="0.30"),
                    [po("ITEM_22", "3", "0.10")])
    check_eq("no float artefact on 3 x 0.10", v.flagged, False)


# ==========================================================================
# 6.6  signed price -- undercharges never flag
# ==========================================================================

def test_signed_price() -> None:
    section("6.6 signed price")
    v = verdict_for(invoice([line("ITEM_22", "100", "1.00")], total="100.00"),
                    [po("ITEM_22", "100", "41.00")])
    check_eq("a $4,000 undercharge does not flag", v.flagged, False,)
    check_eq("...and reports no fault_type", v.fault_type, None)

    # An undercharge on one line must not mask a genuine overcharge on another.
    con = contract(["ITEM_22", "ITEM_23"], [])
    v = verdict_for(
        invoice([line("ITEM_22", "100", "1.00"), line("ITEM_23", "100", "52.00")]),
        [po("ITEM_22", "100", "41.00"), po("ITEM_23", "100", "45.00")], con)
    check_eq("undercharge does not mask an overcharge", v.fault_type, "PRICE_DELTA")
    check_in("the overcharged item is the one reported", "ITEM_23", v.reason)


# ==========================================================================
# 6.7  fee isolation between suppliers
# ==========================================================================

def test_fee_isolation() -> None:
    section("6.7 fee isolation between suppliers")
    supplier_a = contract(["ITEM_22"], ["expedite_surcharge", "freight"], "Supplier_1")
    supplier_b = contract(["ITEM_22"], ["freight"], "Supplier_7")

    inv = invoice([line("ITEM_22", "100", "41.00")],
                  [Fee("expedite_surcharge", D("250.00"))])
    po_lines = [po("ITEM_22", "100", "41.00")]

    v = verdict_for(inv, po_lines, supplier_a)
    check_eq("fee allowed for Supplier_1 is clean there", v.flagged, False)

    v = verdict_for(inv, po_lines, supplier_b)
    check_eq("same fee billed by Supplier_7 flags", v.fault_type, "OFF_CONTRACT_FEE")
    check_in("reason names the billing supplier", "Supplier_7", v.reason)

    # Membership, never substring: 'freight' must not authorise 'freight_premium'.
    v = verdict_for(invoice([line("ITEM_22", "100", "41.00")],
                            [Fee("freight_premium", D("75.00"))]),
                    po_lines, supplier_b)
    check_eq("allowed label is not a prefix licence", v.fault_type, "OFF_CONTRACT_FEE")

    # ...and the amount is irrelevant: $1 of an unauthorised fee still flags.
    v = verdict_for(invoice([line("ITEM_22", "100", "41.00")],
                            [Fee("expedite_surcharge", D("1.00"))]),
                    po_lines, supplier_b)
    check_eq("unauthorised fee flags whatever the amount", v.fault_type, "OFF_CONTRACT_FEE")

    # Case and surrounding whitespace do not change authorisation.
    v = verdict_for(invoice([line("ITEM_22", "100", "41.00")],
                            [Fee("  Freight  ", D("75.00"))]),
                    po_lines, supplier_b)
    check_eq("allowed fee matches case-insensitively", v.flagged, False)


# ==========================================================================
# 6.8  allowed-items derivation
# ==========================================================================

def test_allowed_items() -> None:
    section("6.8 allowed-items derivation")
    con = contract(["ITEM_22", "ITEM_23"], ["freight"], "Supplier_7")

    v = verdict_for(invoice([line("ITEM_23", "10", "45.00")]),
                    [po("ITEM_23", "10", "45.00")], con)
    check_eq("item on a later contract row is covered", v.flagged, False)

    v = verdict_for(invoice([line("ITEM_99", "10", "45.00")]),
                    [po("ITEM_99", "10", "45.00")], con)
    check_eq("item absent from this supplier's rows flags", v.fault_type, "OFF_CONTRACT_ITEM")

    # Another supplier's contract does not rescue the item.
    other = contract(["ITEM_99"], ["freight"], "Supplier_1")
    check("ITEM_99 is allowed for Supplier_1", norm_key("ITEM_99") in other.allowed_items)
    v = verdict_for(invoice([line("ITEM_99", "10", "45.00")]),
                    [po("ITEM_99", "10", "45.00")], con)
    check_eq("another supplier's contract does not rescue it",
             v.fault_type, "OFF_CONTRACT_ITEM")

    # Being on the PO does not rescue it either -- PO and contract are separate tests.
    check_eq("on the PO but off contract still flags", v.fault_type, "OFF_CONTRACT_ITEM")


# ==========================================================================
# Part 2 -- a SQLite fixture driven through the real loaders and the real
# /respond pipeline.  No live database, no deployment, no network.
# ==========================================================================

import os          # noqa: E402
import sqlite3     # noqa: E402
import tempfile    # noqa: E402

from models import FAULT_TYPES  # noqa: E402

FIXTURE_DIR = tempfile.mkdtemp(prefix="invoice_selftest_")

_SENTENCE = (
    "Review invoice {inv} (supplier {sup}, PO {po}) against its PO line and the supplier "
    "contract; decide whether to flag it for human review and give a short reason."
)


def sentence(inv: str, sup: str = "Supplier_7", po: str = "PO-000498") -> str:
    return _SENTENCE.format(inv=inv, sup=sup, po=po)


# Money is stored as TEXT so the fixture mirrors what psycopg hands back for a
# PostgreSQL NUMERIC (an exact Decimal) rather than introducing float in the test
# itself.  A separate fixture below covers numeric-typed columns.
STANDARD_DDL = (
    """CREATE TABLE invoices (invoice_number TEXT PRIMARY KEY, supplier_id TEXT,
       po_number TEXT, invoice_total TEXT, duplicate_of TEXT, currency TEXT, doc_type TEXT)""",
    """CREATE TABLE invoice_lines (invoice_number TEXT, line_no INTEGER, item_id TEXT,
       quantity TEXT, unit_price TEXT)""",
    """CREATE TABLE invoice_fees (invoice_number TEXT, fee_label TEXT, fee_amount TEXT)""",
    """CREATE TABLE po_lines (po_number TEXT, po_line_no INTEGER, item_id TEXT,
       quantity TEXT, unit_price TEXT, currency TEXT)""",
    """CREATE TABLE contracts (supplier_id TEXT, item_id TEXT, agreed_unit_price TEXT,
       allowed_fees TEXT)""",
)

CONTRACT_ROWS = (
    ("Supplier_7", "ITEM_22", "41.00", '["freight", "handling"]'),
    ("Supplier_7", "ITEM_23", "45.00", '["freight", "handling"]'),
    ("Supplier_1", "ITEM_99", "20.00", '["expedite_surcharge", "freight"]'),
)

PO_ROWS = (
    ("PO-000498", 1, "ITEM_22", "100", "41.00", "USD"),
    ("PO-000498", 2, "ITEM_23", "50", "45.00", "USD"),
    ("PO-OFFITEM", 1, "ITEM_99", "10", "20.00", "USD"),
    ("PO-NULLPRICE", 1, "ITEM_22", "100", None, "USD"),
    ("PO-EUR", 1, "ITEM_22", "100", "41.00", "USD"),
    # PO-EMPTY deliberately has no rows.
)

# (invoice_number, supplier, po, total, duplicate_of, currency, doc_type)
INVOICE_ROWS = (
    ("INV-CLEAN",       "Supplier_7",  "PO-000498",    "6650.00", None, "USD", "invoice"),
    ("INV-PRICE",       "Supplier_7",  "PO-000498",    "7350.00", None, "USD", "invoice"),
    ("INV-QTY",         "Supplier_7",  "PO-000498",    "7470.00", None, "USD", "invoice"),
    ("INV-TOTAL",       "Supplier_7",  "PO-000498",    "7900.00", None, "USD", "invoice"),
    ("INV-ITEM",        "Supplier_7",  "PO-OFFITEM",    "200.00", None, "USD", "invoice"),
    ("INV-FEE",         "Supplier_7",  "PO-000498",    "6600.00", None, "USD", "invoice"),
    ("INV-DUP",         "Supplier_7",  "PO-000498",    "6650.00", "INV-CLEAN", "USD", "invoice"),
    # gate cases, non-duplicates
    ("INV-NOPOLINE",    "Supplier_7",  "PO-OFFITEM",    "410.00", None, "USD", "invoice"),
    ("INV-LEAK",        "Supplier_7",  "PO-OFFITEM",    "100.00", None, "USD", "invoice"),
    ("INV-NULLPRICE",   "Supplier_7",  "PO-NULLPRICE", "4100.00", None, "USD", "invoice"),
    ("INV-NOCONTRACT",  "Supplier_42", "PO-000498",    "4100.00", None, "USD", "invoice"),
    ("INV-NOPO",        "Supplier_7",  "PO-EMPTY",     "4100.00", None, "USD", "invoice"),
    ("INV-CREDIT",      "Supplier_7",  "PO-000498",    "4100.00", None, "USD", "Credit Note"),
    ("INV-NEGQTY",      "Supplier_7",  "PO-000498",    "4100.00", None, "USD", "invoice"),
    ("INV-NEGAMT",      "Supplier_7",  "PO-000498",   "-4100.00", None, "USD", "invoice"),
    ("INV-EUR",         "Supplier_7",  "PO-EUR",       "4100.00", None, "EUR", "invoice"),
    ("INV-NOTOTAL",     "Supplier_7",  "PO-000498",         None, None, "USD", "invoice"),
    # the same gate cases, but duplicates -- these must short-circuit to DUPLICATE
    ("INV-DUP-NOPOLINE", "Supplier_7", "PO-OFFITEM",    "410.00", "INV-CLEAN", "USD", "invoice"),
    ("INV-DUP-NOCONTRACT", "Supplier_42", "PO-000498", "4100.00", "INV-CLEAN", "USD", "invoice"),
    ("INV-DUP-CREDIT",  "Supplier_7",  "PO-000498",   "-4100.00", "INV-CLEAN", "USD", "Credit Note"),
    ("INV-DUP-EUR",     "Supplier_7",  "PO-EUR",       "4100.00", "INV-CLEAN", "EUR", "invoice"),
    ("INV-DUP-NOPO",    "Supplier_7",  "PO-EMPTY",     "4100.00", "INV-CLEAN", "USD", "invoice"),
    ("INV-DUP-NOTOTAL", "Supplier_7",  "PO-000498",         None, "INV-CLEAN", "USD", "invoice"),
)

# (invoice_number, line_no, item_id, quantity, unit_price)
LINE_ROWS = (
    ("INV-CLEAN", 1, "ITEM_22", "100", "41.00"),
    ("INV-CLEAN", 2, "ITEM_23", "50", "45.00"),
    ("INV-PRICE", 1, "ITEM_22", "100", "48.00"),      # +$7 x 100 = $700 overcharge
    ("INV-PRICE", 2, "ITEM_23", "50", "45.00"),
    ("INV-QTY", 1, "ITEM_22", "120", "41.00"),        # 120 invoiced vs 100 ordered
    ("INV-QTY", 2, "ITEM_23", "50", "45.00"),
    ("INV-TOTAL", 1, "ITEM_22", "100", "41.00"),      # stated total is $1,250 too high
    ("INV-TOTAL", 2, "ITEM_23", "50", "45.00"),
    ("INV-ITEM", 1, "ITEM_99", "10", "20.00"),        # not on Supplier_7's contract
    ("INV-FEE", 1, "ITEM_22", "100", "41.00"),
    ("INV-FEE", 2, "ITEM_23", "50", "45.00"),
    ("INV-DUP", 1, "ITEM_22", "100", "41.00"),
    ("INV-DUP", 2, "ITEM_23", "50", "45.00"),
    ("INV-NOPOLINE", 1, "ITEM_22", "10", "41.00"),    # PO-OFFITEM has no ITEM_22 line
    ("INV-LEAK", 1, "ITEM_77", "10", "10.00"),        # no PO line AND off contract
    ("INV-NULLPRICE", 1, "ITEM_22", "100", "41.00"),
    ("INV-NOCONTRACT", 1, "ITEM_22", "100", "41.00"),
    ("INV-NOPO", 1, "ITEM_22", "100", "41.00"),
    ("INV-CREDIT", 1, "ITEM_22", "100", "41.00"),
    ("INV-NEGQTY", 1, "ITEM_22", "-100", "41.00"),
    ("INV-NEGAMT", 1, "ITEM_22", "100", "41.00"),
    ("INV-EUR", 1, "ITEM_22", "100", "41.00"),
    ("INV-NOTOTAL", 1, "ITEM_22", "100", "41.00"),
    ("INV-DUP-NOPOLINE", 1, "ITEM_22", "10", "41.00"),
    ("INV-DUP-NOCONTRACT", 1, "ITEM_22", "100", "41.00"),
    ("INV-DUP-CREDIT", 1, "ITEM_22", "-100", "41.00"),
    ("INV-DUP-EUR", 1, "ITEM_22", "100", "41.00"),
    ("INV-DUP-NOPO", 1, "ITEM_22", "100", "41.00"),
    ("INV-DUP-NOTOTAL", 1, "ITEM_22", "100", "41.00"),
)

FEE_ROWS = (
    ("INV-CLEAN", "freight", "300.00"),
    ("INV-PRICE", "freight", "300.00"),
    ("INV-QTY", "freight", "300.00"),
    ("INV-TOTAL", "freight", "300.00"),
    ("INV-FEE", "expedite_surcharge", "250.00"),      # not in Supplier_7's allowed_fees
    ("INV-DUP", "freight", "300.00"),
)


def build_standard_fixture() -> str:
    path = os.path.join(FIXTURE_DIR, "standard.db")
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    with conn:
        for statement in STANDARD_DDL:
            conn.execute(statement)
        conn.executemany("INSERT INTO contracts VALUES (?,?,?,?)", CONTRACT_ROWS)
        conn.executemany("INSERT INTO po_lines VALUES (?,?,?,?,?,?)", PO_ROWS)
        conn.executemany("INSERT INTO invoices VALUES (?,?,?,?,?,?,?)", INVOICE_ROWS)
        conn.executemany("INSERT INTO invoice_lines VALUES (?,?,?,?,?)", LINE_ROWS)
        conn.executemany("INSERT INTO invoice_fees VALUES (?,?,?)", FEE_ROWS)
    conn.close()
    return path


def make_client(path: str | None):
    """Point the service at a fixture (or at nothing) and reset its lazy singleton."""
    import server
    from fastapi.testclient import TestClient

    if path is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    os.environ.pop("SCHEMA_OVERRIDES", None)
    server._DATABASE = None
    return TestClient(server.app, raise_server_exceptions=False)


def ask(client, inv: str, sup: str = "Supplier_7", po: str = "PO-000498",
        text: str | None = None):
    content = text if text is not None else sentence(inv, sup, po)
    return client.post("/respond", json={"messages": [{"role": "user", "content": content}]})


def expect_verdict(client, label: str, inv: str, fault: str | None, flagged: bool = True,
                   sup: str = "Supplier_7", po: str = "PO-000498",
                   fragments: tuple[str, ...] = ()) -> None:
    response = ask(client, inv, sup, po)
    if response.status_code != 200:
        check(f"{label}: HTTP 200", False, f"(got {response.status_code} {response.text[:160]})")
        return
    check(f"{label}: HTTP 200", True)
    content = response.json()["choices"][0]["message"]["content"]
    # 6.13 serialisation, asserted on every successful verdict
    check(f"{label}: content is a bare JSON object",
          content[:1] == "{" and content[-1:] == "}", f"(got {content[:40]!r})")
    try:
        data = json.loads(content)
    except ValueError as exc:
        check(f"{label}: content parses as JSON", False, f"({exc})")
        return
    check_eq(f"{label}: exactly the three keys", sorted(data), ["fault_type", "flagged", "reason"])
    check_eq(f"{label}: flagged", data.get("flagged"), flagged)
    check_eq(f"{label}: fault_type", data.get("fault_type"), fault)
    check(f"{label}: fault_type is null or a closed-set label",
          data.get("fault_type") is None or data.get("fault_type") in FAULT_TYPES)
    check(f"{label}: reason is a non-empty string",
          isinstance(data.get("reason"), str) and bool(data["reason"].strip()))
    for fragment in fragments:
        check_in(f"{label}: reason names {fragment}", fragment, data.get("reason", ""))


def expect_error(client, label: str, inv: str, code: str, status: int = 422,
                 sup: str = "Supplier_7", po: str = "PO-000498",
                 text: str | None = None) -> None:
    response = ask(client, inv, sup, po, text)
    check_eq(f"{label}: HTTP {status}", response.status_code, status)
    try:
        body = response.json()
    except ValueError:
        check(f"{label}: body is JSON", False, f"({response.text[:120]!r})")
        return
    check_eq(f"{label}: error code", body.get("error"), code)
    check(f"{label}: body carries no flagged key", "flagged" not in body)
    check(f"{label}: body carries no fault_type key", "fault_type" not in body)
    check(f"{label}: message explains the gap", bool(str(body.get("message", "")).strip()))


# ==========================================================================
# 6.9 / 6.13  end-to-end verdicts and gates over the fixture
# ==========================================================================

def test_end_to_end_verdicts() -> None:
    section("6.9/6.13 end-to-end verdicts through /respond")
    client = make_client(build_standard_fixture())

    expect_verdict(client, "clean", "INV-CLEAN", None, flagged=False)
    expect_verdict(client, "price", "INV-PRICE", "PRICE_DELTA",
                   fragments=("ITEM_22", "$48.00", "$41.00", "$700.00"))
    expect_verdict(client, "qty", "INV-QTY", "QTY_MISMATCH",
                   fragments=("ITEM_22", "120 units", "100 ordered"))
    expect_verdict(client, "total", "INV-TOTAL", "TOTAL_MATH",
                   fragments=("$7,900.00", "$6,650.00", "$1,250.00"))
    expect_verdict(client, "off-contract item", "INV-ITEM", "OFF_CONTRACT_ITEM",
                   po="PO-OFFITEM", fragments=("ITEM_99", "Supplier_7"))
    expect_verdict(client, "off-contract fee", "INV-FEE", "OFF_CONTRACT_FEE",
                   fragments=("expedite_surcharge", "$250.00", "Supplier_7"))
    expect_verdict(client, "duplicate", "INV-DUP", "DUPLICATE",
                   fragments=("INV-CLEAN", "PO-000498"))


def test_gates_end_to_end() -> None:
    section("6.9 one gate per pipeline row")
    client = make_client(build_standard_fixture())

    # Step 1 -- the sentence itself
    expect_error(client, "no identifiers", "INV-CLEAN", "malformed_request",
                 text="please have a look at this one when you get a chance")
    expect_error(client, "supplier and PO missing", "INV-CLEAN", "malformed_request",
                 text="Review invoice INV-CLEAN and decide whether to flag it")
    # Step 2 -- the record
    expect_error(client, "unknown invoice", "INV-NOSUCH", "invoice_not_found")
    # Step 3 -- linkage
    expect_error(client, "supplier contradicts record", "INV-CLEAN",
                 "contradictory_linkage", sup="Supplier_1")
    expect_error(client, "PO contradicts record", "INV-CLEAN",
                 "contradictory_linkage", po="PO-OFFITEM")
    # Step 5 -- referenced records
    expect_error(client, "missing contract", "INV-NOCONTRACT", "contract_not_found",
                 sup="Supplier_42")
    expect_error(client, "missing PO", "INV-NOPO", "po_not_found", po="PO-EMPTY")
    # Step 6a -- document type
    expect_error(client, "credit note", "INV-CREDIT", "unsupported_document")
    expect_error(client, "negative quantity", "INV-NEGQTY", "unsupported_document")
    expect_error(client, "negative total", "INV-NEGAMT", "unsupported_document")
    # Step 6b -- currency
    expect_error(client, "currency mismatch", "INV-EUR", "currency_mismatch", po="PO-EUR")
    # Step 6c -- completeness
    expect_error(client, "missing PO line", "INV-NOPOLINE", "po_line_not_found",
                 po="PO-OFFITEM")
    expect_error(client, "null PO price", "INV-NULLPRICE", "po_line_price_missing",
                 po="PO-NULLPRICE")
    expect_error(client, "null invoice total", "INV-NOTOTAL", "invoice_total_missing")


# ==========================================================================
# 6.10  no lower-priority leakage
# ==========================================================================

def test_no_lower_priority_leakage() -> None:
    section("6.10 no lower-priority leakage")
    client = make_client(build_standard_fixture())
    # ITEM_77 has no PO line AND is off contract.  PRICE_DELTA and QTY_MISMATCH
    # cannot be evaluated, so OFF_CONTRACT_ITEM must not answer in their place.
    response = ask(client, "INV-LEAK", po="PO-OFFITEM")
    check_eq("missing PO line wins over off-contract item", response.status_code, 422)
    body = response.json()
    check_eq("...reported as po_line_not_found", body.get("error"), "po_line_not_found")
    check("...and not as a verdict", "fault_type" not in body and "flagged" not in body)
    check("...body never mentions OFF_CONTRACT_ITEM",
          "OFF_CONTRACT_ITEM" not in response.text)


# ==========================================================================
# 6.11  DUPLICATE short-circuits the data gates
# ==========================================================================

def test_duplicate_short_circuits_gates() -> None:
    section("6.11 DUPLICATE short-circuits the data gates")
    client = make_client(build_standard_fixture())

    pairs = (
        ("missing PO line", "INV-NOPOLINE", "po_line_not_found", "INV-DUP-NOPOLINE",
         "Supplier_7", "PO-OFFITEM"),
        ("missing contract", "INV-NOCONTRACT", "contract_not_found", "INV-DUP-NOCONTRACT",
         "Supplier_42", "PO-000498"),
        ("credit note / negative amount", "INV-NEGAMT", "unsupported_document",
         "INV-DUP-CREDIT", "Supplier_7", "PO-000498"),
        ("currency mismatch", "INV-EUR", "currency_mismatch", "INV-DUP-EUR",
         "Supplier_7", "PO-EUR"),
        ("missing PO", "INV-NOPO", "po_not_found", "INV-DUP-NOPO", "Supplier_7", "PO-EMPTY"),
        ("null invoice total", "INV-NOTOTAL", "invoice_total_missing", "INV-DUP-NOTOTAL",
         "Supplier_7", "PO-000498"),
    )
    for name, control, code, duplicate, sup, po in pairs:
        # Control: the identical data problem, without duplicate_of, is a 422.
        expect_error(client, f"control, non-duplicate {name}", control, code, sup=sup, po=po)
        # And with duplicate_of set, the same data problem never gets a chance to speak.
        expect_verdict(client, f"duplicate despite {name}", duplicate, "DUPLICATE",
                       sup=sup, po=po, fragments=("INV-CLEAN",))


# ==========================================================================
# 6.12  steps 2-3 still bind above DUPLICATE
# ==========================================================================

def test_gates_above_duplicate() -> None:
    section("6.12 steps 2-3 still bind above DUPLICATE")
    client = make_client(build_standard_fixture())
    expect_error(client, "nonexistent invoice", "INV-NOSUCH", "invoice_not_found")
    expect_error(client, "duplicate with contradicting supplier", "INV-DUP",
                 "contradictory_linkage", sup="Supplier_1")
    expect_error(client, "duplicate with contradicting PO", "INV-DUP",
                 "contradictory_linkage", po="PO-OFFITEM")


# ==========================================================================
# 6.14  /health is database-independent
# ==========================================================================

def test_health_is_db_independent() -> None:
    section("6.14 /health is database-independent")
    client = make_client(None)   # no DATABASE_URL at all
    response = client.get("/health")
    check_eq("/health returns 200 with no database", response.status_code, 200)
    check_eq("/health body", response.json(), {"status": "ok"})

    response = ask(client, "INV-CLEAN")
    check_eq("/respond returns 503 in the same state", response.status_code, 503)
    body = response.json()
    check_eq("...as database_unavailable", body.get("error"), "database_unavailable")
    check("...with no fabricated verdict", "flagged" not in body and "fault_type" not in body)

    # An unreachable host, not merely an unset variable.
    client = make_client(None)
    os.environ["DATABASE_URL"] = "postgresql://nobody@127.0.0.1:1/none"
    import server
    server._DATABASE = None
    check_eq("/health still 200 with an unreachable database",
             client.get("/health").status_code, 200)
    check_eq("/respond reports 503", ask(client, "INV-CLEAN").status_code, 503)
    os.environ.pop("DATABASE_URL", None)


# ==========================================================================
# 6.15  the same data under a renamed schema (introspection)
# ==========================================================================

RENAMED_DDL = (
    """CREATE TABLE ap_invoice (inv_no TEXT, vendor_id TEXT, purchase_order_no TEXT,
       gross_total TEXT, dup_of TEXT, currency_code TEXT, document_type TEXT)""",
    """CREATE TABLE ap_invoice_line (inv_no TEXT, line_number INTEGER, sku TEXT,
       invoiced_quantity TEXT, invoiced_unit_price TEXT)""",
    """CREATE TABLE ap_invoice_charge (inv_no TEXT, charge_type TEXT, charge_amount TEXT)""",
    """CREATE TABLE purchase_order_line (purchase_order_no TEXT, line_number INTEGER,
       sku TEXT, ordered_quantity TEXT, ordered_unit_price TEXT, currency_code TEXT)""",
    """CREATE TABLE supplier_contract (vendor_id TEXT, sku TEXT, agreed_unit_price TEXT,
       permitted_fees TEXT)""",
)


def build_renamed_fixture() -> str:
    path = os.path.join(FIXTURE_DIR, "renamed.db")
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    with conn:
        for statement in RENAMED_DDL:
            conn.execute(statement)
        conn.executemany("INSERT INTO supplier_contract VALUES (?,?,?,?)",
                         [(s, i, p, f.replace("[", "{").replace("]", "}").replace('"', ""))
                          for s, i, p, f in CONTRACT_ROWS])
        conn.executemany("INSERT INTO purchase_order_line VALUES (?,?,?,?,?,?)", PO_ROWS)
        conn.executemany(
            "INSERT INTO ap_invoice VALUES (?,?,?,?,?,?,?)",
            [r for r in INVOICE_ROWS if r[0] in ("INV-CLEAN", "INV-PRICE", "INV-FEE", "INV-DUP")])
        conn.executemany(
            "INSERT INTO ap_invoice_line VALUES (?,?,?,?,?)",
            [r for r in LINE_ROWS if r[0] in ("INV-CLEAN", "INV-PRICE", "INV-FEE", "INV-DUP")])
        conn.executemany(
            "INSERT INTO ap_invoice_charge VALUES (?,?,?)",
            [r for r in FEE_ROWS if r[0] in ("INV-CLEAN", "INV-PRICE", "INV-FEE", "INV-DUP")])
    conn.close()
    return path


def test_renamed_schema() -> None:
    section("6.15 introspection resolves a renamed schema")
    path = build_renamed_fixture()
    client = make_client(path)

    import server
    schema = server.get_database().schema()
    check_eq("invoices resolved", schema.get("invoices").table, "ap_invoice")
    check_eq("invoice_lines resolved", schema.get("invoice_lines").table, "ap_invoice_line")
    check_eq("invoice_fees resolved", schema.get("invoice_fees").table, "ap_invoice_charge")
    check_eq("po_lines resolved", schema.get("po_lines").table, "purchase_order_line")
    check_eq("contracts resolved", schema.get("contracts").table, "supplier_contract")
    check_eq("invoice number column", schema.get("invoices").columns["invoice_number"], "inv_no")
    check_eq("duplicate_of column", schema.get("invoices").columns["duplicate_of"], "dup_of")
    check_eq("total column", schema.get("invoices").columns["total"], "gross_total")
    check_eq("fee label column", schema.get("invoice_fees").columns["label"], "charge_type")
    check_eq("allowed_fees column",
             schema.get("contracts").columns["allowed_fees"], "permitted_fees")

    # Same verdicts as under the documented names -- including a Postgres-style
    # array literal for allowed_fees.
    expect_verdict(client, "renamed clean", "INV-CLEAN", None, flagged=False)
    expect_verdict(client, "renamed price", "INV-PRICE", "PRICE_DELTA",
                   fragments=("ITEM_22", "$700.00"))
    expect_verdict(client, "renamed fee", "INV-FEE", "OFF_CONTRACT_FEE",
                   fragments=("expedite_surcharge",))
    expect_verdict(client, "renamed duplicate", "INV-DUP", "DUPLICATE",
                   fragments=("INV-CLEAN",))


# ==========================================================================
# 6.16  lines and fees embedded as JSON on the invoice row
# ==========================================================================

JSON_DDL = (
    """CREATE TABLE invoices (invoice_number TEXT, supplier_id TEXT, po_number TEXT,
       invoice_total TEXT, duplicate_of TEXT, currency TEXT, line_items TEXT, fees TEXT)""",
    """CREATE TABLE po_lines (po_number TEXT, po_line_no INTEGER, item_id TEXT,
       quantity TEXT, unit_price TEXT, currency TEXT)""",
    """CREATE TABLE contracts (supplier_id TEXT, item_id TEXT, agreed_unit_price TEXT,
       allowed_fees TEXT)""",
)


def build_json_fixture() -> str:
    path = os.path.join(FIXTURE_DIR, "json.db")
    if os.path.exists(path):
        os.remove(path)
    clean_lines = json.dumps([
        {"item_id": "ITEM_22", "quantity": "100", "unit_price": "41.00"},
        {"item_id": "ITEM_23", "quantity": "50", "unit_price": "45.00"},
    ])
    price_lines = json.dumps([
        {"item_id": "ITEM_22", "quantity": "100", "unit_price": "48.00"},
        {"item_id": "ITEM_23", "quantity": "50", "unit_price": "45.00"},
    ])
    conn = sqlite3.connect(path)
    with conn:
        for statement in JSON_DDL:
            conn.execute(statement)
        conn.executemany("INSERT INTO contracts VALUES (?,?,?,?)", CONTRACT_ROWS)
        conn.executemany("INSERT INTO po_lines VALUES (?,?,?,?,?,?)", PO_ROWS)
        conn.executemany("INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?)", (
            ("INV-CLEAN", "Supplier_7", "PO-000498", "6650.00", None, "USD",
             clean_lines, json.dumps([{"fee_label": "freight", "amount": "300.00"}])),
            ("INV-PRICE", "Supplier_7", "PO-000498", "7350.00", None, "USD",
             price_lines, json.dumps({"freight": "300.00"})),
        ))
    conn.close()
    return path


def test_json_embedded_details() -> None:
    section("6.16 lines and fees embedded as JSON")
    client = make_client(build_json_fixture())
    import server
    schema = server.get_database().schema()
    check_eq("json lines column found", schema.json_lines_column, "line_items")
    check_eq("json fees column found", schema.json_fees_column, "fees")
    check("no separate invoice_lines table was needed", schema.get("invoice_lines") is None)
    expect_verdict(client, "json clean", "INV-CLEAN", None, flagged=False)
    expect_verdict(client, "json price", "INV-PRICE", "PRICE_DELTA",
                   fragments=("ITEM_22", "$700.00"))


# ==========================================================================
# 6.17  numeric-typed money columns
# ==========================================================================

NUMERIC_DDL = tuple(
    statement.replace("invoice_total TEXT", "invoice_total NUMERIC")
             .replace("quantity TEXT", "quantity NUMERIC")
             .replace("unit_price TEXT", "unit_price NUMERIC")
             .replace("fee_amount TEXT", "fee_amount NUMERIC")
             .replace("agreed_unit_price TEXT", "agreed_unit_price NUMERIC")
    for statement in STANDARD_DDL
)


def build_numeric_fixture() -> str:
    path = os.path.join(FIXTURE_DIR, "numeric.db")
    if os.path.exists(path):
        os.remove(path)
    keep = ("INV-CLEAN", "INV-PRICE")
    conn = sqlite3.connect(path)
    with conn:
        for statement in NUMERIC_DDL:
            conn.execute(statement)
        conn.executemany("INSERT INTO contracts VALUES (?,?,?,?)",
                         [(s, i, float(p), f) for s, i, p, f in CONTRACT_ROWS])
        conn.executemany("INSERT INTO po_lines VALUES (?,?,?,?,?,?)",
                         [(p, n, i, float(q), None if u is None else float(u), c)
                          for p, n, i, q, u, c in PO_ROWS])
        conn.executemany("INSERT INTO invoices VALUES (?,?,?,?,?,?,?)",
                         [(n, s, p, float(t), d, c, k)
                          for n, s, p, t, d, c, k in INVOICE_ROWS if n in keep])
        conn.executemany("INSERT INTO invoice_lines VALUES (?,?,?,?,?)",
                         [(n, ln, i, float(q), float(u))
                          for n, ln, i, q, u in LINE_ROWS if n in keep])
        conn.executemany("INSERT INTO invoice_fees VALUES (?,?,?)",
                         [(n, l, float(a)) for n, l, a in FEE_ROWS if n in keep])
    conn.close()
    return path


def test_numeric_columns() -> None:
    section("6.17 numeric-typed money columns")
    client = make_client(build_numeric_fixture())
    expect_verdict(client, "numeric clean", "INV-CLEAN", None, flagged=False)
    expect_verdict(client, "numeric price", "INV-PRICE", "PRICE_DELTA",
                   fragments=("$48.00", "$41.00", "$700.00"))


# ==========================================================================
# 6.18  the real build schema: one denormalised row per invoice, PO-line
#       foreign key, JSON fee array
#
# Column names, order and types mirror the deployed database exactly:
#   invoices  -- one row per invoice; it is its own single line item
#   po_lines  -- joined on po_line_id, not on item_id
#   contracts -- carries validity dates we deliberately do not act on
# ==========================================================================

FLAT_DDL = (
    """CREATE TABLE invoices (
           invoice_number TEXT, po_line_id TEXT, po_number TEXT, supplier_id TEXT,
           supplier_name TEXT, item_id TEXT, item_number TEXT, currency TEXT,
           invoiced_qty NUMERIC, unit_price NUMERIC, amount NUMERIC, extra_fees TEXT,
           duplicate_of TEXT, invoice_date TEXT, status TEXT)""",
    """CREATE TABLE po_lines (
           po_line_id TEXT, po_number TEXT, item_id TEXT, item_number TEXT,
           item_uom TEXT, quantity NUMERIC, unit_price NUMERIC, received_qty NUMERIC,
           pay_terms TEXT, currency TEXT)""",
    """CREATE TABLE contracts (
           contract_id TEXT, supplier_id TEXT, item_id TEXT, agreed_unit_price NUMERIC,
           allowed_fees TEXT, effective_date TEXT, expiry_date TEXT)""",
)

FUEL = '[{"label": "fuel surcharge", "amount": 169.47}]'
EXPEDITE = '[{"label": "expedite surcharge", "amount": 250.00}]'

# contract_id, supplier_id, item_id, agreed_unit_price, allowed_fees, effective, expiry
FLAT_CONTRACTS = (
    # Supplier_7's ITEM_22 row is deliberately expired: contract validity is not one of
    # the six elicited checks, so it must not change any verdict.
    ("C-1", "Supplier_7", "ITEM_22", 41.00, '["freight", "fuel surcharge"]',
     "2019-01-01", "2020-01-01"),
    ("C-2", "Supplier_7", "ITEM_35", 12.00, '["freight", "fuel surcharge"]',
     "2024-01-01", "2030-01-01"),
    ("C-3", "Supplier_7", "ITEM_50", 7.00, '["freight"]', "2024-01-01", "2030-01-01"),
    ("C-4", "Supplier_7", "ITEM_60", 5.00, '["freight"]', "2024-01-01", "2030-01-01"),
    # ITEM_44 is absent from Supplier_7 on purpose -> OFF_CONTRACT_ITEM.
    # Supplier_8 may bill a fee Supplier_7 may not: fees never generalise.
    ("C-5", "Supplier_8", "ITEM_35", 12.00, '["expedite surcharge"]',
     "2024-01-01", "2030-01-01"),
    ("C-6", "Supplier_8", "ITEM_44", 900.00, '["expedite surcharge"]',
     "2024-01-01", "2030-01-01"),
)

# po_line_id, po_number, item_id, item_number, item_uom, quantity, unit_price,
# received_qty, pay_terms, currency
FLAT_PO_LINES = (
    # received_qty deliberately disagrees with quantity: the elicited rule compares the
    # invoice against the ordered quantity, with no remaining-balance tracking.
    ("POL-1", "PO-000498", "ITEM_22", "22", "EA", 100, 41.00, 40, "NET30", "USD"),
    ("POL-2", "PO-000498", "ITEM_35", "35", "EA", 50, 12.00, 50, "NET30", "USD"),
    ("POL-3", "PO-000498", "ITEM_44", "44", "EA", 10, 900.00, 0, "NET30", "USD"),
    ("POL-4", "PO-000498", "ITEM_50", "50", "EA", 20, None, 20, "NET30", "USD"),
    ("POL-5", "PO-000555", "ITEM_60", "60", "EA", 30, 5.00, 30, "NET30", "EUR"),
    ("POL-8", "PO-000888", "ITEM_35", "35", "EA", 50, 12.00, 50, "NET30", "USD"),
)

# invoice_number, po_line_id, po_number, supplier_id, supplier_name, item_id,
# item_number, currency, invoiced_qty, unit_price, amount, extra_fees, duplicate_of,
# invoice_date, status
FLAT_INVOICES = (
    # 100 x 41.00 + 169.47 fee = 4269.47, all six checks pass.
    ("FLAT-CLEAN", "POL-1", "PO-000498", "Supplier_7", "Seven Ltd", "ITEM_22", "22",
     "USD", 100, 41.00, 4269.47, FUEL, None, "2024-03-01", "posted"),
    # (48.00 - 41.00) x 100 = 700.00 overcharge.
    ("FLAT-PRICE", "POL-1", "PO-000498", "Supplier_7", "Seven Ltd", "ITEM_22", "22",
     "USD", 100, 48.00, 4969.47, FUEL, None, "2024-03-02", "posted"),
    ("FLAT-QTY", "POL-1", "PO-000498", "Supplier_7", "Seven Ltd", "ITEM_22", "22",
     "USD", 120, 41.00, 5089.47, FUEL, None, "2024-03-03", "posted"),
    # Stated 5600.00 against 4269.47 computed: off by 1330.53.
    ("FLAT-TOTAL", "POL-1", "PO-000498", "Supplier_7", "Seven Ltd", "ITEM_22", "22",
     "USD", 100, 41.00, 5600.00, FUEL, None, "2024-03-04", "posted"),
    # Priced and counted correctly, but ITEM_44 is not on Supplier_7's contract.
    ("FLAT-ITEM", "POL-3", "PO-000498", "Supplier_7", "Seven Ltd", "ITEM_44", "44",
     "USD", 10, 900.00, 9169.47, FUEL, None, "2024-03-05", "posted"),
    ("FLAT-FEE", "POL-2", "PO-000498", "Supplier_7", "Seven Ltd", "ITEM_35", "35",
     "USD", 50, 12.00, 850.00, EXPEDITE, None, "2024-03-06", "posted"),
    # The same fee, billed by the supplier whose contract permits it.
    ("FLAT-FEE-OK", "POL-8", "PO-000888", "Supplier_8", "Eight Ltd", "ITEM_35", "35",
     "USD", 50, 12.00, 850.00, EXPEDITE, None, "2024-03-07", "posted"),
    ("FLAT-DUP", "POL-1", "PO-000498", "Supplier_7", "Seven Ltd", "ITEM_22", "22",
     "USD", 100, 41.00, 4269.47, FUEL, "FLAT-CLEAN", "2024-03-08", "posted"),
    # Duplicate whose lower-check inputs are all broken: dangling PO line, negative
    # quantity and amount.  Must still report DUPLICATE.
    ("FLAT-DUP-BAD", "POL-NOPE", "PO-000498", "Supplier_7", "Seven Ltd", "ITEM_99", "99",
     "USD", -5, 41.00, -2000.00, FUEL, "FLAT-CLEAN", "2024-03-09", "posted"),
    # Non-duplicate control for the row above.
    ("FLAT-NEG", "POL-1", "PO-000498", "Supplier_7", "Seven Ltd", "ITEM_22", "22",
     "USD", -5, 41.00, -205.00, FUEL, None, "2024-03-10", "posted"),
    ("FLAT-BADPO", "POL-1", "PO-999999", "Supplier_7", "Seven Ltd", "ITEM_22", "22",
     "USD", 100, 41.00, 4269.47, FUEL, None, "2024-03-11", "posted"),
    ("FLAT-BADSUP", "POL-1", "PO-000498", "Supplier_9", "Nine Ltd", "ITEM_22", "22",
     "USD", 100, 41.00, 4269.47, FUEL, None, "2024-03-12", "posted"),
    ("FLAT-NOPO", "POL-1", "PO-EMPTY", "Supplier_7", "Seven Ltd", "ITEM_22", "22",
     "USD", 100, 41.00, 4269.47, FUEL, None, "2024-03-13", "posted"),
    ("FLAT-NOCONTRACT", "POL-1", "PO-000498", "Supplier_99", "Ninety Ltd", "ITEM_22",
     "22", "USD", 100, 41.00, 4269.47, FUEL, None, "2024-03-14", "posted"),
    # Dangling foreign key.  ITEM_22 would item-match POL-1, so this also proves the
    # key is authoritative rather than a hint.
    ("FLAT-DANGLING", "POL-NOPE", "PO-000498", "Supplier_7", "Seven Ltd", "ITEM_22",
     "22", "USD", 100, 41.00, 4269.47, FUEL, None, "2024-03-15", "posted"),
    ("FLAT-NULLPRICE", "POL-4", "PO-000498", "Supplier_7", "Seven Ltd", "ITEM_50", "50",
     "USD", 20, 7.00, 140.00, None, None, "2024-03-16", "posted"),
    ("FLAT-CURRENCY", "POL-5", "PO-000555", "Supplier_7", "Seven Ltd", "ITEM_60", "60",
     "USD", 30, 5.00, 150.00, None, None, "2024-03-17", "posted"),
    ("FLAT-NOTOTAL", "POL-1", "PO-000498", "Supplier_7", "Seven Ltd", "ITEM_22", "22",
     "USD", 100, 41.00, None, FUEL, None, "2024-03-18", "posted"),
    ("FLAT-NOITEM", "POL-1", "PO-000498", "Supplier_7", "Seven Ltd", None, "22",
     "USD", 100, 41.00, 4269.47, FUEL, None, "2024-03-19", "posted"),
    ("FLAT-NOFEELABEL", "POL-1", "PO-000498", "Supplier_7", "Seven Ltd", "ITEM_22", "22",
     "USD", 100, 41.00, 4269.47, '[{"label": null, "amount": 169.47}]', None,
     "2024-03-20", "posted"),
)


def build_flat_fixture() -> str:
    path = os.path.join(FIXTURE_DIR, "flat.db")
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    with conn:
        for statement in FLAT_DDL:
            conn.execute(statement)
        conn.executemany("INSERT INTO contracts VALUES (?,?,?,?,?,?,?)", FLAT_CONTRACTS)
        conn.executemany("INSERT INTO po_lines VALUES (?,?,?,?,?,?,?,?,?,?)", FLAT_PO_LINES)
        conn.executemany(
            "INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", FLAT_INVOICES
        )
    conn.close()
    return path


def test_flat_schema_resolution() -> None:
    section("6.18a real build schema resolves to the documented mapping")
    import db
    import server

    client = make_client(build_flat_fixture())
    schema = server.get_database().schema()

    invoices = schema.get("invoices")
    check("invoices table found", invoices is not None)
    check_eq("invoices -> invoices", invoices.table, "invoices")
    check_eq("invoice total -> amount", invoices.columns.get("total"), "amount")
    check_eq("duplicate_of resolved", invoices.columns.get("duplicate_of"), "duplicate_of")
    # status is a workflow state, not a document type; reading it as one would turn a
    # credit hold into an unsupported document.
    check("status is not read as a document type",
          invoices.columns.get("doc_type") is None)

    check("no invoice_lines child table", schema.get("invoice_lines") is None)
    check("no invoice_fees child table", schema.get("invoice_fees") is None)
    check_eq("line source is the invoice row", schema.line_source(), "flat")
    check_eq(
        "flat line mapping",
        schema.flat_line_columns,
        {"item_id": "item_id", "quantity": "invoiced_qty", "unit_price": "unit_price",
         "po_line_id": "po_line_id"},
    )
    check_eq("fees come from extra_fees", schema.json_fees_column, "extra_fees")

    po = schema.get("po_lines")
    check_eq("po_lines -> po_lines", po.table, "po_lines")
    check_eq("PO quantity is the ordered quantity, not received_qty",
             po.columns.get("quantity"), "quantity")
    check_eq("PO join key resolved", po.columns.get("po_line_id"), "po_line_id")
    check_eq("item_id preferred over item_number", po.columns.get("item_id"), "item_id")

    contracts = schema.get("contracts")
    check_eq("contracts -> contracts", contracts.table, "contracts")
    check_eq("allowed_fees resolved", contracts.columns.get("allowed_fees"), "allowed_fees")

    # The flat row really does produce one line carrying the foreign key.
    database = server.get_database()
    invoice = db.load_invoice_header(database, "FLAT-CLEAN")
    db.load_invoice_details(database, invoice)
    check_eq("flat row yields exactly one line", len(invoice.lines), 1)
    check_eq("line item id", invoice.lines[0].item_id, "ITEM_22")
    check_eq("line quantity from invoiced_qty", invoice.lines[0].quantity, Decimal("100"))
    check_eq("line unit price", invoice.lines[0].unit_price, Decimal("41.00"))
    check_eq("line carries the PO foreign key", invoice.lines[0].po_line_id, "POL-1")
    check_eq("JSON fee parsed", len(invoice.fees), 1)
    check_eq("fee label", invoice.fees[0].label, "fuel surcharge")
    check_eq("fee amount", invoice.fees[0].amount, Decimal("169.47"))

    # The key, not the item, decides which PO line is the baseline.
    po_lines = db.load_po_lines(database, "PO-000498")
    index = rules.build_po_index(po_lines)
    check_eq("PO lines loaded", len(po_lines), 4)
    matched = index.match(InvoiceLine(item_id="ITEM_22", quantity=Decimal(1),
                                      unit_price=Decimal(1), po_line_id="POL-2"))
    check_eq("foreign key wins over item id", matched.po_line_id, "POL-2")
    dangling = index.match(InvoiceLine(item_id="ITEM_22", quantity=Decimal(1),
                                       unit_price=Decimal(1), po_line_id="POL-NOPE"))
    check("a dangling key does not silently fall back to the item", dangling is None)


def test_flat_schema_verdicts() -> None:
    section("6.18b real build schema: verdicts")
    client = make_client(build_flat_fixture())

    expect_verdict(client, "flat clean", "FLAT-CLEAN", None, flagged=False)
    expect_verdict(client, "flat price", "FLAT-PRICE", "PRICE_DELTA",
                   fragments=("ITEM_22", "$48.00", "$41.00", "$700.00"))
    expect_verdict(client, "flat qty", "FLAT-QTY", "QTY_MISMATCH",
                   fragments=("ITEM_22", "120", "100"))
    expect_verdict(client, "flat total", "FLAT-TOTAL", "TOTAL_MATH",
                   fragments=("$5,600.00", "$4,269.47", "$1,330.53"))
    expect_verdict(client, "flat off-contract item", "FLAT-ITEM", "OFF_CONTRACT_ITEM",
                   fragments=("ITEM_44", "Supplier_7"))
    expect_verdict(client, "flat off-contract fee", "FLAT-FEE", "OFF_CONTRACT_FEE",
                   fragments=("expedite surcharge", "$250.00"))
    expect_verdict(client, "flat duplicate", "FLAT-DUP", "DUPLICATE",
                   fragments=("FLAT-CLEAN", "PO-000498"))

    # Same fee, different supplier: allowed there, so no flag.  Fees never generalise.
    expect_verdict(client, "flat fee allowed for its own supplier", "FLAT-FEE-OK", None,
                   flagged=False, sup="Supplier_8", po="PO-000888")

    # An expired contract row still governs: contract validity is not one of the six
    # checks and must not invent a seventh.
    expect_verdict(client, "expired contract row changes nothing", "FLAT-CLEAN", None,
                   flagged=False)


def test_flat_schema_gates() -> None:
    section("6.18c real build schema: gates and the duplicate short-circuit")
    client = make_client(build_flat_fixture())

    expect_error(client, "flat unknown invoice", "FLAT-NOPE", "invoice_not_found")
    expect_error(client, "flat PO contradicts record", "FLAT-BADPO", "contradictory_linkage")
    expect_error(client, "flat supplier contradicts record", "FLAT-BADSUP",
                 "contradictory_linkage", sup="Supplier_9x")
    expect_error(client, "flat no contract", "FLAT-NOCONTRACT", "contract_not_found",
                 sup="Supplier_99")
    expect_error(client, "flat no PO", "FLAT-NOPO", "po_not_found", po="PO-EMPTY")
    expect_error(client, "flat negative amounts", "FLAT-NEG", "unsupported_document")
    expect_error(client, "flat currency mismatch", "FLAT-CURRENCY", "currency_mismatch",
                 po="PO-000555")
    expect_error(client, "flat dangling PO line", "FLAT-DANGLING", "po_line_not_found")
    expect_error(client, "flat PO line has no price", "FLAT-NULLPRICE",
                 "po_line_price_missing")
    expect_error(client, "flat no invoice total", "FLAT-NOTOTAL", "invoice_total_missing")
    expect_error(client, "flat line has no item", "FLAT-NOITEM", "invoice_line_incomplete")
    expect_error(client, "flat fee has no label", "FLAT-NOFEELABEL", "fee_label_missing")

    # DUPLICATE is check 1 and is decided from the header: a duplicate whose PO line is
    # dangling and whose amounts are negative still reports DUPLICATE, while the
    # identical non-duplicate row above returns 422.
    expect_verdict(client, "flat duplicate short-circuits the gates", "FLAT-DUP-BAD",
                   "DUPLICATE", fragments=("FLAT-CLEAN",))



def test_jsonb_and_numeric_native_types() -> None:
    """psycopg decodes JSONB to Python objects and NUMERIC to Decimal before we see it.
    SQLite can only hand back text, so those types are asserted directly."""
    section("6.18d decoded JSONB and NUMERIC values")
    import db
    from models import to_decimal

    aliases = db.FIELD_ALIASES["invoice_fees"]

    # What psycopg returns for extra_fees: a decoded list of dicts with float amounts.
    fees = db._fees_from_json([{"label": "fuel surcharge", "amount": 169.47}], aliases)
    check_eq("decoded JSONB fee count", len(fees), 1)
    check_eq("decoded JSONB fee label", fees[0].label, "fuel surcharge")
    check_eq("decoded JSONB fee amount is exact", fees[0].amount, Decimal("169.47"))
    check("float amount did not pick up binary noise",
          str(fees[0].amount) == "169.47", f"(got {fees[0].amount})")

    many = db._fees_from_json(
        [{"label": "freight", "amount": 300}, {"label": "expedite surcharge",
                                               "amount": 250.5}], aliases)
    check_eq("two decoded fees", [f.label for f in many], ["freight", "expedite surcharge"])
    check_eq("integer JSON amount", many[0].amount, Decimal("300"))
    check_eq("fractional JSON amount", many[1].amount, Decimal("250.5"))

    # The same column as raw text, which is what SQLite and a TEXT column give.
    as_text = db._fees_from_json('[{"label": "fuel surcharge", "amount": 169.47}]', aliases)
    check_eq("text and decoded JSONB agree",
             [(f.label, f.amount) for f in as_text],
             [(f.label, f.amount) for f in fees])
    check_eq("empty JSONB array yields no fees", db._fees_from_json([], aliases), [])
    check_eq("NULL extra_fees yields no fees", db._fees_from_json(None, aliases), [])

    # allowed_fees as a decoded JSONB array.
    check_eq("decoded allowed_fees", db.parse_fee_labels(["freight", "fuel surcharge"]),
             frozenset({"freight", "fuel surcharge"}))
    check_eq("text allowed_fees agrees",
             db.parse_fee_labels('["freight", "fuel surcharge"]'),
             frozenset({"freight", "fuel surcharge"}))
    check_eq("empty allowed_fees", db.parse_fee_labels("[]"), frozenset())

    # NUMERIC arrives as Decimal and must pass through untouched.
    check_eq("Decimal passes through", to_decimal(Decimal("41.00")), Decimal("41.00"))
    check_eq("Decimal quantity passes through", to_decimal(Decimal("100")), Decimal("100"))
    check_eq("float money is exact via str()", to_decimal(4269.47), Decimal("4269.47"))
    check_eq("int quantity", to_decimal(100), Decimal("100"))



def build_alias_supplier_fixture() -> str:
    """The real invoices table carries both supplier_id and supplier_name.  Here the
    request's supplier token lives in supplier_name while contracts join on that value,
    and supplier_id holds a bare code.  Looking the supplier up by the wrong column
    would report a missing contract for every invoice."""
    path = os.path.join(FIXTURE_DIR, "alias_supplier.db")
    if os.path.exists(path):
        os.remove(path)
    invoices = (
        ("ALIAS-CLEAN", "POL-1", "PO-000498", "7", "Supplier_7", "ITEM_22", "22",
         "USD", 100, 41.00, 4269.47, FUEL, None, "2024-04-01", "posted"),
        ("ALIAS-FEE", "POL-2", "PO-000498", "7", "Supplier_7", "ITEM_35", "35",
         "USD", 50, 12.00, 850.00, EXPEDITE, None, "2024-04-02", "posted"),
        ("ALIAS-OTHER", "POL-1", "PO-000498", "9", "Supplier_9", "ITEM_22", "22",
         "USD", 100, 41.00, 4269.47, FUEL, None, "2024-04-03", "posted"),
    )
    contracts = tuple(
        (cid, "Supplier_7" if sup == "Supplier_7" else sup, item, price, fees, eff, exp)
        for cid, sup, item, price, fees, eff, exp in FLAT_CONTRACTS
    )
    conn = sqlite3.connect(path)
    with conn:
        for statement in FLAT_DDL:
            conn.execute(statement)
        conn.executemany("INSERT INTO contracts VALUES (?,?,?,?,?,?,?)", contracts)
        conn.executemany("INSERT INTO po_lines VALUES (?,?,?,?,?,?,?,?,?,?)", FLAT_PO_LINES)
        conn.executemany(
            "INSERT INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", invoices
        )
    conn.close()
    return path


def test_supplier_identified_by_either_column() -> None:
    section("6.18e supplier named by either identifying column")
    import server

    client = make_client(build_alias_supplier_fixture())
    schema = server.get_database().schema()
    invoices = schema.get("invoices")
    check_eq("supplier_id resolved", invoices.columns.get("supplier_id"), "supplier_id")
    check_eq("supplier_name resolved separately",
             invoices.columns.get("supplier_name"), "supplier_name")

    # The request names Supplier_7; the record's supplier_id is "7".  Agreement on the
    # other column is agreement, and the contract is found by the value that joins.
    expect_verdict(client, "supplier matched by name column", "ALIAS-CLEAN", None,
                   flagged=False)

    # The fallback must not widen the contract: Supplier_7 still may not bill a fee only
    # Supplier_8 is permitted.
    expect_verdict(client, "fallback does not widen the contract", "ALIAS-FEE",
                   "OFF_CONTRACT_FEE", fragments=("expedite surcharge",))

    # Matching neither column is still a contradiction.
    expect_error(client, "matching neither column contradicts", "ALIAS-OTHER",
                 "contradictory_linkage")
    # And a supplier present in no contract row is still reported as missing.
    expect_error(client, "unknown supplier still has no contract", "ALIAS-OTHER",
                 "contract_not_found", sup="Supplier_9")



# ==========================================================================
# 6.19  transport-conformance probe
#
# The evaluator handshakes the transport before any invoice exists.  The guard must
# answer that one sentence and must not become a way in for malformed domain requests.
# ==========================================================================

PROBE = "conformance probe: please respond"


def test_transport_probe() -> None:
    section("6.19 transport-conformance probe")
    client = make_client(build_flat_fixture())

    # (1) the exact probe returns 200 in the normal envelope
    response = ask(client, "", text=PROBE)
    check_eq("probe: HTTP 200", response.status_code, 200)
    body = response.json()
    check("probe: body has choices", isinstance(body.get("choices"), list) and body["choices"])
    message = body["choices"][0].get("message", {}) if body.get("choices") else {}
    check_eq("probe: role is assistant", message.get("role"), "assistant")
    check("probe: content is a string", isinstance(message.get("content"), str))

    # (2) content is valid JSON and carries no business verdict
    content = message.get("content", "")
    check("probe: content is a bare JSON object",
          content[:1] == "{" and content[-1:] == "}", f"(got {content[:40]!r})")
    try:
        data = json.loads(content)
    except ValueError as exc:
        check("probe: content parses as JSON", False, f"({exc})")
        data = {}
    else:
        check("probe: content parses as JSON", True)
    check("probe: content is a JSON object", isinstance(data, dict))
    for key in ("flagged", "fault_type", "reason"):
        check(f"probe: content carries no {key}", key not in data)
    check("probe: no fault label anywhere in the content",
          not any(label in content for label in FAULT_TYPES), f"(got {content!r})")
    check_eq("probe: content is the agreed acknowledgement", data.get("status"), "ok")

    # Normalisation covers case and surrounding/repeated whitespace only.
    for variant, label in (
        ("  conformance probe: please respond  ", "padded"),
        ("Conformance Probe: Please Respond", "capitalised"),
        ("conformance  probe:  please   respond", "inner whitespace"),
    ):
        variant_response = ask(client, "", text=variant)
        check_eq(f"probe ({label}): HTTP 200", variant_response.status_code, 200)

    # (3) malformed domain requests are still rejected -- the guard did not widen
    for text, label in (
        ("please respond", "probe suffix alone"),
        ("conformance probe", "probe prefix alone"),
        ("conformance probe: please respond now", "probe plus a trailing word"),
        ("hello", "unrelated text"),
        ("", "empty text"),
        ("Review invoice INV-1 against its PO line and give a short reason.",
         "invoice with no supplier or PO"),
        ("Review supplier Supplier_7 and PO PO-000498; decide whether to flag it.",
         "supplier and PO with no invoice"),
    ):
        malformed = ask(client, "", text=text)
        check_eq(f"malformed ({label}): HTTP 422", malformed.status_code, 422)
        try:
            malformed_body = malformed.json()
        except ValueError:
            check(f"malformed ({label}): body is JSON", False)
            continue
        check_eq(f"malformed ({label}): error code",
                 malformed_body.get("error"), "malformed_request")
        check(f"malformed ({label}): no status acknowledgement",
              "status" not in malformed_body)

    # (4) genuine invoice traffic is never intercepted
    expect_verdict(client, "probe guard leaves a real invoice alone", "FLAT-CLEAN", None,
                   flagged=False)
    expect_verdict(client, "probe guard leaves a flagged invoice alone", "FLAT-PRICE",
                   "PRICE_DELTA", fragments=("$700.00",))

    # The probe wording paired with domain content is not a probe: it goes through normal
    # invoice processing and gets a verdict, not an acknowledgement.
    combined = ask(client, "", text=PROBE + ". " + sentence("FLAT-PRICE", "Supplier_7",
                                                            "PO-000498"))
    check_eq("probe wording plus a real invoice: HTTP 200", combined.status_code, 200)
    combined_content = combined.json()["choices"][0]["message"]["content"]
    combined_data = json.loads(combined_content)
    check_eq("probe wording plus a real invoice is reviewed normally",
             combined_data.get("fault_type"), "PRICE_DELTA")
    check("probe wording plus a real invoice returns no acknowledgement",
          "status" not in combined_data)

    # Probe wording plus identifiers too incomplete to review is still a 422, not an
    # acknowledgement and not a verdict.
    partial = ask(client, "", text=PROBE + " for invoice FLAT-CLEAN")
    check_eq("probe wording plus a partial invoice: HTTP 422", partial.status_code, 422)
    check_eq("probe wording plus a partial invoice: error code",
             partial.json().get("error"), "malformed_request")


def test_transport_probe_needs_no_database() -> None:
    """The handshake checks transport, so it must answer before the database is reachable
    -- the deployer runs it against a service whose database may not yet be routable."""
    section("6.19b the probe does not depend on the database")
    client = make_client(None)

    response = client.post("/respond", json={"messages": [{"role": "user", "content": PROBE}]})
    check_eq("probe with no DATABASE_URL: HTTP 200", response.status_code, 200)
    content = response.json()["choices"][0]["message"]["content"]
    check_eq("probe with no DATABASE_URL: acknowledgement",
             json.loads(content).get("status"), "ok")

    # A real request in the same state still reports the infrastructure fault.
    real = ask(client, "FLAT-CLEAN")
    check_eq("real request with no DATABASE_URL: HTTP 503", real.status_code, 503)
    check_eq("real request with no DATABASE_URL: error code",
             real.json().get("error"), "database_unavailable")

PURE_TESTS = [
    test_one_case_per_fault_type,
    test_clean_invoice,
    test_order_precedence,
    test_check_major_across_lines,
    test_boundaries,
    test_signed_price,
    test_fee_isolation,
    test_allowed_items,
]

WIRED_TESTS = [
    test_end_to_end_verdicts,
    test_gates_end_to_end,
    test_no_lower_priority_leakage,
    test_duplicate_short_circuits_gates,
    test_gates_above_duplicate,
    test_health_is_db_independent,
    test_renamed_schema,
    test_json_embedded_details,
    test_numeric_columns,
    test_flat_schema_resolution,
    test_flat_schema_verdicts,
    test_flat_schema_gates,
    test_jsonb_and_numeric_native_types,
    test_supplier_identified_by_either_column,
    test_transport_probe,
    test_transport_probe_needs_no_database,
]


def main() -> int:
    print("=" * 74)
    print("Part 1 -- rule engine (pure logic, no database)")
    print("=" * 74)
    for test in PURE_TESTS:
        test()

    print("\n" + "=" * 74)
    print("Part 2 -- loaders and pipeline over a SQLite fixture")
    print("=" * 74)
    for test in WIRED_TESTS:
        test()

    print("\n" + "=" * 74)
    if _FAILURES:
        print(f"FAILED  {_PASSES} passed, {len(_FAILURES)} failed")
        for failure in _FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"PASSED  {_PASSES} assertions, 0 failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
