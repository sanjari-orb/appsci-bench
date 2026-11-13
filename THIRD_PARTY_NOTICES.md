# Third-party notices

Some tasks are built on public research datasets. Their licenses and required attributions are below.
Each task's own provenance file records exactly what was taken and how it was converted.

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
repository is our conversion of it, and is distributed under this repository's license to the extent
we hold rights in it. No license is granted for the original dataset, which is not redistributed here
in its original form.
