# Order-to-Cash — Public Dataset Survey

> **Reading this cold?** This document is self-contained. It surveys public datasets for the
> Order-to-Cash *Discrepancy Detector* benchmark task and records a concrete, reproducible deliverable
> (a labeled eval-seed generator + 60 validated cases). Start with **Task context** below, skim the
> **Headline finding**, then jump to **Produced artifacts & how to reproduce** at the bottom to continue
> the work. No prior conversation knowledge is required.

## Task context (recap — so this doc stands alone)

The Order-to-Cash *Discrepancy Detector* task (`tasks/order_to_cash/spec.yaml`, ground truth in
`tasks/order_to_cash/TASK_SOP.md`): an AP team checks ~12k supplier invoices/month against the
**purchase order** (and, where relevant, the **contract**) before paying. The agent must **flag invoices a
human should review, with a short reason**, and keep false positives low. **"Material" (flag) =** price or
total delta vs the PO **> $500**, OR quantity ≠ PO (any difference), OR a line item/fee **not covered by the
contract** (any amount). Trivial roundings (≤ a few dollars) must **not** be flagged. The task's
`evaluation_data` is `provenance: synthetic`; the environment models data as an OLTP DB (`oltp_db`) with
`invoices`/`pos`/`contracts` tables. **This survey asks: can public data ground or seed that synthetic set?**

**Survey scope (the lens used here):**
- O2C stage: **invoice ↔ purchase-order matching + payment-policy validation** (no double charge, no out-of-policy charge, prompt remittance).
- Modality: **documents** (invoices, POs, remittance/contracts) — secondarily tabular AP/AR.
- Use: **benchmark task seeds** with ground truth.
- Licensing: **research-license OK** (note any commercial restriction; CC BY-NC etc. acceptable for now).

---

## Headline finding

> **Public data already provides invoice↔PO records that are *linked*; what it does not provide is those
> links carrying *labeled match-discrepancies*.** The task's hard part — deciding flag / no-flag against a
> ground truth — therefore needs only a thin synthetic layer (inject + label discrepancies) on top of a
> real, linked substrate. The linkage exists in two modalities:
> - **Linked PO↔invoice documents** (the document modality): the *Company Documents Dataset* (Kaggle,
>   Apache-2.0) — 830 invoice PDFs + 830 purchase-order PDFs (Northwind-derived) that **share an Order ID**,
>   verified 1:1 with no orphans, plus an extracted-text CSV. Paired invoice/PO are two renderings of the
>   same order, so they **agree by construction** (no discrepancies present).
> - **Linked relational P2P tables**: the *Oracle Migration Dataset* — a clean PR→PO→GR→Invoice chain
>   joinable on `PO_LINE_ID`, where invoice price/qty/amount match the PO exactly (again, no faults present).
>
> Two adjacent sources add realism but not the matching link: *AI-Powered ERP AR Reconciliation* (labeled
> AR-balance mismatches with a `Likely Cause` rationale) and the *Procurement Invoice Fraud Dataset* (300k
> invoices + 45k rendered images with fraud labels, but **no PO**, so no invoice↔PO matching).
>
> **The one thing no public source ships is invoice↔PO pairs with *injected, labeled* match-discrepancies**
> (price/total delta, qty mismatch, off-contract item/fee, duplicate). Because every public linked source is
> *clean*, that discrepancy-and-label layer must be synthesized — but on top of a real linked substrate
> (documents or tables), not from scratch.

**Recommendation in one line:** seed a real linked substrate (Company Documents for the document modality, or
Oracle Migration for the tabular/OLTP modality), inject the spec's fault taxonomy to create labeled positives
+ hard negatives, and borrow the label/rationale style from the AR-reconciliation set.

The closest academic work, *Online Similarity Learning for Invoice Line Item Matching* (AAAI-IPA 2020,
arXiv 2001.00288), tackles exactly invoice↔PO line-item matching but uses **proprietary data with no public
release** — consistent with the project's novelty thesis (memory `project_novelty_gaps`).

**Seeding strategies (both backed by real linked data):**
1. **Document-grounded:** start from the Company Documents invoice/PO PDFs linked by Order ID → inject the
   spec's materiality faults on the invoice side (price delta >$500, qty mismatch, off-contract item/fee,
   duplicate; trivial roundings + sub-$500 deltas as negatives) → label.
2. **Relation-grounded:** start from the Oracle Migration PR→PO→GR→Invoice tables → inject the same faults →
   load into the task's `oltp_db`. *(Implemented — see "Produced artifacts" below.)*

---

