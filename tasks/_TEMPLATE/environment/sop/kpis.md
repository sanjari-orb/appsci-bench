# <Task Name> Success Criteria

## What <the customer role> Needs

<Plain-language description of the customer's pain and what a good system does about it, in 2-3
short points -- not a scoring spec. See tasks/order_to_cash/environment/sop/kpis.md for a filled-in
example of this style.>

1. **<need 1>** - <why it matters to them>
2. **<need 2>** - <why it matters to them>

## Key Metrics

<For each metric the customer cares about, one short paragraph in plain business language -- what
it measures and why it matters. This is what the agent reads while building; it is NOT the scoring
config (that's spec.yaml's evaluation_data.objectives) and shouldn't use that vocabulary.>

**<Metric 1 name>**
<What it measures and why the customer cares.>

**<Metric 2 name>**
<What it measures and why the customer cares.>

## Processing Speed

<Latency/throughput expectation in plain terms, if relevant.>

## Important Constraints

- <A hard boundary on what the system may/may not do, e.g. "flags for review, never auto-decides">
- <A trade-off the customer wants respected, e.g. "better to flag something questionable than miss a real issue">
