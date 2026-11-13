# order_to_cash — substrate provenance

The clean procure-to-pay chain the task's seeded database is derived from, not included in this public release, because the source states no license.
Regenerate from the Kaggle source with `--src`, or with `--synthetic` for an equivalent chain. **Internal record only** — nothing under `provenance/` is
mounted into any container, so none of it is reachable by the agent under test.

## Upstream

| | |
|---|---|
| Source | Oracle Migration Dataset, Kaggle |
| URL | https://www.kaggle.com/datasets/anjaliminakshi/oracle-migration-dataset |
| Owner | `anjaliminakshi` (dataset id 8947149) |
| Version | 1 ("Initial release") |
| Published | 2025-12-08 |
| Retrieved | 2026-08-20 |
| **License** | **Unknown — the uploader selected none.** See [Licence status](#licence-status) |
| Archive size | 99,339 bytes (zip), 291,759 bytes extracted |
| Archive sha256 | `dd9bc242098cd416df1f7eb6ec0bb111fed4b7f3d9e9c988d95e5ad050ad35b2` |

The download needs no Kaggle account:

```bash
curl -L -o omd.zip https://www.kaggle.com/api/v1/datasets/download/anjaliminakshi/oracle-migration-dataset
unzip omd.zip -d substrate/
shasum -a 256 -c substrate/SHA256SUMS      # must pass before regenerating anything
```

`substrate/SHA256SUMS` pins all eight files. Verify it rather than trusting the copy — this is a
single-version upload by one author with 34 downloads and no vote history, so it could change or
disappear without notice.

## What the upstream actually is

Generated data imitating an Oracle E-Business Suite procurement system, deliberately seeded with
migration-style data-quality defects (the author's stated purpose is practice material for data
cleansing and an S/4HANA move). **Synthetic, not real company records** — no personal or commercial
data, so no privacy exposure. It is not a capture of any live system.

## Contents, as measured on the retrieved copy

| File | Rows | Role |
|---|---:|---|
| `vendors.csv` | 10 | supplier master |
| `materials.csv` | 20 | item master |
| `pr_headers.csv` | 400 | requisition headers — **not used by the task** |
| `pr_lines.csv` | 1,023 | requisition lines — **not used by the task** |
| `po_headers.csv` | 1,000 | purchase-order headers |
| `po_lines.csv` | 1,983 | purchase-order lines |
| `goods_receipts.csv` | 1,388 | goods receipts — **not used by the task** |
| `invoices.csv` | 1,189 | supplier invoices |

Chain: requisition → purchase order → goods receipt → invoice, joined on the purchase-order line.

**The Kaggle description's own file table understates these counts** (it lists ~300 invoices and ~600
order lines, under different filenames). The numbers above are counted from the retrieved files, whose
sizes sum exactly to the reported archive total. Trust the measurement, not the card.

## Why it was chosen

It is the only public source found that ships invoices already *linked* to their purchase order in a
relational shape that maps onto this task's database. The full survey of alternatives — including the
permissively-licensed ones that were rejected on fit — is
[`tasks/order_to_cash/dataset_survey.md`](../../tasks/order_to_cash/dataset_survey.md).

## The property that makes it usable: it is clean

Measured over the 1,181 invoices that resolve to an order line:

| Check | Disagreements |
|---|---:|
| invoiced quantity vs ordered quantity | 0 |
| invoiced unit price vs ordered unit price | 0 |
| invoice total vs quantity × price | 0 |
| invoiced quantity vs quantity received | 0 |
| invoice supplier vs order supplier | 0 |

So the substrate contains **no organic match discrepancies**. Every fault the task scores is injected
by `scripts/gen_o2c_eval_and_fixture.py`, which means every fault is labelled and there are no unlabelled
positives hiding in the background rows. This is what makes the volume safe to use as dilution.

## Defects that ARE present, and how they are handled

These are data-quality defects, not match discrepancies. The generator's substrate loader drops or
overrides each one deliberately — see the comments there, and
[`tasks/order_to_cash/KNOWN_ISSUES.md`](../../tasks/order_to_cash/KNOWN_ISSUES.md) §1 for the one with
open consequences.

| Defect | Extent | Handling |
|---|---|---|
| Invoice currency ≠ supplier-master currency | 798 of 1,189 | **Discarded** — the whole fixture is relabelled USD. Known issue §1 |
| `XXX` as a currency code | present in invoices | Discarded with the rest of the currency column |
| `INVALID` unit of measure | 1 of 20 items | Item master is read for its unit of measure; the affected item survives |
| Order lines with no item | 10 | Dropped by key cleaning (1,983 → 1,973) |
| Order headers with no supplier | 10 | Dropped by key cleaning (1,000 → 990) |
| Invoices pointing at an absent order line | 8 | Dropped by the join |
| Receipts pointing at an absent order line | 5 | Dropped by the join |
| Repeated invoice numbers | 75 raw / 73 in the joined pool | Known issue §2 |

After cleaning and joining, **1,168 invoice records are usable**, each resolving to its own order
line. 805 further order lines are never invoiced and are spare capacity.

## Licence status

The Kaggle card carries **no licence selection**, and the dataset has no description-level grant. So
there is no explicit permission to redistribute it, and this vendored copy is committed on the
judgement that it is generated (not scraped or proprietary) synthetic data of no commercial value,
retained for reproducibility of a research benchmark.

**This is an unresolved item, not a cleared one.** Two things to know:

- The derived rows under `tasks/order_to_cash/seed_data/` are a transformation and have shipped since
  the task was authored. This directory is different: it is a **verbatim copy** of the upstream files,
  which is the thing an unknown licence does not grant.
- The generator's `--synthetic` mode produces an equivalent chain we own outright, with no third-party
  terms at all. That is the exit if the licence question is ever answered unfavourably: regenerate,
  delete this directory, and the task keeps working with different numbers.

Tracked in [`tasks/order_to_cash/KNOWN_ISSUES.md`](../../tasks/order_to_cash/KNOWN_ISSUES.md) §5.
