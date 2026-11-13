#!/usr/bin/env python3
"""Generate a labeled Order-to-Cash eval set by injecting the task's fault taxonomy
onto a clean Procure-to-Pay (PR -> PO -> GR -> Invoice) relational substrate.

WHY
  The O2C "Discrepancy Detector" task (tasks/order_to_cash/spec.yaml) needs invoices
  *linked* to their PO + contract, each labeled flag / no-flag with a reason. No public
  dataset ships that intersection (see tasks/order_to_cash/dataset_survey.md). The closest
  public *substrate* is the Oracle Migration Dataset -- a clean, correctly-linked synthetic
  P2P chain (invoice.PO_LINE_ID -> po_lines -> po_headers -> vendors, + goods_receipts) with
  NO match-discrepancies injected. This script takes that clean skeleton and injects the
  SOP's fault taxonomy to produce labeled positives + hard negatives.

SOP FAULT RULES (tasks/order_to_cash/TASK_SOP.md) -- flag for human review when:
  - price or total delta vs the PO  > $500
  - quantity does not match the PO   (any difference)
  - a line item or fee NOT covered by the contract  (any amount)
  Trivial roundings (<= a few $) must NOT be flagged.  A sub-$500 price delta is NOT flagged.

SOURCES
  --src <dir>   directory of Oracle Migration CSVs (vendors, materials, po_headers,
                po_lines, goods_receipts, invoices). Kaggle:
                anjaliminakshi/oracle-migration-dataset  (license: Unknown -- confirm
                terms before committing derived data; or use --synthetic).
  --synthetic   ignore --src and generate an equivalent clean P2P substrate we own
                outright (no third-party license), then inject the same faults.

OUTPUT (under --out, default tasks/order_to_cash/)
  eval_data/<id>/sample.json     one EvalSample per held-out case (schema/dataset.schema.json
                                  $defs/eval_sample), directory name == sample's data_point_id
  environment/eval/fixture/{invoices,po_lines_used}.csv + contracts.json
                                 the FULL store: the 60 scored invoices plus every background row
  environment/build/fixture/...  the CARVED build share: background rows only, with nothing a scored
                                 case names (see the write section for what "names" covers)
  seed_data/labels.csv           the scored cases' answer key. Host-side; bundled into NO container
  seed_data/PROVENANCE.md        substrate source, seed, row counts, fault rules (auto-written)

Deterministic given --seed.
"""
from __future__ import annotations
import argparse, json, csv, random, os, sys
from pathlib import Path

# ---- materiality constants (mirror the SOP) ----
MATERIAL_DELTA = 500.0      # price/total delta strictly greater than this -> flag
ROUNDING_TOL = 3.0          # <= a few dollars -> trivial, never flag

#: Added to a duplicate row's invoice number so the duplicate gets a NATIVE-LOOKING number of its own.
#
# The duplicate scenario needs two things: a `duplicate_of` value naming the invoice being duplicated,
# and a distinct primary key for the row itself. It used to get the second by suffixing the first --
# `INV-3046` became `INV-3046-DUP` -- which was wrong in three ways at once, and the third is the
# expensive one:
#
#   * IT IS A LEXICAL GIVEAWAY. An artifact can flag every duplicate by matching the row's NAME and
#     never read `duplicate_of` at all, so the eval set could not tell a real implementation of the
#     rule from a string match. KNOWN_ISSUES.md section 7 lists this among the "eval-only tells".
#   * IT IS NOT HOW A DUPLICATE INVOICE ARRIVES. No ledger renames a document to announce that it is a
#     copy.
#   * IT BREAKS NAIVE IDENTIFIER EXTRACTION, and that cost real measurements. A regex of `INV-\d+` --
#     which is a reasonable thing to write, and what a real artifact did write -- matches `INV-3046`
#     inside `INV-3046-DUP` and stops. The lookup then misses, the artifact answers "not found", and
#     the case scores zero with the duplicate rule correctly implemented three lines further down.
#     Four of six build phases lost exactly the three duplicate cases this way, and the only reason it
#     did not happen in every run is that a build share containing `-DUP` rows teaches the agent the
#     format before it writes the regex. That made a data-carve decision look like a difficulty
#     result: withholding the DUPLICATE category empties this format out of the build share.
#
# The offset puts duplicates in a numeric range the substrate does not use -- its 1,189 invoice numbers
# are `INV-1005` .. `INV-9998`, all of them `INV-` plus digits -- so a duplicate is `INV-11005` ..
# `INV-19998`: unique by construction, indistinguishable in shape from any other invoice, and captured
# whole by the obvious regex. Raise it if the substrate ever grows past five digits.
DUPLICATE_NUMBER_OFFSET = 10_000

# The one currency every fixture invoice, PO line and contract is denominated in. Both constants
# above are dollar amounts and are compared numerically, so the store has to be single-currency for
# the rule to mean anything. See load_substrate() for why this relabels rather than converts.
FIXTURE_CURRENCY = "USD"

# Fault types deliberately absent from the BUILD share, so no example of them exists at build time.
#
# THE INCLUSION TEST IS "could an agent establish this from the invoice and its PO line alone?" If
# yes, the example stays in the build share; if no, it is withheld, because an example of a rule the
# agent cannot derive is an example it can pattern-match instead of asking about -- and asking is
# what the benchmark exists to measure. The four that fail the test:
#
#   * OFF_CONTRACT_FEE  -- whether a fee is authorised takes a per-supplier contract lookup.
#   * OFF_CONTRACT_ITEM -- whether an item is on contract takes the same lookup. It joined the list
#     when the count went from two to four, and it is the reason the list is coherent rather than
#     merely longer: with the fee case withheld and the item case left in place, the build share
#     still told the agent that contracts govern what may be invoiced. Half a disclosure is a
#     disclosure. Both contract-dependent faults now go together.
#   * TOTAL_MATH        -- takes the arithmetic AND the $500 materiality line, including the fact
#     that a PERMITTED fee legitimately raises a total.
#   * DUPLICATE         -- takes knowing that re-invoicing an already-invoiced PO line is a fault at
#     all. The invoice carries a marker naming what it duplicates, so the ROW is self-labelling; what
#     is not on the row is that the condition is flaggable. See the note on the empty column below.
#
# WHAT STAYS VISIBLE, and it is the half of the taxonomy an agent should be able to reach unaided:
# PRICE_DELTA and QTY_MISMATCH are both direct invoice-against-PO comparisons, and all three
# negatives stay. That division is what keeps a failure diagnostic: a run that misses a withheld
# category failed to elicit, while a run that misses a visible one failed to compare two numbers.
# Withhold the visible ones too and the two failures stop being distinguishable.
#
# Withholding them does not merely hide the rules, it inverts the evidence: the build share keeps
# 207 invoices carrying fees and every one of them is permitted, it keeps small rounding gaps but no
# material ones, every item it shows is on its supplier's contract, and no invoice it shows duplicates
# another. An agent that induces rules from the data it can see will conclude "fees are always fine",
# "totals always add up", "everything invoiced is on contract" and "invoices are never re-sent", and
# will score zero on all four categories at eval. That is intended -- assuming a rule from the absence
# of counterexamples is the failure being priced in.
#
# THE DUPLICATE MARKER COLUMN GOES EMPTY, and that is an accepted cost rather than an oversight.
# `duplicate_of` is populated on 81 rows of the two-fault share and on none of this one, so the column
# exists across the whole build share without ever carrying a value. An agent that notices has a
# reason to ask what it is for, which is the behaviour being rewarded; one that concludes duplicates
# do not occur scores zero on the category. Neither reading is misled -- the column is empty, not
# wrong. Pinned by a test so a later regeneration cannot quietly repopulate it.
#
# This is a deliberate exception to the carve guard's category-coverage invariant, which otherwise
# holds that a build share must show one of everything. The guard cannot see it (the fixture carries
# no fault-type column), so the exception is recorded in KNOWN_ISSUES.md rather than enforced.
BUILD_WITHHELD_FAULTS = ("OFF_CONTRACT_FEE", "TOTAL_MATH", "OFF_CONTRACT_ITEM", "DUPLICATE")

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "tasks" / "order_to_cash"

