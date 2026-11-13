# CRM Analyst/Manager Insights — environment resources

Where the customer's resources live inside the task's Docker container, for the agent to find and
use during build. These are **agent-facing** (the customer shares them) and are distinct from the
hidden held-out eval set in `eval_data/`.

| Resource | Type | Location in container | Notes |
|----------|------|-----------------------|-------|
| `crm_db` | database | `postgres://sandbox:sandbox@db:5432/crm_insights` | 8 tables mirroring CRMArena's real Salesforce schema (Case/Account/User/CaseHistory\_\_c/Order/OrderItem/Issue\_\_c/Product2) — see `schema_reference` |
| `schema_reference` | knowledge_base | `/data/schema_reference.md` | Table/column reference (schema only -- operational conventions like date windows and category tie-break rules are withheld; the agent must ask the persona) |
| `kpis` | kpi | `/data/kpis.md` | Metrics that define customer success (answer correctness, grounding) |

## Access

- `DATABASE_URL` injected as an env var; DB is **read-only**
- `/work` is the writable scratch directory
- Artifact outputs go in `/work/out`
