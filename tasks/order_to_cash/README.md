# Context

An applied-scientist agent is given a vague user request of the form "Our accounts-payable team can't keep up checking supplier invoices against what we agreed to buy, and problems slip through. Build something that reviews incoming invoices and flags the ones a human should stop and review." and must elicit requirements from L1/L2/L3 personas before building a discrepancy detector system artifact. The system artifact is then evaluated by the Oracle: it receives an invoice ID and is expected to return a flag decision with reason that can be scored against the gold solution.

## BUILD TIME
```
applied-scientist agent  ← vague request + persona sim (L[*] persona + task spec)
                        → order-to-cash discrepancy detector system artifact
```

## TASK RUN TIME
```
discrepancy detector system artifact ← invoice ID (references DB for invoice/PO/contract data)
                                     → {flagged: bool, reason: string}
```

## EVAL TIME (Oracle)
```
Oracle → system artifact ← eval sample (invoice ID)
                        → measure of how close to gold solution the system artifact was
```

The system artifact is a queryable agent (kind=agent, primary). An optional design_notes.md may be produced as a documentational artifact.

## Vague Request

The applied-scientist agent receives only this brief at the start of a run:

> "Our accounts-payable team can't keep up checking supplier invoices against what we agreed to buy, and problems slip through. Build something that reviews incoming invoices and flags the ones a human should stop and review."

Everything else: materiality thresholds, fault types, output format, evaluation criteria must be elicited from the available personas.

## Deliverable

Order-to-cash discrepancy detector spec encompasses all the details necessary to execute the task at build time and eval time. Including:

### Task Definition Spec

Given the defined SOP, a specification is provided covering the fault taxonomy which is aligned with the gold solution design.

### Task Personas Specs

Task definition is shared across user personas L1/L2/L3, but a task persona file is created as input for the persona simulator. These specs are consumed at build time.

### Eval Specs

Designed to provide an estimation of what is considered a good result of a test of the system artifact at runtime.

- Validation over functional requirements (flag accuracy)
- Per-fault-type recall evaluation
- False positive rate monitoring
- Reason quality assessment (LLM judge)

## System Artifact (Discrepancy Detector) I/O Contract

### Input

An invoice identifier that references:
- Invoice record (invoice_number, po_line_id, supplier_id, item_id, invoiced_qty, unit_price, amount, extra_fees, duplicate_of)
- PO line record (po_number, quantity, unit_price)
- Contract record (allowed_item_ids, agreed_unit_price, allowed_fees)

All data is available via PostgreSQL database queries.

### Output

A JSON object with flag decision and reason:

```json
{
  "flagged": true,
  "reason": "unit price 148.14 != PO 137.08 (total delta $3174.22 > $500, material)"
}
```

### Deployment Constraints

- Database access via PostgreSQL connection
- SLO: 2000ms per invoice review

## User Personas

Three personas are defined for this task across three ambiguity levels.

| Level | Persona | Key Knowledge | Refusal |
|-------|---------|---------------|---------|
| **L1** | Applied-science manager | Recommends evaluation criteria, approach trade-offs, knows about class imbalance, metric design. | None |
| **L2** | Technical partner | Database schema, integration constraints, deployment pipeline. | Refuses AI-method choice, prompt design, evaluation strategy. |
| **L3** | PM / AP director | Business pain, what "material" means via examples, who reviews. | Does NOT speak to thresholds/architecture. |

## Referent Data Resources

Available inside the appsci-bench/order_to_cash container (agent-facing; distinct from the hidden held-out eval set):

| Resource | Type | Location |
|----------|------|----------|
| OLTP Database | database | `postgres://sandbox:sandbox@db:5432/oltp` |
| Contract terms | docs | `/sop/contract_terms.md` — validation rules |
| KPIs | kpi | `/sop/kpis.md` — success metrics |

## Evaluation Metrics

All objectives run against the held-out `eval_data/` (60 sample directories, spanning all fault
types). The agent gets no labeled examples at build time -- it has to derive the decision rule from
the stakeholder and the database.

