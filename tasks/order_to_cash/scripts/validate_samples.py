#!/usr/bin/env python3
"""Validate eval_data/<data_point_id>/sample.json against fixture data.

Checks:
1. single_turn_input.text has correct invoice/PO/supplier references
2. expected_output.json's flagged/fault_type are correct based on actual invoice data
3. Numeric values (total_delta, qty_delta, off_contract_fee) are accurate

`success_criteria` and a per-sample `customer_info_access` DB-location entry from the old sample
format are not represented here: the DB location is declared once in spec.yaml's
`environment.resources`, not repeated per sample.
"""
import json
import csv
import re
from pathlib import Path


def load_fixture_data(task_dir: Path):
    """Load invoices, PO lines, and contracts from fixture."""
    # The eval phase holds the full store; the build share is a subset and would be missing
    # every scored invoice this script validates against.
    fixture_dir = task_dir / "environment" / "eval" / "fixture"

    invoices = {}
    with open(fixture_dir / "invoices.csv") as f:
        reader = csv.DictReader(f)
        for row in reader:
            invoices[row['invoice_number']] = row

    po_lines = {}
    with open(fixture_dir / "po_lines_used.csv") as f:
        reader = csv.DictReader(f)
        for row in reader:
            po_lines[int(row['PO_LINE_ID'])] = row

    with open(fixture_dir / "contracts.json") as f:
        contracts = json.load(f)

    return invoices, po_lines, contracts