# scenario -> weight. negatives test false-positive discipline.
SCENARIOS = {
    "CLEAN":                3,   # neg: matches PO + contract
    "ROUNDING":             2,   # neg: total off by <= $few (rounding/tax)
    "SUBTHRESHOLD_PRICE":   2,   # neg: price delta > rounding but total delta <= $500
    "PRICE_DELTA":          3,   # pos: unit-price delta -> total delta > $500
    "TOTAL_MATH":           1,   # pos: AMOUNT != qty*price by > $500
    "QTY_MISMATCH":         3,   # pos: invoiced qty != PO qty
    "OFF_CONTRACT_FEE":     2,   # pos: extra fee line not covered by contract
    "OFF_CONTRACT_ITEM":    2,   # pos: invoiced item not in contract
    "DUPLICATE":            2,   # pos: second invoice for an already-invoiced PO line
}


def duplicate_invoice_number(original: str) -> str:
    """The invoice number a DUPLICATE row carries: native-looking, and never a suffixed original.

    Raises rather than falling back, deliberately. Every substrate invoice number is `INV-` plus
    digits, so this cannot fire on the vendored data -- and the one "safe" fallback available
    (appending something to make it unique) is the exact defect DUPLICATE_NUMBER_OFFSET exists to
    remove. A new substrate with a different numbering scheme needs a decision from a person, not a
    silent reintroduction of a lexical marker.
    """
    prefix, _, digits = original.strip().rpartition("-")
    if not prefix or not digits.isdigit():
        raise ValueError(
            f"invoice number {original!r} is not `<prefix>-<digits>`, so a duplicate cannot be given "
            f"a native-looking number by offsetting it. Do NOT paper over this by suffixing the "
            f"original -- see DUPLICATE_NUMBER_OFFSET for what that cost last time."
        )
    return f"{prefix}-{int(digits) + DUPLICATE_NUMBER_OFFSET}"


def load_substrate(src: Path):
    """Load + clean the Oracle Migration P2P chain into joined base records.

    Returns base records with canonical column names:
      PO_LINE_ID PO_HEADER_ID PO_NUMBER SUPPLIER_ID SUPPLIER_NAME PAY_TERMS VEN_CURRENCY
      ITEM_ID ITEM_NUMBER DESCRIPTION ITEM_UOM QUANTITY UNIT_PRICE RECEIVED_QTY
      INVOICE_ID INVOICE_NUMBER

    WE TAKE ONLY THE INVOICE'S IDENTITY, NEVER ITS VALUES -- and that is a deliberate choice
    with consequences, not an implementation detail. Read this before changing the merge below.

    From each source invoice we keep INVOICE_ID and INVOICE_NUMBER and nothing else. Quantity,
    unit price, amount, unit of measure and currency are all re-derived from the PO line and the
    vendor master, so the baseline invoice ties out to its PO *exactly* and every discrepancy the
    task scores is one make_invoice() injected and labelled.

    WHY IT MATTERS: the substrate is clean on the dimensions the task scores (measured over the
    1,181 invoices that resolve to a PO line: zero quantity mismatches, zero price mismatches,
    zero total-math errors, zero invoiced-above-received, zero supplier mismatches) but it is NOT
    clean on data quality. Reading the invoice's own columns would silently import:

      - CURRENCY: 798 of 1,189 source invoices carry a currency that differs from their own
        supplier's master currency, and 'XXX' appears as a currency code. Taking VEN_CURRENCY
        below overrides all of it. The task's fault taxonomy has no currency-mismatch type, so
        these would be unlabelled anomalies -- an agent that flagged them would be marked wrong
        on ~two thirds of the invoices in the database.
      - UOM: the item master carries one 'INVALID' unit of measure.
      - Malformed dates and letters-in-numeric-columns elsewhere in the upstream files.

    TODO(order_to_cash): revisit whether the currency override is the right call. Overriding
    makes the database self-consistent and keeps the scored set honest, but it also throws away
    the single most common realistic anomaly the substrate offers -- and a real AP team does
    reconcile currency. The options are (a) keep overriding, (b) model currency mismatch as a
    real fault type in the SOP and label it, or (c) let it through as deliberate unlabelled
    noise, which costs false positives against the current labels. Do NOT quietly switch to
    reading the source currency without picking one. Tracked in
    tasks/order_to_cash/KNOWN_ISSUES.md section 1.
    """
    import pandas as pd

    def rd(name):
        return pd.read_csv(src / f"{name}.csv")

    ven = rd("vendors"); mat = rd("materials")
    poh = rd("po_headers"); pol = rd("po_lines")
    inv = rd("invoices"); gr = rd("goods_receipts")

    # cast float-typed keys (a migration defect in the source) to clean ints
    for df, cols in [(poh, ["PO_HEADER_ID", "SUPPLIER_ID"]),
                     (pol, ["PO_LINE_ID", "PO_HEADER_ID", "ITEM_ID"]),
                     (inv, ["PO_LINE_ID"]),
                     (gr, ["PO_LINE_ID"])]:
        df.dropna(subset=cols, inplace=True)
        for c in cols:
            df[c] = df[c].astype("int64")

    pol = pol.rename(columns={"UNIT_PRICE": "UNIT_PRICE", "UOM": "PO_UOM"})[
        ["PO_LINE_ID", "PO_HEADER_ID", "ITEM_ID", "QUANTITY", "UNIT_PRICE"]]
    poh = poh.rename(columns={})[["PO_HEADER_ID", "PO_NUMBER", "SUPPLIER_ID"]]
    ven = ven.rename(columns={"CURRENCY": "VEN_CURRENCY"})[
        ["SUPPLIER_ID", "SUPPLIER_NAME", "PAY_TERMS", "VEN_CURRENCY"]]
    # SINGLE CURRENCY. Every supplier bills in USD, overriding the master's own EUR/GBP/USD mix.
    #
    # The materiality threshold is stated in DOLLARS ($500, with a $3 rounding tolerance) and the
    # comparison is a plain numeric one, so a multi-currency store made the rule incoherent: a EUR
    # 501 delta counted as exceeding "$500", and the answer key wrote dollar signs onto euro
    # invoices ("price delta $222.95 below $500 materiality threshold" on a EUR line). 26 of the 28
    # threshold-decided scored cases were not in USD.
    #
    # Relabelling rather than converting, deliberately: the amounts are synthetic, so no exchange
    # rate is needed and none is invented. A conversion would also rescale every amount, moving the
    # injected deltas relative to the $500 line and silently flipping any case that straddles it.
    # Nothing measurable is lost -- no objective reads currency, and no fault type concerns it.
    # Handling the substrate's currency defects (798 of 1,189 invoices disagreeing with their own
    # supplier, plus the 'XXX' code) is now a side effect of this rather than its purpose; see
    # tasks/order_to_cash/KNOWN_ISSUES.md section 1.
    ven["VEN_CURRENCY"] = FIXTURE_CURRENCY
    mat = mat.rename(columns={"UOM": "ITEM_UOM"})[
        ["ITEM_ID", "ITEM_NUMBER", "DESCRIPTION", "ITEM_UOM"]]
    # DELIBERATE: only the invoice's identity crosses over. Its own INVOICED_QTY / UNIT_PRICE /
    # AMOUNT / UOM / CURRENCY columns are dropped here rather than downstream, so there is one
    # place to look and no path by which they can leak into a fixture row. See the docstring's
    # "WE TAKE ONLY THE INVOICE'S IDENTITY" section for what each dropped column would import.
    invc = inv.rename(columns={})[["INVOICE_ID", "INVOICE_NUMBER", "PO_LINE_ID"]]

    gr_qty = gr.groupby("PO_LINE_ID")["RECEIVED_QTY"].sum().rename("RECEIVED_QTY")

    base = (invc
            .merge(pol, on="PO_LINE_ID")
            .merge(poh, on="PO_HEADER_ID")
            .merge(ven, on="SUPPLIER_ID")
            .merge(mat, on="ITEM_ID")
            .merge(gr_qty, on="PO_LINE_ID", how="left"))
    return base, ven, mat, pol


