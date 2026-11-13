# Artifact Design

## Overview

The **Applied Scientist Agent** is responsible for the full end-to-end implementation:
1. Gather requirements (via user sim)
2. Explore the environment (DBs, APIs, docs, sample data)
3. Design the agent logic
4. Write all code (data processing, tool integration, agent execution)
5. **Expose an endpoint** the benchmark can call

The benchmark's role is simple: **call the endpoint** with eval samples and score the responses.

---

## What the Applied Scientist Delivers

```
┌─────────────────────────────────────────────────────────────────┐
│  Applied Scientist Agent                                        │
│                                                                 │
│  1. Gathers requirements from user sim                          │
│  2. Explores environment (DBs, APIs, KBs, sample data)          │
│  3. Writes:                                                     │
│     • Agent prompts/logic                                       │
│     • Data processing code                                      │
│     • Tool integrations                                         │
│     • Execution harness                                         │
│  4. Exposes: ENDPOINT (MCP server or completions API)           │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────┐
                    │    ENDPOINT     │
                    │  (MCP or API)   │
                    └─────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Benchmark Evaluator                                            │
│                                                                 │
│  • Loads eval samples                                           │
│  • Calls endpoint with each sample                              │
│  • Compares response to oracle                                  │
│  • Scores via Judges                                            │
└─────────────────────────────────────────────────────────────────┘
```

---

## Endpoint Contract

The Applied Scientist exposes an endpoint that accepts queries and returns responses.

### Request (from Benchmark)

```json
{
  "intended_task": "Review invoice INV-2024-1847 against its PO and flag any discrepancies",
  "customer_info_access": ["db://invoices", "db://pos", "db://contracts"],
  "context": {}
}
```

### Response (from Artifact)

```json
{
  "output": {
    "flag": true,
    "reason": "Unit price mismatch: invoice shows $45.00, PO shows $42.50. Delta $2.50 x 200 units = $500 exceeds threshold."
  },
  "messages": []
}
```

### Protocols

| Protocol | Endpoint Format |
|----------|-----------------|
| **MCP** | MCP server exposing a `respond` tool |
| **Completions** | OpenAI-compatible `/v1/chat/completions` endpoint |

---

## What the Applied Scientist Builds

For a task like `order_to_cash`, the Applied Scientist writes:

### 1. Agent Logic (prompts + strategy)

```python
SYSTEM_PROMPT = """
You review supplier invoices against POs and contracts.
Flag invoices with material discrepancies for human review.

Material discrepancy:
- Price/total delta > $500
- Any out-of-contract charge

Ignore trivial rounding differences.
"""

STRATEGY = """
1. Parse invoice to extract: po_number, line_items, unit_prices, total
2. Query PO by po_number
3. Compare unit_price, quantity for each line item
4. If contract_id exists, verify charges against contract.allowed_charges
5. Calculate total delta
6. Return {flag: bool, reason: string}
"""
```

### 2. Data Processing Code

```python
def parse_invoice(invoice_data: dict) -> dict:
    """Extract relevant fields from raw invoice."""
    return {
        "po_number": invoice_data["po_reference"],
        "line_items": invoice_data["lines"],
        "total": sum(line["amount"] for line in invoice_data["lines"])
    }

def compare_to_po(invoice: dict, po: dict) -> list[dict]:
    """Find discrepancies between invoice and PO."""
    discrepancies = []
    for inv_line in invoice["line_items"]:
        po_line = find_matching_line(po, inv_line["sku"])
        if po_line and inv_line["unit_price"] != po_line["unit_price"]:
            discrepancies.append({
                "field": "unit_price",
                "invoice": inv_line["unit_price"],
                "po": po_line["unit_price"],
                "delta": abs(inv_line["unit_price"] - po_line["unit_price"])
            })
    return discrepancies
```

### 3. Tool Integration