def validate_sample(i: int, sample: dict, invoices: dict, po_lines: dict, contracts: dict) -> list[str]:
    """Validate a single sample. Returns list of discrepancies."""
    discrepancies = []
    sample_id = sample['data_point_id']
    oracle = json.loads(sample['expected_output']['json'])
    intended_task = sample['single_turn_input']['text']

    # Extract invoice number from intended_task
    match = re.search(r'Review invoice (\S+) \(supplier (\S+), PO (\S+)\)', intended_task)
    if not match:
        discrepancies.append(f"Row {i} ({sample_id}): Cannot parse intended_task format")
        return discrepancies

    task_invoice = match.group(1)
    task_supplier = match.group(2).rstrip(',')
    task_po = match.group(3).rstrip(')')

    # Get invoice data
    if task_invoice not in invoices:
        discrepancies.append(f"Row {i} ({sample_id}): Invoice {task_invoice} not found in fixture")
        return discrepancies

    inv = invoices[task_invoice]
    po_line_id = int(inv['po_line_id'])

    if po_line_id not in po_lines:
        discrepancies.append(f"Row {i} ({sample_id}): PO line {po_line_id} not found in fixture")
        return discrepancies

    po = po_lines[po_line_id]
    supplier_id = str(inv['supplier_id'])
    contract = contracts.get(supplier_id, {})

    # 1. Validate invoice and PO in intended_task
    if inv['po_number'] != task_po:
        discrepancies.append(f"Row {i} ({sample_id}): PO mismatch - task says {task_po}, invoice has {inv['po_number']}")

    if inv['supplier_name'] != task_supplier:
        discrepancies.append(f"Row {i} ({sample_id}): Supplier mismatch - task says {task_supplier}, invoice has {inv['supplier_name']}")

    # 2. Validate oracle data against actual invoice
    oracle_flagged = oracle['flagged']
    fault_type = oracle['fault_type']
    inv_qty = float(inv['invoiced_qty'])
    inv_price = float(inv['unit_price'])
    inv_amount = float(inv['amount'])
    po_qty = float(po['QUANTITY'])
    po_price = float(po['UNIT_PRICE'])
    extra_fees = json.loads(inv['extra_fees']) if inv['extra_fees'] else []
    duplicate_of = inv['duplicate_of'] if inv['duplicate_of'] else None
    item_id = int(inv['item_id'])

    expected_amount = inv_qty * inv_price

    if fault_type == 'PRICE_DELTA':
        if inv_price == po_price:
            discrepancies.append(f"Row {i} ({sample_id}): PRICE_DELTA but prices match (inv={inv_price}, po={po_price})")
        total_delta = abs(inv_qty * inv_price - inv_qty * po_price)
        if 'total_delta' in oracle:
            oracle_delta = oracle['total_delta']
            if abs(total_delta - oracle_delta) > 0.1:
                discrepancies.append(f"Row {i} ({sample_id}): total_delta mismatch - calculated {total_delta:.2f}, oracle says {oracle_delta}")
        if total_delta <= 500:
            discrepancies.append(f"Row {i} ({sample_id}): PRICE_DELTA but total_delta {total_delta:.2f} <= $500")
        if not oracle_flagged:
            discrepancies.append(f"Row {i} ({sample_id}): PRICE_DELTA should be flagged")

    elif fault_type == 'QTY_MISMATCH':
        if inv_qty == po_qty:
            discrepancies.append(f"Row {i} ({sample_id}): QTY_MISMATCH but quantities match (inv={inv_qty}, po={po_qty})")
        if 'qty_delta' in oracle:
            expected_delta = inv_qty - po_qty
            if oracle['qty_delta'] != expected_delta:
                discrepancies.append(f"Row {i} ({sample_id}): qty_delta mismatch - calculated {expected_delta}, oracle says {oracle['qty_delta']}")
        if not oracle_flagged:
            discrepancies.append(f"Row {i} ({sample_id}): QTY_MISMATCH should be flagged")

    elif fault_type == 'DUPLICATE':
        if not duplicate_of:
            discrepancies.append(f"Row {i} ({sample_id}): DUPLICATE but no duplicate_of in invoice")
        if 'duplicate_of' in oracle and oracle['duplicate_of'] != duplicate_of:
            discrepancies.append(f"Row {i} ({sample_id}): duplicate_of mismatch - invoice has {duplicate_of}, oracle says {oracle['duplicate_of']}")
        if not oracle_flagged:
            discrepancies.append(f"Row {i} ({sample_id}): DUPLICATE should be flagged")

    elif fault_type == 'OFF_CONTRACT_FEE':
        if not extra_fees:
            discrepancies.append(f"Row {i} ({sample_id}): OFF_CONTRACT_FEE but no extra_fees in invoice")
        else:
            fee_amount = extra_fees[0]['amount']
            if 'off_contract_fee' in oracle:
                if abs(oracle['off_contract_fee'] - fee_amount) > 0.01:
                    discrepancies.append(f"Row {i} ({sample_id}): off_contract_fee mismatch - invoice has {fee_amount}, oracle says {oracle['off_contract_fee']}")
        if not oracle_flagged:
            discrepancies.append(f"Row {i} ({sample_id}): OFF_CONTRACT_FEE should be flagged")

    elif fault_type == 'OFF_CONTRACT_ITEM':
        allowed_items = contract.get('allowed_item_ids', [])
        if item_id in allowed_items:
            discrepancies.append(f"Row {i} ({sample_id}): OFF_CONTRACT_ITEM but item {item_id} IS in allowed_item_ids")
        if not oracle_flagged:
            discrepancies.append(f"Row {i} ({sample_id}): OFF_CONTRACT_ITEM should be flagged")

    elif fault_type == 'TOTAL_MATH':
        math_delta = abs(inv_amount - expected_amount)
        if math_delta <= 500:
            discrepancies.append(f"Row {i} ({sample_id}): TOTAL_MATH but delta {math_delta:.2f} <= $500")
        if 'total_delta' in oracle:
            if abs(oracle['total_delta'] - math_delta) > 0.1:
                discrepancies.append(f"Row {i} ({sample_id}): total_delta mismatch - calculated {math_delta:.2f}, oracle says {oracle['total_delta']}")
        if not oracle_flagged:
            discrepancies.append(f"Row {i} ({sample_id}): TOTAL_MATH should be flagged")

    elif fault_type == 'CLEAN':
        if oracle_flagged:
            discrepancies.append(f"Row {i} ({sample_id}): CLEAN should NOT be flagged")
        if inv_qty != po_qty:
            discrepancies.append(f"Row {i} ({sample_id}): CLEAN but qty mismatch (inv={inv_qty}, po={po_qty})")
        if abs(inv_price - po_price) > 0.01:
            discrepancies.append(f"Row {i} ({sample_id}): CLEAN but price mismatch (inv={inv_price}, po={po_price})")
        if extra_fees:
            discrepancies.append(f"Row {i} ({sample_id}): CLEAN but has extra_fees")
        if duplicate_of:
            discrepancies.append(f"Row {i} ({sample_id}): CLEAN but has duplicate_of")

    elif fault_type == 'ROUNDING':
        math_delta = abs(inv_amount - expected_amount)
        if math_delta > 3:
            discrepancies.append(f"Row {i} ({sample_id}): ROUNDING but delta {math_delta:.2f} > $3")
        if 'total_delta' in oracle:
            if abs(oracle['total_delta'] - math_delta) > 0.1:
                discrepancies.append(f"Row {i} ({sample_id}): total_delta mismatch - calculated {math_delta:.2f}, oracle says {oracle['total_delta']}")
        if oracle_flagged:
            discrepancies.append(f"Row {i} ({sample_id}): ROUNDING should NOT be flagged")

    elif fault_type == 'SUBTHRESHOLD_PRICE':
        total_delta = abs(inv_qty * inv_price - inv_qty * po_price)
        if total_delta > 500:
            discrepancies.append(f"Row {i} ({sample_id}): SUBTHRESHOLD_PRICE but total_delta {total_delta:.2f} > $500")
        if 'total_delta' in oracle:
            if abs(oracle['total_delta'] - total_delta) > 0.1:
                discrepancies.append(f"Row {i} ({sample_id}): total_delta mismatch - calculated {total_delta:.2f}, oracle says {oracle['total_delta']}")
        if oracle_flagged:
            discrepancies.append(f"Row {i} ({sample_id}): SUBTHRESHOLD_PRICE should NOT be flagged")

    return discrepancies


def main():
    task_dir = Path(__file__).parent.parent

    # Load data: one subdirectory per data_point_id under eval_data/, each with a sample.json
    eval_data_dir = task_dir / "eval_data"
    samples = [
        json.loads((d / "sample.json").read_text())
        for d in sorted(p for p in eval_data_dir.iterdir() if p.is_dir())
    ]

    invoices, po_lines, contracts = load_fixture_data(task_dir)

    # Validate all samples
    all_discrepancies = []
    for i, sample in enumerate(samples, 1):
        discrepancies = validate_sample(i, sample, invoices, po_lines, contracts)
        all_discrepancies.extend(discrepancies)

    # Report results
    print(f"Validated {len(samples)} samples")
    print(f"Found {len(all_discrepancies)} discrepancies:\n")

    for d in all_discrepancies:
        print(d)

    if not all_discrepancies:
        print("All validations passed!")
        return 0
    return 1


if __name__ == "__main__":
    exit(main())