## ⭐ Most-relevant finds (inspected in depth)

These are the closest sources to what the task needs, inspected at the row/column level (datasets were
downloaded or range-read; #2 is a code notebook whose source was read).

### Linked invoice↔PO substrates at a glance

Several public sources already link invoices to their PO (the hard-to-find property). All are **clean**
(invoice agrees with the PO) — none ships injected/labeled discrepancies — so each is a *substrate* to
inject faults onto, differing by modality, license, and realism:

| Source | Modality | Linkage key | License | Notes |
|---|---|---|---|---|
| **Company Documents Dataset** | **Documents** (invoice + PO PDFs + text CSV) | shared **Order ID** (830 pairs, 1:1) | Apache 2.0 | Northwind-derived; no contract layer. The document-modality answer. |
| **Oracle Migration Dataset** | Tabular (PR→PO→GR→Invoice) | `PO_LINE_ID` | **Unknown** ⚠️ | Clean P2P chain; maps to `oltp_db`. Used by the generator below. |
| **SAP BigQuery (IDES)** | Tabular (authentic SAP schema) | `rseg.ebeln+ebelp → ekpo` | MIT | Real PO→GR(`ekbe`)→invoice(`rbkp`/`rseg`)→payment(`bkpf`/`bseg`); cryptic fields, multi-client demo data, ~366 MB. |
| **Vendor Performance Analysis** | Tabular (retail) | `PONumber` | **CC0** | `purchases.csv` (receipt lines) ↔ `vendor_invoice.csv` (invoice totals + `Freight`, `PayDate`, `Approval`); real liquor-distribution data; ~508 MB. |

A real **contract layer + match-status vocabulary** (which the linked sources above lack) exists in
**Purchase Orders & Supplier Performance** (`Contract Type`: Spot/Long-term/Master Supply/Framework;
`Invoice Match Type`: 3-Way/2-Way/No-Match; payment terms; budget-vs-actual price). And a small **labeled**
price-discrepancy set exists in **Procurement & Payroll Fraud** (`price_system` vs `price_actual` +
`fraud_flag`, 31% positive). See Group B for both.

### 1. Procurement Invoice Fraud Dataset — Kaggle `tokelomashile2/procurement-invoice-fraud-dataset`
*Multimodal, synthetic/semi-synthetic; Apache 2.0; ~2.1 GB zip; updated 2025-12-14.*

**Verified contents** (read directly from the parquet tables via range-download):
- **300,000 invoices**, 2,000 suppliers, 50 departments, **45,000 rendered invoice PNGs** (subset), plus
  `behavioural_features`, `labels`, `images_metadata`, `splits`, `manifest.json`.
- `invoices`: `invoice_id, supplier_id, department_id, invoice_date, invoice_amount, currency, payment_terms (NET30/60/90), invoice_type (GOODS/SERVICES), submission_hour, image_path`.
- `suppliers`: country, age_days, **risk_score, blacklisted_flag**, avg_invoice_amount. `departments`: region, annual_budget.
- `images_metadata`: `ocr_total_extracted`, `image_tamper_flag`.
- `labels`: `is_fraud` (**22.1% positive**), `fraud_type`, multi-label `fraud_tags`, `explanations` (JSON, but
  rule-name only — e.g. `{"rules":["SPLIT"]}` — *not* the rich NL rationale the card implies).

**Fraud taxonomy:** `SPLIT` (split to evade approval thresholds, 42k) · `GHOST_SUPPLIER` (12.6k) ·
`INFLATED` (5.6k) · `DOC_TAMPER` (2.4k) · `DUPLICATE` (70).

**Fit verdict — partial, complementary (NOT a drop-in):** This is **invoice-level fraud/anomaly detection**
(behavioural + amount patterns + image tampering), **not invoice↔PO matching** — there is **no PO table and
no line items**, so the task's core check ("does the invoice match what we agreed to buy?") cannot be posed
from it. Only `DUPLICATE` (≈ "no double charge") and `INFLATED` (≈ "out-of-policy charge") overlap our spec.
**Best used for:** (a) realistic **invoice document images**; (b) realistic supplier/amount/timing
distributions incl. the SPLIT-below-threshold pattern (relevant to our materiality thresholds); (c) a
reference **label + explanation schema** that mirrors our "flag + short reason" output; (d) a complementary
fraud/anomaly signal alongside synthetic 3-way-match data. Caveats: synthetic and slightly incoherent
(all ZAR currency, US-state "regions"); rationales are thin.

### 2. PO-GRN-INV Playground notebook — Kaggle `nguyenngochuyen87/po-grn-inv-playground-for-ateliers` (v10, run 2026-06-12)
*Kaggle **notebook (code), not a dataset** — a synthetic three-way-match data generator + extractor.*

**Verified contents** (read the notebook source):
- **This is the 3-way match** our task is about: **PO + GRN (Goods Receipt Note) + Invoice.** Builds a SQLite
  DB `three_way_matching.db` with `tbl_PO (PO_ID, Vendor_ID, Item_Name, Quantity_Ordered, UoM, Unit_Price_Ordered)`,
  `tbl_GRN (GRN_ID, PO_ID→FK, Item_Name, Quantity_Received, UoM)`, `tbl_invoices`, `tbl_detail_invoice`.
- **Flow 1 — GenAI PDF-invoice ETL:** PyPDF2 + LLM extracts invoice PDFs → structured rows.
- **Flow 2 — Smart test-data generation:** LLM generates PO & GRN data across **scenarios** with controlled
  cardinalities/constraints (1 INV:1 PO:1 GRN; 1:1:3; 1:3:1; 1:2:3, with "sum of GRN/PO qty = invoice" rules)
  — i.e. it deliberately constructs the matching relationships and partial-delivery/split cases.
- **Matching:** `rapidfuzz` fuzzy item-name matching + few-shot LLM mapping to handle abbreviations/typos/synonyms.
- Uses **Gemini** (`google-genai`); approach transfers directly to Claude. License: Kaggle default (Apache 2.0).

**Fit verdict — methodology gold, no released data.** This is the concrete recipe for **synthesizing the
matching ground truth** the public datasets lack (validates "Strategy 2" above). Adopt: the scenario-config
pattern with cardinality/sum constraints, the PO↔GRN↔invoice schema, and fuzzy line-item matching. Replace
Gemini with Claude; add our spec's fault taxonomy (price delta >$500, qty mismatch, off-contract fee, trivial
rounding negatives) on top of its scenario generator.

