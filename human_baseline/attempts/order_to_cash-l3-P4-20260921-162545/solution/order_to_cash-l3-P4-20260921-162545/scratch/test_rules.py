"""Exercise the fault ladder on fabricated records: the four fault types absent from the build set,
plus precedence, threshold boundaries and request parsing."""
from decimal import Decimal as D
from server import review, extract_invoice_number, parse_allowed_fees

def row(**over):
    base = dict(invoice_number="INV-9001", supplier_id=7, supplier_name="Supplier_7",
                po_number="PO-000498", item_id=1006, item_number="ITEM-006",
                invoiced_qty=D("100"), unit_price=D("10.00"), amount=D("1000.00"),
                extra_fees=[], duplicate_of=None, matched_po_line_id=994,
                po_qty=D("100"), po_unit_price=D("10.00"),
                contract_id=18, agreed_unit_price=D("411.01"),
                allowed_fees=["fuel surcharge", "handling fee"])
    base.update(over); return base

fail = []
def check(name, r, want_flag, want_type, reason_must_contain=()):
    flagged, ft, reason = review(r)
    bad = []
    if flagged != want_flag: bad.append(f"flagged={flagged} want {want_flag}")
    if ft != want_type: bad.append(f"fault_type={ft} want {want_type}")
    for frag in reason_must_contain:
        if frag.lower() not in reason.lower(): bad.append(f"reason missing {frag!r}")
    print(("  ok  " if not bad else "  FAIL") + f" {name}" + ("" if not bad else f"  <- {bad}"))
    if bad: fail.append(name)

print("-- the four fault types the build set never produces")
check("DUPLICATE", row(duplicate_of="INV-1877"), True, "DUPLICATE", ["INV-1877"])
check("TOTAL_MATH over",  row(amount=D("1600.01")), True, "TOTAL_MATH", ["600.01", "over"])
check("TOTAL_MATH under", row(amount=D("400.00")),  True, "TOTAL_MATH", ["600.00", "under"])
check("OFF_CONTRACT_ITEM", row(contract_id=None, allowed_fees=None), True, "OFF_CONTRACT_ITEM", ["ITEM-006"])
check("OFF_CONTRACT_FEE", row(extra_fees=[{"label": "admin charge", "amount": 267.96}],
                              amount=D("1267.96")), True, "OFF_CONTRACT_FEE", ["admin charge"])

print("-- precedence: first failing check wins, rest named in the reason")
check("dup beats price", row(duplicate_of="INV-1", unit_price=D("30.00"), amount=D("3000.00")),
      True, "DUPLICATE", ["INV-1", "also", "2,000.00"])
check("price beats fee", row(unit_price=D("30.00"), amount=D("3267.96"),
                             extra_fees=[{"label": "admin charge", "amount": 267.96}]),
      True, "PRICE_DELTA", ["also", "admin charge"])
# qty 140 @ 10.00 => line amount 1400; billed total 800 is a 600 gap, so both rules fire
check("qty beats total", row(invoiced_qty=D("140"), amount=D("800.00")),
      True, "QTY_MISMATCH", ["also", "total"])
check("item beats fee", row(contract_id=None, allowed_fees=None, amount=D("1267.96"),
                            extra_fees=[{"label": "admin charge", "amount": 267.96}]),
      True, "OFF_CONTRACT_ITEM")

print("-- threshold boundaries (both rules are strictly greater than $500)")
check("price delta exactly 500", row(unit_price=D("15.00"), amount=D("1500.00")), False, None)
check("price delta 500.01", row(invoiced_qty=D("100"), unit_price=D("15.0001"),
                                amount=D("1500.01")), True, "PRICE_DELTA")
check("underbill not flagged", row(unit_price=D("1.00"), amount=D("100.00")), False, None)
check("total gap exactly 500", row(amount=D("1500.00")), False, None)

print("-- clean and tolerant cases")
check("clean", row(), False, None)
check("clean with authorised fee", row(extra_fees=[{"label": "fuel surcharge", "amount": 169.47}],
                                       amount=D("1169.47")), False, None)
check("fee label case/space insensitive",
      row(extra_fees=[{"label": "  Fuel Surcharge "}], amount=D("1000.00")), False, None)
check("rounding noise ignored", row(amount=D("1002.40")), False, None)
check("missing PO line -> human look", row(matched_po_line_id=None, po_qty=None, po_unit_price=None),
      True, None, ["PO line"])

print("-- request parsing: the sentence also names a PO, which must never be taken as the invoice")
cases = [
    ("Review invoice INV-1877 (supplier Supplier_7, PO PO-000498) against its PO line.", "INV-1877"),
    ("please review inv-0042 (supplier Supplier_1, PO PO-000001) and flag if needed", "INV-0042"),
    ("PO PO-000498 invoice INV-2953 supplier Supplier_6", "INV-2953"),
]
for text, want in cases:
    got = extract_invoice_number([{"role": "user", "content": text}])
    print(("  ok  " if got == want else "  FAIL") + f" parse -> {got!r} (want {want!r})")
    if got != want: fail.append(f"parse {text[:30]}")

got = extract_invoice_number([{"role": "user", "content": "Review invoice INV-1111 (PO PO-1)."},
                              {"role": "assistant", "content": "{}"},
                              {"role": "user", "content": "Now invoice INV-2222 (PO PO-2)."}])
print(("  ok  " if got == "INV-2222" else "  FAIL") + f" newest message wins -> {got!r}")
if got != "INV-2222": fail.append("newest message")
got = extract_invoice_number([{"role": "user", "content": [{"type": "text", "text": "invoice INV-3333, PO PO-9"}]}])
print(("  ok  " if got == "INV-3333" else "  FAIL") + f" content parts -> {got!r}")
if got != "INV-3333": fail.append("content parts")
print(("  ok  " if extract_invoice_number([]) is None else "  FAIL") + " empty conversation -> None")

print(f"\n{'ALL PASS' if not fail else f'{len(fail)} FAILURES: {fail}'}")
