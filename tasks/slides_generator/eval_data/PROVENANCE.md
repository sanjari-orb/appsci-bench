# Provenance — `slides_generator` eval samples

This file records where the source material for each eval sample came from. It covers the two
sample families under `eval_data/`: `generated-sample-*` and `product-slidesgen-*`.

## Product samples (`product-slidesgen-01..05`)

Entirely simulated by Claude, by a member of a product team. No real customer data — content, requests,
and grading targets are synthetic. Each sample's `template.pptx` is based on an anonymized version
of a real corporate template.

## Generated samples (`generated-sample-00..04`)

Built from open-source documents. Templates for this family come from the Zenodo10K dataset
(sourced via PPTAgent, see `tasks/slides_generator/README.md`'s Public Datasets table). Each
sample's gold was generated using the sample's template, then via Microsoft Copilot in PowerPoint,
followed by manual curation.

| Sample | Source document(s) | Notes |
|--------|---------------------|-------|
| `generated-sample-00` | SEC EDGAR: Apple Inc. (AAPL), CIK 001-36743, DEF 14A (Proxy Statement), filed 2026-01-08 / period 2026-02-24 (`Apple Inc. - DEF 14A.pdf`) | Document is a cropped subset of pages from the filing — no other alteration. |
| `generated-sample-01` | [Zenodo record 17256797](https://zenodo.org/records/17256797) — *SECreTour - Sustainable, Engaging and CREative Tourism as a driver for a better future in rural and remote areas* (EU Open Research Repository) | No separate grounding document: `prior-deck.pptx` (the deck this sample tests re-templating against) **is** the source — a PDF-to-PPTX export of the cited SECreTour presentation, cropped. There is no additional input file for this sample. |
| `generated-sample-02` | [Zenodo record 8262919](https://zenodo.org/records/8262919) — two files derived from it: `BECoop_Potential for the Bioenergy Market Expansion.xlsx` (data taken directly from the record) and `GENERATED_PROJECT_NAME_Research_Introduction.docx` (generated narrative, using the record's abstract as source material) | The `.docx` is a generated document, not an extract; a deliberate mismatch was introduced between the two files as the sample's test condition. |
| `generated-sample-03` | [Zenodo record 21871990](https://zenodo.org/records/21871990) — *An Integrated Economics–IT–Management Framework for Creating, Governing, and Measuring Digital Value in Organizations* (Nayab, Anwer; published 2026-06-10, v1) | Used as obtained; no cropping/alteration noted. |
| `generated-sample-04` | SEC EDGAR: Alphabet Inc. Q2 2026 earnings release ([exhibit 99.1](https://www.sec.gov/Archives/edgar/data/1652044/000165204426000066/googexhibit991q22026.htm)) | A subset of pages was used (`google.pdf`); data was stripped from the filing and loaded into `Alphabet_Q2_2026_Financials.xlsx`, creating two separate source files (PDF + spreadsheet) for this sample. |

`generated-sample-00`'s and `generated-sample-04`'s SEC filings and `generated-sample-01`–`03`'s
Zenodo records are all publicly available at the time of collection. No customer or confidential
data is included in any generated sample.
