# Applied AI Scientist Agent Design

## 1. Overview

The Applied AI Scientist (AppSci-Agent) is a **meta-agent**: its job is not to answer user queries directly, but to **build another agent** that does.

### Architecture

The benchmark uses a **3-container architecture**:

```
+--------------------------------------------------------------------------------------+
|                                  BENCHMARK SYSTEM                                    |
+--------------------------------------------------------------------------------------+
|                                                                                      |
|  +-----------------------------+    +-------------------+    +-----------------------+
|  |       TASK CONTAINER        |    |    APPSCI-AGENT   |    |       ARTIFACT        |
|  |                             |    |     CONTAINER     |    |       CONTAINER       |
|  |  +-----------------------+  |    |                   |    |                       |
|  |  |       RESOURCES       |  |    |  The meta-agent   |    |  The agent produced   |
|  |  |  * Database           |  |<-->|  being            |--->|  by AppSci-Agent      |
|  |  |  * Knowledge Base     |  |    |  benchmarked      |    |                       |
|  |  |  * APIs               |  |    |                   |    |  Serves:              |
|  |  |  * Sample Data        |  |    |  Produces:        |    |  * HTTP endpoint      |
|  |  |  * Documentation      |  |    |  * Dockerfile     |    |                       |
|  |  +-----------------------+  |    |  * Agent code     |    |                       |
|  |                             |    |  * Endpoint spec  |    +-----------------------+
|  |  +-----------------------+  |    +-------------------+              ^
|  |  | APPSCI AGENT USER SIM |  |            ^                          |
|  |  |    Answers questions  |-------------> |                          |
|  |  +-----------------------+  |            |                          |
|  |                             |            |                          |
|  |  +-----------------------+  |            |                          |
|  |  |       EVALUATOR       |------------------------------------------+
|  |  |    Queries artifact   |  |
|  |  |    Scores responses   |  |
|  |  +-----------------------+  |
|  |                             |
|  +-----------------------------+
|                                                                                      |
+--------------------------------------------------------------------------------------+
```

### Two-Phase Execution

| Phase | What Happens |
|-------|--------------|
| **BUILD** | AppSci-Agent explores the environment, gathers requirements, builds the artifact, deploys it to the artifact container, and calls `complete` |
| **EVAL** | Benchmark evaluator sends queries to the artifact endpoint and scores responses |

---

## 2. Inputs

The AppSci-Agent receives the following inputs at the start of a task:

### Task Brief

A short, non-revealing description of what the customer needs. Example:

> "A company's accounts-payable team wants help catching problematic supplier invoices before they pay them. Their AP director is available to tell you what they need."

### Environment Resources

Access to the task container, which may include:

| Resource Type | Description | Example |
|---------------|-------------|---------|
| **Database** | SQL/transactional store to query | `postgres://task-container/oltp` (invoices, POs, contracts) |
| **Knowledge Base** | Reference material | Queue definitions, routing rules |
| **APIs** | Operations/actions available | Search, update, refund endpoints |
| **Sample Data** | Labeled examples to learn from | `/data/samples/invoice_po_pairs.jsonl` |
| **Documentation** | Policies, schemas, procedures | `/data/docs/contract_terms.md` |

### User Access

The agent can talk to a user (e.g., a PM, engineer, or domain expert) to gather requirements. The user may or may not be able to answer all your questions depending on their knowledge—if they can't provide what you need, you have to figure it out on your own.

### Budgets

Limits on resources the agent can consume:

```yaml
budgets:
  build:
    turns: 40           # Max agentic turns
    tokens: 2000000     # Max tokens consumed
    usd: 20             # Max API cost
    wall_clock_s: 7200  # Max wall-clock time (2 hours)
  latency_slo_ms: 2000  # Target response latency for the artifact
```

**Budget enforcement:** The benchmark tracks usage and terminates the agent if limits are exceeded.

### System Prompt

The AppSci-Agent receives a system prompt rendered from a **Jinja template**. The template is the same across all tasks, but it contains placeholders that are populated with task-specific values at runtime (e.g., task brief, environment details, budget limits).

The system prompt contains:

- Role definition (you are an Applied AI Scientist)
- High-level instructions (understand, gather requirements, build, test, submit)
- Constraints (budget limits, no eval data access, artifact must be queryable)
- Tool documentation
- Task-specific values injected from the task spec

---

## 3. Action Space (Tools)

> This section documents the agent's original three tools. `deploy_artifact`,
> `eval_generator` and `optimize_agent` landed later; the self-evaluation pair and the
> build-phase scoring loop are described in
> [optimize_agent_design.md](optimize_agent_design.md).

The AppSci-Agent has access to three tools:

### 3.1 `talk_to_user`

Send a message to the user to gather requirements or ask clarifying questions.

