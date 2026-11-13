# order_to_cash — L3 domain knowledge (agent-facing extract)

Role: AP director and product owner for this effort

## monthly_invoice_volume

The AP team processes approximately 12,000 invoices per month.

## current_pain

The team misses price overruns and out-of-contract charges, which leads to overpayment.

## materiality_bar

An invoice needs human review when either condition is true:

- A price or total difference has more than $500 of impact.
- The invoice contains any item or fee that is not covered by the supplier contract.

## final_reviewer

The AP team remains the human reviewer. The system only flags invoices for review.

## success_signal

The effort succeeds when the AP team trusts the flags and stops missing material issues.

## what_counts_as_problem

Material discrepancies that require human review include:

- Price differences where the total dollar impact exceeds $500.
- Quantity mismatches between the invoice and purchase order.
- Invoices for items or fees not covered by the supplier contract.
- Duplicate invoice submissions.

## what_doesnt_count

These cases should not be flagged because they create noise:

- Trivial rounding differences of a few dollars.
- Price variances where the total impact is under $500.
- Invoices that match the purchase order and contract.

## validation_rules

Apply the validation checks in this order. The first check that fails is the problem with the
invoice:

1. Duplicate check: the invoice has a `duplicate_of` field set.
2. Price check: `(invoice_unit_price - po_unit_price) * quantity > $500`.
3. Quantity check: `invoice_quantity != po_quantity`.
4. Total check: `|invoice_total - computed_total| > $500`.
5. Contract item coverage: `item_id` is not in the supplier's `allowed_items`.
6. Contract fee check: the fee type is not in the contract's `allowed_fees`.

If any check fails, flag the invoice for human review. If all checks pass, the invoice is clean.
