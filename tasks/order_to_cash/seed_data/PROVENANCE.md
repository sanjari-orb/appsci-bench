# P2P seed — provenance

Auto-written by `scripts/gen_o2c_eval_and_fixture.py` on each run. Do not hand-edit.

## What produced this

- substrate: Oracle Migration Dataset @ provenance/order_to_cash/substrate (license: Unknown)
- seed: 20260615
- generator: `scripts/gen_o2c_eval_and_fixture.py`
- fault rules: `tasks/order_to_cash/TASK_SOP.md` (material delta > $500, rounding tol $3)

## What is in the eval store

`environment/eval/fixture/` — the full store, seen only by the artifact during scoring.

| | Rows |
|---|---:|
| scored eval cases | 60 |
| unlabelled background invoices | 1035 |
| **invoices total** | **1095** |
| purchase-order lines | 1095 |
| suppliers | 10 |
| items | 20 |

## What is in the build share

`environment/build/fixture/` — a carve-out of the above, all the AppSci agent sees.

| | Rows |
|---|---:|
| invoices | 662 (60% of the eval store) |
| purchase-order lines | 662 |
| withheld: scored invoices | 60 |
| withheld: fault type OFF_CONTRACT_FEE/TOTAL_MATH/OFF_CONTRACT_ITEM/DUPLICATE | 354 |
| withheld: shares a PO or invented item with a scored case | 19 |

The fault-type withholding is deliberate and is NOT a coverage bug: those 4 rules cannot be established from an invoice and its PO line alone, so an example in the build share lets an agent infer what it was meant to ask the customer for. See `KNOWN_ISSUES.md` section 10.

The scored cases are the first 60 data rows of `environment/eval/fixture/invoices.csv`, in eval-case order; `seed_data/labels.csv` is their answer key, covers them only, and is bundled into no container. The background rows carry the same injected fault mix but produce no eval sample, are never scored, and get **no label file** — what each one is can be re-derived from the fixture by comparing the invoice to its PO line and contract, so a committed label file would only be state that can rot.

Background candidates skipped because their invoice number was already taken: 73. Skipping rather than renaming keeps the fixture free of duplicate keys, so the row count the database ends up with is exactly the count above — see `KNOWN_ISSUES.md` section 2.

## Reproducing

```bash
python3 scripts/gen_o2c_eval_and_fixture.py --src <substrate> \
    --n 60 --seed 20260615 --background all
```

The vendored substrate and its checksums are at `provenance/order_to_cash/`; verify them before regenerating. Substrate licence is **Unknown** — treat `environment/` as derivative, confirm terms, or regenerate with `--synthetic`. See `KNOWN_ISSUES.md` section 5.