> **Absent in the no-user ablation arm.** `--no-user-sim` (sweep key `user_sim_enabled: false`)
> starts no simulator, removes this tool from the registry and from every prompt surface, and mounts
> what the stakeholder would have held read-only in the agent container instead: the task's
> `user_sim_docs/` tree at `/requirements/docs` and its `user_personas/domain_knowledge/` extracts at
> `/requirements/policies`. Same task, same facts, same scoring; only the elicitation is removed. See
> [`tasks/README.md`](../tasks/README.md#the-one-exception-the-no-user-ablation-arm).

**Input:**
```json
{
  "question": "What counts as a 'material' discrepancy?",
  "context": "I see the sample data has various price differences..."
}
```

**Output:**
```json
{
  "answer": "Any price/total delta over $500, or any out-of-contract charge.",
  "persona": "l3_pm_collaborative"
}
```

**Guidelines:**
- Ask focused questions—don't dump a list of 20 questions at once
- The user won't do your job for you—they provide requirements, not solutions

### 3.2 `mini_swe_agent`

Delegate coding and shell tasks to a specialized subagent that handles implementation autonomously.

**How it works:**
- AppSci-Agent provides a high-level task description
- mini_swe_agent executes the task using bash, file operations, and code editing
- mini_swe_agent returns a summary of what was done and any outputs

**Input:**
```json
{
  "task": "Explore the database schema and list all tables with their row counts",
  "context": "I need to understand what data is available before designing the agent"
}
```

**Output:**
```json
{
  "status": "completed",
  "summary": "Found 12 tables in the OLTP database. Key tables: invoices (45,231 rows), purchase_orders (38,102 rows), vendors (892 rows), contracts (156 rows)...",
  "artifacts": ["/workspace/schema_report.md"],
  "stderr": ""
}
```

**Sandboxing:**
- mini_swe_agent runs inside the AppSci-Agent container, but as a second, unprivileged user
  (`swe`) — not as the agent process's root
- The agent framework's own installed code is root-only, so the sub-agent cannot read the harness
  (LLM client and gateway wiring, user-sim client, tool implementations). It is there to write the
  task's code, and what it needs to know about model access is stated in its prompt instead
- The harness's own environment variables (user-sim URL/token, submit URL, observability plumbing)
  are stripped from its commands; the LLM credentials stay, so it can smoke-test the artifact's model
  client before deploying
- Resource limits (CPU, memory, disk)
- Network access to task container and artifact container

**Use cases:**
- Explore the environment (query databases, read files, inspect APIs)
- Write and edit code files
- Build and deploy the artifact to the artifact container
- Run tests and verify the artifact works

**Guidelines:**
- Provide clear, specific task descriptions—the subagent works best with well-defined goals
- Include relevant context (what you've learned, what you're trying to achieve)
- The subagent handles implementation details; you focus on what needs to be built and why

### 3.3 `complete`

Signal that the artifact is built, deployed, and ready for evaluation. Calling this tool ends the BUILD phase and hands off to the orchestrator, which begins the EVAL phase.

**Input:** none — `complete` takes no arguments. The endpoint that gets evaluated is whatever `deploy_artifact` most recently deployed and recorded on the task context; calling `complete` before a successful `deploy_artifact` is an error.

**Output:**
```json
{
  "status": "complete",
  "endpoint_url": "http://artifact-container:8080/respond",
  "message": "Build complete. Evaluation will begin against the deployed endpoint."
}
```

**Notes:**
- Takes no arguments. Before ending the BUILD phase it re-verifies that the deployed endpoint is still reachable, then notifies the orchestrator that evaluation may begin.

---

## 4. Output: The Agent Artifact

The AppSci-Agent must produce three things:

### 4.1 Environment Setup (Dockerfile)

A complete Dockerfile that defines the artifact's runtime environment. The AppSci-Agent writes the full Dockerfile—there is no base image or template provided.

Example:
```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY agent/ ./agent/
COPY prompts/ ./prompts/

EXPOSE 8000
CMD ["python", "-m", "agent.server"]
```

### 4.2 Agent Execution Code

The agent logic that solves the task. The format is **flexible**—the AppSci-Agent decides how to structure the code:

- State machine
- LangGraph / LangChain
- Plain Python with tool calls
- Any other approach

The only requirement is that the code implements the endpoint contract (see below).

### 4.3 Endpoint

An HTTP endpoint that the benchmark evaluator can query. The endpoint uses the **completions protocol** (OpenAI-compatible chat completions format).

The artifact handles all agentic behavior internally—tool calls, reasoning, actions—and returns the final response to the evaluator.

---

## 5. Artifact Contract

The artifact must expose an HTTP endpoint that the evaluator can call using the completions protocol.

### Request Format

```json
{
  "messages": [
    {"role": "user", "content": "Review invoice INV-2024-0042"}
  ]
}
```

### Response Format

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "Invoice INV-2024-0042 has a $847 overcharge..."
      }
    }
  ]
}
```

### How Evaluation Works

1. Evaluator sends a query to `endpoint_url`
2. Artifact processes the query and returns a response
3. Evaluator scores the response against ground truth
4. Repeat for all eval samples

---

## 6. Workflow

The AppSci-Agent follows an **iterative, non-linear workflow** during the BUILD phase. Exploration, user communication, and implementation happen **in parallel**, not sequentially.

**Key principles:**
- **Interleave activities**: Don't wait until you've fully explored to talk to the user, or fully gathered requirements to start building
- **Learn by doing**: Start implementing early; discoveries during implementation often reveal new questions to ask
- **Ask questions when they arise**: If you discover something unclear while exploring or building, ask the user immediately
- **Explore as needed**: Return to exploration whenever you need more information, even during implementation

**Example interleaved workflow:**
```
1. Receive task brief + environment access
2. Start exploring the environment (mini_swe_agent)
3. Find something unclear → ask the user (talk_to_user)
4. Continue exploring while user's answer informs your understanding
5. Start implementing based on what you know so far (mini_swe_agent)
6. Hit a design question → ask the user (talk_to_user)
7. Explore a specific resource you just learned about (mini_swe_agent)
8. Continue building, testing, iterating...
9. Deploy and verify the artifact works
10. Call complete → BUILD phase ends, EVAL phase begins
```

**The only hard rule**: Call `complete` only after the artifact is deployed and verified.
