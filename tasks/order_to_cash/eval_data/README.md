# Evaluation Data — Order-to-Cash Discrepancy Detector

## Layout

**Status**: Generated from Oracle Migration Dataset substrate using `scripts/gen_o2c_eval_and_fixture.py`.

> The evaluation samples cover all fault types from the SOP's fault taxonomy: price deltas,
> quantity mismatches, duplicates, off-contract items/fees, total math errors, and negative
> cases (clean, rounding, sub-threshold). The 60 samples provide coverage across the fault
> distribution to test both recall and false-positive discipline.

One subdirectory per `data_point_id` (`p2p-0001/` .. `p2p-0060/`), each holding a `sample.json` --
no sample here needs its own files (the invoice/PO/contract data lives in the seeded database, see
`environment/` below, not attached files). All 60 sample directories are held out; the table below is
the full distribution.

Ten of them (`p2p-0001`, `0002`, `0005`, `0006`, `0008`, `0010`, `0014`, `0017`, `0018`, `0030`) were
previously carved out of this set and mounted for the agent as labeled examples at
`/data/examples.jsonl`. They are now scored like every other sample and the agent gets no labeled data
at all, which is deliberate: those labels taught the response shape and the decision rule together, so
an agent could fit them instead of eliciting the requirement -- the thing this task exists to measure.
The response contract now reaches the agent only by asking the stakeholder. Their ids and oracle rows
come from `seed_data/labels.csv`, the generator's own record of all 60.

Note the negative fault types below are *scenario* names from the generator, kept here because they
describe the coverage. On disk a not-flagged sample carries `fault_type: null` -- a fault type is only
named when the invoice is flagged.

| Fault Type | Count | Should Flag | Description |
|------------|-------|-------------|-------------|
| PRICE_DELTA | 11 | Yes | Unit price delta > $500 total |
| QTY_MISMATCH | 9 | Yes | Invoiced qty ≠ PO qty |
| OFF_CONTRACT_FEE | 10 | Yes | Fee not in contract |
| OFF_CONTRACT_ITEM | 2 | Yes | Item not in supplier's allowed items |
| TOTAL_MATH | 6 | Yes | Invoice total ≠ qty × price by > $500 |
| DUPLICATE | 3 | Yes | Duplicate invoice |
| CLEAN | 8 | No | Matches PO and contract |
| ROUNDING | 8 | No | Trivial rounding ≤ $3 |
| SUBTHRESHOLD_PRICE | 3 | No | Price delta but < $500 threshold |

**Distribution**: 41 flagged (positive), 19 not-flagged (negative)

## `sample.json`

Each is an EvalSample (schema/dataset.schema.json `$defs/eval_sample`):
- `data_point_id`: the invoice case id (e.g. `p2p-0003`), matching the subdirectory name
- `single_turn_input.text`: the review task for that specific invoice (which invoice/PO/supplier
  to check; the artifact resolves the rest via the seeded database)
- `expected_output.json`: the old `oracle.data` fields, JSON-encoded verbatim (`flagged`,
  `fault_type`, `reason`, plus fault-specific fields like `qty_delta`/`total_delta`/`duplicate_of`
  where applicable) -- see `tasks/order_to_cash/eval.py` for how each objective parses this back out

No `eval_rubric` per sample -- `reason_quality`'s scoring criteria are task-level (baked into its
judge prompt in `eval.py`), not per-sample.

## Where the data lives (sibling directories)

The store is **split by phase** — the two directories are not alternatives, the build one is carved
out of the eval one:

| Directory | Invoices | Who sees it |
|---|---:|---|
| `../environment/eval/fixture/` | **1,095** | only the artifact, during scoring |
| `../environment/build/fixture/` | **832** | the AppSci agent, for the whole build phase |

`../environment/eval/fixture/` — the full store:
- `invoices.csv` — **1,095** invoice records with amounts, fees, and duplicate markers. The **first
  60 data rows, in eval-case order, are the scored cases**; the remaining 1,035 are unlabelled
  background invoices that exist so that browsing the database does not enumerate the eval set.
  They carry the same injected fault mix (66% flagged, against the scored set's 68%) and are never
  scored.
- `po_lines_used.csv` — 1,095 PO line records with agreed quantities and prices, one per invoice
- `contracts.json` — 10 supplier contracts, each with its allowed item ids, agreed unit prices, and
  a **per-supplier list of permitted fee labels**. A fee label absent from a supplier's list is not
  authorized for that supplier, and no label is permitted everywhere — which is why the fee check
  cannot be done from the invoice alone. Built from the whole substrate rather than the sampled
  rows, so it does not move when the background size changes.

`../environment/build/fixture/` — the carved build share, **832** invoices. Withheld from it: the 60
scored invoices and their PO lines; 35 background rows sharing a purchase order or an invented item
number with a scored case; and **168 background rows whose fault is `OFF_CONTRACT_FEE` or
`TOTAL_MATH`**. That last exclusion is by fault rather than by record, and is deliberate — those two
rules cannot be read off an invoice, so an example in the build share would let an agent infer what it
was supposed to ask the customer for. See [`../KNOWN_ISSUES.md`](../KNOWN_ISSUES.md) §10. Contracts go
to both phases identically.

`../seed_data/labels.csv` — ground truth for the **60 scored samples only** (`id`, `invoice_number`,
`flagged`, `fault_type`, `reason`). It sits **above** both phase directories precisely so that it is
bundled into no container; see [`../KNOWN_ISSUES.md`](../KNOWN_ISSUES.md) §8.

The background rows have **no answer key**, on purpose. What each one is can be re-derived from the
fixture by comparing the invoice to its PO line and contract — the task's own comparison — so a
committed label file would be duplicated state that could only drift.
`tests/tasks/order_to_cash/test_fixture_volume.py` re-derives it in memory when it checks the fault
mix, and validates that classifier against the 60 known labels first.

`../seed_data/PROVENANCE.md` records the generation parameters (substrate source, random seed, row
counts for both phases, fault injection rules from `../TASK_SOP.md`) and is rewritten by the generator
on each run. The substrate itself is vendored with checksums at `provenance/order_to_cash/`.

Verify the carve at any time with `python scripts/carve_guard.py order_to_cash`.

Known problems with this data — the single-currency simplification, the arbitrary contract price, thin
negative cases at scale, the unsettled substrate licence, and the two deliberately withheld fault
types — are in [`../KNOWN_ISSUES.md`](../KNOWN_ISSUES.md).

These samples are used during the EVAL phase to measure the agent's performance.
They are held-out and never shown to the agent during BUILD.
