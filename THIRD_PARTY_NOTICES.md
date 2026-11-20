# Third-party notices

This repository's code is licensed under the Apache License 2.0 (`LICENSE`), and its benchmark data
under CC BY-NC 4.0 (`LICENSE-DATA`). Some of that data is adapted from the third-party sources below.
Their attribution requirements still apply, and the files listed under
[Files under their original license](#files-under-their-original-license) are distributed under
their source's license instead of CC BY-NC 4.0. Each task's own provenance file records exactly what
was taken and how it was converted.

The CRMArena data is itself CC BY-NC 4.0, so the benchmark data as a whole may be used for
non-commercial purposes only.

## τ³-bench (`airline_rebooking`, `banking_support`)

Source: https://github.com/sierra-research/tau2-bench, release v1.0.1, which includes the τ³
task-quality pass.
Provenance: [`provenance/airline/`](provenance/airline/), [`provenance/banking_support/`](provenance/banking_support/).

```
MIT License

Copyright (c) 2025 Sierra Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## CRMArena (`crm_insights`)

Source: https://github.com/SalesforceAIResearch/CRMArena and the Hugging Face dataset
`Salesforce/CRMArena`, by Salesforce AI Research. Huang et al., "CRMArena: Understanding the Capacity
of LLM Agents to Perform Professional CRM Tasks in Realistic Environments", arXiv:2411.02305.
Provenance: [`tasks/crm_insights/seed_data/PROVENANCE.md`](tasks/crm_insights/seed_data/PROVENANCE.md).

License: **Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)**,
https://creativecommons.org/licenses/by-nc/4.0/. The query and answer pairs were filtered to six task
categories, and CRM records were extracted from CRMArena's database into fixture files. **This data
may be used for non-commercial purposes only.**

## CUAD (`compliance_review`)

Source: https://github.com/TheAtticusProject/cuad, CUAD v1, by The Atticus Project. Hendrycks, Burns,
Chen and Ball, "CUAD: An Expert-Annotated NLP Dataset for Legal Contract Review", NeurIPS 2021
Datasets and Benchmarks, arXiv:2103.06268.
Provenance: [`tasks/compliance_review/seed_data/PROVENANCE.md`](tasks/compliance_review/seed_data/PROVENANCE.md).

License: **Creative Commons Attribution 4.0 International (CC BY 4.0)**,
https://creativecommons.org/licenses/by/4.0/. Contract excerpts and clause annotations were adapted
into policy-violation review cases. The underlying contracts are public SEC EDGAR filings.

## Order-to-cash source data (`order_to_cash`)

Source and conversion: [`provenance/order_to_cash/`](provenance/order_to_cash/). The source is a
synthetic procure-to-pay dataset published on Kaggle with **no license stated**. The version in this
repository is our conversion of it, and is distributed under this repository's data license
(CC BY-NC 4.0) to the extent we hold rights in it. No license is granted for the original dataset,
which is not redistributed here in its original form.

## Slides sources (`slides_generator`)

Provenance: [`tasks/slides_generator/eval_data/PROVENANCE.md`](tasks/slides_generator/eval_data/PROVENANCE.md).

The `product-slidesgen-*` samples were written for this benchmark. The `generated-sample-*` samples
use the following third-party documents:

| Sample | Source | License |
|---|---|---|
| `generated-sample-00` | Apple Inc., DEF 14A proxy statement filed 2026-01-08, from SEC EDGAR. A subset of its pages (`Apple Inc. - DEF 14A.pdf`). | public SEC filing |
| `generated-sample-01` | Viktor Smith (Copenhagen Business School), *What makes a place to visit a "place to visit"?*, [Zenodo record 17256797](https://zenodo.org/records/17256797). Converted from PDF to PPTX and cropped (`prior-deck.pptx`). | CC BY-SA 4.0 |
| `generated-sample-02` | Chapizanis, Kalimeri, Kostopoulos, Chamalaki et al., *EU-Level Crowdsourced Survey Data on Biomass Communities, BECoop project*, [Zenodo record 8262919](https://zenodo.org/records/8262919). Survey responses taken directly from the record (`BECoop_Potential for the Bioenergy Market Expansion.xlsx`); the accompanying `.docx` is generated text based on the record's abstract. | CC BY 4.0 |
| `generated-sample-03` | Anwer Nayab, *An Integrated Economics–IT–Management Framework for Creating, Governing, and Measuring Digital Value in Organizations*, [Zenodo record 21871990](https://zenodo.org/records/21871990). Used unmodified (`DVAM_Digital_Value_Alignment_Model (2).pdf`). | CC BY 4.0 |
| `generated-sample-04` | Alphabet Inc., Q2 2026 earnings release, [exhibit 99.1](https://www.sec.gov/Archives/edgar/data/1652044/000165204426000066/googexhibit991q22026.htm), from SEC EDGAR. A subset of its pages (`google.pdf`), and figures taken from it (`Alphabet_Q2_2026_Financials.xlsx`). | public SEC filing |

The `template.pptx` files of the `generated-sample-*` samples are derived from presentations in
Zenodo10K, collected from Zenodo by PPTAgent (Zheng et al., "PPTAgent: Generating and Evaluating
Presentations Beyond Text-to-Slides", arXiv:2501.03936, https://github.com/icip-cas/PPTAgent). Each
template keeps the license and attribution of its original Zenodo record.

## Files under their original license

These files are distributed under their source's license, not under CC BY-NC 4.0:

- `tasks/slides_generator/eval_data/generated-sample-01/prior-deck.pptx`: CC BY-SA 4.0
  (https://creativecommons.org/licenses/by-sa/4.0/), adapted from Zenodo record 17256797 as
  described above.
- The `generated-sample-*` `template.pptx` files: the license of each one's original Zenodo record.