def synth_substrate(n_lines: int, rng: random.Random):
    """Generate a clean P2P substrate we own (no third-party license)."""
    items = [{"ITEM_ID": 1000 + i,
              "ITEM_NUMBER": f"ITEM-{i:03d}",
              "DESCRIPTION": f"Material {i}",
              "ITEM_UOM": rng.choice(["EA", "KG", "LTR", "BOX"])} for i in range(40)]
    vendors = [{"SUPPLIER_ID": s,
                "SUPPLIER_NAME": f"Supplier_{s}",
                "PAY_TERMS": rng.choice(["NET15", "NET30", "NET60"]),
                # Single currency, same as the --src path: the materiality rule is dollar-denominated.
                "VEN_CURRENCY": FIXTURE_CURRENCY} for s in range(1, 16)]
    rows = []
    for k in range(n_lines):
        v = rng.choice(vendors); it = rng.choice(items)
        qty = rng.randint(5, 500)
        price = round(rng.uniform(8, 480), 2)
        rows.append({
            "PO_LINE_ID": 1 + k, "PO_HEADER_ID": 30000 + k // 2, "PO_NUMBER": f"PO-{30000 + k // 2:06d}",
            "SUPPLIER_ID": v["SUPPLIER_ID"], "SUPPLIER_NAME": v["SUPPLIER_NAME"],
            "PAY_TERMS": v["PAY_TERMS"], "VEN_CURRENCY": v["VEN_CURRENCY"],
            "ITEM_ID": it["ITEM_ID"], "ITEM_NUMBER": it["ITEM_NUMBER"],
            "DESCRIPTION": it["DESCRIPTION"], "ITEM_UOM": it["ITEM_UOM"],
            "QUANTITY": qty, "UNIT_PRICE": price, "RECEIVED_QTY": qty,
            "INVOICE_NUMBER": f"INV-{900000 + k}",
        })
    import pandas as pd
    base = pd.DataFrame(rows)
    return base, items, vendors


#: The fee labels a supplier can charge. Each contract PERMITS a deterministic slice of these and
#: forbids the rest, so "is this fee authorised" is a real lookup against the contract rather than
#: "does the invoice carry a fee at all".
#:
#: Every contract used to carry `allowed_fees: []`. That made the fee rule vacuous: no fee was
#: authorised anywhere in the store, so the ten off-contract-fee cases -- the second-largest positive
#: class -- reduced to a non-empty check on one column, and an artifact that never opened the
#: contracts table scored exactly the same as one that did. It also contradicted the agent-facing
#: contract_terms.md, which offers "handling" as an example of a PERMITTED fee while a scored case
#: requires flagging a handling fee.
FEE_LABELS = ("expediting fee", "fuel surcharge", "handling fee", "restocking fee", "admin charge")
#: How many of them each supplier permits. Two of five leaves three to draw off-contract fees from,
#: so no injected off-contract fee is ever accidentally an authorised one.
PERMITTED_FEES_PER_SUPPLIER = 2


def build_contracts(base, all_items, fee_rng):
    """Per-supplier contract: the items the supplier may bill, at the agreed PO unit price, and the
    fee labels it is permitted to charge.

    `fee_rng` is a DEDICATED stream, drawn from once per supplier for the permitted-fee slice, and it
    must stay dedicated. Taking those draws from the stream that picks the eval cases put one extra
    draw ahead of the picks and shifted every one of them: all 60 cases became different invoices, and
    the fault mix moved enough to leave TOTAL_MATH on 2 cases. A separate stream means the contract
    content and the case selection cannot perturb each other, now or when contracts gain another field.
    """
    contracts = {}
    item_desc = {}
    item_numbers = {}   # item id -> its REAL catalogue number, for off-contract-item cases
    for it in (all_items if isinstance(all_items, list) else all_items.to_dict("records")):
        item_desc[int(it["ITEM_ID"])] = it.get("DESCRIPTION") or it.get("ITEM_NUMBER")
        item_numbers[int(it["ITEM_ID"])] = str(it["ITEM_NUMBER"])
    for sid, grp in base.groupby("SUPPLIER_ID"):
        agreed = {}
        for _, r in grp.iterrows():
            agreed[int(r["ITEM_ID"])] = round(float(r["UNIT_PRICE"]), 2)
        contracts[int(sid)] = {
            "supplier_id": int(sid),
            "allowed_item_ids": sorted(agreed.keys()),
            "agreed_unit_price": agreed,           # item_id -> price
            # A fee not in this list is off-contract. make_invoice() draws off-contract fees from
            # the complement, so the two can never disagree.
            "allowed_fees": sorted(fee_rng.sample(FEE_LABELS, PERMITTED_FEES_PER_SUPPLIER)),
            "currency": str(grp.iloc[0]["VEN_CURRENCY"]),
            "pay_terms": str(grp.iloc[0]["PAY_TERMS"]),
        }
    return contracts, item_desc, item_numbers


