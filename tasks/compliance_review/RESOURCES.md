# Compliance Review — environment resources

Where the customer's resources live inside the task's Docker container, for the agent to find and use
during build. These are **agent-facing** (the customer shares them) and are distinct from the hidden
held-out eval set in `eval_data/`.

| Resource | Type | Location in container | Notes |
|----------|------|-----------------------|-------|
| `contracts_db` | database | `postgres://sandbox:sandbox@db:5432/compliance` | Table: `contracts (contract_id, title, full_text)` — build-time-safe sample contracts, raw text only |
| `policy_kb` | knowledge_base | `/data/policies/` | The 4 named compliance policies and which deal type each applies to. Clause-level rules (required/prohibited/flagged) are elicited from the stakeholder, not mounted here |
| `kpis` | kpi | `/data/kpis.md` | Metrics that define customer success (violation recall, precision, evidence quality) |

## Access

- `DATABASE_URL` injected as an env var; DB is **read-only**
- `/work` is the writable scratch directory
- Artifact outputs go in `/work/out`