### 3. ⭐ Oracle Migration Dataset — Kaggle `anjaliminakshi/oracle-migration-dataset`
*Synthetic Oracle E-Business Suite **Procure-to-Pay** chain; license **Unknown** (⚠️ flag before use); ~292 KB.*

**Verified contents** (downloaded + read all CSVs). 8 linked tables modelling 1,000 PR→PO→GR→Invoice chains:
- `vendors` (10): `SUPPLIER_ID, SUPPLIER_NAME, TAX_ID, PAY_TERMS (NET15/30/…), CURRENCY`
- `materials` (20): `ITEM_ID, ITEM_NUMBER, DESCRIPTION, UOM`
- `pr_headers` (400) / `pr_lines` (1023): requisitions w/ `ITEM_ID, QUANTITY, EST_UNIT_PRICE`
- `po_headers` (1000) / `po_lines` (1983): `PO_LINE_ID, ITEM_ID, QUANTITY, UNIT_PRICE, DELIVERY_DATE`
- `goods_receipts` (1388): `GR_ID, PO_LINE_ID→, ITEM_ID, SUPPLIER_ID, RECEIPT_DATE, RECEIVED_QTY`
- `invoices` (1189): `INVOICE_ID, INVOICE_NUMBER, SUPPLIER_ID, PO_LINE_ID→, INVOICED_QTY, UNIT_PRICE, CURRENCY, AMOUNT`

**This is the relational substrate the task needs:** invoices join to PO lines (and PO lines to GRs) on
`PO_LINE_ID` — enabling a genuine **3-way match** (ordered vs received vs invoiced qty/price) plus
payment-terms/currency checks via the vendor master. Verified joinability: **all 1,189 invoices link to a
PO line; AMOUNT = qty×price exactly**.

**Critical caveat:** the invoice↔PO relation is **clean** — price/qty/amount match the PO **exactly** for
all rows, and no PO line is invoiced more than once. So the **match-discrepancies our task must catch are
NOT present** and must still be injected. The dataset's own "defects" are *data-quality/migration* issues
(float-typed IDs, missing values, UOM inconsistencies, multi-currency: 798/1189 invoices use a currency ≠
the vendor-master currency) — useful as noise, but distinct from 3-way-match faults.

**Fit verdict — best public substrate for the matching task.** Maps almost 1:1 onto `spec.yaml`'s `oltp_db`
(postgres `invoices`/`pos` + a `goods_receipts`/`contracts` analogue). Use it as the clean linked skeleton,
then inject the spec's fault taxonomy (price delta >$500, qty mismatch, off-contract fee; trivial roundings
as negatives) to create labeled positives/negatives. ⚠️ License is "Unknown" — confirm reuse terms, or treat
as a structural reference and regenerate equivalently.