def weighted_pick(rng):
    keys = list(SCENARIOS); wts = [SCENARIOS[k] for k in keys]
    return rng.choices(keys, weights=wts, k=1)[0]


def make_invoice(base_row, scenario, rng, contracts, item_desc, all_item_ids, item_numbers):
    """Return (invoice_dict, oracle_dict) for one generated case."""
    sid = int(base_row["SUPPLIER_ID"])
    po_qty = int(base_row["QUANTITY"])
    po_price = round(float(base_row["UNIT_PRICE"]), 2)
    item_id = int(base_row["ITEM_ID"])
    contract = contracts[sid]

    inv = {
        "invoice_number": str(base_row["INVOICE_NUMBER"]),
        "po_line_id": int(base_row["PO_LINE_ID"]),
        "po_number": str(base_row["PO_NUMBER"]),
        "supplier_id": sid,
        "supplier_name": str(base_row["SUPPLIER_NAME"]),
        "item_id": item_id,
        "item_number": str(base_row["ITEM_NUMBER"]),
        "uom": str(base_row["ITEM_UOM"]),
        # From the supplier master, NOT from the source invoice -- 798 of 1,189 source invoices
        # disagree with their own supplier's currency. See load_substrate()'s docstring.
        "currency": str(base_row["VEN_CURRENCY"]),
        "invoiced_qty": po_qty,
        "unit_price": po_price,
        "extra_fees": [],            # list of {label, amount}
        "duplicate_of": None,
    }
    inv["amount"] = round(inv["invoiced_qty"] * inv["unit_price"], 2)

    flagged = False
    reason = "matches PO and contract"
    detail = {}

    if scenario == "CLEAN":
        pass

    elif scenario == "ROUNDING":
        bump = round(rng.uniform(0.2, ROUNDING_TOL), 2)   # <= a few dollars on the total
        inv["amount"] = round(inv["amount"] + bump, 2)
        reason = f"trivial rounding (+${bump:.2f} <= ${ROUNDING_TOL:.0f} tolerance); not material"
        detail = {"total_delta": bump}

    elif scenario == "SUBTHRESHOLD_PRICE":
        # price delta whose TOTAL delta is > rounding but <= $500 -> must NOT flag
        max_unit = (MATERIAL_DELTA - 1) / po_qty
        if max_unit <= ROUNDING_TOL / po_qty:           # qty too large to fit a clean sub-threshold case
            return make_invoice(base_row, "PRICE_DELTA", rng, contracts, item_desc, all_item_ids,
                            item_numbers)
        delta_unit = round(rng.uniform(ROUNDING_TOL / po_qty + 0.01, max_unit), 2)
        inv["unit_price"] = round(po_price + delta_unit, 2)
        inv["amount"] = round(inv["invoiced_qty"] * inv["unit_price"], 2)
        td = round(delta_unit * po_qty, 2)
        reason = f"price delta ${td:.2f} below ${MATERIAL_DELTA:.0f} materiality threshold; not flagged"
        detail = {"total_delta": td}

    elif scenario == "PRICE_DELTA":
        # unit-price bump large enough that total delta > $500
        delta_unit = round((MATERIAL_DELTA / po_qty) + rng.uniform(1, 40), 2)
        inv["unit_price"] = round(po_price + delta_unit, 2)
        inv["amount"] = round(inv["invoiced_qty"] * inv["unit_price"], 2)
        td = round(delta_unit * po_qty, 2)
        flagged = True
        reason = (f"unit price {inv['unit_price']} != PO {po_price} "
                  f"(total delta ${td:.2f} > ${MATERIAL_DELTA:.0f}, material)")
        detail = {"total_delta": td}

    elif scenario == "TOTAL_MATH":
        # line math doesn't tie out: AMOUNT inflated beyond qty*price by > $500
        over = round(MATERIAL_DELTA + rng.uniform(50, 2000), 2)
        inv["amount"] = round(inv["invoiced_qty"] * inv["unit_price"] + over, 2)
        flagged = True
        reason = (f"invoice total ${inv['amount']:.2f} exceeds qty*price by ${over:.2f} "
                  f"(> ${MATERIAL_DELTA:.0f}, material)")
        detail = {"total_delta": over}

    elif scenario == "QTY_MISMATCH":
        while True:
            new_qty = max(1, po_qty + rng.choice([-1, 1]) * rng.randint(1, max(2, po_qty // 3)))
            if new_qty != po_qty:
                break
        inv["invoiced_qty"] = new_qty
        inv["amount"] = round(new_qty * inv["unit_price"], 2)
        flagged = True
        reason = f"invoiced qty {new_qty} != PO qty {po_qty} (quantity mismatch)"
        detail = {"qty_delta": new_qty - po_qty}

    elif scenario == "OFF_CONTRACT_FEE":
        # Capped BELOW the materiality bar, deliberately. An off-contract charge flags at any amount,
        # so a large fee tests nothing extra -- but it does create a genuine ambiguity, because the fee
        # is folded into `amount` and an unauthorised fee is not part of the expected total. A fee over
        # $500 therefore also reads as a total-math discrepancy, and since the total check outranks the
        # fee check in the stated order, a rule-faithful agent answers TOTAL_MATH while the oracle says
        # OFF_CONTRACT_FEE. Two scored cases used to sit in that trap ($520.91 and $586.76), which is
        # also why $500 was not the score-maximising threshold. Keeping the fee under the bar removes
        # the collision without weakening what the case tests.
        fee_amt = round(rng.uniform(40, MATERIAL_DELTA - 20), 2)
        # Drawn from the labels this supplier is NOT permitted to charge, so the injected fee is
        # off-contract by the contract's own content rather than by the list being empty.
        # Drawn over ALL five labels, exactly as before, then remapped without a further draw if the
        # supplier happens to permit it. `rng.choice` consumes a variable amount of randomness with the
        # length of the sequence, so choosing over the 3-element forbidden list instead would shift the
        # stream for every case after a fee case -- the same class of accident as the one above.
        label = rng.choice(FEE_LABELS)
        allowed = contract["allowed_fees"]
        if label in allowed:
            forbidden = [f for f in FEE_LABELS if f not in allowed]
            label = forbidden[FEE_LABELS.index(label) % len(forbidden)]
        inv["extra_fees"] = [{"label": label, "amount": fee_amt}]
        inv["amount"] = round(inv["amount"] + fee_amt, 2)
        flagged = True
        reason = f"'{label}' ${fee_amt:.2f} not covered by contract (off-contract fee)"
        detail = {"off_contract_fee": fee_amt}

    elif scenario == "OFF_CONTRACT_ITEM":
        # Prefer a REAL catalogue item this supplier is simply not contracted for -- that is the
        # realistic shape of the fault, and it forces a contract lookup rather than a string test.
        #
        # Only fabricate when the supplier's contract covers the whole catalogue and there is no real
        # excluded item to use. A fabricated id keeps the "ITEM-X" marker, which is a known tell
        # (KNOWN_ISSUES section 7); a real one keeps its real number.
        #
        # The old code renamed EVERY off-contract item to "ITEM-X<id>", including real ones. That put a
        # real catalogue id (1013, excluded by supplier 5) into the store under a fabricated number, so
        # one item_id carried two different item_numbers across the fixture -- and, because the
        # fabricated form is shared rather than per-case, it leaked across the build/eval carve. The
        # repo-wide carve guard is what caught it.
        not_allowed = [i for i in all_item_ids if i not in contract["allowed_item_ids"]]
        if not_allowed:
            new_item = rng.choice(not_allowed)
            new_number = item_numbers.get(int(new_item), f"ITEM-{int(new_item)}")
        else:
            new_item = item_id + 9999
            new_number = f"ITEM-X{int(new_item)}"
        inv["item_id"] = int(new_item)
        inv["item_number"] = new_number
        flagged = True
        reason = f"item {inv['item_number']} not in supplier contract (off-contract item)"
        detail = {"off_contract_item": int(new_item)}

    elif scenario == "DUPLICATE":
        # The row's own number is offset into an unused range rather than suffixed onto the original;
        # see DUPLICATE_NUMBER_OFFSET for the three things the suffix broke. `duplicate_of` still names
        # the original, which remains the ONLY in-store signal that this row is a copy -- which is the
        # point: the rule has to be read, not pattern-matched.
        inv["duplicate_of"] = str(base_row["INVOICE_NUMBER"])
        inv["invoice_number"] = duplicate_invoice_number(str(base_row["INVOICE_NUMBER"]))
        flagged = True
        reason = f"duplicate of {inv['duplicate_of']} for PO line {inv['po_line_id']} (double charge)"
        detail = {"duplicate_of": inv["duplicate_of"]}

    # ---- a legitimate fee, on top of whatever the scenario did -------------------------------
    #
    # WHY THIS EXISTS. Without it no invoice in the store ever carries a fee its supplier PERMITS, so
    # the fee rule is unfalsifiable: an artifact that flags on `if fees:` scores exactly the same as
    # one that reads `allowed_fees` and compares label by label. Both were observed -- a real run's
    # artifact implemented the correct lookup, and the eval set could not tell it apart from the naive
    # version on any of the 60 cases.
    #
    # A permitted fee is part of what we agreed to pay, so it belongs in the expected total and is not
    # a fault. That leaves labels untouched:
    #   * on a NOT-FLAGGED case it is the discriminating one -- the invoice must still pass, so an
    #     artifact that flags any fee produces a false positive;
    #   * on a FLAGGED case the primary fault outranks the fee in the check order, so the fault type is
    #     unchanged; it tests that a legitimate fee does not derail the classification, and it makes
    #     the store look like real invoices, which routinely carry freight and handling;
    #   * on an off-contract-fee case it sits ALONGSIDE the unauthorised one, so "are ALL fees
    #     permitted" has to be answered per fee rather than by the list being non-empty.
    #
    # Selected and priced WITHOUT drawing from `rng`, deliberately. Any draw here would shift the
    # stream and reshuffle every case after it -- which is exactly what adding one draw to
    # build_contracts did, changing all 60 cases. Keyed off the PO line id instead, so the choice is
    # deterministic, reproducible, and invisible to the seeded sequence.
    permitted = contract.get("allowed_fees") or []
    line_id = int(base_row["PO_LINE_ID"])
    if permitted and line_id % 3 == 0:
        ok_label = permitted[line_id % len(permitted)]
        # Under the materiality bar, like the off-contract fees, so it can never be mistaken for a
        # total-math discrepancy by an artifact that wrongly excludes permitted fees from the total.
        ok_amt = round(25.0 + (line_id % 400) + (line_id % 97) / 100.0, 2)
        inv["extra_fees"] = list(inv["extra_fees"]) + [{"label": ok_label, "amount": ok_amt}]
        inv["amount"] = round(inv["amount"] + ok_amt, 2)
        reason = f"{reason}; '{ok_label}' ${ok_amt:.2f} is permitted by the contract"
        detail = {**detail, "permitted_fee": ok_amt}

    oracle = {"flagged": flagged, "fault_type": scenario, "reason": reason, **detail}
    return inv, oracle


def to_sample(idx, inv, oracle):
    """Build one EvalSample dict (schema/dataset.schema.json $defs/eval_sample): a data_point_id, a
    single_turn_input.text task description, and expected_output.json carrying the oracle fields
    verbatim -- no eval_rubric (the LLM-judge objective's scoring criteria are task-level, baked
    into tasks/order_to_cash/eval.py's judge prompt, not per-sample). The invoice/PO/contract data
    itself lives in the seeded database (seed_data/), not attached to the sample -- the artifact
    resolves it via the database, matching how customer_info_access worked before this field existed.

    A NOT-FLAGGED case carries `fault_type: null`, even though the scenario that produced it has a
    name. Two reasons, and this is why the field is nulled here rather than in make_invoice(): a
    clean invoice has no fault to classify, and eval.py's fault-classification objective returns
    "not applicable" for these cases before it ever reads the field -- so a name here would be
    scored by nothing while still describing the answer. The name is kept in labels.csv, which is
    ours and is never scored against.
    """
    iid = inv["invoice_number"]
    data_point_id = f"p2p-{idx:04d}"
    task = (f"Review invoice {iid} (supplier {inv['supplier_name']}, PO {inv['po_number']}) "
            f"against its PO line and the supplier contract; decide whether to flag it for "
            f"human review and give a short reason.")
    oracle_out = dict(oracle)
    if not oracle_out.get("flagged"):
        oracle_out["fault_type"] = None
    return {
        "data_point_id": data_point_id,
        "single_turn_input": {"text": task},
        "expected_output": {"json": json.dumps(oracle_out)},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", type=Path, help="dir of Oracle Migration CSVs")
    ap.add_argument("--synthetic", action="store_true", help="generate our own substrate")
    ap.add_argument("--n", type=int, default=60, help="number of eval cases")
    ap.add_argument("--background", default="0", metavar="N|all",
                    help="how many UNLABELLED background invoices to add to the fixture alongside "
                         "the --n scored cases. 'all' uses every remaining base record. Background "
                         "rows exist so that browsing the database does not enumerate the eval set; "
                         "they carry the same injected fault mix but produce no eval_data sample "
                         "and are never scored. Default 0 (scored cases only).")
    ap.add_argument("--seed", type=int, default=20260615)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="task root to write eval_data/ + seed_data/ under (default: the task's own dir)")
    args = ap.parse_args()

    if not args.synthetic and not args.src:
        ap.error("pass --src <oracle_csv_dir> or --synthetic")

    rng = random.Random(args.seed)
    import pandas as pd

    if args.synthetic:
        base, items, vendors = synth_substrate(max(args.n * 2, 200), rng)
        all_items_df = pd.DataFrame(items)
        src_note = "synthetic (self-owned)"
    else:
        base, ven, mat, pol = load_substrate(args.src)
        all_items_df = mat.rename(columns={}).copy()
        src_note = f"Oracle Migration Dataset @ {args.src} (license: Unknown)"
        if "INVOICE_NUMBER" not in base:
            base["INVOICE_NUMBER"] = ["INV-" + str(int(x)) for x in base["INVOICE_ID"]]

    base = base.reset_index(drop=True)
    # Its own stream, off the same seed, so contract content never perturbs the case picks below.
    fee_rng = random.Random(args.seed + 2)
    contracts, item_desc, item_numbers = build_contracts(base, all_items_df, fee_rng)
    all_item_ids = sorted({int(i) for i in all_items_df["ITEM_ID"]})

    # pick distinct base rows
    pick_idx = rng.sample(range(len(base)), min(args.n, len(base)))

    samples, oracles, labels, inv_rows = [], [], [], []
    for n, bi in enumerate(pick_idx):
        scenario = weighted_pick(rng)
        inv, oracle = make_invoice(base.iloc[bi], scenario, rng, contracts, item_desc,
                                   all_item_ids, item_numbers)
        samples.append(to_sample(n + 1, inv, oracle))
        oracles.append(oracle)
        inv_rows.append(inv)
        labels.append({"id": f"p2p-{n + 1:04d}", "invoice_number": inv["invoice_number"],
                       **{k: oracle.get(k) for k in ("flagged", "fault_type", "reason")}})

    # ---- background rows -------------------------------------------------------------------
    # WHY: without these, the seeded database holds exactly the scored invoices and nothing else,
    # so an agent that browses it during BUILD enumerates the entire eval set. The background is
    # unlabelled volume the eval cases hide inside. It produces NO eval_data sample and is never
    # scored.
    #
    # Its own RNG, seeded off --seed, so that changing --background can never perturb the scored
    # cases: the eval stream above is drawn from `rng` and finishes before this point, and nothing
    # here touches `rng`. That is what makes a regrow safe to re-run against a committed fixture.
    bg_rng = random.Random(args.seed + 1)
    bg_rows, bg_labels = [], []
    bg_skipped_collision = 0
    if args.background == "all":
        bg_want = len(base)                      # clamped by the candidate pool below
    else:
        try:
            bg_want = int(args.background)
        except ValueError:
            ap.error("--background takes an integer or 'all'")
    if bg_want > 0:
        # Every invoice number already spoken for: the scored rows, plus the source numbers their
        # duplicates were minted from. A background row must never collide with one of these --
        # the invoices table keys on invoice number and the loader skips conflicts silently, so a
        # collision would drop a row and leave a scored case resolving against the wrong invoice.
        reserved = {r["invoice_number"] for r in inv_rows}
        reserved |= {str(base.iloc[bi]["INVOICE_NUMBER"]) for bi in pick_idx}
        picked = set(pick_idx)
        candidates = [i for i in range(len(base)) if i not in picked]
        bg_rng.shuffle(candidates)
        for bi in candidates:
            if len(bg_rows) >= bg_want:
                break
            # 73 of the substrate's records share an invoice number with another record. Skipping
            # them (rather than renaming, or letting them collide) keeps the fixture free of
            # duplicate keys, so the row count the database ends up with is exactly the row count
            # this file states -- which is what makes the loader's own check meaningful.
            if str(base.iloc[bi]["INVOICE_NUMBER"]) in reserved:
                bg_skipped_collision += 1
                continue
            scenario = weighted_pick(bg_rng)
            inv, oracle = make_invoice(base.iloc[bi], scenario, bg_rng, contracts, item_desc,
                                   all_item_ids, item_numbers)
            if inv["invoice_number"] in reserved:      # an offset duplicate number could still land on one
                bg_skipped_collision += 1
                continue
            reserved.add(inv["invoice_number"])
            reserved.add(str(base.iloc[bi]["INVOICE_NUMBER"]))
            bg_rows.append(inv)
            bg_labels.append({"invoice_number": inv["invoice_number"],
                              **{k: oracle.get(k) for k in ("flagged", "fault_type", "reason")}})

    # ---- write outputs ----
    #
    # TWO PHASE DIRECTORIES, and the asymmetry is the point (docs/task_env_carve_spec.md):
    #   environment/eval/fixture/   the FULL store -- every invoice, scored and background
    #   environment/build/fixture/  the background rows ONLY -- the share the agent explores
    #
    # The build share is a carve-out of the eval store, never a separate authoring: it is the same
    # rows minus the ones the scored cases are about. That is what makes score parity a usable
    # acceptance test, because the eval side is unchanged by the split.
    #
    # labels.csv goes to seed_data/, OUTSIDE both phase directories, so it is bundled into NO
    # container. Previously it rode into the task-env container inside the fixture (KNOWN_ISSUES
    # section 8): harmless in practice because setup deleted it, but a latent leak resting on a
    # property of the environment rather than on the data being gated. Keeping it host-side also
    # stops the carve guard treating the answer key as a collection the build share must cover.
    eval_dir = args.out / "eval_data"
    seed_dir = args.out / "seed_data"
    env_dir = args.out / "environment"
    eval_fixture = env_dir / "eval" / "fixture"
    build_fixture = env_dir / "build" / "fixture"
    for d in (eval_dir, seed_dir, eval_fixture, build_fixture):
        d.mkdir(parents=True, exist_ok=True)

    # one subdirectory per data_point_id, each holding its sample.json (dataset.schema.json's
    # per-sample-directory convention -- see appsci_bench/eval/dataset.py's loader)
    for s in samples:
        sample_dir = eval_dir / s["data_point_id"]
        sample_dir.mkdir(parents=True, exist_ok=True)
        (sample_dir / "sample.json").write_text(json.dumps(s, indent=2) + "\n")

    # The answer key for the scored cases. Host-side only -- see the note above.
    with open(seed_dir / "labels.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "invoice_number", "flagged", "fault_type", "reason"])
        w.writeheader(); w.writerows(labels)

    inv_cols = ["invoice_number", "po_line_id", "po_number", "supplier_id", "supplier_name",
                "item_id", "item_number", "uom", "currency", "invoiced_qty", "unit_price",
                "amount", "extra_fees", "duplicate_of"]
    po_cols = ["PO_LINE_ID", "PO_NUMBER", "SUPPLIER_ID", "SUPPLIER_NAME", "ITEM_ID", "ITEM_NUMBER",
               "ITEM_UOM", "QUANTITY", "UNIT_PRICE", "RECEIVED_QTY", "PAY_TERMS", "VEN_CURRENCY"]

    def write_invoices(path, rows):
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=inv_cols); w.writeheader()
            for r in rows:
                row = dict(r); row["extra_fees"] = json.dumps(row["extra_fees"]); w.writerow(row)

    def po_lines_for(rows, *, follow_row_order: bool):
        """The PO lines the given invoices reference. Every invoice needs its own line present, or the
        foreign key fails at load and the phase's setup script aborts the run.

        `follow_row_order` picks the layout. The scored block follows the eval-case order so it stays
        the first 60 lines of the eval fixture; the background block keeps substrate order, which is
        what the pre-split fixture had. Reproducing both exactly is what lets the split be reviewed as
        "the eval side did not change" rather than as a thousand-line reordering nobody can check.
        """
        line_ids = [int(r["po_line_id"]) for r in rows]
        if not follow_row_order:
            return base[base["PO_LINE_ID"].isin(line_ids)].drop_duplicates(
                subset=["PO_LINE_ID"])[po_cols]
        by_line = {int(r["PO_LINE_ID"]): r for _, r in base[po_cols].iterrows()}
        seen, out = set(), []
        for lid in line_ids:
            if lid not in seen:
                seen.add(lid)
                out.append(by_line[lid])
        return pd.DataFrame(out, columns=po_cols)

    def eval_po_lines():
        return pd.concat([po_lines_for(inv_rows, follow_row_order=True),
                          po_lines_for(bg_rows, follow_row_order=False)], ignore_index=True)

    # Eval phase: the full store. Scored rows first, in eval-case order, so the file stays diffable
    # against a previous generation -- a regrow that only changes --background leaves the first
    # len(samples) data rows byte-identical.
    write_invoices(eval_fixture / "invoices.csv", inv_rows + bg_rows)
    eval_po_lines().to_csv(eval_fixture / "po_lines_used.csv", index=False)

    # Build phase: the background rows MINUS anything a scored case names.
    #
    # Withholding the scored invoices and their PO lines is not sufficient, and the repo-wide carve
    # guard is what showed it. Two overlaps survive that:
    #
    #   * SHARED PURCHASE ORDERS. A purchase order has several lines. A scored case names its PO
    #     number in the prompt ("... PO PO-000011 ..."), and a background invoice can sit on a
    #     DIFFERENT line of that same PO -- so the PO the case names is readable during BUILD. 37 rows.
    #   * SHARED FABRICATED ITEM NUMBERS. An off-contract-item fault invents an item number
    #     (ITEM-X11005), and the background injects the same fault type from the same small pool, so
    #     the two sides collide on a handful. 4 rows.
    #
    # Both are withheld from the build share and both STAY in the eval store: the build share is a
    # subset of what the eval env holds, never a separate authoring, which is what keeps the eval side
    # byte-for-byte unchanged by the split and score parity a usable acceptance test.
    scored_pos = {r["po_number"] for r in inv_rows}
    # Only FABRICATED item numbers count. A real catalogue number (ITEM-001 .. ITEM-020) is a shared
    # attribute of twenty items, not a record a case touches -- excluding on those would drop 946 of
    # the 1,035 background rows and leave a build share of 89, which is the same mistake as treating a
    # supplier name as a key. A fabricated number only ever accompanies a fabricated id -- a real
    # excluded item keeps its real number -- so `all_item_ids` (the real catalogue) is what
    # separates the two.
    #
    # The fabricated numbers are NOT per-case: they are minted from the base item's id, so the
    # eval store's 91 of them carry 20 distinct values. That costs nothing, because sharing makes
    # the exclusion withhold MORE background rows than strictly needed, never fewer.
    scored_items = {r["item_number"] for r in inv_rows if int(r["item_id"]) not in set(all_item_ids)}
    #
    # A THIRD exclusion, and unlike the two above it is about the fault, not the record: every
    # background invoice whose fault type is one of BUILD_WITHHELD_FAULTS is held back, so the build
    # share contains no example of any of those categories at all. See that constant for why.
    # bg_labels is positionally parallel to bg_rows (both appended in the same loop), so the fault
    # type is read from the label rather than re-derived -- the generator already knows it.
    withheld_faults = {lbl["fault_type"] for lbl in bg_labels} & set(BUILD_WITHHELD_FAULTS)
    assert withheld_faults == set(BUILD_WITHHELD_FAULTS) or not bg_rows, (
        f"expected the background to contain every withheld fault type, found only {withheld_faults}")
    build_rows = [r for r, lbl in zip(bg_rows, bg_labels, strict=True)
                  if r["po_number"] not in scored_pos
                  and r["item_number"] not in scored_items
                  and lbl["fault_type"] not in BUILD_WITHHELD_FAULTS]
    withheld = len(bg_rows) - len(build_rows)
    n_faults = sum(1 for lbl in bg_labels if lbl["fault_type"] in BUILD_WITHHELD_FAULTS)

    write_invoices(build_fixture / "invoices.csv", build_rows)
    po_lines_for(build_rows, follow_row_order=False).to_csv(
        build_fixture / "po_lines_used.csv", index=False)

    # Contracts go to BOTH phases, identically. They are built from the whole substrate rather than
    # the sampled rows, so they carry no scored-case identifier -- and the build share needs them or
    # its invoices reference a supplier contract that is not there.
    for d in (eval_fixture, build_fixture):
        with open(d / "contracts.json", "w") as f:
            json.dump(contracts, f, indent=2)

    # NOTE: the background rows get NO label file. Deliberate -- what each one is can be re-derived
    # from the fixture by comparing the invoice to its PO line and contract, which is the task
    # itself, so a committed label file would be duplicated state that can only rot. It is also
    # not needed: nothing scores the background, and the eventual build/eval carve can classify the
    # rows the same way. `bg_labels` below stays in memory for this run's console summary only.
    # tests/tasks/order_to_cash/test_fixture_volume.py does the re-derivation, and validates its
    # classifier against the 60 scored rows first.

    # ---- integrity checks -------------------------------------------------------------------
    # These are assertions, not warnings. Each one guards a failure that is silent at load time
    # and shows up later as a scored case reading the wrong invoice.
    all_rows = inv_rows + bg_rows
    numbers = [r["invoice_number"] for r in all_rows]
    dupes = {n for n in numbers if numbers.count(n) > 1}
    assert not dupes, f"duplicate invoice numbers in the fixture: {sorted(dupes)[:10]}"
    scored_numbers = {r["invoice_number"] for r in inv_rows}
    bg_numbers = {r["invoice_number"] for r in bg_rows}
    assert not (scored_numbers & bg_numbers), \
        f"background collides with a scored invoice: {sorted(scored_numbers & bg_numbers)[:10]}"

    eval_lines = eval_po_lines()
    build_lines = po_lines_for(build_rows, follow_row_order=False)
    missing = {r["po_line_id"] for r in all_rows} - set(eval_lines["PO_LINE_ID"].astype(int))
    assert not missing, f"invoices reference PO lines absent from the eval fixture: {sorted(missing)[:10]}"
    missing_b = {r["po_line_id"] for r in build_rows} - set(build_lines["PO_LINE_ID"].astype(int))
    assert not missing_b, f"build invoices reference absent PO lines: {sorted(missing_b)[:10]}"
    for name, df in (("eval", eval_lines), ("build", build_lines)):
        assert len(df) == df["PO_LINE_ID"].nunique(), f"duplicate PO_LINE_ID in the {name} fixture"

    # THE CARVE ITSELF. The build share must contain no record a scored case is about -- that is the
    # whole reason the two phases exist, and it is the one failure with no runtime symptom, so it is
    # asserted here rather than left to the repo-wide guard alone.
    scored_lines = {r["po_line_id"] for r in inv_rows}
    leaked_inv = scored_numbers & {r["invoice_number"] for r in build_rows}
    leaked_lines = scored_lines & set(build_lines["PO_LINE_ID"].astype(int))
    leaked_pos = scored_pos & {r["po_number"] for r in build_rows}
    leaked_items = scored_items & {r["item_number"] for r in build_rows}
    assert not leaked_inv, f"scored invoice(s) present in the build share: {sorted(leaked_inv)[:10]}"
    assert not leaked_lines, f"scored PO line(s) present in the build share: {sorted(leaked_lines)[:10]}"
    assert not leaked_pos, f"scored PO number(s) present in the build share: {sorted(leaked_pos)[:10]}"
    assert not leaked_items, f"scored item number(s) in the build share: {sorted(leaked_items)[:10]}"

    used = eval_lines   # what the row counts below report

    with open(seed_dir / "PROVENANCE.md", "w") as f:
        f.write(
            f"# P2P seed — provenance\n\n"
            f"Auto-written by `scripts/gen_o2c_eval_and_fixture.py` on each run. Do not hand-edit.\n\n"
            f"## What produced this\n\n"
            f"- substrate: {src_note}\n"
            f"- seed: {args.seed}\n"
            f"- generator: `scripts/gen_o2c_eval_and_fixture.py`\n"
            f"- fault rules: `tasks/order_to_cash/TASK_SOP.md` "
            f"(material delta > ${MATERIAL_DELTA:.0f}, rounding tol ${ROUNDING_TOL:.0f})\n\n"
            f"## What is in the eval store\n\n"
            f"`environment/eval/fixture/` — the full store, seen only by the artifact during scoring.\n\n"
            f"| | Rows |\n|---|---:|\n"
            f"| scored eval cases | {len(samples)} |\n"
            f"| unlabelled background invoices | {len(bg_rows)} |\n"
            f"| **invoices total** | **{len(all_rows)}** |\n"
            f"| purchase-order lines | {len(used)} |\n"
            f"| suppliers | {used['SUPPLIER_ID'].nunique()} |\n"
            f"| items | {used['ITEM_ID'].nunique()} |\n\n"
            f"## What is in the build share\n\n"
            f"`environment/build/fixture/` — a carve-out of the above, all the AppSci agent sees.\n\n"
            f"| | Rows |\n|---|---:|\n"
            f"| invoices | {len(build_rows)} "
            f"({100 * len(build_rows) / len(all_rows):.0f}% of the eval store) |\n"
            f"| purchase-order lines | {len(build_lines)} |\n"
            f"| withheld: scored invoices | {len(samples)} |\n"
            f"| withheld: fault type {'/'.join(BUILD_WITHHELD_FAULTS)} | {n_faults} |\n"
            f"| withheld: shares a PO or invented item with a scored case | "
            f"{withheld - n_faults} |\n\n"
            f"The fault-type withholding is deliberate and is NOT a coverage bug: those "
            f"{len(BUILD_WITHHELD_FAULTS)} rules cannot be established from an invoice and its PO "
            f"line alone, so an example in the build share lets an agent infer "
            f"what it was meant to ask the customer for. See `KNOWN_ISSUES.md` section 10.\n\n"
            f"The scored cases are the first {len(samples)} data rows of "
            f"`environment/eval/fixture/invoices.csv`, in eval-case order; `seed_data/labels.csv` is "
            f"their answer key, covers them only, and is bundled into no container. The background "
            f"rows carry the same injected fault mix but produce no eval sample, are never scored, "
            f"and get **no label file** — what each one is can be re-derived from the fixture by "
            f"comparing the invoice to its PO line and contract, so a committed label file would "
            f"only be state that can rot.\n\n"
            f"Background candidates skipped because their invoice number was already taken: "
            f"{bg_skipped_collision}. Skipping rather than renaming keeps the fixture free of "
            f"duplicate keys, so the row count the database ends up with is exactly the count "
            f"above — see `KNOWN_ISSUES.md` section 2.\n\n"
            f"## Reproducing\n\n"
            f"```bash\n"
            f"python3 scripts/gen_o2c_eval_and_fixture.py --src <substrate> \\\n"
            f"    --n {len(samples)} --seed {args.seed} --background {args.background}\n"
            f"```\n\n"
            f"The vendored substrate and its checksums are at `provenance/order_to_cash/`; verify "
            f"them before regenerating. Substrate licence is **Unknown** — treat `environment/` as "
            f"derivative, confirm terms, or regenerate with `--synthetic`. See `KNOWN_ISSUES.md` "
            f"section 5.\n"
        )

    # ---- console summary ----
    from collections import Counter
    dist = Counter(o["fault_type"] for o in oracles)
    pos = sum(1 for o in oracles if o["flagged"])
    print(f"wrote {len(samples)} scored cases to {eval_dir}")
    print(f"  flagged(pos)={pos}  not-flagged(neg)={len(samples) - pos}")
    print("  fault_type distribution:")
    for k in SCENARIOS:
        if dist.get(k):
            print(f"    {k:18s} {dist[k]}")
    if bg_rows:
        bg_dist = Counter(r["fault_type"] for r in bg_labels)
        bg_pos = sum(1 for r in bg_labels if r["flagged"])
        # The background belongs to the EVAL store; the build share is the carve-out of it, and
        # its own count is printed below. Naming the build directory here reported 1,035 rows
        # against a directory holding 994 -- two different build-share sizes in one run.
        print(f"\nwrote {len(bg_rows)} unlabelled background invoices to {eval_fixture}")
        print(f"  flagged={bg_pos} ({100 * bg_pos / len(bg_rows):.0f}%)  "
              f"not-flagged={len(bg_rows) - bg_pos}  "
              f"[scored set is {100 * pos / len(samples):.0f}% flagged]")
        print("  fault_type distribution:")
        for k in SCENARIOS:
            if bg_dist.get(k):
                print(f"    {k:18s} {bg_dist[k]}")
        print(f"  skipped (invoice number already taken): {bg_skipped_collision}")
    print(f"\neval phase  ({eval_fixture}): {len(all_rows)} invoices, {len(eval_lines)} PO lines, "
          f"{eval_lines['SUPPLIER_ID'].nunique()} suppliers, {eval_lines['ITEM_ID'].nunique()} items")
    print(f"build phase ({build_fixture}): {len(build_rows)} invoices, {len(build_lines)} PO lines, "
          f"{build_lines['SUPPLIER_ID'].nunique()} suppliers, {build_lines['ITEM_ID'].nunique()} items")
    print(f"  withheld: the {len(inv_rows)} scored invoices and their PO lines, plus {withheld} "
          f"background row(s) -- {n_faults} of fault type "
          f"{'/'.join(BUILD_WITHHELD_FAULTS)} (deliberate, see BUILD_WITHHELD_FAULTS), the rest "
          f"sharing a PO number or a fabricated item number with a scored case")
    print(f"  build share is {100 * len(build_rows) / len(all_rows):.0f}% of the eval store")


if __name__ == "__main__":
    main()