```python
def query_database(query: str) -> list[dict]:
    """Execute SQL against the task environment DB."""
    conn = get_env_connection("postgres://sandbox/oltp")
    return conn.execute(query).fetchall()

def lookup_po(po_number: str) -> dict:
    return query_database(f"SELECT * FROM pos WHERE po_number = '{po_number}'")[0]

def lookup_contract(contract_id: str) -> dict:
    return query_database(f"SELECT * FROM contracts WHERE id = '{contract_id}'")[0]
```

### 4. Agent Execution

```python
def run_agent(query: ArtifactQuery) -> ArtifactResponse:
    """Main agent loop — called by the endpoint."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + STRATEGY},
        {"role": "user", "content": query.intended_task}
    ]

    while True:
        response = llm.chat(messages, tools=TOOLS)

        if response.tool_calls:
            for call in response.tool_calls:
                result = execute_tool(call.name, call.args)
                messages.append({"role": "tool", "content": result})
        else:
            return ArtifactResponse(output=parse_final_answer(response.content))
```

### 5. Endpoint

```python
# MCP server or FastAPI app exposing the artifact

@app.post("/respond")
def respond(query: ArtifactQuery) -> ArtifactResponse:
    return run_agent(query)
```

---

## Artifact Checkpoints

Every deploy the agent asks for is copied, before its next edit can overwrite the directory.

The artifact directory is a single mutable directory bind-mounted into the agent's container: each
deploy builds whatever is in it at that moment, and only whatever the agent happens to leave behind at
the end outlives the run. Once the optimization stage iterates, that directory holds a succession of
distinct candidates and all but the last are lost — so no candidate can be rebuilt or scored later,
and one whose deploy failed leaves no trace at all.

| | |
|---|---|
| **Where** | `runs/<run>/checkpoints/<artifact-container-name>-<NNN>/`, plus `runs/<run>/checkpoints_manifest.json` beside it. `<run>` is the trace id when observability is on, else the run's own timestamped directory — capture does not require observability |
| **When** | Every deploy **attempt** through the agent's deploy route, successes and failures alike |
| **Contents** | The artifact's own files and nothing else, minus dependency and build trees — the container snapshot's existing exclusion list (`appsci_bench/core/agent_container.py`: caches, `.git`, `node_modules`, `.venv`, `dist`, `build`, …) plus `*.pyc`. Reused rather than reinvented, since it's the same problem on the same directory. Every hash is taken with the same exclusions on both sides, or a pruned copy would never match the live directory it came from |
| **Naming** | The task's artifact container name plus a zero-padded deploy ordinal, so candidates sort in deploy order and a checkpoint copied out of its run still says what it is |
| **Mirroring** | Written locally, then pushed to the run's S3 mirror **synchronously**. A failed upload fails the run: a candidate reachable only on the host that built it is not one a later pass can read. So a live AWS session is a prerequisite for running at all, and a slow upload delays the deploy response. Runs with no mirror configured skip it |
| **Visibility** | None. The agent gets only its own observability log *files* mounted, never the run directory |

Three properties are deliberate and easy to undo by accident:

**Only the agent's deploy route captures.** Three places build an artifact container: that route, the
forced redeploy at the build→eval handover, and the eval-only path that stands a pre-built directory
up (also used by each trial of a replicate sweep). Only the first is an agent decision. Hooking the
shared deployer instead would add a spurious checkpoint at every handover duplicating the previous
one, would checkpoint a checkpoint when a later pass deploys one to score it, and would run where
there is no run directory to write into.

**Failed attempts are kept.** That source is exactly what the next attempt destroys, and it is never
scored, so the files are the only evidence the candidate existed. The manifest records which of the
three outcomes each capture had: `deployed`, `unhealthy` (answers, but its health route did not return
200), or `failed`.

**Nothing goes inside a checkpoint.** A checkpoint directory *is* the Docker build context when it is
rebuilt, so a metadata file dropped inside would ship into the image and change the directory's
content hash away from what was actually deployed. The manifest holds the metadata instead: per
capture, the ordinal, outcome, content hash, base-image digests, and file counts — plus a `submitted`
marker naming the checkpoint matching the artifact the run submitted. That marker is sometimes
**none**: an agent that edits after its last deploy submits something no checkpoint holds, which is the
case the pre-eval staleness check exists to catch.