### 4. AI-Powered ERP AR Reconciliation — Kaggle `asiryi/ai-powered-erp-ar-reconciliation`
*Synthetic AR reconciliation w/ scripts + **labeled mismatches**; license "Other (in description)"; ~151 KB.*

**Verified contents** (downloaded + read): `erp_export.csv` (1,000 customers; per-customer Invoice/Payment/
Credit/Adjustment totals + applied amounts + exchange rates + Customer Balance), `mismatches_rule_based.csv`
(**110 labeled rows** with `Expected Balance, Delta, Abs Delta, Likely Cause` e.g. *"Check Invoice/exchange
rate/rounding"*), `anomalies_ml.csv` (26 rows, ML residuals), plus `generate_ar.py`, `reconcile.py`,
`anomalies.py`. ~10–12% of rows intentionally broken (missing data, wrong FX, rounding).

**Fit verdict — relevant to the "responsible remittance" angle + a labeling-pattern reference.** Operates at
**AR customer-balance level** (recompute balance from invoices+payments+credits+adjustments), not line-item
invoice↔PO matching — so not a direct substrate. But it's a clean public example of **explainable rule-based
+ ML anomaly labeling with a `Likely Cause` rationale** (mirrors our "flag + short reason"), and its FX/
rounding fault types are reusable. Borrow the labeling design and the reconcile-script approach.

### 5. ⭐ Company Documents Dataset — Kaggle `ayoubcherguelaine/company-documents-dataset`
*Linked invoice + purchase-order **documents** (PDF) sharing an Order ID; Apache 2.0; ~10 MB.*

**Verified contents** (downloaded + parsed). `CompanyDocuments/` holds 2,676 PDFs in four folders, plus
`company-document-text.csv` (extracted `text` + `label` + `word_count` for every doc):
- **invoices** — 830 PDFs; text carries `order id, customer id, order date, customer details (name / address /
  city / postal / country / phone / fax), product lines (product id, name, quantity, unit price), totalprice`.
- **purchase orders** — 830 PDFs; text carries `order id, order date, customer name, product lines (product
  id, name, quantity, unit price)`.
- **Shipping orders** — 809 PDFs (also carry an order id). **Inventory Report** — 207 PDFs.

**Linkage — verified:** invoices and POs **share an Order ID, 1:1 across all 830 pairs (0 orphans** on
either side). A matched pair (e.g. order id 10248) agrees on customer, order date, and line items (product
id / qty / unit price); the invoice additionally carries billing address + `totalprice`. The data is
**Northwind-derived** (classic sample DB — orders 10248+, customers like VINET / Paul Henriot), so each
invoice/PO pair are two renderings of the same order and **agree by construction**.

**Fit verdict — the document-modality linked substrate the task wants.** Real, openly-licensed, linked
PO↔invoice *documents* (PDF + extracted text) keyed by Order ID — exactly what an extract-then-match agent
consumes (query an incoming invoice against a PO knowledge base by Order ID, verify customer/product
alignment, flag discrepancies). Caveats: like every public linked source it is **clean** (invoice matches
PO), so positive cases require injecting discrepancies on the invoice side; and it has **no contract layer**,
so "off-contract item/fee" must be synthesized (or borrowed from Purchase Orders & Supplier Performance).

---

## Group A — Document / KIE datasets (realistic invoice/PO layouts + field labels)

| Dataset | Modality | Size | License | What it offers | Gaps vs. our task |
|---|---|---|---|---|---|
| **DocILE** (Rossum, ICDAR'23) — docile.rossum.ai, arXiv 2302.05658 | Invoice/order document images | 6.7k annotated + 100k synthetic + ~1M unlabeled; 55 field classes | Research, via access-request form | Largest business-doc KIE set; tasks **KILE** (key info localization) + **LIR** (line-item recognition). Best source of realistic invoice layouts + line items. | No PO↔invoice link, no matching/validation labels. |
| **VRDU** (Google, KDD'23) — arXiv 2211.15421 | Registration forms + ad-buy forms | 2 datasets | CC BY-SA 4.0 (toolkit open-sourced) | Hierarchical fields incl. invoice-style line items; strong few-shot extraction benchmark. | Not invoices/POs per se; extraction only, no matching. |
| **SROIE** (ICDAR'19) — Kaggle `ryanznie/sroie-datasetv2-with-labels` | Scanned receipt images + OCR + KIE | ~1k receipts, ~875 MB | MIT | Classic receipt KIE (company, date, total). Clean, well-labeled. | Receipts not invoices; single-doc; no matching. |
| **CORD** (Clova) | Receipt images + KIE w/ line items | ~1k | CC BY 4.0 (research) | Receipt parsing with line-item hierarchy. | Receipts; no matching. |
| **FUNSD** | Scanned form images | 199 forms | research-only | Form understanding / entity linking *within* a doc. | Forms, tiny, no invoice/PO matching. |
| **RVL-CDIP — invoice subset** — HF `chainyo/rvl-cdip-invoice` | Invoice document images | 19.9k | RVL-CDIP terms (research) | Largest pool of raw invoice *images* (classification origin). | Labels are doc-class only; no fields, no matching. |
| **High-Quality Invoice Images for OCR** — Kaggle `osamahosamabdellatif/...` | Invoice images | ~1.2 GB | Open Database (ODbL) | Large curated invoice image pool for OCR/extraction. | No structured matching ground truth. |
| **High-Quality OCR-Ready Invoice PDFs** — Kaggle `devp1866/...` | Synthetic invoice PDFs + JSON + raw text | 100 PDFs (consumer electronics, India) | Apache 2.0 | **PDFs with embedded text + paired JSON ground truth** — clean extraction seed; shows a synthesis recipe (ReportLab). | Tiny; invoices only, no PO/contract; synthetic. |
| **Invoice NER Dataset** — Kaggle `nikitpatel/invoice-ner-dataset` | Text + NER tags | small | CC BY-NC-SA 4.0 | Field-tagging seed for invoice text. | NC license; no matching. |
| **Real World Documents Collections** — Kaggle `shaz13/...` | Mixed doc-type images | ~445 MB | Unknown ⚠️ | RVL-CDIP-style doc-type variety. | License unclear; classification only. |
| **Company Documents Dataset** — Kaggle `ayoubcherguelaine/...` | **Linked invoice + PO PDFs** (+ extracted-text CSV) | ~10 MB (2,676 PDFs) | Apache 2.0 | **830 invoice + 830 PO documents sharing an Order ID** (Northwind-derived), 1:1; + 809 shipping + 207 inventory. See ⭐ find #5. | Clean (invoice agrees with PO); no contract layer; discrepancies must be injected. |
| **Electronic Tax Document (PT) OCR** — Kaggle `sc0v1n0/...` | Brazilian e-invoice images (text in PT) | ~260 MB | CC0 | Real e-invoice imagery (non-English). | OCR test set; no matching. |

## Group B — Tabular procurement / AP / AR datasets (relational structure + spend realism)

| Dataset | Modality | Size | License | What it offers | Gaps vs. our task |
|---|---|---|---|---|---|
| **SAP Replicated Data (BigQuery / IDES)** — Kaggle `mustafakeser4/sap-dataset-bigquery-dataset` | Relational ERP tables (78 SAP tables) | ~366 MB | MIT | **Verified: authentic SAP P2P schema** — `ekko`/`ekpo` (PO header/line), `ekbe` (PO history = GR/IR), `rbkp`/`rseg` (logistics invoice), `bkpf`/`bseg` (accounting/payment), `lfa1` (vendor). `rseg` links to PO via **`ebeln`+`ebelp`**. Genuine PO→GR→invoice→payment chain. | IDES **demo** data (multi-client `mandt`, placeholder values); cryptic field names need SAP knowledge; no labeled discrepancies; large. |
| **Purchase Orders & Supplier Performance** — Kaggle `thuandao/...` | Tabular PO lines (xlsx, 5,200 rows) | ~2 MB | Apache 2.0 | **Verified: the contract + match-status layer the linked sources lack** — `Contract ID/Type` (Spot/Long-term/Master Supply/Framework), `Invoice Match Type` (3-Way/2-Way/No-Match), `Invoice Status`, `Payment Status`, `Payment Terms`, budget-vs-actual `Unit Price`/`Savings`. | PO-centric: invoice fields are *status only* — no separate invoice-side amounts to reconcile. |
| **Customer Invoices / Payment Date Prediction** — Kaggle `pradumn203/...` | Tabular AR ledger (HighRadius) | ~1.7 MB | CC BY-NC 4.0 | **Verified:** real-style B2B AR invoices — `due_in_date`, `clear_date`, `total_open_amount`, `cust_payment_terms`, `isOpen` → directly serves **payment-timeliness / "remitting promptly"**. | NC license; AR only, no PO/document, no policy labels. |
| **Accounts Payable Dataset** — Kaggle `sonalisingh1411/accounts-payable-dataset` | Tabular AP | ~38 KB | Apache 2.0 | Small AP ledger example. | Tiny; low usability; no matching. |
| **Vendor Payment Dataset (ICAI book)** — Kaggle `kmldas/...` | Tabular vendor payments | ~229 KB | CDLA-Permissive 1.0 | Clean accounting teaching dataset (permissive). | Small; payments only. |
| **Nova: 50M Transactions Super-App** — Kaggle `adityathakekar/...` | Relational payments + supply-chain logs | ~2.9 GB | Apache 2.0 | Large **relational** payments + supply-chain; could model invoice↔payment linkage at scale. | Consumer super-app, not B2B PO/invoice; needs heavy adaptation. |
| **Online Retail / Online Sales** — Kaggle (multiple) | Tabular order lines | varies | CC0 / CC BY | Order/line-item realism (qty, unit price, totals) to back-derive POs. | No documents, no invoice/PO split, no matching. |
| **Vendor Performance Analysis** — Kaggle `harshmadhavan/...` | Tabular retail (purchases, sales, inventory, `vendor_invoice`) + SQLite | ~508 MB | **CC0** | **Verified: invoice↔PO link** — `purchases.csv` (receipt lines: `PONumber, PODate, ReceivingDate, InvoiceDate, PayDate, PurchasePrice, Quantity, Dollars`) ↔ `vendor_invoice.csv` (invoice totals: `PONumber, Dollars, Freight, PayDate, Approval`) join on **`PONumber`**. Real liquor-distribution data. | Invoice is an aggregate total (not line-level vs PO); no injected discrepancies (organic variances may exist via Freight/partial receipts). |
| **Payment Practices of UK Buyers** — Kaggle `saikiran0684/...` | Tabular (real UK-gov filings) | ~4.7 MB | Other (UK OGL-style) | **Real** statutory late-payment reports (avg days to pay, % paid late) — strong for the **prompt-remittance / payment-timeliness** dimension. | Aggregate buyer-level; no invoices/POs/documents. |
| **Procurement and Payroll Fraud Dataset** — Kaggle `ramadhanridhoa/...` | Tabular synthetic (multi-CSV) | ~241 KB | Unknown ⚠️ | **Verified: labeled price-discrepancy table** — `procurement.csv` has `price_system` vs `price_actual`, `price_difference`, `account_mismatch_flag`, **`fraud_flag` (31% positive)**; `vendors.csv` has bank account (ghost-vendor). A rare public *labeled* discrepancy example. | Tiny (278 rows); license unknown; no PO line / document; conceptual not document-grounded. |
| **msme invoices and transactions** — Kaggle `kiruthikas005/...` | Tabular | ~0.5 KB | Unknown ⚠️ | (Triage only.) | Negligible size; skip. |

## Group C — Government open spend / procurement (relational, openly licensed)

These publish **real** procurement→payment records with linking IDs and permissive licenses — strong for
relational realism and "proper-invoice-received vs payment-date" timeliness, though they are records (not
document images).

| Dataset | Coverage | License | Relevance |
|---|---|---|---|
| **Canadian Govt Open Transactions** — Kaggle `shivd24coder/...` | Procurement transactions w/ vendor, **proper-invoice-received date**, **payment date**, project # | CC0 | Has the exact invoice-received→payment-date link for **timeliness/promptness** validation. |
| **ProcureData — Canadian Procurement (sample)** — Kaggle `niexon/...` | 5k rows: contracts, tenders, awards | CC BY 4.0 | Procurement/contract dimension. |
| **SF Vendor Payments** — Kaggle `san-francisco/sf-vendor-payments` | City vendor payments | Open data terms | Real vendor→payment ledger. |
| **City of Chicago Payments** — Kaggle `chicago/chicago-payments` | City payments | CC0 / US Gov Works | Real payment records. |
| **NYS Design & Construction Vendor Payments** — Kaggle `new-york-state/...` | State vendor payments | CC0 | Vendor payment ledger. |
| **Oklahoma State Purchase-Card Spending** — Kaggle `thedevastator/...` | Agency/cardholder spend | Other (open) | Card spend by agency. |
| **2021 US Federal Award Data** — Kaggle `skeller/...` (mirror of USASpending) | ~2 GB federal awards | Other (US Gov Works) | Award→spend at scale. Source: **USASpending.gov** (public domain). |
| **(Off-Kaggle) USASpending.gov; UK gov "spend over £25k" / Contracts Finder; EU TED** | National procurement/spend | Public Domain / Open Govt Licence | Authoritative relational spend; UK transparency code mandates invoice/payment publication. *(UK guidance URL 404'd during survey — confirm current portal.)* |

## Group D — Academic precedent (for methodology, not data)

| Work | Relevance | Public data? |
|---|---|---|
| **Online Similarity Learning with Feedback for Invoice Line Item Matching** — arXiv 2001.00288 (AAAI-IPA 2020) | Closest prior work: exactly invoice↔PO line-item matching in procure-to-pay. | **No** — proprietary "real-world data sets". |
| **DocILE** — arXiv 2302.05658 | Extraction backbone (KILE/LIR); synthesis pipeline reference. | Yes (Group A). |
| **VRDU** — arXiv 2211.15421 | Hierarchical line-item extraction + matching-based eval methodology. | Yes (Group A). |

---

## Recommendation

1. **Keep the match-discrepancy labels synthetic, but seed from a real linked substrate** — don't build the
   skeleton from scratch. No public source ships labeled invoice↔PO discrepancies, but linked invoice↔PO
   data exists in several forms (see *at a glance* table). **Pick by the modality you need:**
   - **Document modality** → **Company Documents Dataset** (invoice/PO PDFs + text, linked by Order ID, Apache-2.0).
   - **Tabular / `oltp_db` modality** → **Oracle Migration** (clean, maps 1:1 to the task DB; used by the generator),
     **SAP BigQuery** (authentic schema, MIT, but cryptic + large), or **Vendor Performance Analysis** (CC0, real,
     `PONumber`-linked invoice↔purchase).
   Then **inject the spec's fault taxonomy** (price/total delta >$500, qty mismatch, off-contract item/fee,
   duplicate; trivial roundings + sub-$500 deltas as negatives) and label.
2. **Pull the contract layer + match vocabulary** the linked sources lack from **Purchase Orders & Supplier
   Performance** (`Contract Type`, `Invoice Match Type` 3-Way/2-Way/No-Match, payment terms, budget-vs-actual).
3. **Adopt the PO-GRN-INV notebook's generation recipe** for variety the substrates lack (scenario
   cardinalities, partial-delivery/split cases, fuzzy line-item matching) — swap Gemini→Claude.
4. **Borrow labeling design** from **AI-Powered ERP AR Reconciliation** (`Likely Cause` rationale, FX/rounding
   faults) and the labeled `fraud_flag`/`price_difference` columns in **Procurement & Payroll Fraud** — both
   match our "flag + short reason" output contract.
5. **Ground document/visual realism** (if rendering invoices/POs) ← **DocILE** (layouts/fields), SROIE/CORD
   (receipts), **Procurement Invoice Fraud Dataset** (45k rendered invoice images). Company Documents already
   provides linked invoice/PO PDFs with no rendering needed.
6. **Payment-timeliness (prompt remittance)** ← **Payment Date Prediction** (real AR `due`/`clear` dates),
   **Payment Practices of UK Buyers**, **Vendor Performance** `PayDate`, **Canadian Govt Open Transactions**.
7. **Optional fraud/anomaly axis** (duplicate / inflated / split / ghost-supplier) as a *secondary* difficulty
   dimension ← Procurement Invoice Fraud Dataset + Procurement & Payroll Fraud — distinct from PO matching.
8. **License flags:** prefer the permissive substrates for anything shippable — **Company Documents** (Apache-2.0),
   **SAP** (MIT), **Vendor Performance** (CC0). **Unknown:** Oracle Migration, Procurement & Payroll Fraud;
   **CC BY-NC:** Payment Date Prediction (research-only). Confirm terms before commercial release, or regenerate
   (the generator's `--synthetic` mode sidesteps third-party licenses entirely).

## Open items / not yet covered
- **Oracle Migration license is "Unknown"** — needs a terms check before committing derived data; the
  generator (below) supports a `--synthetic` mode that sidesteps this by generating an owned substrate.
- **Extend the generator to the document modality** — read the Company Documents text CSV, pair invoice/PO by
  Order ID, inject faults on the invoice side (today it seeds from the Oracle tabular substrate only).
- **SAP BigQuery deep join not run** — schema + `rseg→ekpo` linkage verified, but a full PO↔invoice↔payment
  join (and whether any organic discrepancies exist in the IDES data) hasn't been computed.
- **Fraud Dataset `behavioural_features` & `splits` tables not yet inspected**; inspect if we adopt the fraud axis.
- Govt portals (USASpending / UK "spend over £25k" / EU TED) noted but not pulled.
- **Web search was unavailable when this survey was run** — discovery was done via the Kaggle public API
  across many query terms + arXiv/HF page fetches; a future pass with working web search may surface more.

---

## Produced artifacts & how to reproduce (handoff)

This survey produced a **working, validated eval-seed generator** that implements the recommendation
(seed the linked P2P substrate → inject the SOP fault taxonomy → emit labeled, schema-conforming cases).

**Artifacts in the repo:**
- `scripts/gen_o2c_eval_and_fixture.py` — the generator. Self-documented; supports `--src <oracle_csv_dir>`
  (use the Oracle Migration substrate) or `--synthetic` (generate an owned substrate, no third-party license).
- `tasks/order_to_cash/` — generated output (only present after you run the generator):
  - `eval_data/<id>/sample.json` — one held-out EvalSample per case (schema/dataset.schema.json
    `$defs/eval_sample`), directory name == the sample's `data_point_id`.
  - `seed_data/labels.csv` — human-reviewable ground truth (id → flagged / fault_type / reason). Sits
    above both phase directories so it is bundled into no container.
  - `environment/eval/fixture/{invoices.csv, po_lines_used.csv, contracts.json}` — the full store the
    oracle refers to (1,095 invoices).
  - `environment/build/fixture/{invoices.csv, po_lines_used.csv, contracts.json}` — the carved build
    share (662 invoices), all the AppSci agent sees. Four fault types are withheld from it on purpose;
    see `KNOWN_ISSUES.md` §10, and check the carve with `python scripts/carve_guard.py order_to_cash`.
  - `seed_data/PROVENANCE.md` — substrate source, seed, per-phase row counts, fault rules
    (auto-written each run).

**Fault taxonomy the generator injects** (mirrors `TASK_SOP.md`; constants `MATERIAL_DELTA=500`,
`ROUNDING_TOL=3` at the top of the script):
- positives: `PRICE_DELTA` (unit-price delta → total > $500), `TOTAL_MATH` (amount ≠ qty×price by > $500),
  `QTY_MISMATCH` (invoiced qty ≠ PO qty), `OFF_CONTRACT_FEE` (extra fee not in contract),
  `OFF_CONTRACT_ITEM` (item not in supplier contract), `DUPLICATE` (second invoice for same PO line).
- hard negatives (must NOT flag): `CLEAN`, `ROUNDING` (≤ $3), `SUBTHRESHOLD_PRICE` (delta ≤ $500).

**To reproduce from scratch:**
1. Get the substrate (one of):
   - **Oracle Migration** — Kaggle `anjaliminakshi/oracle-migration-dataset` (8 CSVs: vendors, materials,
     pr_headers, pr_lines, po_headers, po_lines, goods_receipts, invoices; ~292 KB; **license Unknown**).
     Download via `kaggle datasets download -d anjaliminakshi/oracle-migration-dataset` (needs a Kaggle API
     token at `~/.kaggle/kaggle.json`) and unzip to a directory, e.g. `/tmp/omd/`.
   - or skip the download entirely and use `--synthetic`.
2. Run: `python3 scripts/gen_o2c_eval_and_fixture.py --src /tmp/omd --n 60 --seed 20260615 --background all`
   (or `python3 scripts/gen_o2c_eval_and_fixture.py --synthetic --n 60`). Omitting `--background all` yields
   a store of only the 60 scored cases, which is not what is committed.
3. Validate: `python -m appsci_bench.core.spec order_to_cash` loads every `eval_data/<id>/sample.json`
   through the real dataset loader (`appsci_bench.eval.dataset`), which enforces the EvalSample shape
   directly -- no separate schema-file check needed.

**Suggested next steps to continue the work:**
- Wire the `fixture/` into the task's `oltp_db` (load CSVs into the sandbox Postgres) so cases run end-to-end.
- Scale `--n` up; add line-item-level POs (current substrate is one item per PO line) and partial-delivery
  / split scenarios using the **PO-GRN-INV notebook** recipe (Group D / find #2).
- If document modality is required, render fixture rows → invoice/PO PDFs (ReportLab, per the notebook).
- Confirm Oracle Migration's license, or switch the committed seed to `--synthetic`.
- Borrow the `Likely Cause` rationale style from **AI-Powered ERP AR Reconciliation** for richer reasons.

---
*Survey + generator produced 2026-06-15. Discovery tools: Kaggle public API (`/datasets/list`,
`/datasets/view`, unauthenticated), arXiv & Hugging Face page fetches, project docs. Web search was
unavailable when run. License/size fields are as reported by source catalogs — verify each card before use.*