The database it explores during BUILD holds **1,095 invoices**, of which those 60 are the scored
ones. The other 1,035 are unlabelled background volume: without them the database contained exactly
the eval set and nothing else, so browsing it enumerated every case the artifact was about to be
scored on. They are not a build/eval environment split -- both phases still load the same dataset --
they are the volume that makes such a split possible. See
[`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) for what the background does and does not fix.

| Objective | Weight | What It Measures |
|-----------|--------|------------------|
| `flag_accuracy` | 0.3 | Binary accuracy of flag decisions, over all 60 cases |
| `flag_recall` | 0.3 | Share of real faults caught — `TP / (TP + FN)`, over the 41 cases the oracle says to flag |
| `fault_classification` | 0.2 | When flagged correctly, does the artifact's declared `fault_type` match the oracle's? |
| `reason_quality` | 0.2 | Does the reason correctly identify the discrepancy? |

**`flag_recall` was called `flag_fnr` until it was renamed, and reports written before the rename key it
under the old name.** The number is unchanged — it always computed recall, not a false-negative rate
(1.0 for a catch, `positive: true`), and its docstring used to carry a paragraph explaining the
discrepancy. Any analysis reading across runs from both sides of the rename needs to treat the two names
as one objective, or it will show the metric as absent for half the runs.

All four eval functions live in `tasks/order_to_cash/eval.py`. The first three are mechanical: no LLM,
no cost, no judge variance. `fault_classification` compares the `fault_type` the artifact **declared**
against the oracle's, case- and whitespace-insensitively; a missing, blank, or non-canonical value
scores 0. (It previously ran an LLM judge that inferred a type from the artifact's free-text reason,
which left the declared field unread and let an artifact that never emitted one score full marks --
and `reason_quality` already scores the explanation.) `reason_quality` is the one judge left, a
transcript judge (Level 1, `make_transcript_judge`).

**A reply with no structured payload is a contract failure, not an opinion.** The response contract --
a JSON object carrying `flagged` / `fault_type` / `reason` -- is stated to the agent during BUILD, and the
transport fills the structured slot only when the whole reply *is* that object. So an empty slot means
the artifact answered in some other shape: all three mechanical objectives score 0 on that data point
with the reason recorded. In particular an absent flag is never read as "not flagged", which would
otherwise agree with the oracle on all 19 clean invoices and hand a non-compliant artifact the
not-flagged base rate for free.

## Fault Taxonomy

"In build share" is the difficulty decision recorded in `KNOWN_ISSUES.md` §10, not a statement about
what is scored — all nine are present in the eval store and all six positives are scored. A category
marked *no* has no example anywhere in the build environment, so its rule has to be elicited from the
stakeholder rather than inferred from data. This file is host-side and is mounted into no container.

| Fault Type | Should Flag | In build share | Description |
|------------|-------------|----------------|-------------|
| PRICE_DELTA | Yes | Yes | Unit price delta results in total delta > $500 |
| QTY_MISMATCH | Yes | Yes | Invoiced quantity ≠ PO quantity |
| DUPLICATE | Yes | **No** | Duplicate invoice for same PO line |
| OFF_CONTRACT_FEE | Yes | **No** | Fee not covered by contract |
| OFF_CONTRACT_ITEM | Yes | **No** | Item not in supplier's allowed items |
| TOTAL_MATH | Yes | **No** | Invoice total ≠ qty × price by > $500 |
| CLEAN | No | Yes | Matches PO and contract |
| ROUNDING | No | Yes | Trivial rounding ≤ $3 |
| SUBTHRESHOLD_PRICE | No | Yes | Price delta but total delta ≤ $500 |

## Budget

| Resource | Build Limit |
|----------|-------------|
| Turns | 40 |
| Tokens | 20,000,000 |
| Cost | $20 |
| Wall clock | 10,800 s |
| Latency SLO | 2,000 ms |

## Ownership

**Owner**: a contributor
**Co-owners**: a contributor