**A failed capture fails the run.** A missed copy is not a missed diagnostic: the artifact directory is
overwritten by the next deploy, so the candidate is gone and the meta-eval it was captured for would
draw its conclusions from a silently incomplete set. A lost run can be re-run; an undetectably partial
result cannot. So the run aborts, and the deploy response stays truthful — the agent is not told its
code broke when the host's disk did, which would have it rewrite working code on the way out.

This is deliberately *not* observability's "never break a real run" rule. That rule is conditioned on
never losing data — the run mirror can swallow a failure because the run is already complete on disk
and one idempotent command re-uploads it. Nothing can re-read an overwritten artifact directory.

The **upload is held to the same bar**: a checkpoint is not captured until it is in the mirror, so the
push is synchronous and a failure ends the run. "Copied locally but never uploaded" is treated as not
captured rather than as a deferred upload, because the later pass cannot read a candidate that exists
only on the machine that built it.

### Joining a score back to its checkpoint

The agent scores its own candidates during BUILD (`optimize_agent`), and every score it records names
the checkpoint the running container was built from, so a number can be traced to the exact files that
produced it after the run is over. The join key is the **content hash** in the manifest.

The agent cannot work that out for itself: checkpoints live under the run directory, which is never
mounted into its container. **`GET /deploy/status`** is the read-only route that answers, reporting
what is deployed, whether the artifact directory has changed since (`stale`), and the matching manifest
entry (`checkpoint`, or `null` when there honestly is no match).

Two things make that join hold, and both are easy to break by accident:

- **Nothing the optimizer writes goes into the artifact directory.** The score history lives in the
  agent's workspace (`<workspace>/.optimize/score_history.json`) and refers back to a candidate by the
  checkpoint id recorded in the manifest. Writing it next to the code instead would give the same
  unchanged candidate a new hash on every measurement — reporting the agent as having abandoned work it
  never touched, and detaching each score from its checkpoint. Keeping it out is what lets the
  fingerprint stay exactly as it is, with no exclusion for the eval harness or anything else.
  `/workspace` is copied out of the container at the end of a run, so the history outlives the run.
- **The lookup re-hashes the live directory rather than reusing the staleness fingerprint.** Those two
  hashes use different exclusion sets (the staleness one keeps dependency and build trees; the
  checkpoint one drops them), so they differ on any run where the agent left a `.venv` or
  `node_modules` in its output directory. Keying the lookup off the staleness fingerprint would return
  `null` on exactly the runs where an agent installed its own dependencies.

Scoring checkpoints *after the run* is deliberately **not** part of this: it happens in a later pass,
off the critical path of a scored run.

**Capture is independent of observability**, and so is the marker. Every run keeps its candidates,
including the plain command-line run that passes no `--enable-observability`; what an observability-off
run loses is the S3 mirror, which is a deliberate configuration rather than a degraded one. Keying the
marker on the trace id instead of the run directory would leave exactly those runs holding a set of
candidates with no record of which one was delivered — the one question the manifest is read for later.

## Separation of Concerns

| Concern | Owner |
|---------|-------|
| **Requirements gathering** | Applied Scientist |
| **Environment exploration** | Applied Scientist |
| **Agent design (prompts, logic)** | Applied Scientist |
| **Data processing code** | Applied Scientist |
| **Tool integrations** | Applied Scientist |
| **Execution harness** | Applied Scientist |
| **Endpoint** | Applied Scientist |
| **Evaluation** | Benchmark |

---

## What This Means

The Applied Scientist is evaluated on **full implementation quality**:
- Did they understand the requirements?
- Did they explore the environment correctly?
- Did they write working code?
- Does their agent produce correct outputs?

The benchmark only provides:
- Task spec (brief, environment resources, user personas)
- Eval samples (held-out test cases with oracles)
- Scoring (judges)
